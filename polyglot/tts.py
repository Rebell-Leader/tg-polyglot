"""Text-to-speech dubbing: synthesize each phrase remotely (edge-tts, gTTS fallback) and lay the clips
on a timeline with stream-copy concatenation, so the Pi never decodes or re-encodes the voice track."""
from __future__ import annotations

import asyncio
import logging
import struct
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import edge_tts

from . import langs
from .config import settings
from .media import MediaError
from .proc import run
from .subtitles import Segment

log = logging.getLogger(__name__)

MAX_SPEEDUP = 1.6  # don't make the voice faster than this to fit a slot
FRAME = 576 / 24000  # one MPEG-2 Layer III frame at 24 kHz (edge-tts and gTTS both output this)
CONCURRENCY = 4

_voices: dict[str, str] = {}


class TTSError(MediaError):
    pass


# ---------------------------------------------------------------- mp3 frame parsing

_BITRATES = {
    1: [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],  # MPEG-1 layer III
    2: [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],  # MPEG-2/2.5 layer III
}
_RATES = {3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000]}


def mp3_duration(data: bytes) -> float:
    """Exact duration by walking MPEG layer III frame headers (no ffprobe process per clip)."""
    pos = 0
    if data[:3] == b"ID3" and len(data) >= 10:
        size = data[6] << 21 | data[7] << 14 | data[8] << 7 | data[9]
        pos = 10 + size
    seconds = 0.0
    while pos + 4 <= len(data):
        (header,) = struct.unpack(">I", data[pos : pos + 4])
        version = (header >> 19) & 3
        layer = (header >> 17) & 3
        br_idx = (header >> 12) & 15
        sr_idx = (header >> 10) & 3
        if (header >> 21) != 0x7FF or version == 1 or layer != 1 or br_idx in (0, 15) or sr_idx == 3:
            pos += 1  # not a frame header: resync
            continue
        mpeg1 = version == 3
        bitrate = _BITRATES[1 if mpeg1 else 2][br_idx] * 1000
        rate = _RATES[version][sr_idx]
        samples = 1152 if mpeg1 else 576
        length = (144 if mpeg1 else 72) * bitrate // rate + ((header >> 9) & 1)
        seconds += samples / rate
        pos += length
    return seconds


# ---------------------------------------------------------------- engines


async def pick_voice(lang: str) -> str:
    if lang in langs.TTS_VOICES:
        return langs.TTS_VOICES[lang]
    if not _voices:
        for v in await edge_tts.list_voices():
            _voices.setdefault(langs.base(v["Locale"]) or "", v["ShortName"])
    if lang not in _voices:
        raise TTSError(f"No voice for language '{lang}'")
    return _voices[lang]


async def _edge(text: str, voice: str, speed: float, out: Path) -> bytes:
    rate = f"+{round((speed - 1) * 100)}%"
    audio = bytearray()
    async for chunk in edge_tts.Communicate(text, voice, rate=rate).stream():
        if chunk["type"] == "audio":
            audio += chunk["data"]
    if not audio:
        raise TTSError("edge-tts returned no audio")
    out.write_bytes(audio)
    return bytes(audio)


async def _gtts(text: str, lang: str, out: Path) -> bytes:
    from gtts import gTTS  # sync library; runs in a thread

    await asyncio.to_thread(gTTS(text, lang=lang).save, str(out))
    return out.read_bytes()


async def _speed_up(path: Path, speed: float) -> bytes:
    """Local tempo change for engines without a rate parameter; output keeps the 24 kHz mono format."""
    tmp = path.with_suffix(".fast.mp3")
    await run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-i", str(path),
         "-filter:a", f"atempo={speed:.3f}", "-ar", "24000", "-ac", "1",
         "-c:a", "libmp3lame", "-b:a", "48k", "-write_xing", "0", str(tmp)],
        timeout=120,
    )
    tmp.replace(path)
    return path.read_bytes()


_edge_down_until = 0.0  # edge-tts is an unofficial API; after a failure skip it for a while


class Synth:
    """Picks one TTS engine per job (so the voice doesn't change mid-video), with per-clip fallback."""

    def __init__(self, lang: str, voice: str):
        self.lang = lang
        self.voice = voice
        self.engine: str | None = None
        self.engines_used: set[str] = set()

    @property
    def concurrency(self) -> int:
        return CONCURRENCY if self.engine == "edge" else 2

    async def choose(self, text: str, out: Path, slot: float) -> float:
        """Synthesize the first clip with the first engine that works; remember it for the job."""
        global _edge_down_until
        errors = []
        for name in settings.tts_engines:
            if name == "edge" and (not self.voice or time.monotonic() < _edge_down_until):
                continue
            try:
                duration = await self._say_with(name, text, out, slot)
            except Exception as err:
                errors.append(f"{name}: {err}")
                log.warning("TTS engine %s unavailable: %s", name, err)
                if name == "edge":
                    _edge_down_until = time.monotonic() + 30 * 60
                continue
            self.engine = name
            return duration
        raise TTSError("No speech synthesis engine works: " + "; ".join(errors))

    async def say(self, text: str, out: Path, slot: float) -> float:
        assert self.engine
        try:
            return await self._say_with(self.engine, text, out, slot)
        except Exception as err:
            log.warning("%s failed on a clip, retrying: %s", self.engine, err)
            await asyncio.sleep(2)
        try:
            return await self._say_with(self.engine, text, out, slot)
        except Exception:
            fallback = next((e for e in settings.tts_engines if e != self.engine), None)
            if not fallback:
                raise
            return await self._say_with(fallback, text, out, slot)

    async def _say_with(self, engine: str, text: str, out: Path, slot: float) -> float:
        fits = slot > 0.3
        if engine == "edge":
            duration = mp3_duration(await _edge(text, self.voice, 1.0, out))
            if fits and duration > slot * 1.05:
                # Too long for its slot: ask the service to speak faster (costs no local CPU)
                duration = mp3_duration(await _edge(text, self.voice, min(duration / slot, MAX_SPEEDUP), out))
            self.engines_used.add("edge-tts")
        elif engine == "gtts":
            duration = mp3_duration(await _gtts(text, self.lang, out))
            if fits and duration > slot * 1.05:
                duration = mp3_duration(await _speed_up(out, min(duration / slot, MAX_SPEEDUP)))
            self.engines_used.add("Google TTS")
        else:
            raise TTSError(f"unknown TTS engine {engine}")
        return duration


# ---------------------------------------------------------------- timeline


async def _silence(workdir: Path, seconds: float = 30.0) -> Path:
    path = workdir / "silence.mp3"
    if not path.exists():
        await run(
            ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
             "-filter_complex", "anullsrc=r=24000:cl=mono", "-t", str(seconds),
             "-c:a", "libmp3lame", "-b:a", "48k", "-write_xing", "0", str(path)],
            timeout=120,
        )
    return path


def build_concat_list(clips: list[tuple[float, str, float]], silence_name: str, silence_len: float) -> list[str]:
    """clips: (start, filename, duration). Insert frame-exact silences so each clip starts on time,
    or right after the previous one if that ran long."""
    lines = []
    cursor = 0.0
    for start, name, duration in clips:
        frames = round((start - cursor) / FRAME)
        if frames > 0:
            cursor += frames * FRAME
            per_file = int(silence_len / FRAME) - 1
            while frames > 0:
                n = min(frames, per_file)
                lines += [f"file '{silence_name}'", f"outpoint {(n - 0.5) * FRAME:.4f}"]
                frames -= n
        lines.append(f"file '{name}'")
        cursor += duration
    return lines


async def dub(
    segments: list[Segment], lang: str, workdir: Path, on_progress: Callable[[float], Any] | None = None
) -> tuple[Path, str]:
    """Return (voice track mp3, engine description)."""
    if not segments:
        raise TTSError("Nothing to speak")
    try:
        voice = await pick_voice(lang)
    except Exception as err:  # the voice list is fetched from Microsoft; gTTS works without it
        log.warning("no edge-tts voice: %s", err)
        voice = ""
    synth = Synth(lang, voice)
    durations: list[float] = [0.0] * len(segments)
    done = 0

    def slot(i: int) -> float:
        nxt = segments[i + 1].start if i + 1 < len(segments) else segments[i].end + 1.5
        return nxt - segments[i].start

    async def report() -> None:
        if on_progress:
            res = on_progress(done / len(segments))
            if asyncio.iscoroutine(res):
                await res

    durations[0] = await synth.choose(segments[0].text, workdir / "tts_00000.mp3", slot(0))
    done = 1
    await report()
    sem = asyncio.Semaphore(synth.concurrency)

    async def one(i: int) -> None:
        nonlocal done
        async with sem:
            durations[i] = await synth.say(segments[i].text, workdir / f"tts_{i:05}.mp3", slot(i))
        done += 1
        await report()

    await asyncio.gather(*(one(i) for i in range(1, len(segments))))

    silence = await _silence(workdir)
    lines = build_concat_list(
        [(s.start, f"tts_{i:05}.mp3", durations[i]) for i, s in enumerate(segments)], silence.name, 30.0
    )
    concat = workdir / "tts_concat.txt"
    concat.write_text("\n".join(lines) + "\n")
    out = workdir / "voice_tts.mp3"
    await run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
         "-f", "concat", "-safe", "0", "-i", str(concat), "-c", "copy", str(out)],
        timeout=600,
    )
    return out, " + ".join(sorted(synth.engines_used))
