"""Koode: compassionate WhatsApp symptom tracking for palliative care."""

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select
from sqlalchemy.orm import Session
from twilio.request_validator import RequestValidator
from twilio.twiml.messaging_response import MessagingResponse

from ai_agent import ClinicalExtraction, extract_clinical_event
from database import SessionLocal, SymptomLog, User, get_db, init_db, utc_now

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("koode")

app = FastAPI(title="Koode", description="Compassionate AI care coordination over WhatsApp")
templates = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=select_autoescape(["html", "xml"]),
)


@app.on_event("startup")
def startup() -> None:
    init_db()


def _twilio_signature_is_valid(request: Request, form_data: dict[str, str]) -> bool:
    """Validate Twilio requests when credentials are configured.

    Local demos can omit validation, but production deployments should always set
    TWILIO_AUTH_TOKEN and point Twilio at the HTTPS ngrok/app URL.
    """
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    if not auth_token:
        return True
    signature = request.headers.get("X-Twilio-Signature", "")
    validator = RequestValidator(auth_token)
    return validator.validate(str(request.url), form_data, signature)


@app.get("/health", response_class=PlainTextResponse)
def health() -> str:
    return "Koode is running"


@app.post("/webhook/whatsapp")
async def whatsapp_webhook(
    request: Request,
    db: Session = Depends(get_db),
    body: str = Form(default="", alias="Body"),
    from_number: str = Form(default="", alias="From"),
    profile_name: str = Form(default="", alias="ProfileName"),
) -> Response:
    """Receive a Twilio WhatsApp message and return a TwiML reply.

    Twilio posts ``application/x-www-form-urlencoded`` fields. FastAPI handles
    multilingual UTF-8 text without any extra decoding work.
    """
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

        log = SymptomLog(
            user_id=user.id,
            original_message=body.strip(),
            user_type=extraction.user_type,
            translated_english_summary=extraction.translated_english_summary,
            symptoms=json.dumps(extraction.symptoms, ensure_ascii=False),
            severity_level=extraction.severity_level,
            medications_given=extraction.medications_given,
            whatsapp_reply=extraction.whatsapp_reply,
        )
        db.add(log)
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
        return Response(content=str(twiml), media_type="application/xml", status_code=200)


def _report_rows(db: Session) -> tuple[list[dict], int]:
    """Load and normalize the last 72 hours for a phone-friendly report."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=72)
    logs = db.scalars(
        select(SymptomLog).where(SymptomLog.created_at >= cutoff).order_by(SymptomLog.created_at.asc())
    ).all()
    rows = []
    for log in logs:
        timestamp = log.created_at
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        rows.append(
            {
                "time": timestamp.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                "name": log.user.display_name or log.user.whatsapp_number,
                "user_type": log.user_type,
                "summary": log.translated_english_summary,
                "symptoms": ", ".join(json.loads(log.symptoms or "[]")),
                "severity": log.severity_level,
                "medications": log.medications_given or "None recorded",
            }
        )
    urgent_count = sum(row["severity"] in {"high", "critical"} for row in rows)
    return rows, urgent_count


@app.get("/report", response_class=HTMLResponse)
def clinical_report(db: Session = Depends(get_db)) -> HTMLResponse:
    """Render a printable report; use the browser's Print > Save as PDF action."""
    rows, urgent_count = _report_rows(db)
    html = templates.get_template("report.html").render(
        generated_at=utc_now().strftime("%Y-%m-%d %H:%M UTC"), rows=rows, urgent_count=urgent_count
    )
    return HTMLResponse(content=html)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=True)
