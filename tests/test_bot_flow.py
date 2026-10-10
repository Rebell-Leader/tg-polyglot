"""End-to-end bot flow against a fake Telegram API (real yt-dlp/Yandex/ffmpeg).

Needs network access: RUN_NETWORK_TESTS=1 python -m pytest tests/test_bot_flow.py
"""
import asyncio
import os

import pytest
from aiogram.types import User
from helpers import FakeTelegram, button, message, tap, wait_idle

from polyglot import bot as botmod

pytestmark = pytest.mark.skipif(os.getenv("RUN_NETWORK_TESTS") != "1", reason="needs network")

URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"  # 19 s


def test_full_flow(tmp_path, configure):
    configure(data_dir=tmp_path, telegram_token="42:TEST", free_daily_limit=1, admin_ids=frozenset({99}))

    async def scenario():
        bot, dp = botmod.build()
        fake = FakeTelegram()
        bot.session = fake
        botmod.queue.start()
        user = User(id=7, is_bot=False, first_name="T", username="t")
        admin = User(id=99, is_bot=False, first_name="A")

        await dp.feed_update(bot, message(user, "/start"))
        assert "Send me a link" in fake.texts("SendMessage")[0]

        # link -> mode buttons -> job -> video uploaded
        await dp.feed_update(bot, message(user, f"please translate {URL}"))
        await dp.feed_update(bot, tap(user, button(fake, "go:video:"), 555))
        await wait_idle()
        assert fake.uploads and fake.uploads[-1][0] == "video" and fake.uploads[-1][2] > 100_000
        assert any("✅ Done" in t for t in fake.texts("EditMessageText")), fake.texts("EditMessageText")[-3:]
        assert not list((tmp_path / "tmp").iterdir()), "job files must be cleaned up"

        # same video again: sent from Telegram's cache instantly, doesn't use the quota
        uploads = len(fake.uploads)
        await dp.feed_update(bot, message(user, URL))
        await dp.feed_update(bot, tap(user, button(fake, "go:video:"), 556))
        sent = [c for c in fake.calls if type(c).__name__ == "SendVideo"]
        assert sent[-1].video == "VIDEO-ID" and len(fake.uploads) == uploads

        # new request over the daily limit (1) is refused
        await dp.feed_update(bot, message(user, URL))
        await dp.feed_update(bot, tap(user, button(fake, "go:audio:"), 557))
        assert "Daily limit" in fake.answers()[-1]

        # admin: no limit; text mode; target language changed via the buttons
        await dp.feed_update(bot, message(admin, URL))
        req = button(fake, "go:text:").rsplit(":", 1)[1]
        await dp.feed_update(bot, tap(admin, f"set:dst:de:{req}", 558))
        await dp.feed_update(bot, tap(admin, f"go:text:{req}", 558))
        await wait_idle()
        assert fake.uploads[-1][1].endswith(".de.srt"), fake.uploads[-3:]
        assert any("elefant" in t.lower() for t in fake.texts("SendMessage")), fake.texts("SendMessage")[-2:]

        await dp.feed_update(bot, message(admin, "/stats"))
        assert "Admin" in fake.texts("SendMessage")[-1]
        await botmod.queue.stop()

    asyncio.run(scenario())
