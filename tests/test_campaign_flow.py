from sqlalchemy import select

from app.database import SessionLocal
from app.models import Call, CallStatus, CampaignStatus
from app.worker import tasks


def _calls(campaign_id):
    with SessionLocal() as db:
        return db.scalars(select(Call).where(Call.campaign_id == campaign_id).order_by(Call.id)).all()


def test_create_campaign_imports_and_generates_audio(client, create_campaign):
    data = create_campaign()
    assert data["import_summary"] == {
        "imported": 2, "duplicates": 1, "invalid": 1, "errors": [{"row": 5, "error": "invalid phone number '12345'"}]
    }
    campaign = client.get(f"/api/campaigns/{data['campaign']['id']}").json()
    assert campaign["status"] == "ready"
    assert campaign["has_audio"] is True
    assert campaign["personalized"] is False
    assert campaign["status_counts"]["pending"] == 2

    audio = client.get(f"/api/campaigns/{campaign['id']}/audio", follow_redirects=False)
    assert audio.status_code == 307
    assert audio.headers["location"].startswith("https://voice.example.test/media/voice-reminders/campaigns/")
    local = audio.headers["location"].replace("https://voice.example.test", "")
    assert client.get(local).content[:4] == b"RIFF"


def test_rejects_unknown_placeholder(client):
    files = {"file": ("p.csv", b"phone\n9876543210\n", "text/csv")}
    r = client.post("/api/campaigns", data={"name": "x", "message": "Hi {name} {date}"}, files=files)
    assert r.status_code == 422
    assert "date" in r.json()["detail"]


def test_rejects_unknown_language(client):
    files = {"file": ("p.csv", b"phone\n9876543210\n", "text/csv")}
    r = client.post("/api/campaigns", data={"name": "x", "message": "Hi", "language": "xx"}, files=files)
    assert r.status_code == 422


def test_start_dials_all_recipients(client, create_campaign):
    cid = create_campaign()["campaign"]["id"]
    r = client.post(f"/api/campaigns/{cid}/start")
    assert r.status_code == 200
    assert r.json()["status"] == "running"
    calls = _calls(cid)
    assert {c.status for c in calls} == {CallStatus.initiated}
    assert all(c.attempts == 1 and c.plivo_request_uuid.startswith("fake-") for c in calls)
    assert client.post(f"/api/campaigns/{cid}/start").status_code == 409


def test_auto_start(client, create_campaign):
    cid = create_campaign(auto_start="true")["campaign"]["id"]
    assert client.get(f"/api/campaigns/{cid}").json()["status"] == "running"


def test_personalized_audio_per_recipient(client, create_campaign):
    cid = create_campaign(message="Hello {name}, your appointment is on {appointment_date}.")["campaign"]["id"]
    campaign = client.get(f"/api/campaigns/{cid}").json()
    assert campaign["personalized"] is True
    client.post(f"/api/campaigns/{cid}/start")
    calls = _calls(cid)
    assert calls[0].spoken_message == "Hello Asha, your appointment is on 28 Nov."
    assert all(c.audio_key and "/calls/" in c.audio_key for c in calls)


def test_full_call_lifecycle_and_campaign_completion(client, create_campaign):
    cid = create_campaign()["campaign"]["id"]
    client.post(f"/api/campaigns/{cid}/start")
    first, second = _calls(cid)

    r = client.post(f"/webhooks/plivo/ring?call_id={first.id}", data={"CallStatus": "ringing", "CallUUID": "u1"})
    assert r.status_code == 200
    r = client.post(f"/webhooks/plivo/answer?call_id={first.id}", data={"CallStatus": "in-progress", "CallUUID": "u1"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/xml")
    assert "<Play>https://voice.example.test/media/voice-reminders/campaigns/" in r.text
    assert "<Hangup" in r.text

    client.post(
        f"/webhooks/plivo/hangup?call_id={first.id}",
        data={"CallStatus": "completed", "HangupCauseName": "End Of XML Instructions", "Duration": "14",
              "BillDuration": "60", "CallUUID": "u1"},
    )
    # late ring event must not downgrade a final status
    client.post(f"/webhooks/plivo/ring?call_id={first.id}", data={"CallStatus": "ringing"})

    client.post(
        f"/webhooks/plivo/hangup?call_id={second.id}",
        data={"CallStatus": "failed", "HangupCauseName": "Unallocated Number", "Duration": "0"},
    )

    first, second = _calls(cid)
    assert first.status == CallStatus.completed
    assert first.duration_seconds == 14 and first.answered_at and first.plivo_call_uuid == "u1"
    assert second.status == CallStatus.failed and second.last_error == "Unallocated Number"
    campaign = client.get(f"/api/campaigns/{cid}").json()
    assert campaign["status"] == CampaignStatus.completed.value

    export = client.get(f"/api/campaigns/{cid}/export").text.splitlines()
    assert export[0].startswith("name,phone_number,status")
    assert "completed" in export[1] and "failed" in export[2]


def test_no_answer_retries_then_gives_up(client, create_campaign, monkeypatch):
    scheduled = []
    monkeypatch.setattr(tasks.place_call, "apply_async", lambda args, countdown: scheduled.append((args, countdown)))
    cid = create_campaign(csv_text="phone\n9876543210\n")["campaign"]["id"]
    client.post(f"/api/campaigns/{cid}/start")
    (call,) = _calls(cid)

    client.post(f"/webhooks/plivo/hangup?call_id={call.id}", data={"CallStatus": "no-answer"})
    (call,) = _calls(cid)
    assert call.status == CallStatus.retry_scheduled
    assert scheduled == [([call.id], 60)]

    # duplicate hangup callback must not schedule a second retry
    client.post(f"/webhooks/plivo/hangup?call_id={call.id}", data={"CallStatus": "no-answer"})
    assert len(scheduled) == 1

    tasks.place_call(call.id)
    (call,) = _calls(cid)
    assert call.status == CallStatus.initiated and call.attempts == 2

    client.post(
        f"/webhooks/plivo/hangup?call_id={call.id}", data={"CallStatus": "busy", "HangupCauseName": "Busy Line"}
    )
    (call,) = _calls(cid)
    assert call.status == CallStatus.busy
    assert len(scheduled) == 1
    assert client.get(f"/api/campaigns/{cid}").json()["status"] == "completed"


def test_hangup_lookup_by_request_uuid(client, create_campaign):
    cid = create_campaign(csv_text="phone\n9876543210\n")["campaign"]["id"]
    client.post(f"/api/campaigns/{cid}/start")
    (call,) = _calls(cid)
    r = client.post("/webhooks/plivo/hangup", data={"RequestUUID": call.plivo_request_uuid, "CallStatus": "completed",
                                                    "Duration": "5"})
    assert r.json() == {"ok": True, "status": "completed"}


def test_cancel_campaign(client, create_campaign):
    cid = create_campaign()["campaign"]["id"]
    r = client.post(f"/api/campaigns/{cid}/cancel")
    assert r.json()["status"] == "cancelled"
    assert {c.status for c in _calls(cid)} == {CallStatus.cancelled}


def test_dial_error_marks_failed(client, create_campaign, monkeypatch):
    class Broken:
        def place_call(self, **kwargs):
            raise RuntimeError("boom")

    monkeypatch.setattr(tasks, "get_telephony", lambda settings=None: Broken())
    cid = create_campaign(csv_text="phone\n9876543210\n")["campaign"]["id"]
    client.post(f"/api/campaigns/{cid}/start")
    (call,) = _calls(cid)
    assert call.status == CallStatus.failed and call.last_error == "boom" and call.attempts == 1
    assert client.get(f"/api/campaigns/{cid}").json()["status"] == "completed"


def test_simulated_fake_call(client, create_campaign):
    cid = create_campaign(csv_text="phone\n9876543210\n")["campaign"]["id"]
    client.post(f"/api/campaigns/{cid}/start")
    (call,) = _calls(cid)
    tasks.simulate_fake_call(call.id)
    (call,) = _calls(cid)
    assert call.status == CallStatus.completed and call.duration_seconds == 12


def test_reconcile_fails_stale_calls_and_completes_campaign(client, create_campaign, monkeypatch):
    from datetime import timedelta

    from app.services.call_status import utcnow

    cid = create_campaign(csv_text="phone\n9876543210\n")["campaign"]["id"]
    client.post(f"/api/campaigns/{cid}/start")
    (call,) = _calls(cid)
    with SessionLocal() as db:
        db.get(Call, call.id).updated_at = utcnow() - timedelta(hours=2)
        db.commit()
    monkeypatch.setattr(tasks.place_call, "apply_async", lambda args, countdown: None)
    stats = tasks.reconcile_calls()
    assert stats["stale_failed"] == 1
    (call,) = _calls(cid)
    assert call.status == CallStatus.failed
    assert client.get(f"/api/campaigns/{cid}").json()["status"] == "completed"
