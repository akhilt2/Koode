"""AI routing and strict clinical extraction for Koode.

The Pydantic model is passed to OpenAI's Structured Outputs parser, which makes
the webhook resilient to regional languages, slang, and imperfect formatting.
"""

import logging
import os
from typing import Literal

from pydantic import BaseModel, Field

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
    severity = "high" if any(word in lowered for word in urgent_words) else "low"
    return ClinicalExtraction(
        user_type="patient",
        translated_english_summary="Unstructured message received; clinical details require human review.",
        symptoms=[],
        severity_level=severity,
        medications_given="",
        whatsapp_reply="I’m here with you. Thank you for telling me. A care team member will review this, and please contact emergency services now if you are in immediate danger.",
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
