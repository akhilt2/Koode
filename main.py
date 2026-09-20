"""Koode: compassionate WhatsApp symptom tracking and care monitoring."""

import json
import logging
import os
import re
import uuid
from html import escape
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, Field
from markupsafe import Markup
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from twilio.request_validator import RequestValidator
from twilio.twiml.messaging_response import MessagingResponse

from ai_agent import ClinicalExtraction, extract_clinical_event, generate_clinical_summary
from database import SymptomLog, User, get_db, get_logs_by_date_range, init_db, utc_now

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("koode")

app = FastAPI(title="Koode", description="Compassionate AI care coordination over WhatsApp")
templates = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=select_autoescape(["html", "xml"]),
)
# This is intentionally local and bounded for the hackathon's single-process demo.
SUMMARY_CACHE: dict[str, dict] = {}


@app.on_event("startup")
def startup() -> None:
    init_db()


def _twilio_signature_is_valid(request: Request, form_data: dict[str, str]) -> bool:
    """Validate Twilio requests when credentials are configured."""
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    if not auth_token:
        return True
    signature = request.headers.get("X-Twilio-Signature", "")
    return RequestValidator(auth_token).validate(str(request.url), form_data, signature)


@app.get("/health", response_class=PlainTextResponse)
def health() -> str:
    return "Koode is running"


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard() -> HTMLResponse:
    return HTMLResponse(content=templates.get_template("dashboard.html").render())


@app.post("/webhook/whatsapp")
async def whatsapp_webhook(
    request: Request,
    db: Session = Depends(get_db),
    body: str = Form(default="", alias="Body"),
    from_number: str = Form(default="", alias="From"),
    profile_name: str = Form(default="", alias="ProfileName"),
) -> Response:
    """Receive a Twilio WhatsApp message, persist it, and return TwiML."""
    form = await request.form()
    form_data = {str(key): str(value) for key, value in form.items()}
    if not _twilio_signature_is_valid(request, form_data):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")
    if not body.strip() or not from_number.strip():
        raise HTTPException(status_code=400, detail="Twilio message body and sender are required")
    try:
        extraction: ClinicalExtraction = extract_clinical_event(body.strip())
        user = db.scalar(select(User).where(User.whatsapp_number == from_number))
        if user is None:
            user = User(whatsapp_number=from_number, display_name=profile_name or None)
            db.add(user)
            db.flush()
        elif profile_name and not user.display_name:
            user.display_name = profile_name
        db.add(
            SymptomLog(
                user_id=user.id,
                original_message=body.strip(),
                user_type=extraction.user_type,
                translated_english_summary=extraction.translated_english_summary,
                symptoms=json.dumps(extraction.symptoms, ensure_ascii=False),
                severity_level=extraction.severity_level,
                medications_given=extraction.medications_given,
                whatsapp_reply=extraction.whatsapp_reply,
            )
        )
        db.commit()
        twiml = MessagingResponse()
        twiml.message(extraction.whatsapp_reply)
        return Response(content=str(twiml), media_type="application/xml")
    except HTTPException:
        raise
    except Exception:
        db.rollback()
        logger.exception("Could not process incoming WhatsApp message")
        twiml = MessagingResponse()
        twiml.message("I’m sorry, I couldn’t record that just now. Please try again, or contact your care team if this is urgent.")
        return Response(content=str(twiml), media_type="application/xml")


def _log_to_dict(log: SymptomLog) -> dict:
    """Convert an ORM event to a JSON/template-safe dashboard record."""
    timestamp = log.created_at
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    try:
        symptoms = json.loads(log.symptoms or "[]")
    except json.JSONDecodeError:
        symptoms = []
    return {
        "id": log.id,
        "user_id": log.user_id,
        "created_at": timestamp.astimezone(timezone.utc).isoformat(),
        "time": timestamp.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "patient_name": log.user.display_name or log.user.whatsapp_number,
        "user_type": log.user_type,
        "summary": log.translated_english_summary,
        "translated_english_summary": log.translated_english_summary,
        "symptoms": symptoms,
        "severity_level": log.severity_level,
        "medications_given": log.medications_given or "",
        "whatsapp_reply": log.whatsapp_reply,
        "resolved": log.resolved,
    }


@app.get("/api/patients")
def patients(db: Session = Depends(get_db)) -> JSONResponse:
    """Return selectable WhatsApp participants for dashboard filters."""
    users = db.scalars(select(User).order_by(User.display_name.asc(), User.whatsapp_number.asc())).all()
    return JSONResponse(
        content={
            "patients": [
                {"id": user.id, "name": user.display_name or user.whatsapp_number}
                for user in users
            ]
        }
    )


def _markdown_to_html(markdown_text: str) -> Markup:
    """Render the small, predictable Markdown subset used by report summaries.

    The model output is escaped before formatting to prevent it from injecting
    markup into a clinician's dashboard or printable report.
    """
    blocks = []
    paragraph = []
    list_items = []

    def flush_paragraph() -> None:
        if paragraph:
            blocks.append(f"<p>{'<br>'.join(paragraph)}</p>")
            paragraph.clear()

    def flush_list() -> None:
        if list_items:
            blocks.append(f"<ul>{''.join(f'<li>{item}</li>' for item in list_items)}</ul>")
            list_items.clear()

    for raw_line in markdown_text.splitlines():
        line = escape(raw_line.strip())
        if not line:
            flush_paragraph()
            flush_list()
        elif line.startswith("### "):
            flush_paragraph()
            flush_list()
            blocks.append(f"<h4>{line[4:]}</h4>")
        elif line.startswith("## "):
            flush_paragraph()
            flush_list()
            blocks.append(f"<h3>{line[3:]}</h3>")
        elif line.startswith("# "):
            flush_paragraph()
            flush_list()
            blocks.append(f"<h2>{line[2:]}</h2>")
        elif re.match(r"^(?:[-*]|\d+\.)\s+", line):
            flush_paragraph()
            item = re.sub(r"^(?:[-*]|\d+\.)\s+", "", line)
            item = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", item)
            list_items.append(item)
        else:
            flush_list()
            line = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", line)
            paragraph.append(line)
    flush_paragraph()
    flush_list()
    return Markup("".join(blocks) or "<p>No summary was generated.</p>")


def _parse_report_range(start_date: str, end_date: str) -> tuple[datetime, datetime, str, str]:
    """Parse inclusive YYYY-MM-DD values into UTC datetimes."""
    try:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Dates must use YYYY-MM-DD format") from exc
    if end < start:
        raise HTTPException(status_code=400, detail="end_date must be on or after start_date")
    return (
        datetime.combine(start, time.min, tzinfo=timezone.utc),
        datetime.combine(end, time.max, tzinfo=timezone.utc),
        start.isoformat(),
        end.isoformat(),
    )


class EventResolution(BaseModel):
    resolved: bool = True


@app.post("/api/events/{event_id}/resolve")
def resolve_event(event_id: int, request: EventResolution, db: Session = Depends(get_db)) -> JSONResponse:
    """Mark an event resolved or reopen it for continued monitoring."""
    event = db.get(SymptomLog, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found")
    event.resolved = request.resolved
    db.commit()
    db.refresh(event)
    return JSONResponse(content={"event": _log_to_dict(event)})


@app.get("/api/events/recent")
def recent_events(
    db: Session = Depends(get_db),
    since: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=5, le=100),
    patient_id: int | None = Query(default=None, ge=1),
    severity_group: str = Query(default="all", pattern="^(all|attention|urgent)$"),
    include_resolved: bool = Query(default=False),
) -> JSONResponse:
    """Newest-first paginated feed plus incremental polling support."""
    if since:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            if since_dt.tzinfo is None:
                since_dt = since_dt.replace(tzinfo=timezone.utc)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="since must be a valid ISO timestamp") from exc
        statement = select(SymptomLog).where(SymptomLog.created_at > since_dt)
        if patient_id is not None:
            statement = statement.where(SymptomLog.user_id == patient_id)
        if not include_resolved:
            statement = statement.where(SymptomLog.resolved.is_(False))
        if severity_group == "attention":
            statement = statement.where(SymptomLog.severity_level.in_(["medium", "high"]))
        elif severity_group == "urgent":
            statement = statement.where(SymptomLog.severity_level.in_(["high", "critical"]))
        logs = list(db.scalars(statement.order_by(SymptomLog.created_at.desc()).limit(100)).all())
        return JSONResponse(content={"events": [_log_to_dict(log) for log in logs]})

    filters = [SymptomLog.user_id == patient_id] if patient_id is not None else []
    if not include_resolved:
        filters.append(SymptomLog.resolved.is_(False))
    if severity_group == "attention":
        filters.append(SymptomLog.severity_level.in_(["medium", "high"]))
    elif severity_group == "urgent":
        filters.append(SymptomLog.severity_level.in_(["high", "critical"]))
    total = db.scalar(select(func.count()).select_from(SymptomLog).where(*filters)) or 0
    total_pages = max(1, (total + page_size - 1) // page_size)
    if page > total_pages:
        page = total_pages
    logs = list(
        db.scalars(
            select(SymptomLog)
            .where(*filters)
            .order_by(SymptomLog.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
    )
    attention_count = db.scalar(
        select(func.count()).select_from(SymptomLog).where(
            *filters, SymptomLog.severity_level.in_(["medium", "high", "critical"])
        )
    ) or 0
    urgent_count = db.scalar(
        select(func.count()).select_from(SymptomLog).where(
            *filters, SymptomLog.severity_level.in_(["high", "critical"])
        )
    ) or 0
    return JSONResponse(
        content={
            "events": [_log_to_dict(log) for log in logs],
            "pagination": {"page": page, "page_size": page_size, "total": total, "total_pages": total_pages},
            "counts": {"attention": attention_count, "urgent": urgent_count},
        }
    )


class ReportRequest(BaseModel):
    start_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    end_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    patient_id: int | None = Field(default=None, ge=1)
    include_resolved: bool = False


@app.post("/api/report/generate")
def generate_report(request: ReportRequest, db: Session = Depends(get_db)) -> JSONResponse:
    """Synthesize a selected range and return a link to its print view."""
    start_dt, end_dt, start_label, end_label = _parse_report_range(request.start_date, request.end_date)
    rows = [
        _log_to_dict(log)
        for log in get_logs_by_date_range(
            db, start_dt, end_dt, user_id=request.patient_id, include_resolved=request.include_resolved
        )
    ]
    summary = generate_clinical_summary(rows, start_label, end_label)
    summary_id = uuid.uuid4().hex
    SUMMARY_CACHE[summary_id] = {
        "summary": summary,
        "start_date": start_label,
        "end_date": end_label,
        "patient_id": request.patient_id,
        "include_resolved": request.include_resolved,
        "events": rows,
    }
    while len(SUMMARY_CACHE) > 20:
        SUMMARY_CACHE.pop(next(iter(SUMMARY_CACHE)))
    patient_query = f"&patient_id={request.patient_id}" if request.patient_id is not None else ""
    resolved_query = "&include_resolved=true" if request.include_resolved else ""
    return JSONResponse(
        content={
            "summary_id": summary_id,
            "summary": summary,
            "summary_html": str(_markdown_to_html(summary)),
            "event_count": len(rows),
            "empty": not rows,
            "print_url": f"/report/print?start_date={start_label}&end_date={end_label}{patient_query}{resolved_query}&summary_id={summary_id}&autoprint=1",
        }
    )


@app.get("/report/print", response_class=HTMLResponse)
def printable_report(
    db: Session = Depends(get_db),
    start_date: str = Query(...),
    end_date: str = Query(...),
    summary_id: str | None = Query(default=None),
    patient_id: int | None = Query(default=None, ge=1),
    include_resolved: bool = Query(default=False),
) -> HTMLResponse:
    """Render a clean A4 view for browser printing or Save as PDF."""
    start_dt, end_dt, start_label, end_label = _parse_report_range(start_date, end_date)
    cached = SUMMARY_CACHE.get(summary_id or "")
    if (
        cached
        and cached["start_date"] == start_label
        and cached["end_date"] == end_label
        and cached["patient_id"] == patient_id
        and cached["include_resolved"] == include_resolved
    ):
        summary, rows = cached["summary"], cached["events"]
    else:
        rows = [
            _log_to_dict(log)
            for log in get_logs_by_date_range(
                db, start_dt, end_dt, user_id=patient_id, include_resolved=include_resolved
            )
        ]
        summary = generate_clinical_summary(rows, start_label, end_label)
    html = templates.get_template("report_print.html").render(
        generated_at=utc_now().strftime("%Y-%m-%d %H:%M UTC"),
        start_date=start_label,
        end_date=end_label,
        summary=summary,
        summary_html=_markdown_to_html(summary),
        rows=rows,
        patient_identifier=", ".join(dict.fromkeys(row["patient_name"] for row in rows)) or "No patient events",
    )
    return HTMLResponse(content=html)


def _report_rows(db: Session) -> tuple[list[dict], int]:
    """Preserve the original 72-hour report route for compatibility."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=72)
    rows = [_log_to_dict(log) for log in get_logs_by_date_range(db, cutoff, datetime.now(timezone.utc))]
    return rows, sum(row["severity_level"] in {"high", "critical"} for row in rows)


@app.get("/report", response_class=HTMLResponse)
def clinical_report(db: Session = Depends(get_db)) -> HTMLResponse:
    """Legacy 72-hour report; the richer primary interface is /dashboard."""
    rows, urgent_count = _report_rows(db)
    html = templates.get_template("report.html").render(
        generated_at=utc_now().strftime("%Y-%m-%d %H:%M UTC"), rows=rows, urgent_count=urgent_count
    )
    return HTMLResponse(content=html)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=True)
