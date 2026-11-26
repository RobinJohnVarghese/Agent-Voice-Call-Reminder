from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Voice Call Reminder"
    environment: str = "development"
    log_level: str = "INFO"

    # Public URL where Plivo can reach this API (webhooks + local audio), e.g. https://xyz.ngrok.app
    public_base_url: str = "http://localhost:8000"

    database_url: str = "postgresql+psycopg://voice:voice@localhost:5432/voice_reminder"

    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str | None = None
    celery_result_backend: str | None = None
    celery_task_always_eager: bool = False

    # Text-to-speech
    tts_provider: Literal["gemini", "fake"] = "gemini"
    gemini_api_key: str = ""
    gemini_tts_model: str = "gemini-2.5-flash-preview-tts"
    gemini_text_model: str = "gemini-2.5-flash"
    gemini_female_voice: str = "Kore"
    gemini_male_voice: str = "Charon"
    tts_style_instruction: str = (
        "Read the following reminder aloud in {language}, in a clear, warm and natural voice "
        "at a moderate pace. Only speak the reminder text itself"
    )
    audio_format: Literal["mp3", "wav"] = "mp3"

    # Audio storage
    storage_backend: Literal["database", "s3", "gcs", "local"] = "database"
    storage_prefix: str = "voice-reminders"
    signed_url_expiry_seconds: int = 3600
    local_media_dir: str = "media"
    s3_bucket: str = ""
    s3_region: str | None = None
    s3_endpoint_url: str | None = None
    s3_public_base_url: str | None = None
    gcs_bucket: str = ""
    gcs_public_base_url: str | None = None

    # Telephony
    telephony_provider: Literal["plivo", "fake"] = "plivo"
    plivo_auth_id: str = ""
    plivo_auth_token: str = ""
    plivo_from_number: str = ""
    plivo_validate_signature: bool = True
    plivo_caller_name: str | None = None
    ring_timeout_seconds: int = 30
    call_time_limit_seconds: int = 300
    fake_call_outcome: str = "completed"
    fake_call_delay_seconds: int = 3

    # Batch dialing
    call_batch_size: int = 20
    call_batch_interval_seconds: float = 10
    calls_per_second: float = 2

    # Retries
    max_call_attempts: int = 2
    retry_delay_seconds: int = 900
    retry_on_statuses: list[str] = Field(default_factory=lambda: ["no_answer", "busy"])
    stale_call_timeout_minutes: int = 30

    # Personalised {placeholder} messages (one audio per recipient). Off: one shared message per campaign.
    enable_personalization: bool = False

    # Recipient import
    default_country_code: str = "IN"
    max_upload_rows: int = 10000
    max_upload_mb: int = 10

    @field_validator("retry_on_statuses", mode="before")
    @classmethod
    def _split_statuses(cls, value):
        if isinstance(value, str):
            value = value.strip()
            if value.startswith("["):
                return value
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("public_base_url")
    @classmethod
    def _strip_slash(cls, value: str) -> str:
        return value.rstrip("/")

    def voice_for(self, gender: str) -> str:
        return self.gemini_male_voice if gender == "male" else self.gemini_female_voice

    @property
    def broker_url(self) -> str:
        return self.celery_broker_url or self.redis_url

    @property
    def result_backend(self) -> str:
        return self.celery_result_backend or self.redis_url


@lru_cache
def get_settings() -> Settings:
    return Settings()
