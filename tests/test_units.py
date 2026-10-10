import asyncio
import datetime as dt
import sqlite3
import subprocess

import pytest

from polyglot import media, subtitles, translate, tts
from polyglot.db import Database
from polyglot.jobs import AlreadyQueued, JobQueue, QueuedJob
from polyglot.pipeline import cache_key


# ---------------------------------------------------------------- subtitles


def test_parse_json3_skips_appends_and_clips_overlaps():
    data = {
        "events": [
            {"tStartMs": 0, "dDurationMs": 4000, "segs": [{"utf8": "hello"}, {"utf8": " world", "tOffsetMs": 500}]},
            {"tStartMs": 2000, "dDurationMs": 10, "aAppend": 1, "segs": [{"utf8": "\n"}]},
            {"tStartMs": 2500, "dDurationMs": 3000, "segs": [{"utf8": "second &amp; line"}]},
            {"tStartMs": 6000, "dDurationMs": 1000},
        ]
    }
    segs = subtitles.parse_json3(data)
    assert [(s.start, s.end, s.text) for s in segs] == [(0, 2.5, "hello world"), (2.5, 5.5, "second & line")]


def test_parse_vtt_drops_youtube_rolling_duplicates():
    vtt = """WEBVTT

00:00:00.000 --> 00:00:02.000
<c>first</c> line

00:00:02.000 --> 00:00:04.000
first line
second line

00:00:04.000 --> 00:00:06.000
second line
third line
"""
    assert [s.text for s in subtitles.parse_vtt(vtt)] == ["first line", "second line", "third line"]


def test_srt_round_trip_and_merge():
    segs = [
        subtitles.Segment(0, 1, "This is the first sentence of a long enough phrase here."),
        subtitles.Segment(1.1, 2, "Next one"),
        subtitles.Segment(2.1, 3, "continues."),
        subtitles.Segment(6, 7, "After a pause."),
    ]
    merged = subtitles.merge_phrases(segs)
    assert [m.text for m in merged] == [segs[0].text, "Next one continues.", "After a pause."]
    parsed = subtitles.parse_srt(subtitles.to_srt(merged))
    assert [(round(p.start, 3), round(p.end, 3), p.text) for p in parsed] == [
        (m.start, m.end, m.text) for m in merged
    ]
    assert subtitles.to_text(merged).count("\n\n") == 1


# ---------------------------------------------------------------- tts timeline


def test_mp3_duration_is_exact(tmp_path):
    path = tmp_path / "t.mp3"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-filter_complex", "anullsrc=r=24000:cl=mono", "-t", "3.3",
         "-c:a", "libmp3lame", "-b:a", "48k", "-write_xing", "0", str(path)],
        check=True,
    )
    data = path.read_bytes()
    one = tts.mp3_duration(data)
    assert one == pytest.approx(3.3, abs=0.1)  # encoders add a few frames of padding
    assert one / tts.FRAME == pytest.approx(round(one / tts.FRAME))  # whole frames
    assert tts.mp3_duration(data + data) == pytest.approx(2 * one)  # concatenation, as the TTS timeline does
    assert tts.mp3_duration(b"ID3\x03\x00\x00\x00\x00\x00\x00" + data) == pytest.approx(one)


def test_concat_list_is_frame_exact_and_handles_long_gaps():
    f = tts.FRAME
    clips = [(1.0, "a.mp3", 10 * f), (75.0, "b.mp3", 5 * f), (75.01, "c.mp3", f)]
    lines = tts.build_concat_list(clips, "s.mp3", 30.0)
    files = [line for line in lines if line.startswith("file")]
    assert files[-2:] == ["file 'b.mp3'", "file 'c.mp3'"]  # c starts right after b (b ran long)
    silence_frames = sum(
        round(float(line.split()[1]) / f + 0.5) for line in lines if line.startswith("outpoint")
    )
    # total silence + first clip puts b exactly at 75 s (to one frame)
    assert abs(silence_frames * f + 10 * f - 75.0) <= f
    assert files.count("file 's.mp3'") >= 3  # 74 s of gaps need several 30 s silence chunks


# ---------------------------------------------------------------- format selection


def _fmt(fid, vcodec="none", acodec="none", height=None, size=None, ext="mp4", **kw):
    return {"format_id": fid, "vcodec": vcodec, "acodec": acodec, "height": height, "filesize": size,
            "ext": ext, "protocol": "https", **kw}


INFO = {
    "duration": 600,
    "formats": [
        _fmt("140", acodec="mp4a.40.2", ext="m4a", abr=128, size=9e6, format_note="English original (default)",
             language="en", language_preference=10),
        _fmt("140-ru", acodec="mp4a.40.2", ext="m4a", abr=128, size=9e6, format_note="Russian", language="ru",
             language_preference=-1),
        _fmt("139-ru", acodec="mp4a.40.5", ext="m4a", abr=48, size=3e6, format_note="Russian, low", language="ru",
             language_preference=-1),
        _fmt("136", vcodec="avc1.4d401f", height=720, width=1280, size=40e6),
        _fmt("247", vcodec="vp9", height=720, width=1280, size=30e6, ext="webm"),
        _fmt("135", vcodec="avc1.4d401e", height=480, width=854, size=20e6),
        _fmt("137", vcodec="avc1.640028", height=1080, width=1920, size=80e6),
        _fmt("18", vcodec="avc1.42001E", acodec="mp4a.40.2", height=360, width=640, size=15e6),
        _fmt("sb0", ext="mhtml", vcodec="none", acodec="none"),
    ],
}


def test_pick_video_prefers_h264_and_respects_budget():
    assert media.pick_video(INFO, 45e6, 720).format_id == "136"
    assert media.pick_video(INFO, 35e6, 720).format_id == "135"  # 720p h264 too big; vp9 loses to h264
    assert media.pick_video(INFO, 45e6, 480).format_id == "135"
    sel = media.pick_video(INFO, 16e6, 1080)
    assert (sel.format_id, sel.muxed) == ("18", True)
    with pytest.raises(media.MediaError):
        media.pick_video(INFO, 1e6, 720)


def test_vertical_video_uses_short_side():
    info = {"duration": 60, "formats": [_fmt("v", vcodec="avc1", height=1280, width=720, size=5e6)]}
    assert media.pick_video(info, 45e6, 720).format_id == "v"


def test_audio_track_choice():
    assert media.pick_original_audio(INFO)["format_id"] == "140"
    assert media.pick_dub_audio(INFO, "ru")["format_id"] == "140-ru"
    assert media.pick_dub_audio(INFO, "de") is None
    assert media.pick_dub_audio(INFO, "en") is None  # the original is not a dub


def test_unknown_codecs_count_as_muxed():
    info = {"duration": 10, "formats": [{"format_id": "0", "ext": "mp4", "url": "x", "protocol": "https"}]}
    assert media.pick_video(info, 45e6, 720).muxed
    assert media.pick_any_audio(info)[1] is True


def test_extract_url():
    assert media.extract_url("look: https://youtu.be/abc?t=1).") == "https://youtu.be/abc?t=1"
    assert media.extract_url("no link here") is None
    assert media.extract_url(None) is None


def test_private_urls_are_rejected():
    for url in ("http://127.0.0.1/x", "http://localhost:8080/", "http://192.168.1.1/video.mp4"):
        with pytest.raises(media.MediaError):
            asyncio.run(media.check_url_is_public(url))


def test_cache_key_normalises_youtube_links():
    keys = {
        cache_key(u, "video", "auto", "ru")
        for u in (
            "https://www.youtube.com/watch?v=jNQXAC9IVRw",
            "https://youtu.be/jNQXAC9IVRw?si=xyz",
            "https://m.youtube.com/watch?feature=share&v=jNQXAC9IVRw",
            "https://www.youtube.com/shorts/jNQXAC9IVRw",
        )
    }
    assert keys == {"yt:jNQXAC9IVRw|auto|ru|video"}


def test_translate_batches_respect_limits():
    lines = ["a" * 40] * 10
    chunks = list(translate.batches(lines, max_chars=100, max_items=3))
    assert [len(c) for _, c in chunks] == [2, 2, 2, 2, 2]
    assert [o for o, _ in chunks] == [0, 2, 4, 6, 8]


# ---------------------------------------------------------------- database


def test_quota_resets_daily(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    db.upsert_user(1, "u")
    day1, day2 = dt.date(2026, 1, 1), dt.date(2026, 1, 2)
    db.count_translation(1, day1)
    db.count_translation(1, day1)
    assert db.used_today(1, day1) == 2
    assert db.used_today(1, day2) == 0
    db.count_translation(1, day2)
    assert db.used_today(1, day2) == 1
    assert db.user_stats(999)["total"] == 0  # unknown users don't crash (the old bot did)


def test_migrates_old_schema(tmp_path):
    path = tmp_path / "users.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE users (user_id INTEGER PRIMARY KEY, username TEXT, last_used DATE, "
                "translations_today INTEGER DEFAULT 0, is_premium BOOLEAN DEFAULT 0)")
    old.execute("CREATE TABLE history (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, username TEXT, "
                "video_url TEXT, translation_mode TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)")
    old.execute("INSERT INTO users VALUES (5, 'old', '2024-12-10', 3, 1)")
    old.execute("INSERT INTO history (user_id, video_url, translation_mode) VALUES (5, 'u', 'video')")
    old.commit()
    old.close()
    db = Database(path)
    assert db.is_premium(5)
    assert db.langs(5, "auto", "ru") == ("auto", "ru")
    db.set_lang(5, "dst", "en")
    assert db.langs(5, "auto", "ru") == ("auto", "en")
    assert db.user_stats(5)["total"] == 1


# ---------------------------------------------------------------- queue


def test_queue_runs_one_at_a_time_and_cancels():
    async def scenario():
        started, release = [], asyncio.Event()

        async def runner(job):
            started.append(job.user_id)
            await release.wait()

        q = JobQueue(runner, workers=1, max_queue=5)
        q.start()
        jobs = [QueuedJob(uid, None, uid, "u", "video", "auto", "ru", "k") for uid in (1, 2, 3)]
        assert [await q.submit(j) for j in jobs] == [0, 1, 2]
        await asyncio.sleep(0.05)
        assert started == [1] and q.position(jobs[1]) == 1 and q.position(jobs[2]) == 2
        with pytest.raises(AlreadyQueued):
            await q.submit(QueuedJob(2, None, 2, "u", "audio", "auto", "ru", "k"))
        q.cancel(2)  # waiting job: dropped
        q.cancel(1)  # running job: task cancelled
        await asyncio.sleep(0.05)
        assert started == [1, 3]
        release.set()
        await asyncio.sleep(0.05)
        assert not q.running and not q.waiting
        await q.stop()

    asyncio.run(scenario())


def test_long_video_trades_audio_bitrate_for_resolution(tmp_path):
    """34-min screencast (real sizes): 64k audio only leaves room for 360p, 40k fits 480p."""
    from polyglot.config import settings
    from polyglot.pipeline import Job, Pipeline

    mib = 2**20
    p = Pipeline(Job("u", "video", "auto", "ru", tmp_path))
    p.info = {
        "duration": 2064,
        "formats": [
            _fmt("134", vcodec="avc1.4d401e", height=360, width=640, size=20.7 * mib),
            _fmt("135", vcodec="avc1.4d401f", height=480, width=854, size=35.6 * mib),
            _fmt("136", vcodec="avc1.64001f", height=720, width=1280, size=66.9 * mib),
        ],
    }
    assert settings.max_audio_kbps == 64 and settings.upload_limit == 49_000_000
    sel, kbps = p.plan_video(p._lean_options())
    assert (sel.format_id, kbps) == ("135", 40)
    # a short video keeps the best audio
    p.info["duration"] = 300
    assert p.plan_video(p._lean_options())[1] == 64


def test_single_language_video_picks_plain_https_m4a():
    """Regression: yt-dlp marks every track language_preference=-1 on single-language videos; the old check
    treated them all as dubs and fell back to an HLS stream that failed with HTTP 401."""
    info = {
        "duration": 600,
        "language": "en",
        "formats": [
            _fmt("139", acodec="mp4a.40.5", ext="m4a", abr=48, language="en", language_preference=-1),
            _fmt("140-drc", acodec="mp4a.40.2", ext="m4a", abr=129, language="en", language_preference=-1),
            _fmt("140", acodec="mp4a.40.2", ext="m4a", abr=129, language="en", language_preference=-1),
            _fmt("251", acodec="opus", ext="webm", abr=107, language="en", language_preference=-1),
            {**_fmt("233", acodec="mp4a.40.5", abr=None), "protocol": "m3u8_native", "format_note": "Default, low"},
        ],
    }
    assert media.pick_original_audio(info)["format_id"] == "140"
    assert media.pick_dub_audio(info, "ru") is None


def test_metadata_is_pruned():
    info = {"description": "x" * 10_000, "formats": [{"format_id": "1", "url": "u", "fragments": [1] * 100}],
            "automatic_captions": {"en-orig": [{"ext": "json3", "url": "https://..."}]}}
    pruned = media._prune(info)
    assert "description" not in pruned and "url" not in pruned["formats"][0]
    assert pruned["automatic_captions"] == {"en-orig": [{"ext": "json3", "name": None}]}
