"""Tender catalog + crawl audit log.

Architecture:
  seen_bids   → tracks what was SENT to Telegram (dedup for cron, không bị thay đổi)
  tenders     → full catalog of ALL crawled bids — searchable by /tim without Playwright
  crawl_logs  → audit trail of every cron / interactive crawl run

Khi /tim được gọi:
  1. search_tenders() → instant (< 10ms, no Playwright)
  2. Nếu có kết quả → trả ngay với tag "⚡ từ kho dữ liệu"
  3. Nếu không có → fallback sang Playwright live crawl (hiện có)
  4. Live crawl kết quả cũng được lưu vào tenders → lần sau nhanh hơn
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from loguru import logger

from .storage import DB_PATH

# ── Province extraction ───────────────────────────────────────────────────────

_PROVINCE_RE = re.compile(
    r'(?:Tỉnh|TP\.|T\.P|Thành\s+phố)\s+([^;,\n\-]+)',
    re.IGNORECASE,
)


def extract_province(location: str) -> str:
    """Trích tên tỉnh/thành từ location string.

    "Phường Mạo Khê - Tỉnh Quảng Ninh;" → "Quảng Ninh"
    "P. Long An - Tây Ninh;"             → "Tây Ninh"
    """
    if not location:
        return ""
    m = _PROVINCE_RE.search(location)
    if m:
        return m.group(1).strip().rstrip(";").strip()
    # fallback: phần cuối sau dấu " - "
    parts = [p.strip().rstrip(";").strip() for p in location.split("-")]
    if len(parts) >= 2:
        candidate = parts[-1].strip()
        # Loại "Phường X" / "Quận X" / "Huyện X" — không phải tỉnh
        if candidate and not re.match(r'^(Phường|Quận|Huyện|Xã|Thị\s+trấn)\s', candidate, re.IGNORECASE):
            return candidate
    return ""


def _norm(text: str) -> str:
    """Normalize text for SQLite search (remove diacritics, lowercase)."""
    if not text:
        return ""
    try:
        from .filter import normalize
        return normalize(text).lower()
    except Exception:
        return text.lower()


# ── DDL ───────────────────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS tenders (
    tbmt_code        TEXT PRIMARY KEY,
    title            TEXT,
    status           TEXT,
    investor         TEXT,
    procuring_entity TEXT,
    published_at     TEXT,
    closed_at        TEXT,
    field_label      TEXT,
    location         TEXT,
    province         TEXT,
    bid_method       TEXT,
    bid_form         TEXT,
    bid_mode         TEXT,
    budget_vnd       INTEGER,
    detail_url       TEXT,
    keywords_matched TEXT,
    search_text      TEXT,
    raw_json         TEXT,
    first_seen_at    TEXT NOT NULL,
    last_updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tenders_closed_at  ON tenders(closed_at);
CREATE INDEX IF NOT EXISTS idx_tenders_province   ON tenders(province);
CREATE INDEX IF NOT EXISTS idx_tenders_updated    ON tenders(last_updated_at);
CREATE INDEX IF NOT EXISTS idx_tenders_search     ON tenders(search_text);

CREATE TABLE IF NOT EXISTS crawl_logs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    job_type      TEXT    NOT NULL DEFAULT 'cron',
    started_at    TEXT    NOT NULL,
    finished_at   TEXT,
    status        TEXT    NOT NULL DEFAULT 'running',
    total_found   INTEGER NOT NULL DEFAULT 0,
    total_new     INTEGER NOT NULL DEFAULT 0,
    total_updated INTEGER NOT NULL DEFAULT 0,
    total_sent    INTEGER NOT NULL DEFAULT 0,
    total_failed  INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    duration_ms   INTEGER,
    keywords      TEXT
);
"""


def init_tender_tables() -> None:
    """Create tenders + crawl_logs if not exist (idempotent)."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(_DDL)


# ── Upsert ────────────────────────────────────────────────────────────────────

def upsert_tender(
    bid: Any,
    *,
    keywords_matched: list[str] | None = None,
) -> bool:
    """Insert or update a bid in the tenders catalog.

    Returns True if the row is NEW, False if it was UPDATED.
    Never raises — logs exception and returns False on error.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    raw: dict = getattr(bid, "raw", None) or {}

    # Build denormalized search_text for LIKE queries
    procuring = (raw.get("procuringEntityName") or "").strip()
    search_parts = [
        bid.title or "",
        bid.investor or "",
        bid.location or "",
        getattr(bid, "field", "") or "",
        procuring,
    ]
    search_text = _norm(" ".join(p for p in search_parts if p))

    province = extract_province(bid.location or "")
    kw_str = ",".join(keywords_matched) if keywords_matched else None

    try:
        with sqlite3.connect(DB_PATH) as conn:
            existing = conn.execute(
                "SELECT first_seen_at FROM tenders WHERE tbmt_code = ?",
                (bid.tbmt_code,),
            ).fetchone()

            closed_iso: str | None = None
            published_iso: str | None = None
            try:
                closed_iso = bid.closing_at.isoformat() if bid.closing_at else None
            except AttributeError:
                closed_iso = str(bid.closing_at) if bid.closing_at else None
            try:
                published_iso = bid.posted_at.isoformat() if bid.posted_at else None
            except AttributeError:
                published_iso = str(bid.posted_at) if bid.posted_at else None

            raw_json_str = None
            if raw:
                try:
                    raw_json_str = json.dumps(raw, ensure_ascii=False)
                except Exception:
                    pass

            values_common = (
                bid.title,
                bid.status,
                bid.investor,
                procuring or None,
                published_iso,
                closed_iso,
                getattr(bid, "field", None),
                bid.location,
                province or None,
                bid.bid_method,
                raw.get("bidForm") or None,
                raw.get("bidMode") or None,
                bid.budget_vnd,
                bid.detail_url,
                kw_str,
                search_text,
                raw_json_str,
            )

            if existing:
                conn.execute(
                    """UPDATE tenders SET
                        title=?, status=?, investor=?, procuring_entity=?,
                        published_at=?, closed_at=?, field_label=?, location=?,
                        province=?, bid_method=?, bid_form=?, bid_mode=?,
                        budget_vnd=?, detail_url=?, keywords_matched=?,
                        search_text=?, raw_json=?, last_updated_at=?
                    WHERE tbmt_code=?""",
                    (*values_common, now_iso, bid.tbmt_code),
                )
                return False
            else:
                conn.execute(
                    """INSERT INTO tenders (
                        tbmt_code,
                        title, status, investor, procuring_entity,
                        published_at, closed_at, field_label, location,
                        province, bid_method, bid_form, bid_mode,
                        budget_vnd, detail_url, keywords_matched,
                        search_text, raw_json,
                        first_seen_at, last_updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (bid.tbmt_code, *values_common, now_iso, now_iso),
                )
                return True
    except Exception:
        logger.exception("upsert_tender failed for {}", getattr(bid, "tbmt_code", "?"))
        return False


def upsert_tenders_bulk(
    bids: list[Any],
    keywords_matched_map: dict[str, list[str]] | None = None,
) -> tuple[int, int]:
    """Bulk upsert list of bids. Returns (new_count, updated_count)."""
    new_c = updated_c = 0
    for bid in bids:
        kws = (keywords_matched_map or {}).get(getattr(bid, "tbmt_code", ""))
        if upsert_tender(bid, keywords_matched=kws):
            new_c += 1
        else:
            updated_c += 1
    return new_c, updated_c


# ── Search ────────────────────────────────────────────────────────────────────

def _parse_iso_safe(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _tender_row_to_bid(row: dict[str, Any]) -> Any:
    """Convert a tenders DB row back to a Bid object for formatting."""
    from .models import Bid

    now = datetime.now(timezone.utc)
    raw: dict | None = None
    if row.get("raw_json"):
        try:
            raw = json.loads(row["raw_json"])
        except Exception:
            pass

    return Bid(
        tbmt_code=row["tbmt_code"],
        title=row.get("title") or "",
        status=row.get("status") or "Chưa cập nhật",
        investor=row.get("investor") or "",
        posted_at=_parse_iso_safe(row.get("published_at")) or now,
        field=row.get("field_label") or "",
        location=row.get("location") or "",
        closing_at=_parse_iso_safe(row.get("closed_at")) or now,
        bid_method=row.get("bid_method") or "",
        detail_url=row.get("detail_url") or "",
        budget_vnd=row.get("budget_vnd"),
        raw=raw,
    )


def search_tenders(
    phrases: list[str],
    *,
    mode: str = "any",
    open_only: bool = True,
    limit: int = 15,
) -> list[Any]:
    """Search tenders catalog and return list of Bid objects.

    Dùng normalized LIKE search — không cần Playwright, instant.
    mode="any": ít nhất 1 phrase khớp (OR).
    mode="all": tất cả phrase phải khớp (AND).
    """
    if not phrases:
        return []

    norm_phrases = [_norm(p) for p in phrases if p.strip()]
    if not norm_phrases:
        return []

    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row

            params: list[Any] = []
            phrase_conds = []
            for p in norm_phrases:
                phrase_conds.append("search_text LIKE ?")
                params.append(f"%{p}%")

            text_clause = (
                "(" + " AND ".join(phrase_conds) + ")"
                if mode == "all"
                else "(" + " OR ".join(phrase_conds) + ")"
            )

            extra_conds = []
            if open_only:
                extra_conds.append(
                    "(closed_at IS NULL OR closed_at >= ?)"
                )
                params.append(datetime.now(timezone.utc).isoformat())

            where_parts = [text_clause] + extra_conds
            where = " AND ".join(where_parts)

            sql = f"""
                SELECT * FROM tenders
                WHERE {where}
                ORDER BY last_updated_at DESC
                LIMIT ?
            """
            params.append(limit)

            rows = conn.execute(sql, params).fetchall()
            return [_tender_row_to_bid(dict(r)) for r in rows]
    except Exception:
        logger.exception("search_tenders failed for phrases={}", phrases)
        return []


def get_last_crawl_time() -> datetime | None:
    """Thời điểm cập nhật mới nhất trong catalog tenders."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute(
                "SELECT MAX(last_updated_at) FROM tenders"
            ).fetchone()
            if row and row[0]:
                return _parse_iso_safe(row[0])
    except Exception:
        pass
    return None


def count_tenders(*, open_only: bool = False) -> int:
    """Số lượng gói trong catalog (open_only=True → chỉ đếm chưa đóng)."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            if open_only:
                row = conn.execute(
                    "SELECT COUNT(*) FROM tenders WHERE closed_at IS NULL OR closed_at >= ?",
                    (datetime.now(timezone.utc).isoformat(),),
                ).fetchone()
            else:
                row = conn.execute("SELECT COUNT(*) FROM tenders").fetchone()
            return int(row[0]) if row else 0
    except Exception:
        return 0


# ── Crawl logs ────────────────────────────────────────────────────────────────

def log_crawl_start(
    job_type: str = "cron",
    keywords: list[str] | None = None,
) -> int:
    """Tạo crawl log entry. Trả về log_id (0 nếu lỗi)."""
    now_iso = datetime.now(timezone.utc).isoformat()
    kw_str = ",".join(keywords) if keywords else None
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.execute(
                """INSERT INTO crawl_logs (job_type, started_at, status, keywords)
                   VALUES (?, ?, 'running', ?)""",
                (job_type, now_iso, kw_str),
            )
            return cur.lastrowid or 0
    except Exception:
        logger.exception("log_crawl_start failed")
        return 0


def log_crawl_finish(
    log_id: int,
    *,
    status: str = "success",
    total_found: int = 0,
    total_new: int = 0,
    total_updated: int = 0,
    total_sent: int = 0,
    total_failed: int = 0,
    error_message: str | None = None,
) -> None:
    """Hoàn tất crawl log entry với stats và thời gian."""
    if not log_id:
        return
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute(
                "SELECT started_at FROM crawl_logs WHERE id = ?", (log_id,)
            ).fetchone()
            duration_ms: int | None = None
            if row and row[0]:
                try:
                    start_dt = _parse_iso_safe(row[0])
                    if start_dt:
                        duration_ms = int(
                            (datetime.now(timezone.utc) - start_dt).total_seconds() * 1000
                        )
                except Exception:
                    pass
            conn.execute(
                """UPDATE crawl_logs SET
                    finished_at=?, status=?,
                    total_found=?, total_new=?, total_updated=?,
                    total_sent=?, total_failed=?,
                    error_message=?, duration_ms=?
                WHERE id=?""",
                (
                    now_iso, status,
                    total_found, total_new, total_updated,
                    total_sent, total_failed,
                    error_message, duration_ms,
                    log_id,
                ),
            )
    except Exception:
        logger.exception("log_crawl_finish failed for log_id={}", log_id)


def list_crawl_logs(limit: int = 10) -> list[dict[str, Any]]:
    """N bản ghi crawl gần nhất."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM crawl_logs ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []
