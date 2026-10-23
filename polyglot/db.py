"""SQLite storage: users and their settings, daily quota, history, and a cache of sent Telegram files.

Compatible with the old users.db schema (columns are added on start)."""
from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path
from typing import Any


class Database:
    def __init__(self, path: Path | str):
        self.conn = sqlite3.connect(str(path), isolation_level=None)  # autocommit; every statement is tiny
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")  # fewer fsyncs: kinder to an SD card
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._migrate()

    def _migrate(self) -> None:
        c = self.conn
        c.execute(
            """CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                last_used DATE,
                translations_today INTEGER DEFAULT 0,
                is_premium BOOLEAN DEFAULT 0
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                video_url TEXT,
                translation_mode TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        have = {row["name"] for row in c.execute("PRAGMA table_info(users)")}
        for column, decl in (("src_lang", "TEXT"), ("dst_lang", "TEXT")):
            if column not in have:
                c.execute(f"ALTER TABLE users ADD COLUMN {column} {decl}")
        have = {row["name"] for row in c.execute("PRAGMA table_info(history)")}
        for column, decl in (("engine", "TEXT"), ("status", "TEXT"), ("seconds", "REAL")):
            if column not in have:
                c.execute(f"ALTER TABLE history ADD COLUMN {column} {decl}")
        # Jobs survive restarts and power cuts: a row lives from "queued" until the job ends
        c.execute(
            """CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                chat_id INTEGER NOT NULL,
                url TEXT NOT NULL,
                mode TEXT NOT NULL,
                src TEXT NOT NULL,
                dst TEXT NOT NULL,
                cache_key TEXT NOT NULL,
                status_message_id INTEGER,
                attempts INTEGER DEFAULT 0,
                created DATETIME DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS file_cache (
                key TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                file_id TEXT NOT NULL,
                caption TEXT,
                created DATETIME DEFAULT CURRENT_TIMESTAMP
            )"""
        )

    # ------------------------------------------------------------ users

    def upsert_user(self, user_id: int, username: str | None) -> None:
        self.conn.execute(
            "INSERT INTO users (user_id, username) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET username = excluded.username",
            (user_id, username),
        )

    def get_user(self, user_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()

    def langs(self, user_id: int, default_src: str, default_dst: str) -> tuple[str, str]:
        row = self.get_user(user_id)
        if not row:
            return default_src, default_dst
        return row["src_lang"] or default_src, row["dst_lang"] or default_dst

    def set_lang(self, user_id: int, which: str, code: str) -> None:
        column = {"src": "src_lang", "dst": "dst_lang"}[which]
        self.conn.execute(f"UPDATE users SET {column} = ? WHERE user_id = ?", (code, user_id))

    def is_premium(self, user_id: int) -> bool:
        row = self.get_user(user_id)
        return bool(row and row["is_premium"])

    def set_premium(self, user_id: int, value: bool) -> bool:
        cur = self.conn.execute("UPDATE users SET is_premium = ? WHERE user_id = ?", (int(value), user_id))
        return cur.rowcount > 0

    # ------------------------------------------------------------ quota

    def used_today(self, user_id: int, today: dt.date | None = None) -> int:
        today_s = (today or dt.date.today()).isoformat()
        row = self.get_user(user_id)
        if not row or row["last_used"] != today_s:
            return 0
        return row["translations_today"] or 0

    def count_translation(self, user_id: int, today: dt.date | None = None) -> None:
        today_s = (today or dt.date.today()).isoformat()
        self.conn.execute(
            """UPDATE users SET
                 translations_today = CASE WHEN last_used = :today THEN translations_today + 1 ELSE 1 END,
                 last_used = :today
               WHERE user_id = :id""",
            {"today": today_s, "id": user_id},
        )

    # ------------------------------------------------------------ history / stats

    def log(
        self, user_id: int, username: str | None, url: str, mode: str, engine: str, status: str, seconds: float
    ) -> None:
        self.conn.execute(
            "INSERT INTO history (user_id, username, video_url, translation_mode, engine, status, seconds) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, username, url, mode, engine, status, round(seconds, 1)),
        )

    def user_stats(self, user_id: int) -> dict[str, Any]:
        total = self.conn.execute(
            "SELECT COUNT(*) FROM history WHERE user_id = ? AND (status IS NULL OR status IN ('ok', 'cached'))",
            (user_id,),
        ).fetchone()[0]
        return {"total": total, "premium": self.is_premium(user_id), "today": self.used_today(user_id)}

    def global_stats(self) -> dict[str, Any]:
        c = self.conn
        return {
            "users": c.execute("SELECT COUNT(*) FROM users").fetchone()[0],
            "jobs": c.execute("SELECT COUNT(*) FROM history").fetchone()[0],
            "failed": c.execute("SELECT COUNT(*) FROM history WHERE status = 'error'").fetchone()[0],
            "avg_seconds": c.execute("SELECT AVG(seconds) FROM history WHERE status = 'ok'").fetchone()[0] or 0,
            "engines": c.execute(
                "SELECT engine, COUNT(*) n FROM history WHERE status = 'ok' GROUP BY engine ORDER BY n DESC LIMIT 5"
            ).fetchall(),
        }

    # ------------------------------------------------------------ persistent job queue

    def job_add(self, **fields: Any) -> int:
        cols = ", ".join(fields)
        marks = ", ".join(f":{k}" for k in fields)
        return self.conn.execute(f"INSERT INTO jobs ({cols}) VALUES ({marks})", fields).lastrowid

    def job_started(self, job_id: int, status_message_id: int | None) -> None:
        self.conn.execute(
            "UPDATE jobs SET attempts = attempts + 1, status_message_id = ? WHERE id = ?",
            (status_message_id, job_id),
        )

    def job_done(self, job_id: int) -> None:
        self.conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))

    def jobs_unfinished(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()

    # ------------------------------------------------------------ Telegram file cache

    def cache_get(self, key: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM file_cache WHERE key = ?", (key,)).fetchone()

    def cache_put(self, key: str, kind: str, file_id: str, caption: str | None) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO file_cache (key, kind, file_id, caption) VALUES (?, ?, ?, ?)",
            (key, kind, file_id, caption),
        )

    def cache_drop(self, key: str) -> None:
        self.conn.execute("DELETE FROM file_cache WHERE key = ?", (key,))
