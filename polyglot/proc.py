"""Async subprocess helper: never blocks the event loop, kills the whole process tree on cancel/timeout."""
from __future__ import annotations

import asyncio
import inspect
import logging
import os
import shutil
import signal
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

from .config import settings

log = logging.getLogger(__name__)

LineCallback = Callable[[str], Awaitable[None] | None]


class ProcError(RuntimeError):
    def __init__(self, program: str, code: int | None, stderr: str):
        self.program = program
        self.code = code
        self.stderr = stderr
        reason = "killed, probably out of memory" if code == -signal.SIGKILL else last_error_line(stderr)
        super().__init__(f"{program} failed ({code}): {reason}")

    @property
    def out_of_memory(self) -> bool:
        return self.code == -signal.SIGKILL


# Children get a high OOM score: if memory runs out, the kernel kills the ffmpeg/yt-dlp/node child
# (that job fails with a clear message) instead of the bot itself.
_CHOOM = ["choom", "-n", "500", "--"] if shutil.which("choom") else []


def last_error_line(stderr: str) -> str:
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    for line in reversed(lines):
        if line.startswith("ERROR:"):
            return line
    return lines[-1] if lines else "no output"


async def run(
    args: Sequence[str],
    *,
    on_line: LineCallback | None = None,
    timeout: float | None = None,
    env: dict[str, str] | None = None,
    low_priority: bool = True,
    stdout_path: Path | None = None,
) -> str:
    """Run a command, return its stdout (or feed stdout lines to on_line, or write it to stdout_path).
    Raise ProcError on non-zero exit."""
    cmd = list(args)
    if low_priority and settings.nice:
        cmd = ["nice", "-n", str(settings.nice), *cmd]
    cmd = [*_CHOOM, *cmd]
    out_file = stdout_path.open("wb") if stdout_path else None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=out_file or asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, **env} if env else None,
            start_new_session=True,  # own process group, so we can kill children (ffmpeg, js runtime, ...)
            limit=1024 * 1024,
        )
    finally:
        if out_file:
            out_file.close()  # the child has its own copy of the descriptor
    out: list[str] = []
    err: deque[str] = deque(maxlen=60)

    async def read_stdout() -> None:
        if proc.stdout is None:
            return
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").rstrip("\r\n")
            if on_line is None:
                out.append(line)
                continue
            result = on_line(line)
            if inspect.isawaitable(result):
                await result

    async def read_stderr() -> None:
        assert proc.stderr
        async for raw in proc.stderr:
            err.append(raw.decode(errors="replace").rstrip())

    try:
        await asyncio.wait_for(asyncio.gather(read_stdout(), read_stderr(), proc.wait()), timeout)
    except BaseException as exc:
        _kill(proc)
        await proc.wait()
        if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
            raise ProcError(os.path.basename(args[0]), None, f"timed out after {timeout:.0f}s") from None
        raise
    if proc.returncode != 0:
        stderr = "\n".join(err)
        log.debug("%s stderr:\n%s", args[0], stderr)
        raise ProcError(os.path.basename(args[0]), proc.returncode, stderr)
    return "\n".join(out)


def _kill(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
