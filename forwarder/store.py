# -*- coding: utf-8 -*-
"""SQLite 持久化：归档索引、失败收件箱、按日统计。

为什么单独用 SQLite：
  - state.json 只存去重指纹与少量会话值，不适合持续追加归档/失败记录；
  - SQLite 原子、可查询，服务重启后 /search、/failures、/retry 仍然可用；
  - 单文件 forwarder_data/forwarder.db，备份/迁移简单。

所有方法都是同步的（单次操作很小）；调用方在事件循环中直接调用即可。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any


def _day(value: str | None = None) -> str:
    return value or date.today().isoformat()


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=15)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=15000")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS archive (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    video_id TEXT DEFAULT '',
                    content_hash TEXT DEFAULT '',
                    title TEXT DEFAULT '',
                    author TEXT DEFAULT '',
                    source_url TEXT DEFAULT '',
                    raw_url TEXT DEFAULT '',
                    output_dir TEXT DEFAULT '',
                    size INTEGER DEFAULT 0,
                    file_count INTEGER DEFAULT 0,
                    live_count INTEGER DEFAULT 0,
                    tg_chat_id INTEGER,
                    tg_message_id INTEGER,
                    tg_link TEXT DEFAULT '',
                    status TEXT DEFAULT 'archived',
                    created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_archive_created ON archive(created_at);
                CREATE INDEX IF NOT EXISTS idx_archive_video ON archive(video_id);
                CREATE INDEX IF NOT EXISTS idx_archive_hash ON archive(content_hash);

                CREATE TABLE IF NOT EXISTS failures (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fp TEXT DEFAULT '',
                    url TEXT DEFAULT '',
                    raw_url TEXT DEFAULT '',
                    error TEXT DEFAULT '',
                    category TEXT DEFAULT '',
                    attempts INTEGER DEFAULT 1,
                    status TEXT DEFAULT 'open',
                    notified INTEGER DEFAULT 0,
                    tg_message_id INTEGER,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_failures_fp ON failures(fp);
                CREATE INDEX IF NOT EXISTS idx_failures_status ON failures(status);
                CREATE INDEX IF NOT EXISTS idx_failures_updated ON failures(updated_at);

                CREATE TABLE IF NOT EXISTS stats (
                    day TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(day, key)
                );
                """
            )

    # ────────────────────────── 归档索引 ──────────────────────────

    def record_archive(
        self,
        *,
        video_id: str = "",
        content_hash: str = "",
        title: str = "",
        author: str = "",
        source_url: str = "",
        raw_url: str = "",
        output_dir: str = "",
        size: int = 0,
        file_count: int = 0,
        live_count: int = 0,
        tg_chat_id: int | None = None,
        tg_message_id: int | None = None,
        tg_link: str = "",
        status: str = "archived",
    ) -> int:
        now = int(time.time())
        with self._lock, self._conn:
            cur = self._conn.execute(
                """INSERT INTO archive
                   (video_id, content_hash, title, author, source_url, raw_url, output_dir,
                    size, file_count, live_count, tg_chat_id, tg_message_id, tg_link, status, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    video_id,
                    content_hash,
                    title[:500],
                    author[:200],
                    source_url,
                    raw_url,
                    output_dir,
                    int(size),
                    int(file_count),
                    int(live_count),
                    tg_chat_id,
                    tg_message_id,
                    tg_link,
                    status,
                    now,
                ),
            )
            return int(cur.lastrowid)

    def archive_count(self, day: str | None = None) -> int:
        day = _day(day)
        start = int(datetime.fromisoformat(day).timestamp())
        end = start + 86400
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM archive WHERE created_at >= ? AND created_at < ?",
                (start, end),
            ).fetchone()
        return int(row["n"] if row else 0)

    def archive_count_range(self, days: int = 1) -> int:
        """最近 N 天（含今天）的归档数量。"""
        start_day = date.today() - timedelta(days=max(1, int(days)) - 1)
        start = int(datetime.combine(start_day, datetime.min.time()).timestamp())
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM archive WHERE created_at >= ?", (start,)
            ).fetchone()
        return int(row["n"] if row else 0)

    def recent_archive(self, limit: int = 5) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM archive ORDER BY id DESC LIMIT ?", (max(1, int(limit)),)
            ).fetchall()
        return [dict(r) for r in rows]

    def search_archive(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        q = (query or "").strip()
        if not q:
            return []
        like = f"%{q}%"
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM archive
                   WHERE title LIKE ? OR author LIKE ? OR video_id LIKE ?
                      OR source_url LIKE ? OR raw_url LIKE ?
                   ORDER BY id DESC LIMIT ?""",
                (like, like, like, like, like, max(1, int(limit))),
            ).fetchall()
        return [dict(r) for r in rows]

    def archive_by_hash(self, content_hash: str) -> dict[str, Any] | None:
        if not content_hash:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM archive WHERE content_hash = ? ORDER BY id DESC LIMIT 1",
                (content_hash,),
            ).fetchone()
        return dict(row) if row else None

    # ────────────────────────── 失败收件箱 ──────────────────────────

    def record_failure(
        self,
        *,
        fp: str,
        url: str,
        raw_url: str = "",
        error: str,
        category: str,
        attempts: int,
        status: str = "open",
    ) -> dict[str, Any]:
        """新建或更新同一个链接的未结失败记录，返回整行。"""
        now = int(time.time())
        with self._lock, self._conn:
            row = self._conn.execute(
                """SELECT * FROM failures
                   WHERE fp = ? AND status IN ('open','retrying')
                   ORDER BY id DESC LIMIT 1""",
                (fp,),
            ).fetchone()
            if row:
                self._conn.execute(
                    """UPDATE failures
                       SET error=?, category=?, attempts=?, status=?, raw_url=?,
                           notified=0, updated_at=?
                       WHERE id=?""",
                    (error[:1000], category, int(attempts), status, raw_url, now, int(row["id"])),
                )
                failure_id = int(row["id"])
            else:
                cur = self._conn.execute(
                    """INSERT INTO failures
                       (fp, url, raw_url, error, category, attempts, status, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        fp,
                        url,
                        raw_url,
                        error[:1000],
                        category,
                        int(attempts),
                        status,
                        now,
                        now,
                    ),
                )
                failure_id = int(cur.lastrowid)
            out = self._conn.execute("SELECT * FROM failures WHERE id = ?", (failure_id,)).fetchone()
        return dict(out) if out else {}

    def get_failure(self, failure_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM failures WHERE id = ?", (int(failure_id),)
            ).fetchone()
        return dict(row) if row else None

    def set_failure_status(
        self, failure_id: int, status: str, *, notified: bool | None = None
    ) -> None:
        fields = ["status = ?", "updated_at = ?"]
        values: list[Any] = [status, int(time.time())]
        if notified is not None:
            fields.append("notified = ?")
            values.append(1 if notified else 0)
        values.append(int(failure_id))
        with self._lock, self._conn:
            self._conn.execute(f"UPDATE failures SET {', '.join(fields)} WHERE id = ?", values)

    def mark_notified(self, failure_id: int, message_id: int | None = None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE failures SET notified = 1, tg_message_id = ?, updated_at = ? WHERE id = ?",
                (message_id, int(time.time()), int(failure_id)),
            )

    def resolve_failures(self, fp: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE failures SET status='resolved', updated_at=? WHERE fp=? AND status IN ('open','retrying')",
                (int(time.time()), fp),
            )

    def list_failures(self, status: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            if status:
                rows = self._conn.execute(
                    "SELECT * FROM failures WHERE status = ? ORDER BY id DESC LIMIT ?",
                    (status, max(1, int(limit))),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM failures ORDER BY id DESC LIMIT ?", (max(1, int(limit)),)
                ).fetchall()
        return [dict(r) for r in rows]

    def failure_categories(self, days: int = 1) -> list[dict[str, Any]]:
        """按分类统计最近 N 天失败数量，供摘要使用。"""
        start_day = date.today() - timedelta(days=max(1, int(days)) - 1)
        start = int(datetime.combine(start_day, datetime.min.time()).timestamp())
        with self._lock:
            rows = self._conn.execute(
                """SELECT category, COUNT(*) AS n FROM failures
                   WHERE created_at >= ? GROUP BY category ORDER BY n DESC LIMIT 6""",
                (start,),
            ).fetchall()
        return [dict(r) for r in rows]

    def open_failure_count(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM failures WHERE status IN ('open','retrying')"
            ).fetchone()
        return int(row["n"] if row else 0)

    # ────────────────────────── 统计 ──────────────────────────

    def bump_stat(self, key: str, amount: int = 1, day: str | None = None) -> None:
        day = _day(day)
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO stats(day, key, value) VALUES (?,?,?)
                   ON CONFLICT(day, key) DO UPDATE SET value = value + excluded.value""",
                (day, key, int(amount)),
            )

    def get_stats(self, day: str | None = None) -> dict[str, int]:
        day = _day(day)
        with self._lock:
            rows = self._conn.execute("SELECT key, value FROM stats WHERE day = ?", (day,)).fetchall()
        return {str(r["key"]): int(r["value"]) for r in rows}

    def stats_range(self, days: int = 7) -> dict[str, int]:
        """汇总最近 N 天（含今天）的统计。"""
        start = (date.today() - timedelta(days=max(1, days) - 1)).isoformat()
        with self._lock:
            rows = self._conn.execute(
                "SELECT key, SUM(value) AS value FROM stats WHERE day >= ? GROUP BY key", (start,)
            ).fetchall()
        return {str(r["key"]): int(r["value"] or 0) for r in rows}

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass
