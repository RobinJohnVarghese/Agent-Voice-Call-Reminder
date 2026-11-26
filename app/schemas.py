from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models import CallStatus, CampaignStatus


class CampaignOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    message: str
    spoken_message: str | None
    language: str
    voice_gender: str
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
    email: str | None
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


class RecipientIn(BaseModel):
    name: str | None = Field(None, max_length=200)
    phone: str = Field(..., max_length=32, examples=["+919876543210"])
    email: EmailStr | None = None


class CampaignCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200, examples=["Clinic appointment reminder"])
    message: str = Field(..., min_length=1, max_length=2000)
    language: str = Field("en-IN", examples=["ml-IN"])
    voice_gender: Literal["female", "male"] = "female"
    translate: bool = False
    auto_start: bool = False
    recipients: list[RecipientIn] = Field(..., min_length=1)
