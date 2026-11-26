import os
import tempfile
from pathlib import Path

_tmp = Path(tempfile.mkdtemp(prefix="vcr-tests-"))
os.environ.update(
    {
        "DATABASE_URL": os.environ.get("TEST_DATABASE_URL", f"sqlite:///{_tmp / 'test.db'}"),
        "CELERY_TASK_ALWAYS_EAGER": "true",
        "CELERY_BROKER_URL": "memory://",
        "CELERY_RESULT_BACKEND": "cache+memory://",
        "TTS_PROVIDER": "fake",
        "TELEPHONY_PROVIDER": "fake",
        "FAKE_CALL_DELAY_SECONDS": "-1",
        "FAKE_CALL_OUTCOME": "completed",
        "STORAGE_BACKEND": "database",
        "LOCAL_MEDIA_DIR": str(_tmp / "media"),
        "AUDIO_FORMAT": "wav",
        "PUBLIC_BASE_URL": "https://voice.example.test",
        "CALLS_PER_SECOND": "1000",
        "PLIVO_VALIDATE_SIGNATURE": "false",
        "PLIVO_AUTH_TOKEN": "test-token",
        "MAX_CALL_ATTEMPTS": "2",
        "RETRY_DELAY_SECONDS": "60",
    }
)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.database import Base, engine  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield


@pytest.fixture
def settings():
    return get_settings()


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


CSV = (
    "Name,Phone Number,Email ID,Appointment Date\n"
    "Asha,+919876543210,asha@example.com,28 Nov\n"
    "Rahul,9876501234,rahul@example.com,29 Nov\n"
    "Dup,919876543210,,30 Nov\n"
    "Bad,12345,,1 Dec\n"
)


@pytest.fixture
def create_campaign(client):
    def _create(message="Hello, this is your reminder.", csv_text=CSV, **fields):
        data = {"name": "Test", "message": message, "language": "en-IN", **fields}
        files = {"file": ("people.csv", csv_text.encode(), "text/csv")}
        response = client.post("/api/campaigns/upload", data=data, files=files)
        assert response.status_code == 201, response.text
        return response.json()

    return _create
