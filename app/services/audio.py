import io
import logging
import shutil
import subprocess
import wave

logger = logging.getLogger(__name__)


def pcm_to_wav(pcm: bytes, sample_rate: int = 24000, channels: int = 1, sample_width: int = 2) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(sample_width)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return buffer.getvalue()


def wav_to_mp3(wav_bytes: bytes) -> bytes | None:
    """Convert WAV to a telephony-friendly mono MP3. Returns None when ffmpeg is unavailable."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        logger.warning("ffmpeg not found; storing audio as WAV")
        return None
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "wav", "-i", "pipe:0",
         "-ac", "1", "-ar", "16000", "-b:a", "48k", "-f", "mp3", "pipe:1"],
        input=wav_bytes,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        logger.warning("ffmpeg conversion failed: %s", proc.stderr.decode(errors="ignore"))
        return None
    return proc.stdout


def encode(wav_bytes: bytes, preferred_format: str) -> tuple[bytes, str, str]:
    """Return (data, extension, content_type)."""
    if preferred_format == "mp3":
        mp3 = wav_to_mp3(wav_bytes)
        if mp3:
            return mp3, "mp3", "audio/mpeg"
    return wav_bytes, "wav", "audio/wav"
