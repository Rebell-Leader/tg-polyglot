"""Fake Telegram Bot API for driving the real handlers in tests."""
import asyncio
import datetime as dt
import itertools
from pathlib import Path

from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramNetworkError
from aiogram.types import Audio, CallbackQuery, Chat, Document, FSInputFile, Message, Update, Video

from polyglot import bot as botmod

ids = itertools.count(1000)


class FakeTelegram(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.uploads = []
        self.fail_next: dict[str, int] = {}  # method name -> how many network failures to inject

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):  # pragma: no cover
        raise NotImplementedError
        yield b""

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        name = type(method).__name__
        if self.fail_next.get(name):
            self.fail_next[name] -= 1
            raise TelegramNetworkError(method=method, message="injected network failure")
        chat = Chat(id=getattr(method, "chat_id", None) or 1, type="private")

        def msg(**kw):
            return Message(message_id=next(ids), date=dt.datetime.now(), chat=chat, **kw)

        for field in ("video", "audio", "document"):
            value = getattr(method, field, None)
            if isinstance(value, FSInputFile):
                path = Path(value.path)
                self.uploads.append((field, path.name, path.stat().st_size))
        if name in ("SendMessage", "EditMessageText"):
            return msg(text=method.text)
        if name == "SendVideo":
            return msg(video=Video(file_id="VIDEO-ID", file_unique_id="v", width=1, height=1, duration=1))
        if name == "SendAudio":
            return msg(audio=Audio(file_id="AUDIO-ID", file_unique_id="a", duration=1))
        if name == "SendDocument":
            return msg(document=Document(file_id="DOC-ID", file_unique_id="d"))
        return True

    def texts(self, kind):
        return [c.text for c in self.calls if type(c).__name__ == kind]

    def answers(self):
        return [c.text for c in self.calls if type(c).__name__ == "AnswerCallbackQuery" and c.text]


def message(user, text):
    chat = Chat(id=user.id, type="private")
    return Update(
        update_id=next(ids),
        message=Message(message_id=next(ids), date=dt.datetime.now(), chat=chat, from_user=user, text=text),
    )


def tap(user, data, message_id):
    chat = Chat(id=user.id, type="private")
    msg = Message(message_id=message_id, date=dt.datetime.now(), chat=chat, text="…")
    return Update(
        update_id=next(ids),
        callback_query=CallbackQuery(id=str(next(ids)), from_user=user, chat_instance="x", data=data, message=msg),
    )


def button(fake, prefix):
    for call in reversed(fake.calls):
        markup = getattr(call, "reply_markup", None)
        for row in getattr(markup, "inline_keyboard", None) or []:
            for b in row:
                if b.callback_data and b.callback_data.startswith(prefix):
                    return b.callback_data
    raise AssertionError(f"no button {prefix}")


async def wait_idle(timeout=300):
    for _ in range(timeout * 10):
        await asyncio.sleep(0.1)
        if not botmod.queue.running and not botmod.queue.waiting:
            return
    raise AssertionError("job did not finish")
