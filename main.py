"""Koode: compassionate WhatsApp symptom tracking and care monitoring."""

import json
import logging
import os
import uuid
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, Field
from sqlalchemy import select
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
    }


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


@app.get("/api/events/recent")
def recent_events(
    db: Session = Depends(get_db),
    since: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> JSONResponse:
    """Polling feed for near-real-time dashboard updates."""
    if since:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            if since_dt.tzinfo is None:
                since_dt = since_dt.replace(tzinfo=timezone.utc)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="since must be a valid ISO timestamp") from exc
        statement = select(SymptomLog).where(SymptomLog.created_at > since_dt).order_by(SymptomLog.created_at.asc()).limit(limit)
    else:
        statement = select(SymptomLog).order_by(SymptomLog.created_at.desc()).limit(limit)
    logs = list(db.scalars(statement).all())
    if not since:
        logs.reverse()
    return JSONResponse(content={"events": [_log_to_dict(log) for log in logs]})


class ReportRequest(BaseModel):
    start_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    end_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


@app.post("/api/report/generate")
def generate_report(request: ReportRequest, db: Session = Depends(get_db)) -> JSONResponse:
    """Synthesize a selected range and return a link to its print view."""
    start_dt, end_dt, start_label, end_label = _parse_report_range(request.start_date, request.end_date)
    rows = [_log_to_dict(log) for log in get_logs_by_date_range(db, start_dt, end_dt)]
    summary = generate_clinical_summary(rows, start_label, end_label)
    summary_id = uuid.uuid4().hex
    SUMMARY_CACHE[summary_id] = {
        "summary": summary,
        "start_date": start_label,
        "end_date": end_label,
        "events": rows,
    }
    while len(SUMMARY_CACHE) > 20:
        SUMMARY_CACHE.pop(next(iter(SUMMARY_CACHE)))
    return JSONResponse(
        content={
            "summary_id": summary_id,
            "summary": summary,
            "event_count": len(rows),
            "empty": not rows,
            "print_url": f"/report/print?start_date={start_label}&end_date={end_label}&summary_id={summary_id}&autoprint=1",
        }
    )


@app.get("/report/print", response_class=HTMLResponse)
def printable_report(
    db: Session = Depends(get_db),
    start_date: str = Query(...),
    end_date: str = Query(...),
    summary_id: str | None = Query(default=None),
) -> HTMLResponse:
    """Render a clean A4 view for browser printing or Save as PDF."""
    start_dt, end_dt, start_label, end_label = _parse_report_range(start_date, end_date)
    cached = SUMMARY_CACHE.get(summary_id or "")
    if cached and cached["start_date"] == start_label and cached["end_date"] == end_label:
        summary, rows = cached["summary"], cached["events"]
    else:
        rows = [_log_to_dict(log) for log in get_logs_by_date_range(db, start_dt, end_dt)]
        summary = generate_clinical_summary(rows, start_label, end_label)
    html = templates.get_template("report_print.html").render(
        generated_at=utc_now().strftime("%Y-%m-%d %H:%M UTC"),
        start_date=start_label,
        end_date=end_label,
        summary=summary,
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
