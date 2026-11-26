import logging
import time
import uuid
from datetime import timedelta

from sqlalchemy import select

from app.config import get_settings
from app.database import session_scope
from app.models import (
    IN_FLIGHT_CALL_STATUSES,
    Call,
    CallStatus,
    Campaign,
    CampaignStatus,
)
from app.services import audio as audio_utils
from app.services.call_status import (
    apply_plivo_hangup,
    check_campaign_completion,
    update_call_status,
    utcnow,
)
from app.services.storage import build_key, get_storage
from app.services.telephony import get_telephony
from app.services.templating import render
from app.services.tts import get_tts
from app.worker.celery_app import celery_app

logger = logging.getLogger(__name__)


def webhook_urls(call_id: int) -> dict[str, str]:
    base = get_settings().public_base_url
    return {
        "answer_url": f"{base}/webhooks/plivo/answer?call_id={call_id}",
        "hangup_url": f"{base}/webhooks/plivo/hangup?call_id={call_id}",
        "ring_url": f"{base}/webhooks/plivo/ring?call_id={call_id}",
    }


def synthesize_and_store(text: str, language: str, voice: str, *key_parts: str) -> str:
    settings = get_settings()
    wav = get_tts(settings).synthesize(text, language, voice)
    data, ext, content_type = audio_utils.encode(wav, settings.audio_format)
    key = build_key(settings, *key_parts[:-1], f"{key_parts[-1]}-{uuid.uuid4().hex[:8]}.{ext}")
    return get_storage(settings).save(key, data, content_type)


def schedule_retry(call_id: int, delay: int | None) -> None:
    if delay is not None:
        place_call.apply_async(args=[call_id], countdown=delay)


@celery_app.task(name="app.worker.tasks.generate_campaign_audio")
def generate_campaign_audio(campaign_id: int) -> str:
    with session_scope() as db:
        campaign = db.get(Campaign, campaign_id)
        if campaign is None:
            return "missing"
        campaign.status = CampaignStatus.generating_audio
        campaign.error = None

    try:
        with session_scope() as db:
            campaign = db.get(Campaign, campaign_id)
            template = campaign.message
            if campaign.translate:
                template = get_tts().translate(template, campaign.language)
            campaign.spoken_message = template

            if campaign.personalized:
                sample = db.scalars(select(Call).where(Call.campaign_id == campaign_id).order_by(Call.id)).first()
                text = render(template, sample.variables if sample else {})
                key_name = "sample"
            else:
                text = template
                key_name = "message"
            campaign.audio_key = synthesize_and_store(
                text, campaign.language, campaign.voice, "campaigns", str(campaign_id), key_name
            )
            campaign.status = CampaignStatus.ready
            auto_start = campaign.auto_start
    except Exception as exc:
        logger.exception("Audio generation failed for campaign %s", campaign_id)
        with session_scope() as db:
            campaign = db.get(Campaign, campaign_id)
            campaign.status = CampaignStatus.audio_failed
            campaign.error = str(exc)
        return "failed"

    if auto_start:
        start_campaign.delay(campaign_id)
    return "ready"


@celery_app.task(name="app.worker.tasks.start_campaign")
def start_campaign(campaign_id: int) -> int:
    settings = get_settings()
    with session_scope() as db:
        campaign = db.execute(select(Campaign).where(Campaign.id == campaign_id).with_for_update()).scalar_one()
        if campaign.status not in (CampaignStatus.ready, CampaignStatus.running):
            logger.warning("Campaign %s cannot start from status %s", campaign_id, campaign.status)
            return 0
        campaign.status = CampaignStatus.running
        campaign.started_at = campaign.started_at or utcnow()
        calls = db.scalars(
            select(Call).where(Call.campaign_id == campaign_id, Call.status == CallStatus.pending).order_by(Call.id)
        ).all()
        call_ids = [c.id for c in calls]
        size = max(1, settings.call_batch_size)
        interval = max(settings.call_batch_interval_seconds, size / max(settings.calls_per_second, 0.01))
        now = utcnow()
        for position, c in enumerate(calls):
            c.status = CallStatus.queued
            c.next_attempt_at = now + timedelta(seconds=(position // size) * interval)
        if not call_ids:
            check_campaign_completion(db, campaign_id)

    for index in range(0, len(call_ids), size):
        process_call_batch.apply_async(args=[call_ids[index:index + size]], countdown=(index // size) * interval)
    logger.info("Campaign %s queued %s calls", campaign_id, len(call_ids))
    return len(call_ids)


@celery_app.task(name="app.worker.tasks.process_call_batch")
def process_call_batch(call_ids: list[int]) -> None:
    gap = 1 / max(get_settings().calls_per_second, 0.01)
    for index, call_id in enumerate(call_ids):
        if index:
            time.sleep(gap)
        try:
            dial(call_id)
        except Exception:
            logger.exception("Unexpected error dialing call %s", call_id)


@celery_app.task(name="app.worker.tasks.place_call")
def place_call(call_id: int) -> None:
    dial(call_id)


def dial(call_id: int) -> None:
    settings = get_settings()
    retry_delay = None
    dialed = False
    with session_scope() as db:
        call = db.execute(select(Call).where(Call.id == call_id).with_for_update()).scalar_one_or_none()
        if call is None or call.status not in (CallStatus.queued, CallStatus.retry_scheduled):
            return
        campaign = call.campaign
        if campaign.status != CampaignStatus.running:
            if campaign.status == CampaignStatus.cancelled:
                call.status = CallStatus.cancelled
            return
        call.status = CallStatus.initiated
        try:
            if campaign.personalized and not call.audio_key:
                call.spoken_message = render(campaign.spoken_message or campaign.message, call.variables or {})
                call.audio_key = synthesize_and_store(
                    call.spoken_message, campaign.language, campaign.voice, "calls", str(call.id), "message"
                )
            request_uuid = get_telephony(settings).place_call(to=call.phone_number, **webhook_urls(call.id))
        except Exception as exc:
            logger.warning("Dialing call %s failed: %s", call_id, exc)
            call.attempts += 1
            call.last_error = str(exc)
            retry_delay = update_call_status(db, call, CallStatus.failed, "dial_error", {"error": str(exc)})
        else:
            call.attempts += 1
            call.plivo_request_uuid = request_uuid
            call.plivo_call_uuid = None
            call.hangup_cause = call.hangup_source = None
            call.duration_seconds = call.bill_duration_seconds = None
            call.answered_at = call.ended_at = call.next_attempt_at = None
            call.initiated_at = utcnow()
            call.last_error = None
            update_call_status(db, call, CallStatus.initiated, "dialed", {"request_uuid": request_uuid})
            dialed = True

    schedule_retry(call_id, retry_delay)
    if dialed and settings.telephony_provider == "fake" and settings.fake_call_delay_seconds >= 0:
        simulate_fake_call.apply_async(args=[call_id], countdown=settings.fake_call_delay_seconds)


@celery_app.task(name="app.worker.tasks.simulate_fake_call")
def simulate_fake_call(call_id: int) -> None:
    """Mimic the Plivo ring + hangup callbacks when TELEPHONY_PROVIDER=fake."""
    import random

    outcome = get_settings().fake_call_outcome
    if outcome == "random":
        outcome = random.choice(["completed", "completed", "completed", "no-answer", "busy", "failed"])
    with session_scope() as db:
        call = db.get(Call, call_id)
        if call is None or call.status not in IN_FLIGHT_CALL_STATUSES:
            return
        update_call_status(db, call, CallStatus.ringing, "ring", {"CallStatus": "ringing"})
        answered = outcome == "completed"
        if answered:
            update_call_status(db, call, CallStatus.in_progress, "answer", {"CallStatus": "in-progress"})
        params = {
            "CallUUID": f"fake-{uuid.uuid4()}",
            "CallStatus": outcome,
            "HangupCauseName": "End Of XML Instructions" if answered else outcome,
            "HangupSource": "Plivo",
            "Duration": "12" if answered else "0",
            "BillDuration": "60" if answered else "0",
        }
        retry_delay = apply_plivo_hangup(db, call, params)
    schedule_retry(call_id, retry_delay)


@celery_app.task(name="app.worker.tasks.reconcile_calls")
def reconcile_calls() -> dict:
    """Recover calls whose webhook or Celery task was lost, and close finished campaigns."""
    settings = get_settings()
    now = utcnow()
    stale_before = now - timedelta(minutes=settings.stale_call_timeout_minutes)
    redial: list[int] = []
    retries: list[tuple[int, int | None]] = []
    stats = {"stale_failed": 0, "redialed": 0, "campaigns_completed": 0}

    with session_scope() as db:
        running = select(Campaign.id).where(Campaign.status == CampaignStatus.running)
        stale = db.scalars(
            select(Call).where(
                Call.campaign_id.in_(running),
                Call.status.in_(IN_FLIGHT_CALL_STATUSES),
                Call.updated_at < stale_before,
            )
        ).all()
        for call in stale:
            call.last_error = "No final status received from the telephony provider"
            retries.append((call.id, update_call_status(db, call, CallStatus.failed, "reconcile", {})))
            stats["stale_failed"] += 1

        lost = db.scalars(
            select(Call).where(
                Call.campaign_id.in_(running),
                Call.status.in_([CallStatus.queued, CallStatus.retry_scheduled]),
                Call.next_attempt_at < now - timedelta(minutes=10),
            )
        ).all()
        for call in lost:
            call.next_attempt_at = now
            redial.append(call.id)

        for campaign_id in db.scalars(running).all():
            if check_campaign_completion(db, campaign_id):
                stats["campaigns_completed"] += 1

    for call_id, delay in retries:
        schedule_retry(call_id, delay)
    for call_id in redial:
        place_call.delay(call_id)
    stats["redialed"] = len(redial)
    return stats
