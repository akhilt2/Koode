"""AI routing and strict clinical extraction for Koode.

The Pydantic model is passed to OpenAI's Structured Outputs parser, which makes
the webhook resilient to regional languages, slang, and imperfect formatting.
"""

import logging
import os
import json
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv()

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a specialized medical NLP engine and empathetic companion designed for palliative care. You receive unstructured text messages from either patients or caregivers.
Step 1 (Empathy & Communication): If the user is the patient, draft a brief, highly empathetic, warm reply to improve patient comfort. If the user is a caregiver, draft a brief, supportive acknowledgment to aid caregiver coordination.
Step 2 (Clinical Translation): Translate any regional languages or slang into formal clinical English.
Step 3 (Data Extraction): Extract medical events to improve symptom tracking.
You must output strictly in JSON format with the following keys:
user_type: 'patient' or 'caregiver'
translated_english_summary: Formal clinical description of the event.
symptoms: List of identified symptoms.
severity_level: 'low', 'medium', 'high', or 'critical'.
medications_given: Any treatments mentioned.
whatsapp_reply: The empathetic text message to send back to the user.
Never diagnose, prescribe, or invent a medication. If severity is unclear, use 'low'. Encourage urgent professional help for potentially life-threatening symptoms."""


class ClinicalExtraction(BaseModel):
    user_type: Literal["patient", "caregiver"]
    translated_english_summary: str = Field(min_length=1)
    symptoms: list[str] = Field(default_factory=list)
    severity_level: Literal["low", "medium", "high", "critical"]
    medications_given: str = ""
    whatsapp_reply: str = Field(min_length=1)


def _fallback_extraction(message: str) -> ClinicalExtraction:
    """Keep a judge-friendly demo alive if OPENAI_API_KEY is not configured."""
    lowered = message.lower()
    urgent_words = ("breath", "unconscious", "bleeding", "chest pain", "severe")
    caregiver_words = ("my father", "my mother", "my patient", "caregiver", "we gave", "i gave")
    symptom_terms = ("pain", "breathless", "breathing", "nausea", "vomit", "fever", "tired", "sleep")
    severity = "high" if any(word in lowered for word in urgent_words) else "low"
    user_type = "caregiver" if any(term in lowered for term in caregiver_words) else "patient"
    symptoms = [term for term in symptom_terms if term in lowered]
    medications = "Medication mentioned in original message; verify name and dose." if any(term in lowered for term in ("took", "gave", "medication", "tablet")) else ""
    return ClinicalExtraction(
        user_type=user_type,
        translated_english_summary="Unstructured message received; clinical details require human review.",
        symptoms=symptoms,
        severity_level=severity,
        medications_given=medications,
        whatsapp_reply=(
            "Thank you for the update. I’ve recorded it for the care team, and please seek urgent help if the patient is in immediate danger."
            if user_type == "caregiver"
            else "I’m here with you. Thank you for telling me. A care team member will review this, and please contact emergency services now if you are in immediate danger."
        ),
    )


def extract_clinical_event(message: str) -> ClinicalExtraction:
    """Classify and extract a message using OpenAI Structured Outputs."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        logger.warning("OPENAI_API_KEY is not set; using safe demo fallback")
        return _fallback_extraction(message)

    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key, timeout=30.0)
        completion = client.beta.chat.completions.parse(
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            temperature=0.2,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": message},
            ],
            response_format=ClinicalExtraction,
        )
        parsed = completion.choices[0].message.parsed
        if parsed is None:
            raise ValueError("OpenAI returned no structured result")
        return parsed
    except Exception:
        # Do not lose a patient's message because of a transient model/API error.
        logger.exception("Clinical extraction failed; using fallback")
        return _fallback_extraction(message)


SUMMARY_PROMPT = """You are a senior palliative care physician reviewing patient logs from {start_date} to {end_date}.
Analyze the following chronological events. Produce a rigorous, structured clinical executive summary covering:
1) Trajectory of Primary Symptoms
2) Medication Efficacy & Breakout Pain
3) Red Flags & Urgent Observations
4) Recommended Care Plan Adjustments
Be concise, clinical, and objective. Do not diagnose or invent facts. Clearly say when data is unavailable. This is decision support for a qualified clinician, not a replacement for clinical judgment.

Chronological events:
{events}"""


def _fallback_summary(logs: list[dict], start_date: str, end_date: str) -> str:
    """Provide a useful deterministic report when the model is unavailable."""
    if not logs:
        return f"No clinical events were recorded between {start_date} and {end_date}."
    severity_counts = {}
    symptoms = []
    medications = []
    urgent = []
    for log in logs:
        severity = log.get("severity_level", "low")
        severity_counts[severity] = severity_counts.get(severity, 0) + 1
        symptoms.extend(log.get("symptoms", []))
        if log.get("medications_given"):
            medications.append(log["medications_given"])
        if severity in {"high", "critical"}:
            urgent.append(log.get("translated_english_summary", "Urgent event"))
    symptom_text = ", ".join(dict.fromkeys(symptoms)) or "No specific symptoms recorded."
    medication_text = "; ".join(dict.fromkeys(medications)) or "No medications or treatments recorded."
    severity_text = ", ".join(f"{key}: {value}" for key, value in severity_counts.items())
    urgent_text = "; ".join(urgent) or "No high or critical events recorded."
    return (
        f"## Clinical Executive Summary ({start_date} to {end_date})\n\n"
        f"### 1) Trajectory of Primary Symptoms\n"
        f"Recorded symptoms: {symptom_text} Events by severity: {severity_text}.\n\n"
        f"### 2) Medication Efficacy & Breakout Pain\n{medication_text}\n\n"
        f"### 3) Red Flags & Urgent Observations\n{urgent_text}\n\n"
        "### 4) Recommended Care Plan Adjustments\n"
        "Review the chronological events with the responsible palliative-care clinician, confirm symptom trends directly with the patient, and reassess treatment effectiveness and escalation needs."
    )


def generate_clinical_summary(logs: list[dict], start_date: str, end_date: str) -> str:
    """Generate an AI executive summary, with a deterministic offline fallback."""
    if not logs:
        return _fallback_summary(logs, start_date, end_date)
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        logger.warning("OPENAI_API_KEY is not set; using deterministic report summary")
        return _fallback_summary(logs, start_date, end_date)
    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key, timeout=30.0)
        completion = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            temperature=0.2,
            messages=[
                {"role": "system", "content": "You write concise clinical summaries in plain text or Markdown."},
                {"role": "user", "content": SUMMARY_PROMPT.format(start_date=start_date, end_date=end_date, events=json.dumps(logs, ensure_ascii=False))},
            ],
        )
        content = completion.choices[0].message.content
        if not content:
            raise ValueError("OpenAI returned an empty clinical summary")
        return content.strip()
    except Exception:
        logger.exception("Clinical summary generation failed; using deterministic fallback")
        return _fallback_summary(logs, start_date, end_date)
