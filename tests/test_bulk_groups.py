"""Tests cho bulk group commands: disable_all/enable_all/remove_all + /trangthai."""

from __future__ import annotations

from pathlib import Path

import pytest

from tracker import storage


@pytest.fixture()
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = tmp_path / "seen.db"
    monkeypatch.setattr(storage, "DB_PATH", db)
    storage.init_db()
    # 3 groups: 2 active, 1 inactive
    storage.add_group("Camera LĐ", "all", ["camera", "lâm đồng"])
    storage.add_group("Công an", "any", ["công an"])
    storage.add_group("Tắt sẵn", "all", ["xyz"])
    storage.toggle_group_active("Tắt sẵn", False)
    yield db


def test_disable_all_groups_counts_only_active(temp_db: Path):
    n = storage.disable_all_groups()
    assert n == 2  # 2 đang active → tắt
    # Gọi lần 2: không còn active → 0
    assert storage.disable_all_groups() == 0


def test_enable_all_groups_counts_only_inactive(temp_db: Path):
    n = storage.enable_all_groups()
    assert n == 1  # chỉ "Tắt sẵn" đang inactive
    assert storage.enable_all_groups() == 0


def test_disable_then_enable_round_trip(temp_db: Path):
    storage.disable_all_groups()
    # Tất cả phải tắt
    raw = storage.list_all_groups_raw()
    assert all(not g[3] for g in raw)
    storage.enable_all_groups()
    raw = storage.list_all_groups_raw()
    assert all(g[3] for g in raw)


def test_remove_all_groups_wipes_groups_and_keywords(temp_db: Path):
    n = storage.remove_all_groups()
    assert n == 3
    assert storage.list_all_groups_raw() == []
    # CASCADE: keywords cũng phải hết
    import sqlite3
    with sqlite3.connect(temp_db) as conn:
        kw_count = conn.execute("SELECT COUNT(*) FROM keywords").fetchone()[0]
    assert kw_count == 0


def test_remove_all_groups_idempotent(temp_db: Path):
    storage.remove_all_groups()
    assert storage.remove_all_groups() == 0


def test_remove_all_groups_does_not_affect_seen_bids(temp_db: Path):
    storage.mark_seen("BID-1", "tiêu đề bid")
    storage.remove_all_groups()
    assert storage.is_seen("BID-1") is True
    assert storage.total_bids_in_db() == 1


def test_trangthai_command_contains_sections(temp_db: Path, monkeypatch: pytest.MonkeyPatch):
    """Snapshot test: /trangthai output có đủ 4 section."""
    from types import SimpleNamespace
    from tracker import bot_commands

    monkeypatch.setattr(bot_commands, "init_db", lambda: None)
    secrets = SimpleNamespace(
        poll_interval_minutes=45,
        poll_jitter_seconds=600,
        quiet_hours_start="01:00",
        quiet_hours_end="06:00",
        crawl_max_pages=2,
        crawl_page_size=50,
        crawl_per_keyword=True,
        use_playwright=True,
        playwright_headless=True,
        telegram_admin_chat_id="",
    )
    text = bot_commands.handle_slash(
        "/trangthai", secrets, "private", chat_id=1, user_id=1,
    )
    assert text is not None
    assert "Keyword groups" in text
    assert "Cron / thiết lập cào" in text
    assert "Hoạt động" in text
    assert "Quản lý nhanh" in text
    # 3 groups (2 active, 1 inactive) phải hiện
    assert "<b>2</b> group" in text  # 2 active
    assert "1 group" in text  # 1 tạm tắt
    assert "Camera LĐ" in text
    assert "Công an" in text


def test_trangthai_empty_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """DB không có group nào → /trangthai gợi ý /addgroup."""
    from types import SimpleNamespace
    from tracker import bot_commands

    db = tmp_path / "empty.db"
    monkeypatch.setattr(storage, "DB_PATH", db)
    storage.init_db()

    secrets = SimpleNamespace(
        poll_interval_minutes=45,
        poll_jitter_seconds=600,
        quiet_hours_start="01:00",
        quiet_hours_end="06:00",
        crawl_max_pages=2,
        crawl_page_size=50,
        crawl_per_keyword=True,
        use_playwright=True,
        playwright_headless=True,
        telegram_admin_chat_id="",
    )
    text = bot_commands.handle_slash(
        "/trangthai", secrets, "private", chat_id=1, user_id=1,
    )
    assert text is not None
    assert "Chưa có group nào" in text
    assert "/addgroup" in text


def test_tatallgroup_admin_only(temp_db: Path):
    from types import SimpleNamespace
    from tracker import bot_commands

    secrets = SimpleNamespace(
        telegram_admin_chat_id="999",
        admin_chat_id="999",  # @property của Secrets, mock thẳng
        poll_interval_minutes=45,
        poll_jitter_seconds=600,
    )
    text = bot_commands.handle_slash(
        "/tatallgroup", secrets, "private", chat_id=1, user_id=1,
    )
    assert "admin" in text.lower()


def test_tatallgroup_executes_when_admin(temp_db: Path, monkeypatch):
    from types import SimpleNamespace
    from tracker import bot_commands

    secrets = SimpleNamespace(
        telegram_admin_chat_id="42",
        admin_chat_id="42",
        poll_interval_minutes=45,
        poll_jitter_seconds=600,
    )
    text = bot_commands.handle_slash(
        "/tatallgroup", secrets, "private", chat_id=42, user_id=42,
    )
    assert "Đã tắt 2 group" in text  # 2 active groups in fixture
    # Verify storage state
    raw = storage.list_all_groups_raw()
    assert all(not g[3] for g in raw)


def test_batallgroup_admin_only(temp_db: Path):
    from types import SimpleNamespace
    from tracker import bot_commands

    secrets = SimpleNamespace(
        telegram_admin_chat_id="999",
        admin_chat_id="999",
        poll_interval_minutes=45,
        poll_jitter_seconds=600,
    )
    text = bot_commands.handle_slash(
        "/batallgroup", secrets, "private", chat_id=1, user_id=1,
    )
    assert "admin" in text.lower()


def test_batallgroup_zero_when_all_active(temp_db: Path):
    from types import SimpleNamespace
    from tracker import bot_commands

    storage.enable_all_groups()  # bật tất cả

    secrets = SimpleNamespace(
        telegram_admin_chat_id="42",
        admin_chat_id="42",
        poll_interval_minutes=45,
        poll_jitter_seconds=600,
    )
    text = bot_commands.handle_slash(
        "/batallgroup", secrets, "private", chat_id=42, user_id=42,
    )
    assert "đã bật sẵn" in text.lower()
