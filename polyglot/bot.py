"""Telegram front-end (aiogram 3): send a link, pick a mode, watch progress, get the result."""
from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import secrets
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramEntityTooLarge,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    ErrorEvent,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardRemove,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from . import langs
from .config import settings
from .db import Database
from .jobs import AlreadyQueued, JobQueue, QueuedJob, QueueFull
from .media import MediaError, check_url_is_public, extract_url
from .pipeline import Job, Pipeline, Result, cache_key
from .proc import ProcError

log = logging.getLogger(__name__)

UPLOAD_TIMEOUT = 1800  # seconds; a home uplink can be slow
RETRY_DELAY = 10  # seconds, grows with each attempt
MODE_LABELS = {"video": "🎬 Video", "audio": "🎧 Audio", "text": "📝 Text"}

router = Router()
db: Database
queue: JobQueue
bot: Bot


def esc(text: Any) -> str:
    return html.escape(str(text), quote=False)


def is_admin(user_id: int) -> bool:
    return user_id in settings.admin_ids


def is_allowed(user_id: int) -> bool:
    return not settings.allowed_users or user_id in settings.allowed_users or is_admin(user_id)


def quota_left(user_id: int) -> int | None:
    """None = unlimited."""
    if is_admin(user_id) or settings.free_daily_limit <= 0 or db.is_premium(user_id):
        return None
    return max(0, settings.free_daily_limit - db.used_today(user_id))


def user_langs(user_id: int) -> tuple[str, str]:
    return db.langs(user_id, settings.default_src, settings.default_dst)


def fmt_seconds(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 60}m {seconds % 60:02}s" if seconds >= 60 else f"{seconds}s"


class AccessMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user and not is_allowed(user.id):
            if isinstance(event, Message):
                await event.answer("🔒 This is a private bot.")
            elif isinstance(event, CallbackQuery):
                await event.answer("🔒 This is a private bot.", show_alert=True)
            return None
        if user:
            db.upsert_user(user.id, user.username)
        return await handler(event, data)


# ---------------------------------------------------------------- pending links (between "link" and "mode" taps)


@dataclass
class PendingLink:
    url: str
    user_id: int
    created: float


pending: dict[str, PendingLink] = {}


def remember_link(url: str, user_id: int) -> str:
    now = time.monotonic()
    for key in [k for k, p in pending.items() if now - p.created > 3600]:
        del pending[key]
    mine = [k for k, p in pending.items() if p.user_id == user_id]
    for key in mine[:-4]:  # keep a user's last few links only (bounded memory)
        del pending[key]
    req = secrets.token_urlsafe(6)
    pending[req] = PendingLink(url, user_id, now)
    return req


# ---------------------------------------------------------------- keyboards & texts


def settings_text(user_id: int) -> str:
    src, dst = user_langs(user_id)
    return f"<b>Languages</b>\nFrom: {langs.name(src)}\nTo: {langs.name(dst)}"


def settings_kb(user_id: int, req: str = "-") -> InlineKeyboardMarkup:
    src, dst = user_langs(user_id)
    kb = InlineKeyboardBuilder()
    kb.button(text=f"From: {langs.name(src)}", callback_data=f"pick:src:{req}")
    kb.button(text=f"To: {langs.name(dst)}", callback_data=f"pick:dst:{req}")
    if req != "-":
        kb.button(text="« Back", callback_data=f"back:{req}")
    kb.adjust(2, 1)
    return kb.as_markup()


def lang_grid(which: str, current: str, req: str) -> InlineKeyboardMarkup:
    choices = langs.SOURCE_CHOICES if which == "src" else langs.TARGET_CHOICES
    kb = InlineKeyboardBuilder()
    for code in choices:
        mark = "✅ " if code == current else ""
        kb.button(text=mark + langs.name(code), callback_data=f"set:{which}:{code}:{req}")
    kb.adjust(2)
    kb.row(InlineKeyboardButton(text="« Back", callback_data=f"back:{req}"))
    return kb.as_markup()


def link_prompt(user_id: int, url: str) -> str:
    src, dst = user_langs(user_id)
    note = ""
    if dst not in langs.YANDEX_DST:
        note = "\n<i>Yandex can't voice this language; free TTS or a YouTube dub will be used.</i>"
    return f"🔗 {esc(url)}\n{langs.name(src)} → {langs.name(dst)}{note}\n\nWhat should I make?"


def link_kb(user_id: int, req: str) -> InlineKeyboardMarkup:
    src, dst = user_langs(user_id)
    kb = InlineKeyboardBuilder()
    for mode, label in MODE_LABELS.items():
        kb.button(text=label, callback_data=f"go:{mode}:{req}")
    kb.button(text=f"🌐 {langs.short(src)} → {langs.short(dst)}  change", callback_data=f"langs:{req}")
    kb.adjust(3, 1)
    return kb.as_markup()


def cancel_kb(job: QueuedJob) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✖️ Cancel", callback_data=f"cancel:{job.id}")]]
    )


HELP = (
    "👋 Send me a link to a video (YouTube, VK, Rutube, Vimeo, Twitch, direct .mp4 …) and choose:\n"
    "• 🎬 <b>Video</b> with the translated voice over the original sound\n"
    "• 🎧 <b>Audio</b> only (podcast-style)\n"
    "• 📝 <b>Text</b>: translated transcript (.txt + .srt)\n\n"
    "Engines: YouTube's own AI dub when available, Yandex voice-over (to ru/en/kk), "
    "or free captions → translation → speech for other languages.\n\n"
    "/settings change languages · /cancel stop the current job · /stats your usage"
)


async def tg_retry(call, *args: Any, attempts: int = 4, **kwargs: Any) -> Any:
    """Telegram call that survives flood limits and a flaky home connection (uploads can take minutes)."""
    for attempt in range(attempts):
        try:
            return await call(*args, **kwargs)
        except TelegramRetryAfter as err:
            if attempt == attempts - 1:
                raise
            await asyncio.sleep(err.retry_after + 1)
        except (TelegramNetworkError, TelegramServerError) as err:
            if attempt == attempts - 1:
                raise
            log.warning("Telegram call failed (%s), retrying", err)
            await asyncio.sleep(RETRY_DELAY * (attempt + 1))
    raise AssertionError("unreachable")


# ---------------------------------------------------------------- commands


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    # ReplyKeyboardRemove clears the old bot's menu keyboard for existing users
    await message.answer(HELP, reply_markup=ReplyKeyboardRemove())
    await message.answer(settings_text(message.from_user.id), reply_markup=settings_kb(message.from_user.id))


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP)


@router.message(Command("settings", "lang"))
@router.message(F.text.in_({"Set Source/Target Languages"}))  # buttons of the old version
async def cmd_settings(message: Message) -> None:
    await message.answer(settings_text(message.from_user.id), reply_markup=settings_kb(message.from_user.id))


@router.message(Command("stats"))
@router.message(F.text.in_({"View Stats", "Premium Status"}))
async def cmd_stats(message: Message) -> None:
    uid = message.from_user.id
    stats = db.user_stats(uid)
    left = quota_left(uid)
    lines = [
        f"Translations: <b>{stats['total']}</b> (today {stats['today']})",
        "Daily limit: unlimited" if left is None else f"Left today: {left} of {settings.free_daily_limit}",
        f"Premium: {'yes' if stats['premium'] else 'no'}",
    ]
    if is_admin(uid):
        g = db.global_stats()
        engines = ", ".join(f"{esc(r['engine'])}: {r['n']}" for r in g["engines"]) or "—"
        lines += [
            "",
            f"<b>Admin</b>: {g['users']} users, {g['jobs']} jobs, {g['failed']} failed, "
            f"avg {fmt_seconds(g['avg_seconds'])}",
            f"Engines: {engines}",
            f"Queue: {len(queue.running)} running, {len(queue.waiting)} waiting",
        ]
    await message.answer("\n".join(lines))


@router.message(Command("grant", "revoke"))
async def cmd_grant(message: Message, command: CommandObject) -> None:
    if not is_admin(message.from_user.id):
        return
    if not command.args or not command.args.strip().lstrip("-").isdigit():
        await message.answer(f"Usage: /{command.command} <user_id>")
        return
    target = int(command.args.strip())
    ok = db.set_premium(target, command.command == "grant")
    await message.answer("Done." if ok else "Unknown user (they must /start the bot first).")


async def cancel_user_job(user_id: int) -> QueuedJob | None:
    job = queue.cancel(user_id)
    if job and job.task is None:  # was still waiting: run_job will never see it
        db.job_done(job.id)
        if job.status_message_id:
            with contextlib.suppress(TelegramBadRequest):
                await bot.edit_message_text(
                    job_header(job) + "\n\n✖️ Cancelled.", chat_id=job.chat_id, message_id=job.status_message_id,
                    link_preview_options={"is_disabled": True},
                )
    return job


@router.message(Command("cancel"))
async def cmd_cancel(message: Message) -> None:
    job = await cancel_user_job(message.from_user.id)
    await message.answer("✖️ Cancelling…" if job else "Nothing to cancel.")


@router.message(F.text == "Translate Video")
async def old_translate_button(message: Message) -> None:
    await message.answer("Just send me a video link 🙂", reply_markup=ReplyKeyboardRemove())


# ---------------------------------------------------------------- links


@router.message(F.text | F.caption)
async def on_text(message: Message) -> None:
    url = extract_url(message.text or message.caption)
    if not url:
        if message.chat.type == "private":  # in groups, stay quiet about ordinary chatter
            await message.answer("Send me a link to a video. /help")
        return
    req = remember_link(url, message.from_user.id)
    await message.answer(
        link_prompt(message.from_user.id, url),
        reply_markup=link_kb(message.from_user.id, req),
        link_preview_options={"is_disabled": True},
    )


@router.callback_query(F.data.startswith("langs:"))
async def cb_langs(cq: CallbackQuery) -> None:
    req = cq.data.split(":", 1)[1]
    await cq.message.edit_text(settings_text(cq.from_user.id), reply_markup=settings_kb(cq.from_user.id, req))
    await cq.answer()


@router.callback_query(F.data.startswith("pick:"))
async def cb_pick(cq: CallbackQuery) -> None:
    _, which, req = cq.data.split(":", 2)
    current = user_langs(cq.from_user.id)[0 if which == "src" else 1]
    title = "Translate <b>from</b>:" if which == "src" else "Translate <b>to</b>:"
    await cq.message.edit_text(title, reply_markup=lang_grid(which, current, req))
    await cq.answer()


@router.callback_query(F.data.startswith("set:"))
async def cb_set(cq: CallbackQuery) -> None:
    _, which, code, req = cq.data.split(":", 3)
    valid = langs.SOURCE_CHOICES if which == "src" else langs.TARGET_CHOICES
    if code in valid:
        db.set_lang(cq.from_user.id, which, code)
    await cb_back_to(cq, req)


@router.callback_query(F.data.startswith("back:"))
async def cb_back(cq: CallbackQuery) -> None:
    await cb_back_to(cq, cq.data.split(":", 1)[1])


async def cb_back_to(cq: CallbackQuery, req: str) -> None:
    link = pending.get(req)
    if link:
        await cq.message.edit_text(
            link_prompt(cq.from_user.id, link.url),
            reply_markup=link_kb(cq.from_user.id, req),
            link_preview_options={"is_disabled": True},
        )
    else:
        await cq.message.edit_text(settings_text(cq.from_user.id), reply_markup=settings_kb(cq.from_user.id))
    await cq.answer()


@router.callback_query(F.data.startswith("cancel:"))
async def cb_cancel(cq: CallbackQuery) -> None:
    job = queue.active_for(cq.from_user.id)
    if job and str(job.id) == cq.data.split(":", 1)[1]:
        await cancel_user_job(cq.from_user.id)
        await cq.answer("Cancelling…")
    else:
        await cq.answer("This job is no longer running.")


@router.callback_query(F.data.startswith("go:"))
async def cb_go(cq: CallbackQuery) -> None:
    _, mode, req = cq.data.split(":", 2)
    link = pending.get(req)
    uid = cq.from_user.id
    if not link or link.user_id != uid or mode not in MODE_LABELS:
        await cq.answer("This link expired, please send it again.", show_alert=True)
        return
    src, dst = user_langs(uid)
    key = cache_key(link.url, mode, src, dst)
    # Already translated for someone: re-send Telegram's copy (free, instant, doesn't use the quota)
    if mode != "text" and await send_cached(cq.message.chat.id, key):
        pending.pop(req, None)
        db.log(uid, cq.from_user.username, link.url, mode, "cache", "cached", 0)
        await cq.message.edit_text(f"♻️ Translated before, sent instantly.\n🔗 {esc(link.url)}")
        await cq.answer()
        return

    if quota_left(uid) == 0:
        await cq.answer(
            f"Daily limit reached ({settings.free_daily_limit}). Come back tomorrow!", show_alert=True
        )
        return
    try:
        await check_url_is_public(link.url)
    except MediaError as err:
        await cq.answer(str(err), show_alert=True)
        return

    if queue.active_for(uid):
        await cq.answer("You already have a job running. Wait for it or /cancel it.", show_alert=True)
        return
    job = QueuedJob(uid, cq.from_user.username, cq.message.chat.id, link.url, mode, src, dst, key)
    job.status_message_id = cq.message.message_id
    job.id = db.job_add(
        user_id=uid, username=job.username, chat_id=job.chat_id, url=job.url, mode=mode, src=src, dst=dst,
        cache_key=key, status_message_id=job.status_message_id,
    )
    try:
        ahead = await queue.submit(job)
    except (AlreadyQueued, QueueFull) as err:
        db.job_done(job.id)
        busy = isinstance(err, QueueFull)
        await cq.answer(
            "The bot is busy, the queue is full. Try again in a few minutes." if busy
            else "You already have a job running. Wait for it or /cancel it.",
            show_alert=True,
        )
        return
    pending.pop(req, None)
    if ahead:
        await cq.message.edit_text(job_header(job) + f"\n\n⏳ In queue, {ahead} ahead of you…", reply_markup=cancel_kb(job))
    await cq.answer()


def job_header(job: QueuedJob) -> str:
    return f"{MODE_LABELS[job.mode]} · {langs.short(job.src)} → {langs.short(job.dst)}\n🔗 {esc(job.url)}"


async def on_position(job: QueuedJob, ahead: int) -> None:
    if job.status_message_id and ahead:
        await bot.edit_message_text(
            job_header(job) + f"\n\n⏳ In queue, {ahead} ahead of you…",
            chat_id=job.chat_id,
            message_id=job.status_message_id,
            reply_markup=cancel_kb(job),
            link_preview_options={"is_disabled": True},
        )


# ---------------------------------------------------------------- job execution


class StatusMessage:
    """Live status lines in one message, edited at most every few seconds (Telegram flood limits)."""

    MIN_INTERVAL = 3.0

    def __init__(self, job: QueuedJob):
        self.job = job
        self.lines: dict[str, str] = {}
        self.last_text = ""
        self.last_edit = 0.0
        self._delayed: asyncio.Task[None] | None = None

    async def update(self, slot: str, text: str) -> None:
        self.lines[slot] = text
        wait = self.MIN_INTERVAL - (time.monotonic() - self.last_edit)
        if wait <= 0:
            await self._flush()
        elif not self._delayed or self._delayed.done():
            self._delayed = asyncio.create_task(self._flush_later(wait))

    async def _flush_later(self, wait: float) -> None:
        await asyncio.sleep(wait)
        await self._flush()

    async def finish(self, text: str) -> None:
        if self._delayed and not self._delayed.done():
            self._delayed.cancel()
        self.lines = {"main": text}
        await self._flush(keyboard=False)

    async def _flush(self, keyboard: bool = True) -> None:
        text = job_header(self.job) + "\n\n" + "\n".join(self.lines.values())
        if text == self.last_text:
            return
        self.last_edit = time.monotonic()
        markup = cancel_kb(self.job) if keyboard else None
        try:
            if self.job.status_message_id:
                await bot.edit_message_text(
                    text[:4096], chat_id=self.job.chat_id, message_id=self.job.status_message_id,
                    reply_markup=markup, link_preview_options={"is_disabled": True},
                )
            else:
                msg = await bot.send_message(
                    self.job.chat_id, text[:4096], reply_markup=markup, link_preview_options={"is_disabled": True}
                )
                self.job.status_message_id = msg.message_id
            self.last_text = text
        except TelegramRetryAfter as err:
            self.last_edit = time.monotonic() + err.retry_after
        except TelegramBadRequest as err:
            if "not modified" not in str(err):
                log.debug("status edit failed: %s", err)


async def run_job(job: QueuedJob) -> None:
    started = time.monotonic()
    status = StatusMessage(job)
    db.job_started(job.id, job.status_message_id)
    free = shutil.disk_usage(settings.data_dir).free
    if free < settings.min_free_disk_mb * 1_000_000:
        log.error("only %d MB free in %s", free // 1_000_000, settings.data_dir)
        db.job_done(job.id)
        await status.finish("❌ The bot's disk is almost full. The admin needs to free some space.")
        return
    await status.update("main", "▶️ Starting…")
    workdir = Path(tempfile.mkdtemp(prefix=f"job{job.id}_", dir=settings.tmp_dir))
    engine, outcome = "", "error"
    try:
        pipeline = Pipeline(Job(job.url, job.mode, job.src, job.dst, workdir), status.update)
        result = await asyncio.wait_for(pipeline.run(), settings.job_timeout_min * 60)
        engine = result.engine
        await status.update("main", "⬆️ Sending to Telegram…")
        await deliver(job, result)
        db.count_translation(job.user_id)
        outcome = "ok"
        await status.finish(f"✅ Done in {fmt_seconds(time.monotonic() - started)} · {esc(engine)}")
    except asyncio.CancelledError:
        if job.interrupted:  # bot shutdown: the job stays in the database and resumes after the restart
            outcome = "interrupted"
            await status.finish("🔁 The bot is restarting. Your job will continue automatically.")
        else:
            outcome = "cancelled"
            await status.finish("✖️ Cancelled.")
        raise
    except MediaError as err:
        await status.finish(f"❌ {esc(err)}")
    except TelegramEntityTooLarge:
        await status.finish("❌ Telegram refused the file as too large. Try 🎧 audio mode, or lower UPLOAD_LIMIT_MB.")
    except asyncio.TimeoutError:
        await status.finish(f"❌ Took longer than {settings.job_timeout_min} minutes, gave up.")
    except ProcError as err:
        log.error("job %s: %s\n%s", job.id, err, err.stderr[-2000:])
        if err.out_of_memory:
            await status.finish("❌ Ran out of memory while processing. Try a shorter video or 🎧 audio mode.")
        else:
            await status.finish(f"❌ {esc(err.program)} failed. The admin can check the logs.")
    except Exception as err:
        log.exception("job %s failed", job.id)
        await status.finish(f"❌ Something went wrong ({esc(type(err).__name__)}). The admin can check the logs.")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        if outcome != "interrupted":
            db.job_done(job.id)
        db.log(job.user_id, job.username, job.url, job.mode, engine, outcome, time.monotonic() - started)


async def restore_jobs() -> None:
    """Re-queue jobs that were waiting or running when the bot stopped (restart, crash, power cut)."""
    for row in db.jobs_unfinished():
        job = QueuedJob(
            row["user_id"], row["username"], row["chat_id"], row["url"], row["mode"], row["src"], row["dst"],
            row["cache_key"], row["status_message_id"], id=row["id"],
        )
        if row["attempts"] >= 2 or queue.active_for(job.user_id):
            # It was interrupted twice: maybe it is what crashes the bot. Don't loop forever.
            db.job_done(job.id)
            note = "❌ This job was interrupted twice by bot restarts, so I gave up. Please send the link again."
        else:
            try:
                await queue.submit(job)
            except QueueFull:
                db.job_done(job.id)
                continue
            note = "🔁 The bot restarted. Your job is back in the queue."
        log.info("restored job %s: %s", job.id, note)
        if job.status_message_id:
            with contextlib.suppress(TelegramAPIError):
                await bot.edit_message_text(
                    job_header(job) + "\n\n" + note, chat_id=job.chat_id, message_id=job.status_message_id,
                    link_preview_options={"is_disabled": True},
                )


def result_caption(job: QueuedJob, result: Result) -> str:
    src = result.src or job.src
    caption = (
        f"<b>{esc(result.title[:200])}</b>\n"
        f"{langs.short(src)} → {langs.short(job.dst)} · {esc(result.engine)}\n{esc(job.url)}"
    )
    return caption[:1024]


async def deliver(job: QueuedJob, result: Result) -> None:
    caption = result_caption(job, result)
    if result.kind == "text":
        assert result.text is not None and result.path
        if len(result.text) <= 3900:
            await tg_retry(bot.send_message, job.chat_id, result.text, parse_mode=None)
        else:
            await tg_retry(bot.send_document, job.chat_id, FSInputFile(result.path), request_timeout=UPLOAD_TIMEOUT)
        for extra in result.extra_files:
            await tg_retry(
                bot.send_document, job.chat_id, FSInputFile(extra), caption=caption, request_timeout=UPLOAD_TIMEOUT
            )
        return

    assert result.path
    size = result.path.stat().st_size
    if size > settings.upload_limit:
        raise MediaError(
            f"The result is {size / 2**20:.0f} MB, over Telegram's {settings.upload_limit / 2**20:.0f} MB limit. "
            "Try 🎧 audio mode."
        )
    name = f"{result.title[:60]}.{job.dst}{result.path.suffix}"
    if result.kind == "video":
        msg = await tg_retry(
            bot.send_video, job.chat_id, FSInputFile(result.path, filename=name), caption=caption,
            duration=int(result.duration) or None, width=result.width or None, height=result.height or None,
            supports_streaming=True, request_timeout=UPLOAD_TIMEOUT,
        )
        media = msg.video or msg.document
        kind = "video" if msg.video else "document"
    else:
        msg = await tg_retry(
            bot.send_audio, job.chat_id, FSInputFile(result.path, filename=name), caption=caption,
            title=f"{result.title[:60]} [{job.dst}]", performer=result.performer,
            duration=int(result.duration) or None, request_timeout=UPLOAD_TIMEOUT,
        )
        media = msg.audio or msg.document
        kind = "audio" if msg.audio else "document"
    if media:
        db.cache_put(job.cache_key, kind, media.file_id, caption)


async def send_cached(chat_id: int, key: str) -> bool:
    row = db.cache_get(key)
    if not row:
        return False
    send = {"video": bot.send_video, "audio": bot.send_audio, "document": bot.send_document}[row["kind"]]
    try:
        await tg_retry(send, chat_id, row["file_id"], caption=row["caption"])
    except TelegramBadRequest as err:
        log.info("cached file %s is gone: %s", key, err)
        db.cache_drop(key)
        return False
    return True


# ---------------------------------------------------------------- startup


dp = Dispatcher()
dp.message.outer_middleware(AccessMiddleware())
dp.callback_query.outer_middleware(AccessMiddleware())
dp.include_router(router)


@dp.errors()
async def on_error(event: ErrorEvent) -> None:
    log.exception("handler failed: %s", event.exception, exc_info=event.exception)
    cq = event.update.callback_query
    with contextlib.suppress(Exception):
        if cq:
            await cq.answer("Something went wrong, please try again.", show_alert=True)
        elif event.update.message:
            await event.update.message.answer("Something went wrong, please try again.")


@dp.startup()
async def on_startup() -> None:
    queue.start()
    await restore_jobs()
    with contextlib.suppress(TelegramAPIError):  # cosmetic: never block startup on it
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="How to use"),
                BotCommand(command="settings", description="Languages"),
                BotCommand(command="cancel", description="Cancel the current job"),
                BotCommand(command="stats", description="Usage"),
            ]
        )


@dp.shutdown()
async def on_shutdown() -> None:
    await queue.stop()


def build() -> tuple[Bot, Dispatcher]:
    global db, queue, bot
    settings.tmp_dir.mkdir(parents=True, exist_ok=True)
    db = Database(settings.db_path)
    session_kwargs: dict[str, Any] = {}
    if settings.telegram_api_server:
        session_kwargs["api"] = TelegramAPIServer.from_base(settings.telegram_api_server, is_local=True)
    if settings.telegram_proxy:
        session_kwargs["proxy"] = settings.telegram_proxy
    bot = Bot(
        settings.telegram_token,
        session=AiohttpSession(**session_kwargs),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    queue = JobQueue(run_job, settings.max_parallel_jobs, settings.max_queue, on_position)
    return bot, dp
