"""Entry point: python -m polyglot"""
from __future__ import annotations

import asyncio
import logging
import shutil
import sys

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramUnauthorizedError

from . import bot as botmod
from .config import settings
from .jobs import JobQueue
from .proc import ProcError, run

log = logging.getLogger("polyglot")


async def tool_versions() -> None:
    """Log versions and warn about things that silently break YouTube downloads."""
    for cmd in (["yt-dlp", "--version"], ["node", "--version"], ["ffmpeg", "-version"]):
        try:
            out = (await run(cmd, timeout=60, low_priority=False)).splitlines()[0]
        except (ProcError, OSError) as err:
            log.error("%s is not usable: %s", cmd[0], err)
            continue
        log.info("%s %s", cmd[0], out)
        if cmd[0] == "node" and "node" in settings.ytdlp_js_runtimes:
            major = int(out.lstrip("v").split(".")[0] or 0)
            if major < 22:
                log.warning("yt-dlp needs Node >= 22 as its JS runtime for YouTube; found %s", out)


async def update_ytdlp_forever(queue: JobQueue) -> None:
    """YouTube changes often and old yt-dlp versions break; keep it fresh (pip into our own venv).
    Updates run while no job is running, so a download never sees half-replaced files."""
    while True:
        try:
            async with queue.paused():
                await run(
                    [sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
                     "--no-cache-dir", "--prefer-binary", "--upgrade", "yt-dlp[default]"],
                    timeout=900,
                )
            log.info("yt-dlp is up to date: %s", (await run(["yt-dlp", "--version"], timeout=60)).strip())
        except (ProcError, OSError) as err:
            log.warning("yt-dlp update failed: %s", err)
        await asyncio.sleep(24 * 3600)


async def heartbeat_forever() -> None:
    """Docker's HEALTHCHECK looks at this file: if the event loop is stuck, it goes stale."""
    path = settings.data_dir / "heartbeat"
    while True:
        path.touch()
        await asyncio.sleep(60)


async def wait_for_telegram(bot: Bot) -> None:
    """A Pi often boots before its network is up: wait for Telegram instead of crash-looping."""
    delay = 5
    while True:
        try:
            me = await bot.get_me()
            log.info("running as @%s", me.username)
            return
        except TelegramUnauthorizedError:
            sys.exit("Telegram rejected TELEGRAM_TOKEN: check your .env")
        except (TelegramAPIError, OSError) as err:
            log.warning("Telegram is not reachable yet (%s), retrying in %ds", err, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 120)


async def main() -> None:
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    if not settings.telegram_token:
        sys.exit("TELEGRAM_TOKEN is not set")
    shutil.rmtree(settings.tmp_dir, ignore_errors=True)  # leftovers of a crash/power cut
    settings.tmp_dir.mkdir(parents=True, exist_ok=True)

    await tool_versions()
    bot, dp = botmod.build()
    background: list[asyncio.Task[None]] = []
    try:
        await wait_for_telegram(bot)
        background.append(asyncio.create_task(heartbeat_forever()))
        if settings.ytdlp_auto_update:
            background.append(asyncio.create_task(update_ytdlp_forever(botmod.queue)))
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        for task in background:
            task.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
