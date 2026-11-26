from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models import CallStatus, CampaignStatus


class CampaignOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    message: str
    spoken_message: str | None
    language: str
    voice: str
    translate: bool
    personalized: bool
    auto_start: bool
    status: CampaignStatus
    error: str | None
    source_filename: str | None
    has_audio: bool = False
    total_calls: int = 0
    status_counts: dict[str, int] = {}
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class ImportSummary(BaseModel):
    imported: int
    duplicates: int
    invalid: int
    errors: list[dict]


class CampaignCreated(BaseModel):
    campaign: CampaignOut
    import_summary: ImportSummary


class CallOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    campaign_id: int
    name: str | None
    phone_number: str
    status: CallStatus
    attempts: int
    plivo_request_uuid: str | None
    plivo_call_uuid: str | None
    hangup_cause: str | None
    duration_seconds: int | None
    last_error: str | None
    next_attempt_at: datetime | None
    initiated_at: datetime | None
    answered_at: datetime | None
    ended_at: datetime | None
    updated_at: datetime


class CallPage(BaseModel):
    items: list[CallOut]
    total: int
    page: int
    page_size: int
