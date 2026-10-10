"""Bot-level robustness without network: restarts, cancellation, disk guard, Telegram retries."""
import asyncio

import pytest
from aiogram.types import User
from helpers import FakeTelegram, button, message, tap, wait_idle

from polyglot import bot as botmod
from polyglot.jobs import JobQueue, QueuedJob
from polyglot.pipeline import Result

URL = "https://youtu.be/AAAAAAAAAAA"
USER = User(id=7, is_bot=False, first_name="T")


class FakePipeline:
    """Stands in for the real pipeline: 'works' for `delay` seconds, then returns a tiny audio file."""

    delay = 0.0
    runs = 0

    def __init__(self, job, progress):
        self.job, self.progress = job, progress

    async def run(self):
        FakePipeline.runs += 1
        await self.progress("main", "working…")
        await asyncio.sleep(FakePipeline.delay)
        path = self.job.workdir / "result.m4a"
        path.write_bytes(b"\0" * 1000)
        return Result(kind="audio", title="T", engine="fake", src="en", duration=1, path=path)


@pytest.fixture
def env(tmp_path, configure, monkeypatch):
    configure(data_dir=tmp_path, telegram_token="42:TEST", free_daily_limit=0, min_free_disk_mb=1)
    monkeypatch.setattr(botmod, "Pipeline", FakePipeline)
    monkeypatch.setattr(botmod, "RETRY_DELAY", 0)
    FakePipeline.delay, FakePipeline.runs = 0.0, 0

    def start():
        bot, dp = botmod.build()
        fake = FakeTelegram()
        bot.session = fake
        return bot, dp, fake

    return start


def edits(fake):
    return fake.texts("EditMessageText")


def test_interrupted_job_resumes_after_restart(env):
    async def scenario():
        bot, dp, fake = env()
        botmod.queue.start()
        FakePipeline.delay = 30
        await dp.feed_update(bot, message(USER, URL))
        await dp.feed_update(bot, tap(USER, button(fake, "go:audio:"), 10))
        await asyncio.sleep(0.2)
        assert botmod.queue.running
        await botmod.queue.stop()  # docker stop / power cut
        assert "restarting" in edits(fake)[-1]
        assert len(botmod.db.jobs_unfinished()) == 1

        # new process
        bot, dp, fake = env()
        FakePipeline.delay = 0
        botmod.queue.start()
        await botmod.restore_jobs()
        assert "back in the queue" in edits(fake)[0]
        await wait_idle(10)
        assert fake.uploads[-1][0] == "audio" and not botmod.db.jobs_unfinished()
        await botmod.queue.stop()

    asyncio.run(scenario())


def test_job_interrupted_twice_is_dropped(env):
    async def scenario():
        bot, dp, fake = env()
        botmod.db.job_add(user_id=7, chat_id=7, url=URL, mode="audio", src="auto", dst="ru", cache_key="k",
                          status_message_id=5, attempts=2)
        botmod.queue.start()
        await botmod.restore_jobs()
        assert "gave up" in edits(fake)[-1] and not botmod.db.jobs_unfinished()
        assert FakePipeline.runs == 0
        await botmod.queue.stop()

    asyncio.run(scenario())


def test_cancel_waiting_and_running_jobs(env, configure):
    async def scenario():
        bot, dp, fake = env()
        botmod.queue.start()
        FakePipeline.delay = 30
        other = User(id=8, is_bot=False, first_name="O")
        await dp.feed_update(bot, message(other, URL))
        await dp.feed_update(bot, tap(other, button(fake, "go:audio:"), 20))  # occupies the only worker
        await dp.feed_update(bot, message(USER, URL))
        await dp.feed_update(bot, tap(USER, button(fake, "go:video:"), 21))
        assert "In queue" in edits(fake)[-1]
        await dp.feed_update(bot, message(USER, "/cancel"))  # waiting job
        assert "Cancelled" in edits(fake)[-1]
        await dp.feed_update(bot, message(other, "/cancel"))  # running job
        await wait_idle(10)
        assert "Cancelled" in edits(fake)[-1]
        assert not botmod.db.jobs_unfinished() and FakePipeline.runs == 1
        await botmod.queue.stop()

    asyncio.run(scenario())


def test_disk_guard(env, configure):
    async def scenario():
        bot, dp, fake = env()
        configure(min_free_disk_mb=10**12)
        botmod.queue.start()
        await dp.feed_update(bot, message(USER, URL))
        await dp.feed_update(bot, tap(USER, button(fake, "go:audio:"), 30))
        await wait_idle(10)
        assert "disk is almost full" in edits(fake)[-1] and FakePipeline.runs == 0
        await botmod.queue.stop()

    asyncio.run(scenario())


def test_upload_survives_network_errors(env):
    async def scenario():
        bot, dp, fake = env()
        fake.fail_next["SendAudio"] = 2
        botmod.queue.start()
        await dp.feed_update(bot, message(USER, URL))
        await dp.feed_update(bot, tap(USER, button(fake, "go:audio:"), 40))
        await wait_idle(10)
        sends = [c for c in fake.calls if type(c).__name__ == "SendAudio"]
        assert len(sends) == 3 and "Done" in edits(fake)[-1]
        await botmod.queue.stop()

    asyncio.run(scenario())


def test_group_chatter_is_ignored(env):
    async def scenario():
        from aiogram.types import Chat, Message, Update
        import datetime as dt

        bot, dp, fake = env()
        chat = Chat(id=-100, type="supergroup")
        msg = Message(message_id=1, date=dt.datetime.now(), chat=chat, from_user=USER, text="hello everyone")
        await dp.feed_update(bot, Update(update_id=1, message=msg))
        assert not fake.texts("SendMessage")

    asyncio.run(scenario())


def test_queue_pause_waits_for_running_job_and_blocks_new_ones():
    async def scenario():
        log, release = [], asyncio.Event()

        async def runner(job):
            log.append(f"start {job.user_id}")
            if job.user_id == 1:
                await release.wait()
            log.append(f"end {job.user_id}")

        q = JobQueue(runner, workers=1, max_queue=5)
        q.start()
        await q.submit(QueuedJob(1, None, 1, "u", "audio", "auto", "ru", "k"))
        await asyncio.sleep(0.05)

        async def maintenance():
            async with q.paused():
                log.append("update")
                await q.submit(QueuedJob(2, None, 2, "u", "audio", "auto", "ru", "k"))
                await asyncio.sleep(0.1)

        task = asyncio.create_task(maintenance())
        await asyncio.sleep(0.05)
        release.set()
        await task
        await asyncio.sleep(0.05)
        assert log == ["start 1", "end 1", "update", "start 2", "end 2"]
        await q.stop()

    asyncio.run(scenario())
