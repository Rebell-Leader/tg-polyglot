"""Run one translation without Telegram, for testing on the Pi:

    python -m polyglot.cli https://youtu.be/... --mode video --dst ru --out ./out
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
import tempfile
import time
from pathlib import Path

from .config import settings
from .pipeline import MODES, Job, Pipeline, _safe_name


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("url")
    parser.add_argument("--mode", choices=MODES, default="video")
    parser.add_argument("--src", default="auto")
    parser.add_argument("--dst", default=settings.default_dst)
    parser.add_argument("--out", type=Path, default=Path("out"))
    args = parser.parse_args()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    started = time.monotonic()

    async def progress(slot: str, text: str) -> None:
        print(f"[{time.monotonic() - started:6.1f}s] {slot:>18} | {text}", flush=True)

    settings.tmp_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=settings.tmp_dir) as tmp:
        result = await Pipeline(Job(args.url, args.mode, args.src, args.dst, Path(tmp)), progress).run()
        args.out.mkdir(parents=True, exist_ok=True)
        for path in [result.path, *result.extra_files]:
            if not path:
                continue
            name = path.name
            if path.stem.startswith("result"):
                name = f"{_safe_name(result.title)}.{args.dst}{path.suffix}"
            shutil.copy(path, args.out / name)
            print(f"saved {args.out / name} ({path.stat().st_size / 2**20:.1f} MB)")
    print(f"engine: {result.engine}; took {time.monotonic() - started:.0f}s")


if __name__ == "__main__":
    asyncio.run(main())
