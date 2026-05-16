from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .config import PROJECT_ROOT, KeywordGroup, KeywordsConfig

_dd = os.environ.get("DATA_DIR", "").strip()
DATA_ROOT = Path(_dd) if _dd else (PROJECT_ROOT / "data")
DB_PATH = DATA_ROOT / "seen.db"


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS keyword_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                require TEXT NOT NULL DEFAULT 'all',
                active INTEGER DEFAULT 1,
                created_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS keywords (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id INTEGER NOT NULL REFERENCES keyword_groups(id) ON DELETE CASCADE,
                keyword TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now')),
                UNIQUE(group_id, keyword)
            )
            """
        )


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


def load_groups_from_db() -> KeywordsConfig:
    """Load all active keyword groups from DB — used at runtime."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        groups_rows = conn.execute(
            "SELECT id, name, require FROM keyword_groups WHERE active = 1"
        ).fetchall()
        result: list[KeywordGroup] = []
        for gid, name, require in groups_rows:
            kw_rows = conn.execute(
                "SELECT keyword FROM keywords WHERE group_id = ?", (gid,)
            ).fetchall()
            result.append(KeywordGroup(name=name, require=require, keywords=[r[0] for r in kw_rows]))
    return KeywordsConfig(groups=result)


def seed_groups_from_yaml(cfg: KeywordsConfig) -> None:
    """Insert groups from YAML only if DB has no groups (idempotent seed)."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        count = conn.execute("SELECT COUNT(*) FROM keyword_groups").fetchone()[0]
        if count > 0:
            return
        for group in cfg.groups:
            cur = conn.execute(
                "INSERT OR IGNORE INTO keyword_groups(name, require) VALUES (?, ?)",
                (group.name, group.require),
            )
            gid = cur.lastrowid
            if gid:
                for kw in group.keywords:
                    kw = kw.strip()
                    if kw:
                        conn.execute(
                            "INSERT OR IGNORE INTO keywords(group_id, keyword) VALUES (?, ?)",
                            (gid, kw),
                        )


def add_group(name: str, require: str, keywords: list[str]) -> bool:
    """Create a new keyword group. Returns False if name already exists."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            cur = conn.execute(
                "INSERT INTO keyword_groups(name, require) VALUES (?, ?)",
                (name, require),
            )
            gid = cur.lastrowid
            for kw in keywords:
                kw = kw.strip()
                if kw:
                    conn.execute(
                        "INSERT OR IGNORE INTO keywords(group_id, keyword) VALUES (?, ?)",
                        (gid, kw),
                    )
        return True
    except sqlite3.IntegrityError:
        return False


def remove_group(name: str) -> bool:
    """Delete a group and all its keywords. Returns False if not found."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        cur = conn.execute("DELETE FROM keyword_groups WHERE name = ?", (name,))
        return cur.rowcount > 0


def add_keyword_to_group(group_name: str, keyword: str) -> bool:
    """Add a keyword to an existing group. Returns False if group not found or keyword duplicate."""
    keyword = keyword.strip()
    if not keyword:
        return False
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        row = conn.execute(
            "SELECT id FROM keyword_groups WHERE name = ?", (group_name,)
        ).fetchone()
        if not row:
            return False
        try:
            conn.execute(
                "INSERT INTO keywords(group_id, keyword) VALUES (?, ?)", (row[0], keyword)
            )
            return True
        except sqlite3.IntegrityError:
            return False


def remove_keyword_from_group(group_name: str, keyword: str) -> bool:
    """Remove a keyword from a group. Returns False if group or keyword not found."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        row = conn.execute(
            "SELECT id FROM keyword_groups WHERE name = ?", (group_name,)
        ).fetchone()
        if not row:
            return False
        cur = conn.execute(
            "DELETE FROM keywords WHERE group_id = ? AND keyword = ?", (row[0], keyword)
        )
        return cur.rowcount > 0


# ── Các hàm bổ sung ────────────────────────────────────────────────────────


def lookup_bid_in_db(tbmt_code: str) -> tuple[str, str, int] | None:
    """Tra mã TBMT trong seen.db. Trả về (title, seen_at, sent_to_telegram) hoặc None."""
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT title, seen_at, sent_to_telegram FROM seen_bids WHERE tbmt_code = ?",
            (tbmt_code.strip(),),
        ).fetchone()
        if row is None:
            return None
        return (str(row[0] or ""), str(row[1]), int(row[2]))


def toggle_group_active(name: str, active: bool) -> bool:
    """Tắt/bật group theo tên. Trả về False nếu không tìm thấy."""
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "UPDATE keyword_groups SET active = ? WHERE name = ?",
            (1 if active else 0, name),
        )
        return cur.rowcount > 0


def rename_group(old_name: str, new_name: str) -> bool:
    """Đổi tên group. Trả về False nếu không tìm thấy hoặc tên mới đã tồn tại."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.execute(
                "UPDATE keyword_groups SET name = ? WHERE name = ?",
                (new_name.strip(), old_name.strip()),
            )
            return cur.rowcount > 0
    except sqlite3.IntegrityError:
        return False


def remove_bid_from_db(tbmt_code: str) -> bool:
    """Xóa bid khỏi seen.db để cron gửi lại. Trả về False nếu không tìm thấy."""
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "DELETE FROM seen_bids WHERE tbmt_code = ?", (tbmt_code.strip(),)
        )
        return cur.rowcount > 0


def list_bids_since_hours(hours: int = 24) -> list[tuple[str, str, str, int]]:
    """Gói đã thấy trong N giờ vừa qua (mới nhất trước, tối đa 50)."""
    from datetime import timedelta

    hours = max(1, min(hours, 720))
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            """
            SELECT tbmt_code, title, seen_at, sent_to_telegram
            FROM seen_bids
            WHERE seen_at >= ?
            ORDER BY seen_at DESC
            LIMIT 50
            """,
            (cutoff,),
        )
        return [(str(r[0]), str(r[1] or ""), str(r[2]), int(r[3])) for r in cur.fetchall()]


def list_all_groups_raw() -> list[tuple[str, str, int, list[str]]]:
    """Tất cả groups kể cả inactive — [(name, require, active, [keywords])]."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        rows = conn.execute(
            "SELECT id, name, require, active FROM keyword_groups ORDER BY active DESC, name"
        ).fetchall()
        result: list[tuple[str, str, int, list[str]]] = []
        for gid, name, require, active in rows:
            kws = [r[0] for r in conn.execute(
                "SELECT keyword FROM keywords WHERE group_id = ?", (gid,)
            ).fetchall()]
            result.append((str(name), str(require), int(active), kws))
        return result
