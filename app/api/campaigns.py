import csv
import io

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.languages import LANGUAGES, VOICE_GENDERS
from app.models import Call, CallStatus, Campaign, CampaignStatus
from app.schemas import CallPage, CampaignCreate, CampaignCreated, CampaignOut, ImportSummary
from app.services.call_status import status_counts
from app.services.recipients import ParseResult, RecipientFileError, build_recipients, parse_recipients
from app.services.storage import get_storage
from app.services.templating import placeholders
from app.worker import tasks

router = APIRouter(prefix="/api", tags=["campaigns"])

SAMPLE_CSV = "name,phone,email\nAsha Menon,+919876543210,asha@example.com\nRahul Nair,9876501234,rahul@example.com\n"


def _campaign_out(db: Session, campaign: Campaign) -> CampaignOut:
    counts = status_counts(db, campaign.id)
    out = CampaignOut.model_validate(campaign)
    out.status_counts = counts
    out.total_calls = sum(counts.values())
    out.has_audio = bool(campaign.audio_key)
    return out


def _get_campaign(db: Session, campaign_id: int) -> Campaign:
    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(404, "Campaign not found")
    return campaign


@router.get("/options")
def options():
    settings = get_settings()
    return {
        "languages": [{"code": code, "name": name} for code, name in LANGUAGES.items()],
        "voice_genders": list(VOICE_GENDERS),
        "enable_personalization": settings.enable_personalization,
        "tts_provider": settings.tts_provider,
        "telephony_provider": settings.telephony_provider,
        "storage_backend": settings.storage_backend,
    }


@router.get("/sample-recipients.csv")
def sample_csv():
    return StreamingResponse(
        iter([SAMPLE_CSV]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=sample-recipients.csv"},
    )


def _save_campaign(
    db: Session,
    *,
    name: str,
    message: str,
    language: str,
    voice_gender: str,
    translate: bool,
    auto_start: bool,
    parsed: ParseResult,
    source_filename: str | None,
) -> CampaignCreated:
    settings = get_settings()
    message = message.strip()
    if not message:
        raise HTTPException(422, "Message is required")
    if language not in LANGUAGES:
        raise HTTPException(422, f"Unsupported language '{language}'. Supported: {', '.join(LANGUAGES)}")
    if voice_gender not in VOICE_GENDERS:
        raise HTTPException(422, "voice_gender must be 'female' or 'male'")

    fields: set[str] = set()
    if settings.enable_personalization:
        try:
            fields = placeholders(message)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        missing = fields - set(parsed.columns) - {"name", "phone", "email"}
        if missing:
            raise HTTPException(
                422, f"Message uses placeholders not found in the recipient fields: {', '.join(sorted(missing))}"
            )
    if not parsed.recipients:
        raise HTTPException(422, {"message": "No valid recipients found", "errors": parsed.errors[:50]})

    campaign = Campaign(
        name=name.strip(),
        message=message,
        language=language,
        voice_gender=voice_gender,
        voice=settings.voice_for(voice_gender),
        translate=translate,
        personalized=bool(fields),
        auto_start=auto_start,
        source_filename=source_filename,
        status=CampaignStatus.draft,
    )
    db.add(campaign)
    db.flush()
    db.add_all(
        Call(campaign_id=campaign.id, name=r.name, phone_number=r.phone_number, email=r.email, variables=r.variables)
        for r in parsed.recipients
    )
    db.commit()

    tasks.generate_campaign_audio.delay(campaign.id)
    db.refresh(campaign)
    return CampaignCreated(
        campaign=_campaign_out(db, campaign),
        import_summary=ImportSummary(
            imported=len(parsed.recipients),
            duplicates=parsed.duplicates,
            invalid=len(parsed.errors),
            errors=parsed.errors[:100],
        ),
    )


@router.post("/campaigns", response_model=CampaignCreated, status_code=201)
def create_campaign(payload: CampaignCreate, db: Session = Depends(get_db)):
    """Create a campaign from JSON: recipients (name, phone, email), message, language and voice gender."""
    settings = get_settings()
    if len(payload.recipients) > settings.max_upload_rows:
        raise HTTPException(422, f"At most {settings.max_upload_rows} recipients per campaign")
    rows = [r.model_dump(mode="json") for r in payload.recipients]
    parsed = build_recipients(rows, settings.default_country_code)
    return _save_campaign(
        db,
        name=payload.name,
        message=payload.message,
        language=payload.language,
        voice_gender=payload.voice_gender,
        translate=payload.translate,
        auto_start=payload.auto_start,
        parsed=parsed,
        source_filename=None,
    )


@router.post("/campaigns/upload", response_model=CampaignCreated, status_code=201)
async def upload_campaign(
    file: UploadFile = File(..., description="CSV or Excel file with phone, name and email columns"),
    name: str = Form(..., max_length=200),
    message: str = Form(..., max_length=2000),
    language: str = Form("en-IN"),
    voice_gender: str = Form("female"),
    translate: bool = Form(False),
    auto_start: bool = Form(False),
    db: Session = Depends(get_db),
):
    """Create a campaign from an uploaded CSV/Excel recipients file."""
    settings = get_settings()
    content = await file.read()
    if len(content) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(413, f"File is larger than {settings.max_upload_mb} MB")
    try:
        parsed = parse_recipients(file.filename or "", content, settings.default_country_code, settings.max_upload_rows)
    except RecipientFileError as exc:
        raise HTTPException(422, str(exc)) from exc
    return _save_campaign(
        db,
        name=name,
        message=message,
        language=language,
        voice_gender=voice_gender,
        translate=translate,
        auto_start=auto_start,
        parsed=parsed,
        source_filename=file.filename,
    )


@router.get("/campaigns", response_model=list[CampaignOut])
def list_campaigns(limit: int = Query(50, ge=1, le=200), db: Session = Depends(get_db)):
    campaigns = db.scalars(select(Campaign).order_by(Campaign.id.desc()).limit(limit)).all()
    return [_campaign_out(db, c) for c in campaigns]


@router.get("/campaigns/{campaign_id}", response_model=CampaignOut)
def get_campaign(campaign_id: int, db: Session = Depends(get_db)):
    return _campaign_out(db, _get_campaign(db, campaign_id))


@router.get("/campaigns/{campaign_id}/calls", response_model=CallPage)
def list_calls(
    campaign_id: int,
    status: CallStatus | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
):
    _get_campaign(db, campaign_id)
    query = select(Call).where(Call.campaign_id == campaign_id)
    if status:
        query = query.where(Call.status == status)
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    items = db.scalars(query.order_by(Call.id).offset((page - 1) * page_size).limit(page_size)).all()
    return CallPage(items=items, total=total, page=page, page_size=page_size)


@router.get("/campaigns/{campaign_id}/audio")
def campaign_audio(campaign_id: int, db: Session = Depends(get_db)):
    campaign = _get_campaign(db, campaign_id)
    if not campaign.audio_key:
        raise HTTPException(404, "Audio not generated yet")
    return RedirectResponse(get_storage().playback_url(campaign.audio_key))


@router.post("/campaigns/{campaign_id}/start", response_model=CampaignOut)
def start(campaign_id: int, db: Session = Depends(get_db)):
    campaign = _get_campaign(db, campaign_id)
    if campaign.status != CampaignStatus.ready:
        raise HTTPException(409, f"Campaign is {campaign.status.value}; it can only be started when ready")
    tasks.start_campaign.delay(campaign_id)
    db.expire_all()
    return _campaign_out(db, _get_campaign(db, campaign_id))


@router.post("/campaigns/{campaign_id}/regenerate-audio", response_model=CampaignOut)
def regenerate_audio(campaign_id: int, db: Session = Depends(get_db)):
    campaign = _get_campaign(db, campaign_id)
    if campaign.status not in (CampaignStatus.draft, CampaignStatus.ready, CampaignStatus.audio_failed):
        raise HTTPException(409, f"Cannot regenerate audio while campaign is {campaign.status.value}")
    db.execute(update(Call).where(Call.campaign_id == campaign_id).values(audio_key=None, spoken_message=None))
    db.commit()
    tasks.generate_campaign_audio.delay(campaign_id)
    db.expire_all()
    return _campaign_out(db, _get_campaign(db, campaign_id))


@router.post("/campaigns/{campaign_id}/cancel", response_model=CampaignOut)
def cancel(campaign_id: int, db: Session = Depends(get_db)):
    campaign = _get_campaign(db, campaign_id)
    if campaign.status in (CampaignStatus.completed, CampaignStatus.cancelled):
        raise HTTPException(409, f"Campaign is already {campaign.status.value}")
    campaign.status = CampaignStatus.cancelled
    db.execute(
        update(Call)
        .where(
            Call.campaign_id == campaign_id,
            Call.status.in_([CallStatus.pending, CallStatus.queued, CallStatus.retry_scheduled]),
        )
        .values(status=CallStatus.cancelled, next_attempt_at=None)
    )
    db.commit()
    return _campaign_out(db, campaign)


@router.get("/campaigns/{campaign_id}/export")
def export_calls(campaign_id: int, db: Session = Depends(get_db)):
    campaign = _get_campaign(db, campaign_id)
    calls = db.scalars(select(Call).where(Call.campaign_id == campaign_id).order_by(Call.id)).all()
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        ["name", "phone_number", "email", "status", "attempts", "hangup_cause", "duration_seconds",
         "answered_at", "ended_at", "last_error", "plivo_call_uuid"]
    )
    for c in calls:
        writer.writerow(
            [c.name or "", c.phone_number, c.email or "", c.status.value, c.attempts, c.hangup_cause or "",
             c.duration_seconds if c.duration_seconds is not None else "",
             c.answered_at.isoformat() if c.answered_at else "", c.ended_at.isoformat() if c.ended_at else "",
             c.last_error or "", c.plivo_call_uuid or ""]
        )
    filename = f"campaign-{campaign.id}-results.csv"
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
