# Agent Voice Call Reminder

Upload a list of recipients, type a reminder, choose a language, and the system phones everyone and plays the message.

- **FastAPI**: REST API, Plivo webhooks and a small web dashboard
- **Google Gemini TTS**: turns the message into speech, with optional Gemini translation
- **Cloud storage**: stores the audio (S3, GCS or local disk)
- **Plivo Voice API**: places the outbound calls and plays the audio
- **Celery + Redis**: generates audio and dials in rate-limited batches, with retries
- **PostgreSQL**: stores campaigns, calls and the webhook event log

## How it works

```
Dashboard / API ──► FastAPI ──► PostgreSQL
   (CSV/Excel,          │
    message, language)  └─► Celery (Redis)
                              ├─ Gemini TTS ─► WAV/MP3 ─► S3 / GCS / local
                              └─ Plivo Calls API (batched, rate-limited)
Plivo ──► /webhooks/plivo/answer  → <Play>audio</Play><Hangup/>
      ──► /webhooks/plivo/ring    → status = ringing
      ──► /webhooks/plivo/hangup  → completed / no_answer / busy / failed / cancelled (+ retries)
```

1. `POST /api/campaigns` imports the recipients file and validates every number (E.164, duplicates removed). The response lists invalid rows.
2. A Celery task translates the message (optional) and calls Gemini TTS. It stores the audio under `voice-reminders/campaigns/<id>/…`.
3. Listen to the audio in the dashboard. Then click **Start calls**, or set `auto_start`.
4. Calls go out in batches (`CALL_BATCH_SIZE`). Batches are spaced so dialing never exceeds `CALLS_PER_SECOND`.
5. When a recipient answers, Plivo fetches the answer URL. The API returns XML that plays the audio and then hangs up.
6. Plivo's ring and hangup callbacks update the call status. Every callback is saved in `call_events`.
7. `no_answer` and `busy` calls are retried after `RETRY_DELAY_SECONDS`, up to `MAX_CALL_ATTEMPTS` attempts in total.
8. A campaign becomes `completed` when no call is still active. Results can be downloaded as CSV.

### Personalised messages
If the message has `{placeholders}` that match column names in the file, each recipient gets their own audio, generated just before their call. Example: `Hello {name}, your appointment is on {appointment_date}`. Column names are normalised: `Appointment Date` becomes `appointment_date`. Without placeholders, one shared audio is generated per campaign.

### Recipients file
CSV or Excel. A phone column is required. Accepted column names: `phone`, `phone_number`, `mobile`, `number`, `contact`, and similar. A `name` column is optional, and any extra columns can be used as placeholders. Numbers without a country code use `DEFAULT_COUNTRY_CODE` (default `IN`). Download a sample from the dashboard or at `/api/sample-recipients.csv`.

### Call statuses
`pending → queued → initiated → ringing → in_progress → completed`. The other final states are `no_answer`, `busy`, `failed` and `cancelled`. `retry_scheduled` means the call is waiting for its next attempt. A periodic Celery beat job does two things:
- Marks calls that never received a hangup callback as `failed`, after `STALE_CALL_TIMEOUT_MINUTES`.
- Re-queues dial tasks that were lost.

## Quick start (Docker)

```bash
cp .env.example .env      # fill in Gemini, Plivo and storage settings
docker compose up --build
```
Open http://localhost:8000 for the dashboard, or http://localhost:8000/docs for the API.

Plivo must be able to reach `PUBLIC_BASE_URL`. During development, expose port 8000 with a tunnel and put that HTTPS URL in `.env`:
```bash
ngrok http 8000           # or: cloudflared tunnel --url http://localhost:8000
```

### Try it without any accounts
Set `TTS_PROVIDER=fake`, `TELEPHONY_PROVIDER=fake` and `STORAGE_BACKEND=local`. The fake TTS creates a beep tone. The fake telephony simulates the Plivo callbacks; set `FAKE_CALL_OUTCOME=random` to get a mix of results.

## Local development (without Docker for the app)

```bash
docker compose up -d postgres redis
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
alembic upgrade head
uvicorn app.main:app --reload                               # API + dashboard
celery -A app.worker.celery_app worker --loglevel=INFO      # worker
celery -A app.worker.celery_app beat --loglevel=INFO        # periodic reconcile
pytest
```
To convert audio to MP3, install `ffmpeg`. Without it, audio is stored as WAV.

## Configuration
All settings are environment variables; see [`.env.example`](.env.example). The most important:

| Variable | Purpose |
|---|---|
| `PUBLIC_BASE_URL` | Public HTTPS URL of the API, used for Plivo webhooks and local audio |
| `GEMINI_API_KEY`, `GEMINI_TTS_MODEL`, `GEMINI_DEFAULT_VOICE` | Gemini TTS |
| `STORAGE_BACKEND` | `s3`, `gcs` or `local` |
| `S3_BUCKET`, `S3_REGION`, AWS credentials | S3 storage. The bucket can stay private: a presigned URL is created each time a call is answered |
| `GCS_BUCKET`, `GOOGLE_APPLICATION_CREDENTIALS` | GCS storage, with signed URLs |
| `PLIVO_AUTH_ID`, `PLIVO_AUTH_TOKEN`, `PLIVO_FROM_NUMBER` | Plivo account and caller ID |
| `PLIVO_VALIDATE_SIGNATURE` | Checks `X-Plivo-Signature-V3` on every webhook (default on) |
| `CALL_BATCH_SIZE`, `CALL_BATCH_INTERVAL_SECONDS`, `CALLS_PER_SECOND` | Dialing throughput |
| `MAX_CALL_ATTEMPTS`, `RETRY_DELAY_SECONDS`, `RETRY_ON_STATUSES` | Retry policy |

## API

| Method | Path | Description |
|---|---|---|
| POST | `/api/campaigns` | multipart: `file`, `name`, `message`, `language`, `voice`, `translate`, `auto_start` |
| GET | `/api/campaigns` | List campaigns with status counts |
| GET | `/api/campaigns/{id}` | Campaign detail |
| GET | `/api/campaigns/{id}/calls?status=&page=` | Calls, paginated |
| GET | `/api/campaigns/{id}/audio` | Redirects to the generated audio |
| POST | `/api/campaigns/{id}/start` | Start dialing |
| POST | `/api/campaigns/{id}/regenerate-audio` | Generate the audio again |
| POST | `/api/campaigns/{id}/cancel` | Cancel pending and queued calls |
| GET | `/api/campaigns/{id}/export` | Download results as CSV |
| GET | `/api/options` | Languages and voices |
| GET/POST | `/webhooks/plivo/{answer,ring,hangup}` | Plivo callbacks |

## Project layout
```
app/
  main.py              FastAPI app, dashboard, /media
  config.py            settings (env vars)
  models.py            Campaign, Call, CallEvent
  api/campaigns.py     REST API
  api/webhooks.py      Plivo answer/ring/hangup + signature validation
  services/            recipients import, templating, Gemini TTS, storage, Plivo, call status logic
  worker/              Celery app + tasks (audio, batching, dialing, retries, reconcile)
  static/              dashboard (HTML/JS/CSS)
alembic/               database migrations
tests/                 pytest suite (SQLite by default; set TEST_DATABASE_URL for Postgres)
```
