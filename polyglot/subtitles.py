"""Subtitle parsing/formatting. Segments are the common currency between captions, ASR, MT and TTS."""
from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, replace
from pathlib import Path


@dataclass
class Segment:
    start: float  # seconds
    end: float
    text: str


_SENTENCE_END = (".", "!", "?", "…", "。", "！", "？")
_TAG_RE = re.compile(r"<[^>]+>")
_TIME_RE = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{3})")


def _clean(text: str) -> str:
    text = html.unescape(_TAG_RE.sub("", text))
    return re.sub(r"\s+", " ", text).strip()


def _ts(value: str) -> float:
    m = _TIME_RE.search(value)
    if not m:
        raise ValueError(f"bad timestamp: {value!r}")
    h, mnt, s, ms = m.groups()
    return int(h or 0) * 3600 + int(mnt) * 60 + int(s) + int(ms) / 1000


def _clip_overlaps(segs: list[Segment]) -> list[Segment]:
    segs = sorted((s for s in segs if s.text), key=lambda s: s.start)
    for cur, nxt in zip(segs, segs[1:]):
        if cur.end > nxt.start:
            cur.end = max(cur.start, nxt.start)
    return segs


def parse_json3(data: dict | str) -> list[Segment]:
    """YouTube's json3 captions (manual or auto-generated)."""
    if isinstance(data, str):
        data = json.loads(data)
    segs = []
    for ev in data.get("events") or []:
        parts = ev.get("segs")
        if not parts or ev.get("aAppend"):
            continue
        text = _clean("".join(p.get("utf8", "") for p in parts))
        if not text:
            continue
        start = ev.get("tStartMs", 0) / 1000
        segs.append(Segment(start, start + ev.get("dDurationMs", 0) / 1000, text))
    return _clip_overlaps(segs)


def parse_vtt(text: str) -> list[Segment]:
    """WebVTT, including YouTube's auto-captions whose cues repeat the previous line."""
    segs: list[Segment] = []
    prev_lines: list[str] = []
    for block in re.split(r"\n\s*\n", text.replace("\r", "")):
        lines = block.strip().splitlines()
        idx = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if idx is None:
            continue
        start_s, _, end_s = lines[idx].partition("-->")
        cue_lines = [_clean(line) for line in lines[idx + 1 :]]
        new_lines = [line for line in cue_lines if line and line not in prev_lines]
        prev_lines = cue_lines
        if new_lines:
            segs.append(Segment(_ts(start_s), _ts(end_s), " ".join(new_lines)))
    return _clip_overlaps(segs)


def parse_srt(text: str) -> list[Segment]:
    segs = []
    for block in re.split(r"\n\s*\n", text.replace("\r", "").strip()):
        lines = block.splitlines()
        idx = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if idx is None:
            continue
        start_s, _, end_s = lines[idx].partition("-->")
        body = _clean(" ".join(lines[idx + 1 :]))
        if body:
            segs.append(Segment(_ts(start_s), _ts(end_s), body))
    return _clip_overlaps(segs)


def parse_file(path: Path) -> list[Segment]:
    text = path.read_text(encoding="utf-8", errors="replace")
    suffix = path.suffix.lower()
    if suffix == ".json3":
        return parse_json3(text)
    if suffix == ".vtt":
        return parse_vtt(text)
    if suffix == ".srt":
        return parse_srt(text)
    if text.lstrip().startswith("{"):
        return parse_json3(text)
    return parse_vtt(text) if text.lstrip().startswith("WEBVTT") else parse_srt(text)


def merge_phrases(
    segs: list[Segment], max_chars: int = 250, max_gap: float = 1.0, max_dur: float = 15.0
) -> list[Segment]:
    """Join caption fragments into phrase-sized chunks: better translation context and natural TTS."""
    out: list[Segment] = []
    cur: Segment | None = None
    for seg in segs:
        if cur is None:
            cur = replace(seg)
            continue
        ended = cur.text.endswith(_SENTENCE_END) and len(cur.text) >= 40
        if (
            seg.start - cur.end > max_gap
            or len(cur.text) + 1 + len(seg.text) > max_chars
            or seg.end - cur.start > max_dur
            or ended
        ):
            out.append(cur)
            cur = replace(seg)
        else:
            cur.text = f"{cur.text} {seg.text}"
            cur.end = max(cur.end, seg.end)
    if cur:
        out.append(cur)
    return out


def _srt_time(t: float) -> str:
    ms = max(0, round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def to_srt(segs: list[Segment]) -> str:
    return "\n".join(
        f"{i}\n{_srt_time(s.start)} --> {_srt_time(s.end)}\n{s.text}\n" for i, s in enumerate(segs, 1)
    )


def to_text(segs: list[Segment], paragraph_gap: float = 2.5) -> str:
    """Readable transcript: phrases joined into paragraphs at long pauses."""
    paragraphs: list[list[str]] = []
    last_end = None
    for s in segs:
        if last_end is None or s.start - last_end > paragraph_gap or len(paragraphs[-1]) >= 8:
            paragraphs.append([])
        paragraphs[-1].append(s.text)
        last_end = s.end
    return "\n\n".join(" ".join(p) for p in paragraphs)
