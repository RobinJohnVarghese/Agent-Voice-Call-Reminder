import os
from pathlib import Path
from typing import Protocol

from app.config import Settings, get_settings


class StorageError(RuntimeError):
    pass


class Storage(Protocol):
    def save(self, key: str, data: bytes, content_type: str) -> str: ...

    def playback_url(self, key: str) -> str: ...


def build_key(settings: Settings, *parts: str) -> str:
    prefix = settings.storage_prefix.strip("/")
    return "/".join([p for p in (prefix, *parts) if p])


class DatabaseStorage:
    """Stores audio in the PostgreSQL `audio_files` table; served at {PUBLIC_BASE_URL}/media/<key>."""

    def __init__(self, settings: Settings):
        self.base_url = settings.public_base_url

    def save(self, key: str, data: bytes, content_type: str) -> str:
        from app.database import session_scope
        from app.models import AudioFile

        with session_scope() as db:
            db.merge(AudioFile(key=key, content_type=content_type, size_bytes=len(data), data=data))
        return key

    def playback_url(self, key: str) -> str:
        return f"{self.base_url}/media/{key}"


class LocalStorage:
    """Stores audio on disk; FastAPI serves it at {PUBLIC_BASE_URL}/media/<key>."""

    def __init__(self, settings: Settings):
        self.root = Path(settings.local_media_dir).resolve()
        self.base_url = settings.public_base_url

    def save(self, key: str, data: bytes, content_type: str) -> str:
        path = (self.root / key).resolve()
        if self.root not in path.parents:
            raise StorageError("Invalid storage key")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        return key

    def playback_url(self, key: str) -> str:
        return f"{self.base_url}/media/{key}"


class S3Storage:
    def __init__(self, settings: Settings):
        if not settings.s3_bucket:
            raise StorageError("S3_BUCKET is not configured")
        import boto3

        self.bucket = settings.s3_bucket
        self.expiry = settings.signed_url_expiry_seconds
        self.public_base_url = (settings.s3_public_base_url or "").rstrip("/") or None
        self.client = boto3.client("s3", region_name=settings.s3_region, endpoint_url=settings.s3_endpoint_url)

    def save(self, key: str, data: bytes, content_type: str) -> str:
        try:
            self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)
        except Exception as exc:
            raise StorageError(f"S3 upload failed: {exc}") from exc
        return key

    def playback_url(self, key: str) -> str:
        if self.public_base_url:
            return f"{self.public_base_url}/{key}"
        return self.client.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=self.expiry
        )


class GCSStorage:
    def __init__(self, settings: Settings):
        if not settings.gcs_bucket:
            raise StorageError("GCS_BUCKET is not configured")
        from google.cloud import storage

        self.expiry = settings.signed_url_expiry_seconds
        self.public_base_url = (settings.gcs_public_base_url or "").rstrip("/") or None
        self.bucket = storage.Client().bucket(settings.gcs_bucket)

    def save(self, key: str, data: bytes, content_type: str) -> str:
        try:
            self.bucket.blob(key).upload_from_string(data, content_type=content_type)
        except Exception as exc:
            raise StorageError(f"GCS upload failed: {exc}") from exc
        return key

    def playback_url(self, key: str) -> str:
        if self.public_base_url:
            return f"{self.public_base_url}/{key}"
        from datetime import timedelta

        return self.bucket.blob(key).generate_signed_url(
            version="v4", expiration=timedelta(seconds=self.expiry), method="GET"
        )


_BACKENDS = {"database": DatabaseStorage, "local": LocalStorage, "s3": S3Storage, "gcs": GCSStorage}
_cache: dict[str, Storage] = {}


def get_storage(settings: Settings | None = None) -> Storage:
    settings = settings or get_settings()
    backend = settings.storage_backend
    if backend not in _cache:
        _cache[backend] = _BACKENDS[backend](settings)
    return _cache[backend]
