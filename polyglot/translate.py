"""Machine translation of subtitle lines. Free engines need no key; keyed engines are skipped when unset."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from collections.abc import Callable, Iterable
from typing import Any

import aiohttp

from .config import settings
from .media import MediaError

log = logging.getLogger(__name__)

TIMEOUT = aiohttp.ClientTimeout(total=90)
BROWSER_UA = "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"


class TranslateError(MediaError):
    pass


def batches(lines: list[str], max_chars: int, max_items: int) -> Iterable[tuple[int, list[str]]]:
    """Yield (offset, chunk) so that every chunk stays under the engine's request limits."""
    start, size = 0, 0
    for i, line in enumerate(lines):
        if i > start and (size + len(line) > max_chars or i - start >= max_items):
            yield start, lines[start:i]
            start, size = i, 0
        size += len(line) + 1
    if start < len(lines):
        yield start, lines[start:]


async def _post_json(session: aiohttp.ClientSession, url: str, **kwargs: Any) -> Any:
    for attempt in range(3):
        async with session.post(url, timeout=TIMEOUT, **kwargs) as resp:
            retry_after = resp.headers.get("Retry-After", "")
            if resp.status == 429 and attempt < 2 and retry_after.replace(".", "", 1).isdigit():
                # Keyed APIs (Groq, DeepL) say how long to wait; free endpoints don't, so they fail fast
                wait = float(retry_after)
                if wait <= 30:
                    log.info("%s rate-limited, waiting %.0fs", url.split("/")[2], wait)
                    await asyncio.sleep(wait)
                    continue
            if resp.status >= 400:
                raise TranslateError(f"HTTP {resp.status}: {(await resp.text())[:120]}")
            return await resp.json(content_type=None)
    raise TranslateError("rate limited")


class Engine:
    name = "?"
    max_chars = 4000
    max_items = 100

    def available(self) -> bool:
        return True

    async def translate_batch(self, session: aiohttp.ClientSession, lines: list[str], src: str, dst: str) -> list[str]:
        raise NotImplementedError


class GoogleFree(Engine):
    """Unofficial Google endpoint used by the Chrome dictionary extension: list in, list out."""

    name = "Google"
    max_chars = 3500
    max_items = 80

    async def translate_batch(self, session, lines, src, dst):
        form = [("client", "dict-chrome-ex"), ("sl", src or "auto"), ("tl", dst)] + [("q", line) for line in lines]
        data = await _post_json(
            session, "https://clients5.google.com/translate_a/t", data=form, headers={"User-Agent": BROWSER_UA}
        )
        # With sl=auto every item is [translation, detected_lang]
        return [item[0] if isinstance(item, list) else item for item in data]


class YandexFree(Engine):
    """Unofficial endpoint of the Yandex Translate Android app."""

    name = "Yandex Translate"
    max_chars = 8000
    max_items = 100

    async def translate_batch(self, session, lines, src, dst):
        lang = dst if not src or src == "auto" else f"{src}-{dst}"
        params = {"id": f"{uuid.uuid4().hex}-0-0", "srv": "android", "lang": lang}
        data = await _post_json(
            session,
            "https://translate.yandex.net/api/v1/tr.json/translate",
            params=params,
            data=[("text", line) for line in lines],
            headers={"User-Agent": "ru.yandex.translate/21.15.4.21402814 (Xiaomi Redmi K20 Pro; Android 11)"},
        )
        if data.get("code") != 200:
            raise TranslateError(f"Yandex: {data.get('message') or data.get('code')}")
        return data["text"]


class DeepL(Engine):
    name = "DeepL"
    max_chars = 20000
    max_items = 50

    def available(self):
        return bool(settings.deepl_api_key)

    async def translate_batch(self, session, lines, src, dst):
        key = settings.deepl_api_key
        host = "api-free.deepl.com" if key.endswith(":fx") else "api.deepl.com"
        target = {"en": "EN-US", "pt": "PT-BR", "zh": "ZH-HANS"}.get(dst, dst.upper())
        payload: dict[str, Any] = {"text": lines, "target_lang": target}
        if src and src != "auto":
            payload["source_lang"] = src.upper()
        data = await _post_json(
            session, f"https://{host}/v2/translate", json=payload, headers={"Authorization": f"DeepL-Auth-Key {key}"}
        )
        return [t["text"] for t in data["translations"]]


class LLM(Engine):
    """Any OpenAI-compatible chat API (Groq, Gemini, OpenRouter, a local server...). Best for context."""

    name = "LLM"
    max_chars = 5000
    max_items = 60

    def available(self):
        return bool(settings.llm_api_key)

    async def translate_batch(self, session, lines, src, dst):
        from . import langs

        source = "the source language" if not src or src == "auto" else langs.name(src).split(" ", 1)[-1]
        target = langs.name(dst).split(" ", 1)[-1]
        prompt = (
            f"Translate these consecutive video subtitle lines from {source} to {target}. "
            "Keep the meaning natural for a voice-over; keep names and terms; do not merge or split lines. "
            "Reply with ONLY a JSON array of strings with exactly the same number of items, in order.\n\n"
            + json.dumps(lines, ensure_ascii=False)
        )
        data = await _post_json(
            session,
            f"{settings.llm_base_url}/chat/completions",
            json={"model": settings.llm_model, "temperature": 0.2, "messages": [{"role": "user", "content": prompt}]},
            headers={"Authorization": f"Bearer {settings.llm_api_key}"},
        )
        content = data["choices"][0]["message"]["content"]
        match = re.search(r"\[.*\]", content, re.DOTALL)
        if not match:
            raise TranslateError("LLM reply has no JSON array")
        return [str(x) for x in json.loads(match.group(0))]


ENGINES: dict[str, Callable[[], Engine]] = {
    "google": GoogleFree,
    "yandex": YandexFree,
    "deepl": DeepL,
    "llm": LLM,
}


def chain() -> list[Engine]:
    names = list(settings.mt_engines)
    # Free engines are always kept as a last resort
    names += [n for n in ("yandex", "google") if n not in names]
    engines = [ENGINES[n]() for n in names if n in ENGINES]
    return [e for e in engines if e.available()]


async def translate_lines(lines: list[str], src: str, dst: str) -> tuple[list[str], str]:
    """Translate keeping 1:1 line alignment. Each batch falls through the engine chain on failure."""
    if src == dst:
        return list(lines), "none"
    result: list[str] = list(lines)
    used: list[str] = []
    engines = chain()
    failures: dict[str, int] = {}
    async with aiohttp.ClientSession() as session:
        for offset, chunk in batches(lines, min(e.max_chars for e in engines), min(e.max_items for e in engines)):
            for engine in engines:
                if failures.get(engine.name, 0) >= 2:
                    continue  # circuit breaker: stop hammering an engine that keeps failing
                try:
                    out = await engine.translate_batch(session, chunk, src, dst)
                    if len(out) != len(chunk):
                        raise TranslateError(f"got {len(out)} lines for {len(chunk)}")
                except (TranslateError, aiohttp.ClientError, asyncio.TimeoutError, KeyError, ValueError) as err:
                    log.warning("MT %s failed: %s", engine.name, err)
                    failures[engine.name] = failures.get(engine.name, 0) + 1
                    continue
                result[offset : offset + len(chunk)] = [o.strip() for o in out]
                if engine.name not in used:
                    used.append(engine.name)
                break
            else:
                raise TranslateError("all translation engines failed")
    return result, " + ".join(used)
