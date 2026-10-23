"""Speech recognition for videos without captions: Groq's free Whisper API (needs GROQ_API_KEY)."""
from __future__ import annotations

import logging
from pathlib import Path

import aiohttp

from .config import settings
from .media import MediaError
from .proc import run
from .subtitles import Segment

log = logging.getLogger(__name__)

MAX_UPLOAD = 24 * 1024 * 1024  # free tier accepts 25 MB per file


class ASRError(MediaError):
    pass


def available() -> bool:
    return bool(settings.groq_api_key)


async def compress_for_asr(src: Path, out: Path) -> Path:
    """16 kHz mono AAC at 32 kbps: ~14 MB per hour (Whisper resamples to 16 kHz anyway), cheap on a Pi 3."""
    await run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-i", str(src),
         "-vn", "-ac", "1", "-ar", "16000", "-c:a", "aac", "-b:a", "32k", "-threads", "2", str(out)],
        timeout=settings.job_timeout_min * 60,
    )
    return out


async def transcribe(audio: Path, lang: str | None) -> tuple[list[Segment], str | None]:
    """Return (segments, detected language)."""
    if audio.stat().st_size > MAX_UPLOAD:
        raise ASRError("Audio is too long for the free speech recognition limit")
    form = aiohttp.FormData()
    form.add_field("model", settings.asr_model)
    form.add_field("response_format", "verbose_json")
    if lang and lang != "auto":
        form.add_field("language", lang)
    form.add_field("file", audio.open("rb"), filename=audio.name, content_type="audio/mp4")
    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            data=form,
            headers={"Authorization": f"Bearer {settings.groq_api_key}"},
            timeout=aiohttp.ClientTimeout(total=600),
        ) as resp:
            if resp.status >= 400:
                raise ASRError(f"Groq ASR HTTP {resp.status}: {(await resp.text())[:200]}")
            data = await resp.json()
    segments = [
        Segment(float(s["start"]), float(s["end"]), s["text"].strip())
        for s in data.get("segments") or []
        if s.get("text", "").strip()
    ]
    detected = data.get("language")
    return segments, _whisper_lang(detected)


_WHISPER_NAMES = {
    "english": "en", "russian": "ru", "german": "de", "french": "fr", "spanish": "es", "italian": "it",
    "portuguese": "pt", "japanese": "ja", "korean": "ko", "chinese": "zh", "arabic": "ar", "turkish": "tr",
    "ukrainian": "uk", "polish": "pl", "kazakh": "kk", "hindi": "hi", "uzbek": "uz",
}


def _whisper_lang(value: str | None) -> str | None:
    if not value:
        return None
    value = value.lower()
    return _WHISPER_NAMES.get(value, value if len(value) == 2 else None)
