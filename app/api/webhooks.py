import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from plivo import plivoxml
from plivo.utils import validate_v3_signature
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models import Call, CallStatus
from app.services.call_status import apply_plivo_hangup, update_call_status
from app.services.storage import get_storage
from app.worker.tasks import schedule_retry

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks/plivo", tags=["plivo webhooks"])


async def plivo_params(request: Request) -> dict:
    params = dict(request.query_params)
    if request.method == "POST":
        form = await request.form()
        params.update({k: str(v) for k, v in form.items()})

    settings = get_settings()
    if settings.plivo_validate_signature and settings.telephony_provider == "plivo":
        signature = request.headers.get("X-Plivo-Signature-V3")
        nonce = request.headers.get("X-Plivo-Signature-V3-Nonce")
        if not signature or not nonce:
            raise HTTPException(403, "Missing Plivo signature")
        url = settings.public_base_url + request.url.path
        if request.url.query:
            url += "?" + request.url.query
        body = {k: v for k, v in params.items() if k not in request.query_params} if request.method == "POST" else {}
        try:
            valid = validate_v3_signature(request.method, url, nonce, settings.plivo_auth_token, signature, body)
        except Exception:
            valid = False
        if not valid:
            logger.warning("Rejected Plivo webhook with invalid signature: %s", url)
            raise HTTPException(403, "Invalid Plivo signature")
    return params


def find_call(db: Session, params: dict) -> Call:
    call = None
    call_id = params.get("call_id")
    if call_id and str(call_id).isdigit():
        call = db.get(Call, int(call_id))
    if call is None:
        uuids = [u for u in (params.get("RequestUUID"), params.get("CallUUID")) if u]
        if uuids:
            call = db.scalars(
                select(Call).where(or_(Call.plivo_request_uuid.in_(uuids), Call.plivo_call_uuid.in_(uuids)))
            ).first()
    if call is None:
        raise HTTPException(404, "Call not found")
    return call


def _xml(response: plivoxml.ResponseElement) -> Response:
    return Response(content=response.to_string(), media_type="application/xml")


@router.api_route("/answer", methods=["GET", "POST"])
async def answer(params: dict = Depends(plivo_params), db: Session = Depends(get_db)):
    call = find_call(db, params)
    call.plivo_call_uuid = params.get("CallUUID") or call.plivo_call_uuid
    update_call_status(db, call, CallStatus.in_progress, "answer", params)
    db.commit()

    response = plivoxml.ResponseElement()
    audio_key = call.audio_key or (None if call.campaign.personalized else call.campaign.audio_key)
    if audio_key:
        response.add(plivoxml.PlayElement(get_storage().playback_url(audio_key)))
    else:
        logger.error("No audio available for call %s", call.id)
    response.add(plivoxml.HangupElement())
    return _xml(response)


@router.api_route("/ring", methods=["GET", "POST"])
async def ring(params: dict = Depends(plivo_params), db: Session = Depends(get_db)):
    call = find_call(db, params)
    call.plivo_call_uuid = params.get("CallUUID") or call.plivo_call_uuid
    update_call_status(db, call, CallStatus.ringing, "ring", params)
    db.commit()
    return {"ok": True}


@router.api_route("/hangup", methods=["GET", "POST"])
async def hangup(params: dict = Depends(plivo_params), db: Session = Depends(get_db)):
    call = find_call(db, params)
    retry_delay = apply_plivo_hangup(db, call, params)
    db.commit()
    schedule_retry(call.id, retry_delay)
    return {"ok": True, "status": call.status.value}
