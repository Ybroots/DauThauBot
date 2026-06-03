"""Tests cho tender_store.py — tenders catalog + crawl_logs."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tracker import storage, tender_store
from tracker.models import Bid


@pytest.fixture()
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = tmp_path / "seen.db"
    monkeypatch.setattr(storage, "DB_PATH", db)
    monkeypatch.setattr(tender_store, "DB_PATH", db)
    storage.init_db()
    yield db


def _mk_bid(code: str, title: str, *, investor: str = "CĐT", location: str = "") -> Bid:
    now = datetime(2026, 5, 23, tzinfo=timezone.utc)
    return Bid(
        tbmt_code=code,
        title=title,
        status="Chưa đóng thầu",
        investor=investor,
        posted_at=now,
        field="Hàng hóa",
        location=location,
        closing_at=datetime(2026, 12, 31, tzinfo=timezone.utc),
        bid_method="Qua mạng",
        detail_url="https://x",
        budget_vnd=500_000_000,
        raw={"bidForm": "DTRR", "bidMode": "1_MTHS", "procuringEntityName": "BMT Test"},
    )


# ── Province extraction ───────────────────────────────────────────────────────

def test_extract_province_tinh():
    assert tender_store.extract_province("Phường Mạo Khê - Tỉnh Quảng Ninh;") == "Quảng Ninh"


def test_extract_province_tp():
    assert tender_store.extract_province("Quận Hoàn Kiếm - TP. Hà Nội;") == "Hà Nội"


def test_extract_province_empty():
    assert tender_store.extract_province("") == ""
    assert tender_store.extract_province("Không rõ") == ""


# ── Init tables ───────────────────────────────────────────────────────────────

def test_init_tender_tables_idempotent(temp_db: Path):
    # Should not raise when called multiple times
    tender_store.init_tender_tables()
    tender_store.init_tender_tables()
    import sqlite3
    with sqlite3.connect(temp_db) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "tenders" in tables
    assert "crawl_logs" in tables


# ── Upsert ────────────────────────────────────────────────────────────────────

def test_upsert_tender_new(temp_db: Path):
    bid = _mk_bid("IB001-00", "Camera Lâm Đồng", location="- Tỉnh Lâm Đồng;")
    is_new = tender_store.upsert_tender(bid, keywords_matched=["camera", "lâm đồng"])
    assert is_new is True


def test_upsert_tender_update(temp_db: Path):
    bid = _mk_bid("IB002-00", "Camera")
    tender_store.upsert_tender(bid)
    is_new = tender_store.upsert_tender(bid)  # second call → update
    assert is_new is False


def test_upsert_tender_persists_province(temp_db: Path):
    bid = _mk_bid("IB003-00", "Camera", location="Phường X - Tỉnh Lâm Đồng;")
    tender_store.upsert_tender(bid)
    import sqlite3
    with sqlite3.connect(temp_db) as conn:
        row = conn.execute("SELECT province FROM tenders WHERE tbmt_code = 'IB003-00'").fetchone()
    assert row is not None
    assert row[0] == "Lâm Đồng"


def test_upsert_tenders_bulk(temp_db: Path):
    bids = [_mk_bid(f"IB{i:03d}-00", f"Gói {i}") for i in range(5)]
    new_c, upd_c = tender_store.upsert_tenders_bulk(bids)
    assert new_c == 5
    assert upd_c == 0
    # Second run → all updates
    new_c2, upd_c2 = tender_store.upsert_tenders_bulk(bids)
    assert new_c2 == 0
    assert upd_c2 == 5


def test_upsert_tender_tolerates_none_raw(temp_db: Path):
    bid = _mk_bid("NRAW-00", "Test")
    bid.raw = None
    is_new = tender_store.upsert_tender(bid)
    assert is_new is True


# ── Search ────────────────────────────────────────────────────────────────────

def test_search_tenders_any_mode(temp_db: Path):
    bids = [
        _mk_bid("CAM-00", "Camera Lâm Đồng"),
        _mk_bid("CCT-00", "Camera công an"),
        _mk_bid("XL-00", "Xây lắp cầu đường"),
    ]
    tender_store.upsert_tenders_bulk(bids)
    results = tender_store.search_tenders(["camera"], mode="any", open_only=False)
    codes = [b.tbmt_code for b in results]
    assert "CAM-00" in codes
    assert "CCT-00" in codes
    assert "XL-00" not in codes


def test_search_tenders_all_mode(temp_db: Path):
    bids = [
        _mk_bid("CAM-LD", "Camera Lâm Đồng", location="- Tỉnh Lâm Đồng;"),
        _mk_bid("CAM-HN", "Camera Hà Nội"),
    ]
    tender_store.upsert_tenders_bulk(bids)
    results = tender_store.search_tenders(["camera", "lam dong"], mode="all", open_only=False)
    codes = [b.tbmt_code for b in results]
    assert "CAM-LD" in codes
    assert "CAM-HN" not in codes


def test_search_tenders_empty_phrases(temp_db: Path):
    assert tender_store.search_tenders([]) == []
    assert tender_store.search_tenders(["", "  "]) == []


def test_search_tenders_returns_bid_objects(temp_db: Path):
    from tracker.models import Bid
    bid = _mk_bid("RET-00", "Camera test")
    tender_store.upsert_tender(bid)
    results = tender_store.search_tenders(["camera"], open_only=False)
    assert len(results) == 1
    assert isinstance(results[0], Bid)
    assert results[0].tbmt_code == "RET-00"


def test_search_tenders_limit_respected(temp_db: Path):
    bids = [_mk_bid(f"CAM{i:03d}-00", f"Camera {i}") for i in range(10)]
    tender_store.upsert_tenders_bulk(bids)
    results = tender_store.search_tenders(["camera"], limit=3, open_only=False)
    assert len(results) <= 3


# ── Helpers ───────────────────────────────────────────────────────────────────

def test_count_tenders(temp_db: Path):
    assert tender_store.count_tenders() == 0
    bids = [_mk_bid(f"B{i}", f"Bid {i}") for i in range(3)]
    tender_store.upsert_tenders_bulk(bids)
    assert tender_store.count_tenders() == 3


def test_get_last_crawl_time_none_when_empty(temp_db: Path):
    assert tender_store.get_last_crawl_time() is None


def test_get_last_crawl_time_after_upsert(temp_db: Path):
    tender_store.upsert_tender(_mk_bid("T-1", "Test"))
    last = tender_store.get_last_crawl_time()
    assert last is not None
    assert isinstance(last, datetime)


# ── Crawl logs ────────────────────────────────────────────────────────────────

def test_log_crawl_start_and_finish(temp_db: Path):
    log_id = tender_store.log_crawl_start("cron", keywords=["camera", "lâm đồng"])
    assert log_id > 0
    tender_store.log_crawl_finish(
        log_id,
        status="success",
        total_found=28,
        total_new=5,
        total_updated=23,
        total_sent=3,
    )
    logs = tender_store.list_crawl_logs(1)
    assert len(logs) == 1
    lg = logs[0]
    assert lg["status"] == "success"
    assert lg["total_found"] == 28
    assert lg["total_new"] == 5
    assert lg["total_sent"] == 3
    assert lg["duration_ms"] is not None
    assert lg["duration_ms"] >= 0


def test_log_crawl_finish_with_error(temp_db: Path):
    log_id = tender_store.log_crawl_start("interactive")
    tender_store.log_crawl_finish(
        log_id,
        status="failed",
        error_message="Connection reset",
    )
    logs = tender_store.list_crawl_logs(1)
    assert logs[0]["status"] == "failed"
    assert logs[0]["error_message"] == "Connection reset"


def test_log_crawl_finish_zero_id_is_safe(temp_db: Path):
    # Should not raise
    tender_store.log_crawl_finish(0, status="success")


def test_list_crawl_logs_ordering(temp_db: Path):
    import time
    for i in range(3):
        lid = tender_store.log_crawl_start("cron")
        tender_store.log_crawl_finish(lid, status="success", total_found=i)
        time.sleep(0.01)
    logs = tender_store.list_crawl_logs(3)
    # Most recent first
    assert logs[0]["total_found"] == 2
    assert logs[2]["total_found"] == 0
