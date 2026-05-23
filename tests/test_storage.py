import sqlite3
import time
from pathlib import Path

import pytest

from tracker import storage


@pytest.fixture()
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = tmp_path / "seen.db"
    monkeypatch.setattr(storage, "DB_PATH", db)
    storage.init_db()
    yield db


def test_is_seen_and_mark_seen(temp_db: Path):
    assert storage.is_seen("IB001-00") is False
    storage.mark_seen("IB001-00", "Gói A", sent=True)
    assert storage.is_seen("IB001-00") is True

    storage.mark_seen("IB002-00", "Gói B", sent=False)
    with sqlite3.connect(temp_db) as conn:
        row = conn.execute(
            "SELECT sent_to_telegram FROM seen_bids WHERE tbmt_code = ?",
            ("IB002-00",),
        ).fetchone()
    assert row[0] == 0


def test_was_sent(temp_db: Path):
    storage.mark_seen("IB004-00", "Gói D", sent=False)
    assert storage.was_sent("IB004-00") is False
    storage.mark_seen("IB004-00", "Gói D", sent=True)
    assert storage.was_sent("IB004-00") is True


def test_count_sent_since_hours(temp_db: Path):
    storage.mark_seen("H-1", "Gói", sent=True)
    assert storage.count_sent_since_hours(24) >= 1
    assert storage.count_sent_since_hours(1) >= 1


def test_list_recent_bids_order(temp_db: Path):
    storage.mark_seen("X-1", "First", sent=True)
    time.sleep(0.05)
    storage.mark_seen("X-2", "Second", sent=True)
    rows = storage.list_recent_bids(5)
    assert len(rows) == 2
    assert rows[0][0] == "X-2"
    assert rows[0][3] == 1


def test_total_unsent_list_recent_cap(temp_db: Path):
    storage.mark_seen("U-1", "a", sent=False)
    storage.mark_seen("U-2", "b", sent=True)
    assert storage.total_bids_in_db() == 2
    assert storage.count_unsent_in_db() == 1
    assert len(storage.list_recent_bids(1)) == 1


def test_no_duplicate_primary_key(temp_db: Path):
    storage.mark_seen("IB003-00", "Lần 1", sent=True)
    storage.mark_seen("IB003-00", "Lần 2", sent=True)
    with sqlite3.connect(temp_db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM seen_bids").fetchone()[0]
    assert count == 1


def test_migration_adds_extra_columns(temp_db: Path):
    """init_db phải có đủ 10 cột (4 base + 6 extras) sau migration."""
    with sqlite3.connect(temp_db) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(seen_bids)").fetchall()}
    expected = {
        "tbmt_code", "title", "seen_at", "sent_to_telegram",
        "budget_vnd", "closing_at", "investor", "bid_form", "bid_mode", "location",
    }
    assert expected <= cols, f"Thiếu cột: {expected - cols}"


def test_migration_idempotent_on_legacy_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Mô phỏng DB cũ 4-cột → init_db nâng cấp mà không mất dữ liệu."""
    db = tmp_path / "legacy.db"
    monkeypatch.setattr(storage, "DB_PATH", db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE seen_bids (tbmt_code TEXT PRIMARY KEY, title TEXT, "
            "seen_at TEXT NOT NULL, sent_to_telegram INTEGER DEFAULT 0)"
        )
        conn.execute(
            "INSERT INTO seen_bids VALUES ('LEGACY-1', 'cũ', '2025-01-01T00:00:00Z', 1)"
        )

    storage.init_db()
    storage.init_db()  # gọi 2 lần — phải idempotent

    row = storage.lookup_bid_in_db("LEGACY-1")
    assert row is not None
    title, _, sent, extras = row
    assert title == "cũ"
    assert sent == 1
    # extras phải là None hết cho dòng cũ
    assert all(v is None for v in extras.values())


def test_mark_seen_persists_extras_from_bid():
    from datetime import datetime, timezone
    from tracker.models import Bid

    # Dùng DB tạm trong cwd vì fixture đã không scope
    # → tạo riêng để khớp pytest collection
    bid = Bid(
        tbmt_code="EX-1",
        title="Camera Lâm Đồng",
        status="Chưa đóng thầu",
        investor="UBND Lâm Đồng",
        posted_at=datetime(2025, 12, 10, tzinfo=timezone.utc),
        field="Hàng hóa",
        location="Đà Lạt - Lâm Đồng",
        closing_at=datetime(2025, 12, 19, 10, 0, tzinfo=timezone.utc),
        bid_method="Qua mạng",
        detail_url="https://x",
        budget_vnd=500_000_000,
        raw={"bidForm": "DTRR", "bidMode": "1_MTHS"},
    )
    extras = storage._bid_extras_for_storage(bid)
    assert extras["budget_vnd"] == 500_000_000
    assert extras["investor"] == "UBND Lâm Đồng"
    assert extras["bid_form"] == "Đấu thầu rộng rãi"
    assert extras["bid_mode"] == "Một giai đoạn một túi hồ sơ"
    assert extras["location"] == "Đà Lạt - Lâm Đồng"
    assert extras["closing_at"].startswith("2025-12-19")


def test_mark_seen_extras_round_trip(temp_db: Path):
    from datetime import datetime, timezone
    from tracker.models import Bid

    bid = Bid(
        tbmt_code="RT-1",
        title="Gói RT",
        status="Chưa đóng thầu",
        investor="X",
        posted_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        field="",
        location="HN",
        closing_at=datetime(2025, 12, 31, 15, 0, tzinfo=timezone.utc),
        bid_method="",
        detail_url="",
        budget_vnd=1_500_000_000,
        raw={"bidForm": "DTRR"},
    )
    storage.mark_seen("RT-1", "Gói RT", sent=True, bid=bid)
    row = storage.lookup_bid_in_db("RT-1")
    assert row is not None
    _, _, sent, extras = row
    assert sent == 1
    assert extras["budget_vnd"] == 1_500_000_000
    assert extras["bid_form"] == "Đấu thầu rộng rãi"
    assert extras["bid_mode"] is None  # raw không có bidMode


def test_mark_seen_without_bid_keeps_nulls(temp_db: Path):
    storage.mark_seen("NB-1", "Không Bid", sent=False)
    row = storage.lookup_bid_in_db("NB-1")
    assert row is not None
    _, _, sent, extras = row
    assert sent == 0
    assert extras["budget_vnd"] is None
    assert extras["investor"] is None


def test_list_recent_includes_extras(temp_db: Path):
    from datetime import datetime, timezone
    from tracker.models import Bid

    bid = Bid(
        tbmt_code="LR-1",
        title="LR",
        status="",
        investor="CĐT",
        posted_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        field="",
        location="",
        closing_at=datetime(2025, 1, 2, tzinfo=timezone.utc),
        bid_method="",
        detail_url="",
        budget_vnd=200_000_000,
    )
    storage.mark_seen("LR-1", "LR", sent=True, bid=bid)
    rows = storage.list_recent_bids(5)
    assert rows[0][4]["budget_vnd"] == 200_000_000  # extras dict
    assert rows[0][4]["investor"] == "CĐT"
