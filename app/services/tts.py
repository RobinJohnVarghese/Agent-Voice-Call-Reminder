import logging
import math
import re
import struct
from typing import Protocol

from app.config import Settings, get_settings
from app.languages import language_name
from app.services.audio import pcm_to_wav

logger = logging.getLogger(__name__)


class TTSError(RuntimeError):
    pass


class TTSProvider(Protocol):
    def synthesize(self, text: str, language: str, voice: str) -> bytes:
        """Return WAV bytes for the given text."""

    def translate(self, text: str, language: str) -> str:
        """Translate text into the target language, preserving {placeholders}."""


def _parse_rate(mime_type: str | None, default: int = 24000) -> int:
    match = re.search(r"rate=(\d+)", mime_type or "")
    return int(match.group(1)) if match else default


class GeminiTTS:
    def __init__(self, settings: Settings):
        if not settings.gemini_api_key:
            raise TTSError("GEMINI_API_KEY is not configured")
        from google import genai

        self.settings = settings
        self.client = genai.Client(api_key=settings.gemini_api_key)

    def synthesize(self, text: str, language: str, voice: str) -> bytes:
        from google.genai import types

        instruction = self.settings.tts_style_instruction.format(language=language_name(language))
        prompt = f"{instruction}:\n\n{text}" if instruction else text
        try:
            response = self.client.models.generate_content(
                model=self.settings.gemini_tts_model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_modalities=["AUDIO"],
                    speech_config=types.SpeechConfig(
                        voice_config=types.VoiceConfig(
                            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)
                        )
                    ),
                ),
            )
        except Exception as exc:
            raise TTSError(f"Gemini TTS request failed: {exc}") from exc

        pcm = bytearray()
        rate = 24000
        for candidate in response.candidates or []:
            for part in (candidate.content.parts if candidate.content else None) or []:
                if part.inline_data and part.inline_data.data:
                    pcm.extend(part.inline_data.data)
                    rate = _parse_rate(part.inline_data.mime_type, rate)
        if not pcm:
            raise TTSError("Gemini TTS returned no audio")
        return pcm_to_wav(bytes(pcm), sample_rate=rate)

    def translate(self, text: str, language: str) -> str:
        prompt = (
            f"Translate the following phone call reminder into {language_name(language)}. "
            "Keep any placeholders in curly braces (for example {name}) exactly as they are, untranslated. "
            "Return only the translated text, nothing else.\n\n" + text
        )
        try:
            response = self.client.models.generate_content(model=self.settings.gemini_text_model, contents=prompt)
        except Exception as exc:
            raise TTSError(f"Gemini translation failed: {exc}") from exc
        translated = (response.text or "").strip()
        if not translated:
            raise TTSError("Gemini translation returned no text")
        return translated


class FakeTTS:
    """Generates a short tone instead of speech. For local development and tests."""

    def synthesize(self, text: str, language: str, voice: str) -> bytes:
        rate = 8000
        seconds = min(max(len(text) / 15, 1.0), 10.0)
        frames = b"".join(
            struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate))) for i in range(int(rate * seconds))
        )
        return pcm_to_wav(frames, sample_rate=rate)

    def translate(self, text: str, language: str) -> str:
        return text


def get_tts(settings: Settings | None = None) -> TTSProvider:
    settings = settings or get_settings()
    if settings.tts_provider == "fake":
        return FakeTTS()
    return GeminiTTS(settings)
