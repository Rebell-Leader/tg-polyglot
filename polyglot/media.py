"""yt-dlp and ffmpeg helpers. Everything here is async and runs at low CPU priority."""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from . import langs
from .config import settings
from .proc import ProcError, run

log = logging.getLogger(__name__)

URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


class MediaError(RuntimeError):
    """Error with a message that is safe and useful to show to the user."""

    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable  # a fresh extraction may fix it (expired/rejected signed URL)


# ---------------------------------------------------------------- URL checks


def extract_url(text: str | None) -> str | None:
    if not text:
        return None
    match = URL_RE.search(text)
    if not match:
        return None
    url = match.group(0).rstrip(").,;!?]}»")
    return url if len(url) <= 2048 else None


async def check_url_is_public(url: str) -> None:
    """Refuse URLs that resolve to the LAN/localhost: the bot runs on a home network (SSRF)."""
    if settings.allow_private_urls:
        return
    host = urlsplit(url).hostname
    if not host:
        raise MediaError("This doesn't look like a valid link.")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise MediaError("Can't resolve the link's host name.") from None
    for info in infos:
        addr = ipaddress.ip_address(info[4][0])
        if not addr.is_global:
            raise MediaError("Links to private or local network addresses are not allowed.")


# ---------------------------------------------------------------- yt-dlp


def ytdlp_cmd(*args: str) -> list[str]:
    return ["yt-dlp", *settings.ytdlp_base_args(), *args]


def friendly_ytdlp_error(err: ProcError) -> MediaError:
    text = err.stderr.lower()
    if err.code == -9:
        return MediaError("Ran out of memory while downloading. Try a shorter video or 🎧 audio mode.")
    known = [
        ("sign in to confirm", "YouTube asks to confirm we're not a bot. The admin needs to set up cookies or a proxy."),
        ("private video", "This video is private."),
        ("members-only", "This video is for channel members only."),
        ("unsupported url", "This site or link isn't supported."),
        ("not available in your country", "This video is geo-blocked for the bot's location."),
        ("video unavailable", "This video is unavailable."),
        ("confirm your age", "This video is age-restricted (needs cookies)."),
        ("http error 429", "The site is rate-limiting us. Try again later."),
        ("no space left", "The bot's disk is full. The admin needs to free some space."),
        ("timed out", "Downloading took too long."),
    ]
    for needle, message in known:
        if needle in text:
            return MediaError(message)
    reason = err.stderr.strip().splitlines()[-1][:300] if err.stderr.strip() else str(err)
    # YouTube sometimes rejects a signed media URL (401/403); re-extracting usually helps
    retryable = any(code in text for code in ("http error 401", "http error 403", "unauthorized", "forbidden"))
    return MediaError(f"Couldn't get the video: {reason}", retryable=retryable)


# Fields of yt-dlp's metadata we never use; dropping them keeps the bot process small on a Pi
_INFO_DROP = ("thumbnails", "heatmap", "chapters", "description", "requested_formats", "automatic_captions_raw")
_FORMAT_DROP = ("url", "manifest_url", "fragments", "fragment_base_url", "http_headers", "downloader_options")


def _prune(info: dict[str, Any]) -> dict[str, Any]:
    for key in _INFO_DROP:
        info.pop(key, None)
    for fmt in info.get("formats") or []:
        for key in _FORMAT_DROP:
            fmt.pop(key, None)
    for key in ("subtitles", "automatic_captions"):
        for tracks in (info.get(key) or {}).values():
            tracks[:] = [{"ext": t.get("ext"), "name": t.get("name")} for t in tracks]
    return info


async def probe(url: str, workdir: Path) -> tuple[dict[str, Any], Path]:
    """Extract metadata once; later yt-dlp calls reuse it via --load-info-json (no second extraction).
    yt-dlp writes the (sometimes 10+ MB) JSON straight to disk; we keep a pruned copy in memory."""
    info_path = workdir / "info.json"
    try:
        await run(ytdlp_cmd("-J", "--playlist-items", "1", "--", url), stdout_path=info_path, timeout=180)
    except ProcError as err:
        raise friendly_ytdlp_error(err) from None
    with info_path.open("rb") as f:
        info = json.load(f)
    if info.get("_type") == "playlist":
        entries = [e for e in info.get("entries") or [] if e]
        if not entries:
            raise MediaError("This link is a playlist without playable videos.")
        info = entries[0]
        info_path.write_text(json.dumps(info))  # --load-info-json needs the video itself
    if info.get("is_live"):
        raise MediaError("Live streams are not supported.")
    return _prune(info), info_path


ProgressFn = Callable[[float], Any]

_PROGRESS_PREFIX = "PROGRESS "


async def download_formats(
    info_path: Path,
    format_ids: list[str],
    workdir: Path,
    on_progress: ProgressFn | None = None,
    total_bytes: int | None = None,
    on_file: Callable[[str, Path], Any] | None = None,
) -> dict[str, Path]:
    """Download formats one after another in a single yt-dlp process, as separate files (no merge pass).
    on_file(format_id, path) fires as soon as each one is complete."""
    paths: dict[str, Path] = {}
    done_bytes: dict[str, int] = {}

    def on_line(line: str) -> Any:
        if line.startswith(_PROGRESS_PREFIX):
            parts = line.split()
            if len(parts) >= 3 and parts[2].isdigit():
                done_bytes[parts[1]] = int(parts[2])
                if on_progress and total_bytes:
                    return on_progress(min(1.0, sum(done_bytes.values()) / total_bytes))
        elif line.startswith("FILE "):
            fmt, _, path = line[5:].partition(" ")
            paths[fmt] = Path(path)
            if on_file:
                on_file(fmt, paths[fmt])
        return None

    try:
        await run(
            ytdlp_cmd(
                "--load-info-json", str(info_path),
                "-f", ",".join(format_ids),
                "-o", str(workdir / "dl_%(format_id)s.%(ext)s"),
                "--newline", "--progress-delta", "3",
                "--progress-template", f"download:{_PROGRESS_PREFIX}%(info.format_id)s %(progress.downloaded_bytes)s",
                "--print", "after_move:FILE %(format_id)s %(filepath)s",
                # Skip yt-dlp's ffmpeg "fixup" remux of every DASH/HLS file (a full extra read+write on the
                # SD card and a ~50 MB process): our own ffmpeg step reads the raw files fine.
                "--fixup", "never",
            ),
            on_line=on_line,
            timeout=settings.job_timeout_min * 60,
        )
    except ProcError as err:
        raise friendly_ytdlp_error(err) from None
    missing = [f for f in format_ids if f not in paths or not paths[f].exists()]
    if missing:
        raise MediaError(f"Download finished but files are missing: {', '.join(missing)}")
    return paths


async def download_subtitles(info_path: Path, lang_key: str, workdir: Path, auto: bool) -> Path | None:
    """Fetch one subtitle track as json3 (falls back to vtt)."""
    try:
        await run(
            ytdlp_cmd(
                "--load-info-json", str(info_path),
                "--skip-download",
                "--write-auto-subs" if auto else "--write-subs",
                "--sub-langs", lang_key,
                "--sub-format", "json3/vtt/best",
                "-o", str(workdir / "subs"),
            ),
            timeout=180,
        )
    except ProcError as err:
        log.warning("subtitle download failed: %s", err)
        return None
    for path in sorted(workdir.glob(f"subs.{lang_key}.*")):
        return path
    return None


# ---------------------------------------------------------------- format selection


def _size(fmt: dict[str, Any], duration: float) -> float | None:
    size = fmt.get("filesize") or fmt.get("filesize_approx")
    if size:
        return float(size)
    if fmt.get("tbr") and duration:
        return fmt["tbr"] * 1000 / 8 * duration
    return None


_AUDIO_EXTS = {"m4a", "mp3", "opus", "ogg", "oga", "aac", "wav", "flac", "weba"}


def _has_video(f: dict[str, Any]) -> bool:
    codec = f.get("vcodec")
    if codec is not None:
        return codec != "none"
    # Generic extractors often don't know codecs; judge by extension
    return f.get("ext") not in _AUDIO_EXTS and f.get("video_ext") != "none" and f.get("ext") != "mhtml"


def _has_audio(f: dict[str, Any]) -> bool:
    codec = f.get("acodec")
    if codec is not None:
        return codec != "none"
    return f.get("audio_ext") != "none" and f.get("ext") != "mhtml"


def _is_audio_only(f: dict[str, Any]) -> bool:
    return _has_audio(f) and not _has_video(f)


def _is_video_only(f: dict[str, Any]) -> bool:
    return _has_video(f) and not _has_audio(f)


def _is_muxed(f: dict[str, Any]) -> bool:
    return _has_video(f) and _has_audio(f)


def _is_original_audio(f: dict[str, Any], info: dict[str, Any]) -> bool:
    """Is this the video's own soundtrack (not a dub / audio description)?"""
    note = (f.get("format_note") or "").lower()
    if "original" in note:
        return True
    if "dubbed" in note or "descriptive" in note:
        return False
    video_lang = langs.base(info.get("language"))
    track_lang = langs.base(f.get("language"))
    if video_lang and track_lang:
        return track_lang == video_lang
    # Unknown languages: with several audio languages yt-dlp ranks the original highest
    audio_langs = {langs.base(x.get("language")) for x in info.get("formats") or [] if _is_audio_only(x)}
    return len(audio_langs) <= 1 or (f.get("language_preference") or 0) >= 10


def _height(f: dict[str, Any]) -> int:
    # Use the short side so vertical videos aren't penalised
    w, h = f.get("width"), f.get("height")
    if w and h:
        return min(w, h)
    return h or 0


def _h264(f: dict[str, Any]) -> bool:
    return (f.get("vcodec") or "").startswith(("avc1", "h264"))


@dataclass
class Selection:
    format_id: str
    muxed: bool  # the file also carries the original audio
    size: float  # estimated bytes
    width: int = 0
    height: int = 0

    @property
    def short_side(self) -> int:
        return min(self.width, self.height) if self.width and self.height else self.height


def pick_original_audio(info: dict[str, Any]) -> dict[str, Any] | None:
    duration = info.get("duration") or 0
    audios = [f for f in info.get("formats") or [] if _is_audio_only(f)]
    if not audios:
        return None
    originals = [f for f in audios if _is_original_audio(f, info)] or audios

    # Prefer plain https over HLS, m4a/AAC, no dynamic range compression, around 128 kbps
    def key(f: dict[str, Any]) -> tuple:
        abr = f.get("abr") or f.get("tbr") or 0
        return (
            f.get("protocol", "https") in ("https", "http"),
            f.get("ext") == "m4a",
            "drc" not in str(f.get("format_id")),
            -abs(abr - 128),
            -(_size(f, duration) or 0),
        )

    return max(originals, key=key)


def pick_dub_audio(info: dict[str, Any], lang: str) -> dict[str, Any] | None:
    """YouTube's AI auto-dub track in the target language, if the video has one."""
    dubs = [
        f for f in info.get("formats") or []
        if _is_audio_only(f) and not _is_original_audio(f, info) and langs.base(f.get("language")) == lang
    ]
    if not dubs:
        return None
    # m4a (copies straight into mp4 / Telegram's player), ~128 kbps, plain https
    return max(
        dubs,
        key=lambda f: (
            f.get("ext") == "m4a",
            f.get("protocol", "https") in ("https", "http"),
            "drc" not in str(f.get("format_id")),
            -abs((f.get("abr") or f.get("tbr") or 0) - 128),
        ),
    )


def pick_video(info: dict[str, Any], budget: float, max_height: int) -> Selection:
    """Best picture that fits the Telegram budget without re-encoding. H.264 first: it plays everywhere
    and needs no transcoding on the Pi."""
    duration = info.get("duration") or 0
    candidates: list[tuple[tuple, Selection]] = []
    for f in info.get("formats") or []:
        if not (_is_video_only(f) or _is_muxed(f)):
            continue
        short_side = _height(f)
        if short_side > max_height:
            continue
        size = _size(f, duration) or 0
        if size > budget:
            continue
        sel = Selection(f["format_id"], _is_muxed(f), size, f.get("width") or 0, f.get("height") or 0)
        key = (
            _h264(f) or (f.get("vcodec") is None and f.get("ext") == "mp4"),
            short_side,
            f.get("protocol", "https") in ("https", "http"),
            size,
        )
        candidates.append((key, sel))
    if not candidates:
        raise MediaError(
            f"Even the lowest quality of this video is over Telegram's {budget / 2**20:.0f} MB limit. "
            "Try 🎧 audio or 📝 text mode."
        )
    return max(candidates, key=lambda c: c[0])[1]


def pick_any_audio(info: dict[str, Any]) -> tuple[dict[str, Any], bool] | None:
    """Original audio-only format, or else the smallest muxed one (the bool says 'muxed')."""
    audio = pick_original_audio(info)
    if audio:
        return audio, False
    duration = info.get("duration") or 0
    muxed = [f for f in info.get("formats") or [] if _is_muxed(f)]
    if not muxed:
        return None
    return min(muxed, key=lambda f: _size(f, duration) or float("inf")), True


# ---------------------------------------------------------------- ffmpeg

FFMPEG = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y"]
# Audio-only work gains nothing from many threads; fewer threads = less memory (ffmpeg sizes pools by CPU count)
THREADS = ["-threads", "2", "-filter_complex_threads", "1"]


def audio_encode_args(kbps: int) -> list[str]:
    if settings.audio_quality == "music" and kbps >= 64:
        return ["-ar", "48000", "-ac", "2", "-c:a", "aac", "-b:a", f"{kbps}k"]
    return ["-ar", "24000", "-ac", "1", "-c:a", "aac", "-b:a", f"{kbps}k"]


def _mix_filter(bg_input: str, voice_input: str, volume: float) -> str:
    # Convert both inputs to the output format first, so the mixer and encoder do as little work as possible
    if settings.audio_quality == "music":
        fmt = "aresample=48000,aformat=channel_layouts=stereo"
    else:
        fmt = "aresample=24000,aformat=channel_layouts=mono"
    return (
        f"[{bg_input}]{fmt},volume={volume:.3f}[bg];"
        f"[{voice_input}]{fmt}[fg];"
        "[bg][fg]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]"
    )


async def mux_video(
    video: Path,
    voice: Path,
    out: Path,
    original_audio: Path | None = None,
    mix: bool = True,
    kbps: int | None = None,
) -> Path:
    """The picture is always stream-copied. Audio: translated voice over the ducked original (mix),
    or the voice track as-is (kbps=None: zero CPU) or re-encoded to kbps."""
    volume = settings.mix_original_volume
    cmd = [*FFMPEG, "-i", str(video), "-i", str(voice)]
    if mix and volume > 0:
        bg = "0:a:0"
        if original_audio:
            cmd += ["-i", str(original_audio)]
            bg = "2:a:0"
        cmd += [
            "-filter_complex", _mix_filter(bg, "1:a:0", volume),
            "-map", "0:v:0", "-map", "[a]", "-c:v", "copy", *audio_encode_args(kbps or settings.max_audio_kbps),
            *THREADS,
        ]
    elif kbps:
        cmd += ["-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", *audio_encode_args(kbps), *THREADS]
    else:
        cmd += ["-map", "0:v:0", "-map", "1:a:0", "-c", "copy"]  # mp3/aac both fit in mp4 as they are
    cmd += ["-movflags", "+faststart", str(out)]
    try:
        await run(cmd, timeout=settings.job_timeout_min * 60)
    except ProcError as err:
        if mix and volume > 0 and not original_audio and "matches no streams" in err.stderr:
            return await mux_video(video, voice, out, mix=False, kbps=kbps)  # the source has no audio track
        raise
    return out


async def mix_audio(voice: Path, out: Path, original_audio: Path | None, kbps: int) -> Path:
    """Audio-only result (AAC in .m4a, what Telegram's music player expects)."""
    if not original_audio or settings.mix_original_volume <= 0:
        cmd = [*FFMPEG, "-i", str(voice), "-vn", *audio_encode_args(kbps), *THREADS, str(out)]
    else:
        cmd = [
            *FFMPEG, "-i", str(original_audio), "-i", str(voice),
            "-filter_complex", _mix_filter("0:a:0", "1:a:0", settings.mix_original_volume),
            "-map", "[a]", "-vn", *audio_encode_args(kbps), *THREADS, str(out),
        ]
    await run(cmd, timeout=settings.job_timeout_min * 60)
    return out


async def remux_audio(src: Path, out: Path) -> Path:
    """Stream-copy into a proper .m4a (raw DASH/HLS downloads aren't ideal for Telegram's player)."""
    await run([*FFMPEG, "-i", str(src), "-vn", "-c", "copy", "-movflags", "+faststart", str(out)], timeout=600)
    return out
