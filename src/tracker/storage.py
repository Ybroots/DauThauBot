from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .config import PROJECT_ROOT

_dd = os.environ.get("DATA_DIR", "").strip()
DATA_ROOT = Path(_dd) if _dd else (PROJECT_ROOT / "data")
DB_PATH = DATA_ROOT / "seen.db"


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS seen_bids (
                tbmt_code TEXT PRIMARY KEY,
                title TEXT,
                seen_at TEXT NOT NULL,
                sent_to_telegram INTEGER DEFAULT 0
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_seen_at ON seen_bids(seen_at)")


def is_seen(tbmt_code: str) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute("SELECT 1 FROM seen_bids WHERE tbmt_code = ?", (tbmt_code,))
        return cur.fetchone() is not None


def was_sent(tbmt_code: str) -> bool:
    """Đã gửi Telegram thành công — không xử lý lại."""
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "SELECT sent_to_telegram FROM seen_bids WHERE tbmt_code = ?",
            (tbmt_code,),
        )
        row = cur.fetchone()
        return row is not None and row[0] == 1


def mark_seen(tbmt_code: str, title: str, sent: bool = True) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO seen_bids(tbmt_code, title, seen_at, sent_to_telegram)
            VALUES (?, ?, ?, ?)
            """,
            (tbmt_code, title, datetime.now(timezone.utc).isoformat(), 1 if sent else 0),
        )


def count_sent_since(days: int = 7) -> int:
    from datetime import timedelta

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            """
            SELECT COUNT(*) FROM seen_bids
            WHERE sent_to_telegram = 1 AND seen_at >= ?
            """,
            (cutoff,),
        )
        row = cur.fetchone()
        return int(row[0]) if row else 0


def count_sent_since_hours(hours: int) -> int:
    from datetime import timedelta

    if hours <= 0:
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            """
            SELECT COUNT(*) FROM seen_bids
            WHERE sent_to_telegram = 1 AND seen_at >= ?
            """,
            (cutoff,),
        )
        row = cur.fetchone()
        return int(row[0]) if row else 0


def total_bids_in_db() -> int:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT COUNT(*) FROM seen_bids").fetchone()
        return int(row[0]) if row else 0


def count_unsent_in_db() -> int:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM seen_bids WHERE sent_to_telegram = 0"
        ).fetchone()
        return int(row[0]) if row else 0


def list_recent_bids(limit: int = 10) -> list[tuple[str, str, str, int]]:
    """tbmt_code, title, seen_at (ISO), sent_to_telegram — mới nhất trước."""
    limit = max(1, min(int(limit), 50))
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            """
            SELECT tbmt_code, title, seen_at, sent_to_telegram
            FROM seen_bids
            ORDER BY seen_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = cur.fetchall()
    out: list[tuple[str, str, str, int]] = []
    for r in rows:
        out.append((str(r[0]), str(r[1] or ""), str(r[2]), int(r[3])))
    return out


def list_unsent() -> list[tuple[str, str]]:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "SELECT tbmt_code, title FROM seen_bids WHERE sent_to_telegram = 0"
        )
        return list(cur.fetchall())
