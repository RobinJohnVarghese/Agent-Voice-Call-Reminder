import logging
import uuid
from typing import Protocol

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)


class TelephonyError(RuntimeError):
    pass


class Telephony(Protocol):
    def place_call(self, to: str, answer_url: str, hangup_url: str, ring_url: str) -> str:
        """Start an outbound call and return the provider request id."""


def _plivo_number(number: str) -> str:
    return number.lstrip("+")


class PlivoTelephony:
    def __init__(self, settings: Settings):
        if not (settings.plivo_auth_id and settings.plivo_auth_token and settings.plivo_from_number):
            raise TelephonyError("PLIVO_AUTH_ID, PLIVO_AUTH_TOKEN and PLIVO_FROM_NUMBER must be configured")
        import plivo

        self.settings = settings
        self.client = plivo.RestClient(settings.plivo_auth_id, settings.plivo_auth_token)

    def place_call(self, to: str, answer_url: str, hangup_url: str, ring_url: str) -> str:
        s = self.settings
        try:
            response = self.client.calls.create(
                from_=_plivo_number(s.plivo_from_number),
                to_=_plivo_number(to),
                answer_url=answer_url,
                answer_method="POST",
                hangup_url=hangup_url,
                hangup_method="POST",
                ring_url=ring_url,
                ring_method="POST",
                ring_timeout=s.ring_timeout_seconds,
                time_limit=s.call_time_limit_seconds,
                caller_name=s.plivo_caller_name,
            )
        except Exception as exc:
            raise TelephonyError(f"Plivo call request failed: {exc}") from exc
        request_uuid = getattr(response, "request_uuid", None)
        if request_uuid is None and isinstance(response, dict):
            request_uuid = response.get("request_uuid")
        if isinstance(request_uuid, list):
            request_uuid = request_uuid[0] if request_uuid else None
        if not request_uuid:
            raise TelephonyError(f"Plivo did not return a request_uuid: {response}")
        return str(request_uuid)


class FakeTelephony:
    """Pretends to dial. The worker simulates Plivo callbacks (see simulate_fake_call)."""

    def place_call(self, to: str, answer_url: str, hangup_url: str, ring_url: str) -> str:
        logger.info("[fake telephony] calling %s", to)
        return f"fake-{uuid.uuid4()}"


def get_telephony(settings: Settings | None = None) -> Telephony:
    settings = settings or get_settings()
    if settings.telephony_provider == "fake":
        return FakeTelephony()
    return PlivoTelephony(settings)
