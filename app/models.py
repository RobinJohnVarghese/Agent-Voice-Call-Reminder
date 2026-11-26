import enum
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Enum, ForeignKey, Integer, LargeBinary, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CampaignStatus(str, enum.Enum):
    draft = "draft"
    generating_audio = "generating_audio"
    ready = "ready"
    audio_failed = "audio_failed"
    running = "running"
    completed = "completed"
    cancelled = "cancelled"


class CallStatus(str, enum.Enum):
    pending = "pending"
    queued = "queued"
    initiated = "initiated"
    ringing = "ringing"
    in_progress = "in_progress"
    retry_scheduled = "retry_scheduled"
    completed = "completed"
    no_answer = "no_answer"
    busy = "busy"
    failed = "failed"
    cancelled = "cancelled"


ACTIVE_CALL_STATUSES = {
    CallStatus.pending,
    CallStatus.queued,
    CallStatus.initiated,
    CallStatus.ringing,
    CallStatus.in_progress,
    CallStatus.retry_scheduled,
}
IN_FLIGHT_CALL_STATUSES = {CallStatus.initiated, CallStatus.ringing, CallStatus.in_progress}
FINAL_CALL_STATUSES = {
    CallStatus.completed,
    CallStatus.no_answer,
    CallStatus.busy,
    CallStatus.failed,
    CallStatus.cancelled,
}


def _enum(enum_cls):
    return Enum(enum_cls, native_enum=False, length=32, values_callable=lambda e: [m.value for m in e])


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    message: Mapped[str] = mapped_column(Text)
    spoken_message: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str] = mapped_column(String(16))
    voice_gender: Mapped[str] = mapped_column(String(16), default="female", server_default="female")
    voice: Mapped[str] = mapped_column(String(64))
    translate: Mapped[bool] = mapped_column(Boolean, default=False)
    personalized: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[CampaignStatus] = mapped_column(_enum(CampaignStatus), default=CampaignStatus.draft, index=True)
    audio_key: Mapped[str | None] = mapped_column(String(512))
    error: Mapped[str | None] = mapped_column(Text)
    auto_start: Mapped[bool] = mapped_column(Boolean, default=False)
    source_filename: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    calls: Mapped[list["Call"]] = relationship(back_populates="campaign", cascade="all, delete-orphan")


class Call(Base):
    __tablename__ = "calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)
    name: Mapped[str | None] = mapped_column(String(200))
    phone_number: Mapped[str] = mapped_column(String(32))
    email: Mapped[str | None] = mapped_column(String(320))
    variables: Mapped[dict] = mapped_column(JSON, default=dict)
    spoken_message: Mapped[str | None] = mapped_column(Text)
    audio_key: Mapped[str | None] = mapped_column(String(512))
    status: Mapped[CallStatus] = mapped_column(_enum(CallStatus), default=CallStatus.pending, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    plivo_request_uuid: Mapped[str | None] = mapped_column(String(64), index=True)
    plivo_call_uuid: Mapped[str | None] = mapped_column(String(64), index=True)
    hangup_cause: Mapped[str | None] = mapped_column(String(128))
    hangup_source: Mapped[str | None] = mapped_column(String(64))
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    bill_duration_seconds: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(Text)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    initiated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, server_default=func.now()
    )

    campaign: Mapped[Campaign] = relationship(back_populates="calls")
    events: Mapped[list["CallEvent"]] = relationship(back_populates="call", cascade="all, delete-orphan")


class CallEvent(Base):
    __tablename__ = "call_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    call_id: Mapped[int] = mapped_column(ForeignKey("calls.id", ondelete="CASCADE"), index=True)
    event: Mapped[str] = mapped_column(String(32))
    status: Mapped[str | None] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, server_default=func.now())

    call: Mapped[Call] = relationship(back_populates="events")


class AudioFile(Base):
    """Generated audio stored in PostgreSQL (STORAGE_BACKEND=database)."""

    __tablename__ = "audio_files"

    key: Mapped[str] = mapped_column(String(512), primary_key=True)
    content_type: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer)
    data: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, server_default=func.now())
