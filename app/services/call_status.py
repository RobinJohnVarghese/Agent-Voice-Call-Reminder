import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    ACTIVE_CALL_STATUSES,
    FINAL_CALL_STATUSES,
    Call,
    CallEvent,
    CallStatus,
    Campaign,
    CampaignStatus,
)

logger = logging.getLogger(__name__)

PLIVO_STATUS_MAP = {
    "ringing": CallStatus.ringing,
    "in-progress": CallStatus.in_progress,
    "completed": CallStatus.completed,
    "busy": CallStatus.busy,
    "no-answer": CallStatus.no_answer,
    "timeout": CallStatus.no_answer,
    "failed": CallStatus.failed,
    "cancel": CallStatus.cancelled,
    "canceled": CallStatus.cancelled,
    "cancelled": CallStatus.cancelled,
}

HANGUP_CAUSE_MAP = {
    "busy line": CallStatus.busy,
    "no answer": CallStatus.no_answer,
    "ring timeout reached": CallStatus.no_answer,
    "rejected": CallStatus.no_answer,
    "unallocated number": CallStatus.failed,
    "invalid destination address": CallStatus.failed,
    "destination out of order": CallStatus.failed,
    "network out of order": CallStatus.failed,
}

_PROGRESS_RANK = {
    CallStatus.pending: 0,
    CallStatus.queued: 1,
    CallStatus.retry_scheduled: 1,
    CallStatus.initiated: 2,
    CallStatus.ringing: 3,
    CallStatus.in_progress: 4,
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_int(value) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def status_from_plivo_hangup(params: dict) -> CallStatus:
    raw_status = (params.get("CallStatus") or "").strip().lower()
    cause = (params.get("HangupCauseName") or params.get("HangupCause") or "").strip().lower()
    status = PLIVO_STATUS_MAP.get(raw_status)
    if status in (None, CallStatus.completed, CallStatus.in_progress, CallStatus.ringing):
        by_cause = HANGUP_CAUSE_MAP.get(cause)
        if by_cause:
            return by_cause
    if status is None or status not in FINAL_CALL_STATUSES:
        return CallStatus.completed if _as_int(params.get("Duration")) else CallStatus.failed
    return status


def update_call_status(
    db: Session, call: Call, status: CallStatus, event: str, payload: dict | None = None
) -> int | None:
    """Apply a status transition. Returns a retry delay in seconds if a retry should be scheduled.

    The caller commits and then schedules the retry (see app.worker.tasks.schedule_retry).
    """
    payload = payload or {}
    db.add(CallEvent(call_id=call.id, event=event, status=status.value, payload=payload))

    current = call.status
    if status not in FINAL_CALL_STATUSES:
        if current in FINAL_CALL_STATUSES or current == CallStatus.retry_scheduled:
            return None
        if _PROGRESS_RANK.get(status, 0) <= _PROGRESS_RANK.get(current, 0):
            return None
        call.status = status
        if status == CallStatus.in_progress and not call.answered_at:
            call.answered_at = utcnow()
        return None

    if current in FINAL_CALL_STATUSES or current == CallStatus.retry_scheduled:
        return None

    settings = get_settings()
    call.ended_at = utcnow()
    campaign = call.campaign
    should_retry = (
        status.value in settings.retry_on_statuses
        and call.attempts < settings.max_call_attempts
        and campaign.status == CampaignStatus.running
    )
    if should_retry:
        delay = settings.retry_delay_seconds
        call.status = CallStatus.retry_scheduled
        call.next_attempt_at = utcnow() + timedelta(seconds=delay)
        call.last_error = f"Attempt {call.attempts} ended as {status.value}; retry scheduled"
        db.flush()
        return delay

    call.status = status
    call.next_attempt_at = None
    db.flush()
    check_campaign_completion(db, campaign.id)
    return None


def apply_plivo_hangup(db: Session, call: Call, params: dict) -> int | None:
    status = status_from_plivo_hangup(params)
    call.plivo_call_uuid = params.get("CallUUID") or call.plivo_call_uuid
    call.hangup_cause = params.get("HangupCauseName") or params.get("HangupCause")
    call.hangup_source = params.get("HangupSource")
    call.duration_seconds = _as_int(params.get("Duration"))
    call.bill_duration_seconds = _as_int(params.get("BillDuration"))
    if status == CallStatus.failed and call.hangup_cause:
        call.last_error = call.hangup_cause
    return update_call_status(db, call, status, "hangup", params)


def check_campaign_completion(db: Session, campaign_id: int) -> bool:
    campaign = db.execute(select(Campaign).where(Campaign.id == campaign_id).with_for_update()).scalar_one()
    if campaign.status != CampaignStatus.running:
        return False
    active = db.scalar(
        select(func.count(Call.id)).where(Call.campaign_id == campaign_id, Call.status.in_(ACTIVE_CALL_STATUSES))
    )
    if active:
        return False
    campaign.status = CampaignStatus.completed
    campaign.completed_at = utcnow()
    logger.info("Campaign %s completed", campaign_id)
    return True


def status_counts(db: Session, campaign_id: int) -> dict[str, int]:
    rows = db.execute(
        select(Call.status, func.count(Call.id)).where(Call.campaign_id == campaign_id).group_by(Call.status)
    ).all()
    counts = {s.value: 0 for s in CallStatus}
    for status, count in rows:
        counts[status.value if isinstance(status, CallStatus) else str(status)] = count
    return counts
