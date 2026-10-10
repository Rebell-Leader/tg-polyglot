"""One translation job: probe -> (voice engine ‖ media download) -> assemble. Engines fall back in order."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import asr, langs, media, subtitles, translate, tts
from .config import settings
from .media import MediaError
from .proc import ProcError, run
from .subtitles import Segment

log = logging.getLogger(__name__)

Progress = Callable[[str, str], Awaitable[None]]  # (slot, text) -> shows a status line

MODES = ("video", "audio", "text")
_YT_ID = re.compile(r"(?:youtube\.com/(?:watch\?.*?v=|shorts/|live/|embed/)|youtu\.be/)([\w-]{11})")


def cache_key(url: str, mode: str, src: str, dst: str) -> str:
    m = _YT_ID.search(url)
    video = f"yt:{m.group(1)}" if m else url
    return f"{video}|{src}|{dst}|{mode}"


@dataclass
class Job:
    url: str
    mode: str
    src: str
    dst: str
    workdir: Path


@dataclass
class Result:
    kind: str  # video | audio | text
    title: str
    engine: str
    src: str | None
    duration: float = 0
    path: Path | None = None
    width: int = 0
    height: int = 0
    performer: str | None = None
    text: str | None = None
    extra_files: list[Path] = field(default_factory=list)


async def _noop(slot: str, text: str) -> None:
    pass


async def _all(*aws: Awaitable[Any]) -> list[Any]:
    """Like gather(), but the first failure cancels the rest (no orphaned downloads on a busy Pi)."""
    tasks = [asyncio.ensure_future(a) for a in aws]
    try:
        return await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


def _mark_retrieved(fut: asyncio.Future[Any]) -> None:
    """A failed download nobody awaited (e.g. the job already failed) shouldn't log 'never retrieved'."""
    if not fut.cancelled():
        fut.exception()


def _fmt_time(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 60}:{seconds % 60:02}" if seconds < 3600 else f"{seconds // 3600}h{seconds % 3600 // 60:02}m"


class Pipeline:
    def __init__(self, job: Job, progress: Progress | None = None):
        self.job = job
        self.progress = progress or _noop
        self.info: dict[str, Any] = {}
        self.info_path = job.workdir / "info.json"
        self.src: str | None = None  # resolved source language (None = unknown)
        self._files: dict[str, asyncio.Future[Path]] = {}  # format_id -> downloaded file
        self._downloads: list[asyncio.Task[None]] = []
        self._yandex_audio: asyncio.Future[Path] | None = None  # original audio Yandex may ask for
        self._segments: tuple[list[Segment], str | None, str] | None = None
        self._refresh_lock = asyncio.Lock()
        self._info_generation = 0

    # ------------------------------------------------------------ entry point

    async def run(self) -> Result:
        try:
            return await self._run()
        finally:
            await self.close()

    async def close(self) -> None:
        """Stop downloads nobody waits for anymore (failed job, engine switched, ...): no orphan yt-dlp."""
        for task in self._downloads:
            task.cancel()
        await asyncio.gather(*self._downloads, return_exceptions=True)

    async def _run(self) -> Result:
        job = self.job
        await self.progress("main", "🔎 Reading video info…")
        self.info, self.info_path = await media.probe(job.url, job.workdir)
        duration = self.info.get("duration") or 0
        if duration > settings.max_duration_min * 60:
            raise MediaError(f"The video is longer than {settings.max_duration_min} minutes.")
        detected = langs.base(self.info.get("language"))
        self.src = job.src if job.src != "auto" else detected
        if self.src and self.src == job.dst:
            raise MediaError(f"The video already seems to be in {langs.name(job.dst)}.")
        await self.progress("main", f"🎞 <b>{_esc(self.title)}</b> ({_fmt_time(duration)})")

        if job.mode == "text":
            return await self.make_text()
        if "ytdub" in settings.voice_engines and (dub := media.pick_dub_audio(self.info, job.dst)):
            try:
                return await self.make_from_youtube_dub(dub)
            except (MediaError, ProcError) as err:
                log.warning("YouTube dub failed: %s", err)
                await self.progress("voice", "⚠️ YouTube dub failed, trying the next engine…")
        return await self.make_voiced()

    @property
    def title(self) -> str:
        return self.info.get("title") or self.info.get("id") or "video"

    @property
    def duration(self) -> float:
        return float(self.info.get("duration") or 0)

    def _result(self, kind: str, engine: str, **kwargs: Any) -> Result:
        return Result(
            kind=kind,
            title=self.title,
            engine=engine,
            src=self.src,
            duration=self.duration,
            performer=self.info.get("uploader") or self.info.get("channel"),
            **kwargs,
        )

    # ------------------------------------------------------------ downloads

    def fetch(self, wanted: list[tuple[str, str, float | None]]) -> list[asyncio.Future[Path]]:
        """Request formats as (format_id, label, size). New ones are downloaded by ONE yt-dlp process in the
        given order; already requested ones are shared. Returns a future per format, resolved as soon as that
        file is complete. (One process instead of one per format saves ~60 MB RAM + a slow start-up on a Pi.)"""
        loop = asyncio.get_running_loop()
        batch = []
        for format_id, label, size in wanted:
            if format_id not in self._files:
                fut: asyncio.Future[Path] = loop.create_future()
                fut.add_done_callback(_mark_retrieved)
                self._files[format_id] = fut
                batch.append((format_id, label, size))
        if batch:
            self._downloads.append(asyncio.ensure_future(self._download_batch(batch)))
        return [self._files[format_id] for format_id, _, _ in wanted]

    async def _download_batch(self, batch: list[tuple[str, str, float | None]]) -> None:
        ids = [format_id for format_id, _, _ in batch]
        what = " + ".join(label for _, label, _ in batch)
        slot = f"dl:{ids[0]}"
        total = int(sum(size or 0 for _, _, size in batch)) or None

        async def on_progress(fraction: float) -> None:
            await self.progress(slot, f"⬇️ Downloading {what}… {fraction:.0%}")

        def on_file(format_id: str, path: Path) -> None:
            fut = self._files.get(format_id)
            if fut and not fut.done():
                fut.set_result(path)

        await self.progress(slot, f"⬇️ Downloading {what}…")
        try:
            for attempt in range(2):
                remaining = [i for i in ids if not self._files[i].done()]
                generation = self._info_generation
                try:
                    await media.download_formats(
                        self.info_path, remaining, self.job.workdir, on_progress, total, on_file
                    )
                    break
                except MediaError as err:
                    if attempt or not err.retryable:
                        raise
                    log.info("download of %s rejected (%s), refreshing metadata", remaining, err)
                    await self._refresh_info(generation)
        except asyncio.CancelledError:
            for i in ids:
                self._files[i].cancel()
            raise
        except Exception as err:  # handed to whoever awaits the files
            for i in ids:
                if not self._files[i].done():
                    self._files[i].set_exception(err)
            return
        await self.progress(slot, f"✅ Downloaded {what}")

    async def _refresh_info(self, generation: int) -> None:
        async with self._refresh_lock:
            if generation == self._info_generation:  # parallel failures refresh only once
                self.info, self.info_path = await media.probe(self.job.url, self.job.workdir)
                self._info_generation += 1

    def original_audio(self) -> asyncio.Future[Path]:
        """The original soundtrack (an audio-only format, or the smallest file that has one)."""
        picked = media.pick_any_audio(self.info)
        if not picked:
            raise MediaError("This video has no audio track.")
        fmt, _ = picked
        return self.fetch([(fmt["format_id"], "audio", media._size(fmt, self.duration))])[0]

    def audio_kbps(self) -> int:
        """Bitrate for an audio-only result, lowered for long videos to fit Telegram's limit."""
        if not self.duration:
            return settings.max_audio_kbps
        fits = int(settings.upload_limit * 8 / self.duration / 1000 * 0.92)
        return max(24, min(settings.max_audio_kbps, fits))

    def _audio_bytes(self, kbps: int) -> float:
        return kbps * 1000 / 8 * self.duration

    def plan_video(self, audio_options: list[tuple[int | None, float]]) -> tuple[media.Selection, int | None]:
        """Pick the picture and the audio bitrate together: on long videos a leaner audio track can buy
        a sharper picture (e.g. 480p instead of 360p within 50 MB). audio_options: (kbps or None=copy, bytes),
        most preferred first."""
        best: tuple[media.Selection, int | None] | None = None
        for kbps, audio_size in audio_options:
            budget = settings.upload_limit - audio_size - 2**20
            try:
                sel = media.pick_video(self.info, budget, settings.max_video_height)
            except MediaError:
                continue
            if best is None or sel.short_side > best[0].short_side:
                best = (sel, kbps)
        if best is None:
            raise MediaError(
                f"Even the lowest quality of this video is over Telegram's {settings.upload_limit / 2**20:.0f} MB "
                "limit. Try 🎧 audio or 📝 text mode."
            )
        return best

    def _lean_options(self) -> list[tuple[int | None, float]]:
        rates = sorted({settings.max_audio_kbps, 48, 40, 32}, reverse=True)
        return [(k, self._audio_bytes(k)) for k in rates if k <= settings.max_audio_kbps]

    # ------------------------------------------------------------ voice engines

    async def make_from_youtube_dub(self, dub: dict[str, Any]) -> Result:
        """YouTube already has an AI dub in the target language: no translation needed at all."""
        engine = "YouTube auto-dub"
        await self.progress("voice", f"🗣 Using {engine} track")
        dub_size = media._size(dub, self.duration) or 0
        if self.job.mode == "audio":
            (path,) = self.fetch([(dub["format_id"], "dubbed audio", dub_size)])
            return self._result("audio", engine, path=await self._fit_audio(await path))
        # Copying the dub track costs nothing; re-encoding it smaller only if that gives a sharper picture
        sel, kbps = self.plan_video([(None, dub_size), *self._lean_options()])
        voice, video = await _all(
            *self.fetch([(dub["format_id"], "dubbed audio", dub_size), (sel.format_id, "video", sel.size)])
        )
        await self.progress("main", "🎬 Muxing…")
        out = await media.mux_video(video, voice, self.job.workdir / "result.mp4", mix=False, kbps=kbps)
        return self._result("video", engine, path=out, width=sel.width, height=sel.height)

    async def make_voiced(self) -> Result:
        mixing = settings.mix_original_volume > 0
        sel: media.Selection | None = None
        kbps: int | None = None
        if self.job.mode == "video":
            options = self._lean_options()
            if not mixing:  # the voice can go in as-is; Yandex's mp3 is ~128 kbps
                options.insert(0, (None, self._audio_bytes(128)))
            sel, kbps = self.plan_video(options)

        # The original audio is needed for the mix (unless the video file carries it) and by Yandex, which
        # often asks the client to upload it. It goes first in the single download: Yandex may be waiting.
        wanted: list[tuple[str, str, float | None]] = []
        audio_fut = None
        picked = media.pick_any_audio(self.info)
        if picked:
            fmt, muxed = picked
            yandex_upload = self._yandex_planned() and not muxed
            if (mixing and not (sel and sel.muxed)) or yandex_upload:
                wanted.append((fmt["format_id"], "audio", media._size(fmt, self.duration)))
        if sel:
            wanted.append((sel.format_id, "video", sel.size))
        futures = dict(zip([w[0] for w in wanted], self.fetch(wanted)))
        if picked and picked[0]["format_id"] in futures:
            audio_fut = futures[picked[0]["format_id"]]
            if not picked[1]:
                self._yandex_audio = audio_fut

        # Wait for the translation and the downloads together: whichever fails first stops the rest
        (voice, engine), *_ = await _all(self.voice_track(), *futures.values())
        video = futures[sel.format_id].result() if sel else None
        orig = audio_fut.result() if audio_fut and mixing else None

        await self.progress("main", "🎬 Mixing audio…" if mixing else "🎬 Muxing…")
        if self.job.mode == "video":
            assert video and sel
            out = await media.mux_video(
                video, voice, self.job.workdir / "result.mp4", original_audio=orig, mix=mixing, kbps=kbps
            )
            return self._result("video", engine, path=out, width=sel.width, height=sel.height)
        out = self.job.workdir / "result.m4a"
        await media.mix_audio(voice, out, orig if mixing else None, kbps=self.audio_kbps())
        return self._result("audio", engine, path=out)

    async def voice_track(self) -> tuple[Path, str]:
        """Translated voice-only track from the first engine that works."""
        errors: list[str] = []
        for name in settings.voice_engines:
            try:
                if name == "yandex" and self._yandex_ok():
                    return await self.yandex_voice(), "Yandex"
                if name == "tts":
                    return await self.tts_voice()
            except Exception as err:  # any engine failure -> try the next one
                log.warning("voice engine %s failed: %s", name, err)
                errors.append(f"{name}: {err}")
                await self.progress("voice", f"⚠️ {name} failed, trying the next engine…")
        raise MediaError("No engine could translate this video.\n" + "\n".join(errors[-3:]))

    def _yandex_ok(self) -> bool:
        return self.job.dst in langs.YANDEX_DST and (self.src or "auto") in langs.YANDEX_SRC

    def _yandex_planned(self) -> bool:
        return "yandex" in settings.voice_engines and self._yandex_ok()

    async def yandex_voice(self) -> Path:
        out = self.job.workdir / "voice_yandex.mp3"
        await self._run_vot("audio", out)
        return out

    async def _run_vot(self, mode: str, out: Path) -> None:
        last_error = None

        async def on_line(line: str) -> None:
            nonlocal last_error
            try:
                event = json.loads(line)
            except ValueError:
                return
            kind = event.get("type")
            if kind == "waiting":
                left = f" ~{_fmt_time(event['seconds'])} left" if event.get("seconds") else ""
                await self.progress("voice", f"🗣 Yandex is translating…{left}")
            elif kind == "audio_upload":
                await self.progress("voice", f"📤 Sending audio to Yandex (part {event['chunks']})…")
            elif kind == "error":
                last_error = event.get("message")

        await self.progress("voice", "🗣 Asking Yandex…")
        extra: list[str] = []
        if mode == "audio" and self._yandex_audio is not None:
            # Upload the audio we download anyway instead of a second YouTube extraction + download
            marker = self.job.workdir / "audio_for_yandex"
            extra = ["--audio-file", str(marker)]

            def link(fut: asyncio.Future[Path]) -> None:
                if not fut.cancelled() and fut.exception() is None and not marker.exists():
                    marker.symlink_to(fut.result())

            if self._yandex_audio.done():
                link(self._yandex_audio)
            else:
                self._yandex_audio.add_done_callback(link)
        env = {
            "YTDLP_ARGS": json.dumps(settings.ytdlp_base_args()),
            "YANDEX_OAUTH_TOKEN": settings.yandex_oauth_token,
            "VOT_PROXY": settings.vot_proxy,
        }
        try:
            await run(
                # Small V8 heap: the worker only shuffles a few 5 MB audio chunks (saves ~30 MB on a Pi)
                ["node", "--max-semi-space-size=1", "--max-old-space-size=128", "--expose-gc", str(settings.vot_worker),
                 "--url", self.job.url, "--lang", self.src or "auto", "--reslang", self.job.dst,
                 "--mode", mode, "--out", str(out), *extra],
                on_line=on_line,
                env=env,
                timeout=settings.job_timeout_min * 60,
                low_priority=False,
            )
        except ProcError as err:
            raise MediaError(f"Yandex: {last_error or err}") from None
        await self.progress("voice", "✅ Yandex translation ready")

    async def tts_voice(self) -> tuple[Path, str]:
        segs, mt_engine = await self.translated_segments()

        async def on_progress(fraction: float) -> None:
            await self.progress("voice", f"🎙 Synthesizing voice… {fraction:.0%}")

        voice, tts_engine = await tts.dub(segs, self.job.dst, self.job.workdir, on_progress)
        await self.progress("voice", "✅ Voice ready")
        return voice, f"{mt_engine} + {tts_engine}"

    # ------------------------------------------------------------ text

    async def source_segments(self) -> tuple[list[Segment], str | None, str]:
        """(segments, their language, where they came from): captions first, then speech recognition."""
        if self._segments:
            return self._segments
        await self.progress("text", "📝 Looking for captions…")
        manual = self.info.get("subtitles") or {}
        auto = self.info.get("automatic_captions") or {}
        want = [self.job.dst, self.src] if self.src else [self.job.dst]
        candidates: list[tuple[str, bool, str]] = []  # (key, is_auto, language)
        for lang in want:
            for key in manual:
                if langs.base(key) == lang and key != "live_chat":
                    candidates.append((key, False, lang))
        # Auto-captions: only the original ASR track ("xx-orig"); translated tracks get HTTP 429
        for key in auto:
            if key.endswith("-orig") and (not self.src or langs.base(key) == self.src):
                candidates.append((key, True, langs.base(key) or ""))
        if not self.src:
            candidates += [(k, False, langs.base(k) or "") for k in manual if k != "live_chat"][:1]

        for key, is_auto, lang in candidates:
            path = await media.download_subtitles(self.info_path, key, self.job.workdir, auto=is_auto)
            if not path:
                continue
            segs = subtitles.parse_file(path)
            if segs:
                kind = "auto-captions" if is_auto else "captions"
                self._segments = (segs, lang, f"YouTube {kind}" if "youtube" in self.info.get("extractor", "") else kind)
                return self._segments

        if not asr.available():
            raise MediaError("The video has no usable captions, and speech recognition isn't configured (GROQ_API_KEY).")
        await self.progress("text", "👂 Recognizing speech (Groq Whisper)…")
        audio = await self.original_audio()
        small = await asr.compress_for_asr(audio, self.job.workdir / "asr.m4a")
        segs, detected = await asr.transcribe(small, self.src)
        if not segs:
            raise MediaError("No speech recognized.")
        self._segments = (segs, self.src or detected, "Whisper")
        return self._segments

    async def translated_segments(self) -> tuple[list[Segment], str]:
        segs, lang, origin = await self.source_segments()
        phrases = subtitles.merge_phrases(segs)
        if lang == self.job.dst:
            return phrases, origin
        await self.progress("text", f"🌍 Translating {len(phrases)} phrases…")
        texts, engine = await translate.translate_lines([p.text for p in phrases], lang or "auto", self.job.dst)
        await self.progress("text", f"✅ Translated ({engine})")
        out = [Segment(p.start, p.end, t) for p, t in zip(phrases, texts) if t]
        return out, f"{origin} → {engine}"

    async def make_text(self) -> Result:
        segs: list[Segment] | None = None
        engine = ""
        if "yandex" in settings.voice_engines and self._yandex_ok():
            srt = self.job.workdir / "yandex.srt"
            try:
                await self._run_vot("subs", srt)
                segs, engine = subtitles.parse_srt(srt.read_text()), "Yandex"
            except MediaError as err:
                log.info("no Yandex subtitles: %s", err)
        if not segs:
            segs, engine = await self.translated_segments()
        stem = _safe_name(self.title)
        srt_path = self.job.workdir / f"{stem}.{self.job.dst}.srt"
        srt_path.write_text(subtitles.to_srt(segs), encoding="utf-8")
        text = subtitles.to_text(segs)
        txt_path = self.job.workdir / f"{stem}.{self.job.dst}.txt"
        txt_path.write_text(text, encoding="utf-8")
        return self._result("text", engine, path=txt_path, text=text, extra_files=[srt_path])

    async def _fit_audio(self, path: Path) -> Path:
        """Send a downloaded track as audio: a cheap remux, or a re-encode if it's over the size limit."""
        out = self.job.workdir / "result.m4a"
        if path.stat().st_size <= settings.upload_limit:
            return await media.remux_audio(path, out)
        return await media.mix_audio(path, out, None, kbps=self.audio_kbps())


def _esc(text: str) -> str:
    import html

    return html.escape(text, quote=False)


def _safe_name(title: str) -> str:
    name = re.sub(r"[^\w\- ]+", "", title, flags=re.UNICODE).strip()[:60]
    return name or "transcript"
