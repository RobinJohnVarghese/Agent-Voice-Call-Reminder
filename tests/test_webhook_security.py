from plivo.utils.signature_v3 import construct_post_url, get_signature_v3


def _sign(url, params, nonce="12345", token="test-token"):
    base = construct_post_url(url, params).decode()
    return get_signature_v3(token.encode(), base, nonce.encode()).decode()


def test_signature_required_when_enabled(client, create_campaign, settings, monkeypatch):
    cid = create_campaign(csv_text="phone\n9876543210\n")["campaign"]["id"]
    client.post(f"/api/campaigns/{cid}/start")
    monkeypatch.setattr(settings, "plivo_validate_signature", True)
    monkeypatch.setattr(settings, "telephony_provider", "plivo")

    call_id = client.get(f"/api/campaigns/{cid}/calls").json()["items"][0]["id"]
    path = f"/webhooks/plivo/hangup?call_id={call_id}"
    params = {"CallStatus": "completed", "Duration": "3"}

    assert client.post(path, data=params).status_code == 403
    assert client.post(path, data=params, headers={
        "X-Plivo-Signature-V3": "bad", "X-Plivo-Signature-V3-Nonce": "12345"}).status_code == 403

    signature = _sign(settings.public_base_url + path, params)
    r = client.post(path, data=params, headers={
        "X-Plivo-Signature-V3": signature, "X-Plivo-Signature-V3-Nonce": "12345"})
    assert r.status_code == 200, r.text
