"""Tests cho watchdog trong safe_run — chặn hang vô thời hạn."""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from tracker import scheduler


@pytest.fixture(autouse=True)
def _reset_block_state():
    scheduler._block_state["blocked_until"] = None
    scheduler._block_state["consecutive_blocks"] = 0
    yield
    scheduler._block_state["blocked_until"] = None
    scheduler._block_state["consecutive_blocks"] = 0


def test_watchdog_cancelled_when_run_once_succeeds(monkeypatch):
    """Happy path: run_once xong nhanh → watchdog cancel, không exit."""
    monkeypatch.setattr(scheduler, "WATCHDOG_SECONDS", 5)  # 5s buffer cho test

    called = []
    def fast_run():
        called.append("ran")

    monkeypatch.setattr(scheduler, "run_once", fast_run)
    monkeypatch.setattr(scheduler, "_in_quiet_hours", lambda *a: False)
    monkeypatch.setattr(scheduler, "_in_block_cooldown", lambda *a: False)

    exited = []
    monkeypatch.setattr(scheduler.os, "_exit", lambda code: exited.append(code))

    scheduler.safe_run()
    assert called == ["ran"]
    # Đợi 1s để chắc watchdog không bắn (nếu nó vẫn còn timer)
    time.sleep(1.0)
    assert exited == [], "Watchdog không được fire khi run_once hoàn thành"


def test_watchdog_fires_when_run_once_hangs(monkeypatch):
    """Hang case: run_once block lâu hơn watchdog → os._exit(2) được gọi."""
    monkeypatch.setattr(scheduler, "WATCHDOG_SECONDS", 1)  # 1s cho test nhanh

    def hanging_run():
        time.sleep(3.0)  # Vượt watchdog → bị fire

    monkeypatch.setattr(scheduler, "run_once", hanging_run)
    monkeypatch.setattr(scheduler, "_in_quiet_hours", lambda *a: False)
    monkeypatch.setattr(scheduler, "_in_block_cooldown", lambda *a: False)

    exited = []
    # Stub os._exit để không thật sự kill test process
    monkeypatch.setattr(scheduler.os, "_exit", lambda code: exited.append(code))

    scheduler.safe_run()
    # Watchdog đã fire trong lúc safe_run đang chạy
    assert exited == [2], f"Watchdog phải fire với exit(2), got {exited}"


def test_watchdog_cancelled_when_run_once_raises(monkeypatch):
    """Exception path: run_once raise → except handle → watchdog vẫn cancel trong finally."""
    monkeypatch.setattr(scheduler, "WATCHDOG_SECONDS", 5)

    def failing_run():
        raise ValueError("boom")

    monkeypatch.setattr(scheduler, "run_once", failing_run)
    monkeypatch.setattr(scheduler, "_in_quiet_hours", lambda *a: False)
    monkeypatch.setattr(scheduler, "_in_block_cooldown", lambda *a: False)

    exited = []
    monkeypatch.setattr(scheduler.os, "_exit", lambda code: exited.append(code))

    scheduler.safe_run()  # exception bị bắt bên trong, không raise ra
    time.sleep(1.0)
    assert exited == [], "Exception bình thường không được kích hoạt watchdog"


def test_quiet_hours_skip_does_not_start_watchdog(monkeypatch):
    """Skip trong quiet hours → không spawn watchdog (nhanh, an toàn)."""
    monkeypatch.setattr(scheduler, "WATCHDOG_SECONDS", 1)
    monkeypatch.setattr(scheduler, "_in_quiet_hours", lambda *a: True)
    monkeypatch.setattr(scheduler, "_in_block_cooldown", lambda *a: False)

    ran = []
    monkeypatch.setattr(scheduler, "run_once", lambda: ran.append("ran"))

    exited = []
    monkeypatch.setattr(scheduler.os, "_exit", lambda code: exited.append(code))

    scheduler.safe_run()
    assert ran == [], "Không gọi run_once trong quiet hours"
    time.sleep(1.5)  # > WATCHDOG_SECONDS — watchdog đáng lẽ fire nếu được start
    assert exited == [], "Watchdog không được start khi skip"
