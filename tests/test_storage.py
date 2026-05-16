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
