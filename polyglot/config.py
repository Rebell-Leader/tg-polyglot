"""Runtime settings, read once from environment variables (see .env.example)."""
from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path


def _int(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    return int(value) if value else default


def _float(name: str, default: float) -> float:
    value = os.getenv(name, "").strip()
    return float(value) if value else default


def _ids(name: str) -> frozenset[int]:
    raw = os.getenv(name, "").replace(";", ",")
    return frozenset(int(x) for x in raw.split(",") if x.strip())


def _list(name: str, default: str) -> tuple[str, ...]:
    raw = os.getenv(name, default)
    return tuple(x.strip().lower() for x in raw.split(",") if x.strip())


def _flag(name: str, default: bool) -> bool:
    value = os.getenv(name, "").strip().lower()
    return default if not value else value in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    telegram_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_TOKEN", ""))
    # Self-hosted telegram-bot-api server (raises the upload limit from 50 MB to 2000 MB)
    telegram_api_server: str = field(default_factory=lambda: os.getenv("TELEGRAM_API_SERVER", ""))
    telegram_proxy: str = field(default_factory=lambda: os.getenv("TELEGRAM_PROXY", ""))

    data_dir: Path = field(default_factory=lambda: Path(os.getenv("DATA_DIR", "data")).resolve())

    admin_ids: frozenset[int] = field(default_factory=lambda: _ids("ADMIN_IDS"))
    # Empty = anyone may use the bot
    allowed_users: frozenset[int] = field(default_factory=lambda: _ids("ALLOWED_USERS"))
    free_daily_limit: int = field(default_factory=lambda: _int("FREE_DAILY_LIMIT", 1))

    max_parallel_jobs: int = field(default_factory=lambda: _int("MAX_PARALLEL_JOBS", 1))
    max_queue: int = field(default_factory=lambda: _int("MAX_QUEUE", 10))
    max_duration_min: int = field(default_factory=lambda: _int("MAX_DURATION_MIN", 90))
    job_timeout_min: int = field(default_factory=lambda: _int("JOB_TIMEOUT_MIN", 40))
    max_video_height: int = field(default_factory=lambda: _int("MAX_VIDEO_HEIGHT", 720))
    upload_limit_mb: int = field(default_factory=lambda: _int("UPLOAD_LIMIT_MB", 0))
    # Refuse to start a job below this much free disk (a job needs ~3x the upload limit temporarily)
    min_free_disk_mb: int = field(default_factory=lambda: _int("MIN_FREE_DISK_MB", 400))

    # Volume of the original soundtrack under the translated voice. 0 = drop it (no audio re-encode at all).
    mix_original_volume: float = field(default_factory=lambda: _float("MIX_ORIGINAL_VOLUME", 0.25))
    # speech: mono 24 kHz (the translated voice is mono 22-24 kHz anyway; ~3x cheaper to encode on a Pi)
    # music: stereo 48 kHz, keeps the original soundtrack's quality
    audio_quality: str = field(default_factory=lambda: os.getenv("AUDIO_QUALITY", "speech").lower())
    audio_bitrate: str = field(default_factory=lambda: os.getenv("AUDIO_BITRATE", ""))
    nice: int = field(default_factory=lambda: _int("NICE", 10))

    default_src: str = field(default_factory=lambda: os.getenv("DEFAULT_SRC", "auto"))
    default_dst: str = field(default_factory=lambda: os.getenv("DEFAULT_DST", "ru"))

    # Voice engines, tried in order: ytdub (YouTube's own AI dub track), yandex, tts (captions -> MT -> edge-tts)
    voice_engines: tuple[str, ...] = field(default_factory=lambda: _list("VOICE_ENGINES", "ytdub,yandex,tts"))
    # Machine translation, tried in order; engines without a key are skipped
    mt_engines: tuple[str, ...] = field(default_factory=lambda: _list("MT_ENGINES", "llm,deepl,yandex,google"))
    # Speech synthesis for the tts engine: edge (Microsoft neural voices), gtts (Google, robotic but sturdy)
    tts_engines: tuple[str, ...] = field(default_factory=lambda: _list("TTS_ENGINES", "edge,gtts"))

    yandex_oauth_token: str = field(default_factory=lambda: os.getenv("YANDEX_OAUTH_TOKEN", ""))
    vot_proxy: str = field(default_factory=lambda: os.getenv("VOT_PROXY", ""))
    deepl_api_key: str = field(default_factory=lambda: os.getenv("DEEPL_API_KEY", ""))
    groq_api_key: str = field(default_factory=lambda: os.getenv("GROQ_API_KEY", ""))
    llm_api_key: str = field(default_factory=lambda: os.getenv("LLM_API_KEY", ""))
    llm_base_url: str = field(
        default_factory=lambda: os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")
    )
    llm_model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", "llama-3.3-70b-versatile"))
    asr_model: str = field(default_factory=lambda: os.getenv("ASR_MODEL", "whisper-large-v3-turbo"))

    ytdlp_proxy: str = field(default_factory=lambda: os.getenv("YTDLP_PROXY", ""))
    ytdlp_cookies: str = field(default_factory=lambda: os.getenv("YTDLP_COOKIES", ""))
    ytdlp_extra_args: tuple[str, ...] = field(
        default_factory=lambda: tuple(shlex.split(os.getenv("YTDLP_EXTRA_ARGS", "")))
    )
    # yt-dlp needs a JS runtime for YouTube; node (>=22) is in the image anyway, deno is used too if installed
    ytdlp_js_runtimes: str = field(default_factory=lambda: os.getenv("YTDLP_JS_RUNTIMES", "node"))
    ytdlp_auto_update: bool = field(default_factory=lambda: _flag("YTDLP_AUTO_UPDATE", True))
    allow_private_urls: bool = field(default_factory=lambda: _flag("ALLOW_PRIVATE_URLS", False))

    vot_worker: Path = field(
        default_factory=lambda: Path(
            os.getenv("VOT_WORKER", Path(__file__).resolve().parent.parent / "vot" / "vot_worker.mjs")
        )
    )
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO").upper())

    @property
    def upload_limit(self) -> int:
        """Max bytes we may upload to Telegram (a little under the hard limit)."""
        if self.upload_limit_mb:
            return self.upload_limit_mb * 1_000_000
        # Cloud Bot API: "50 MB"; whether that's MB or MiB isn't documented, so stay under both
        return 1_950_000_000 if self.telegram_api_server else 49_000_000

    @property
    def max_audio_kbps(self) -> int:
        if self.audio_bitrate:
            return int(self.audio_bitrate.lower().rstrip("k"))
        return 128 if self.audio_quality == "music" else 64

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "polyglot.db"

    def ytdlp_base_args(self) -> list[str]:
        args = ["--no-playlist", "--no-warnings", "--ignore-config"]
        if self.ytdlp_proxy:
            args += ["--proxy", self.ytdlp_proxy]
        if self.ytdlp_cookies:
            args += ["--cookies", self.ytdlp_cookies]
        for runtime in filter(None, self.ytdlp_js_runtimes.split(",")):
            args += ["--js-runtimes", runtime.strip()]
        return args + list(self.ytdlp_extra_args)


settings = Settings()
