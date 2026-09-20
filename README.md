# Koode

Koode is a compassionate WhatsApp-based palliative-care assistant for symptom tracking and caregiver coordination. It receives natural messages, including regional languages and casual slang, translates them into a structured clinical event, stores the event locally, and replies with empathy.

## Architecture

- **FastAPI** receives Twilio WhatsApp webhooks and returns TwiML.
- **OpenAI Structured Outputs** classifies the sender and extracts a strict `ClinicalExtraction` object.
- **SQLite + SQLAlchemy** stores users and symptom logs in `koode.db`.
- **Jinja2** renders a responsive clinical timeline at `/report`, which can be printed or saved as PDF from a browser.

## Setup

Requirements: Python 3.10+ and a Twilio account with WhatsApp Sandbox access.

```bash
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Set `OPENAI_API_KEY` in `.env` for AI extraction. The server deliberately has a safe fallback mode when the key is missing, so the webhook can still be demonstrated. For real clinical use, configure the key and never treat automated extraction as a diagnosis.

Load environment variables in your shell before starting:

```bash
export OPENAI_API_KEY="sk-..."
export TWILIO_AUTH_TOKEN="your-twilio-auth-token"
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

On Windows PowerShell, use `$env:OPENAI_API_KEY="sk-..."` and `$env:TWILIO_AUTH_TOKEN="..."` instead. The same server can be started with `python main.py`.

## Connect the Twilio Sandbox

1. Start the server and confirm `http://localhost:8000/health` returns `Koode is running`.
2. In a second terminal, install ngrok and run:

   ```bash
   ngrok http 8000
   ```

3. Copy the HTTPS forwarding URL, such as `https://abc123.ngrok-free.app`.
4. In the Twilio Console, open **Messaging > Try it out > Send a WhatsApp message > Sandbox settings**.
5. Set **When a message comes in** to `https://abc123.ngrok-free.app/webhook/whatsapp` with method `POST`.
6. Join the sandbox from your phone using Twilio's displayed join phrase.
7. Send a message such as `I feel very breathless tonight` or a caregiver update in a regional language. Koode will respond and persist the event.

If `TWILIO_AUTH_TOKEN` is set, Twilio signatures are verified. Keep signature validation enabled outside local testing.

## Test without Twilio

The webhook accepts standard Twilio form fields, so it can be exercised locally:

```bash
curl -X POST http://localhost:8000/webhook/whatsapp \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  --data-urlencode 'From=whatsapp:+15551234567' \
  --data-urlencode 'ProfileName=Demo Patient' \
  --data-urlencode 'Body=My pain is medium today and I took paracetamol'
```

The response is TwiML XML. Open `http://localhost:8000/report` to see all events from the previous 72 hours. Use the browser print dialog to export the phone-friendly report as a PDF.

## Data and safety

The local database is created automatically at `koode.db`. The AI prompt explicitly forbids diagnosis and medication invention, but Koode is a hackathon prototype, not an emergency service or clinical decision maker. The reply tells users to contact emergency services for immediate danger; a human care team should review the generated report.

## Files

- `main.py`: FastAPI app, Twilio validation/webhook, persistence, and report endpoint.
- `ai_agent.py`: system prompt, Pydantic schema, OpenAI Structured Outputs, and API fallback.
- `database.py`: SQLAlchemy engine, `User` and `SymptomLog` models, and DB dependency.
- `templates/report.html`: responsive, printable 72-hour clinical timeline.
