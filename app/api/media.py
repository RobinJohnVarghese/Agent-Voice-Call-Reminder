from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import AudioFile

router = APIRouter(tags=["media"])


@router.api_route("/media/{key:path}", methods=["GET", "HEAD"], include_in_schema=False)
def database_media(key: str, db: Session = Depends(get_db)):
    audio = db.get(AudioFile, key)
    if audio is None:
        raise HTTPException(404, "Audio not found")
    return Response(
        content=audio.data,
        media_type=audio.content_type,
        headers={"Cache-Control": "public, max-age=86400", "Content-Length": str(audio.size_bytes)},
    )
