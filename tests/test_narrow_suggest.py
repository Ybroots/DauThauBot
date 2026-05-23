"""Tests cho auto-suggest hẹp dần tích hợp vào /tim."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tracker import bot_commands
from tracker.models import Bid


def _mk_bid(code: str, title: str, *, investor: str = "", location: str = "") -> Bid:
    now = datetime(2026, 5, 23, tzinfo=timezone.utc)
    return Bid(
        tbmt_code=code,
        title=title,
        status="Chưa đóng thầu",
        investor=investor,
        posted_at=now,
        field="Hàng hóa",
        location=location,
        closing_at=now,
        bid_method="Qua mạng",
        detail_url="https://x",
        budget_vnd=None,
    )


@pytest.fixture(autouse=True)
def _reset_narrow_state():
    bot_commands._narrow_state.clear()
    yield
    bot_commands._narrow_state.clear()


def _fake_secrets() -> SimpleNamespace:
    return SimpleNamespace(
        telegram_bot_token="x",
        interactive_search_max_messages=5,
        interactive_search_cooldown_seconds=0,
    )


def test_narrow_kb_has_pairs_and_cancel():
    suggestions = [("lâm đồng", 12), ("công an", 8), ("camera", 5)]
    kb = bot_commands._narrow_kb(suggestions)
    rows = kb["inline_keyboard"]
    # 3 suggestions → 2 rows (2+1) + 1 footer (cancel + menu) = 3 rows
    assert len(rows) == 3
    # All callback_data fit Telegram 64-byte limit
    for row in rows:
        for btn in row:
            assert len(btn["callback_data"].encode("utf-8")) <= 64


def test_narrow_kb_truncates_long_term():
    suggestions = [("a" * 100, 1)]
    kb = bot_commands._narrow_kb(suggestions)
    label = kb["inline_keyboard"][0][0]["text"]
    # Phải bị truncate, không quá dài
    assert len(label.encode("utf-8")) <= 30


def test_maybe_send_narrow_skips_when_too_few_bids():
    """Dưới 3 bid không gửi gợi ý — quá ít để cần hẹp dần."""
    sent: list = []
    with patch.object(bot_commands, "_reply", lambda *a, **kw: sent.append(a)):
        bot_commands._maybe_send_narrow_suggestions(
            _fake_secrets(),
            123,
            "chat|user",
            phrases=["camera"],
            bids=[_mk_bid("A-1", "camera 1"), _mk_bid("A-2", "camera 2")],
            include_closed=False,
        )
    assert sent == [], "Không được gửi tin nào khi bids < 3"
    assert "chat|user" not in bot_commands._narrow_state


def test_maybe_send_narrow_skips_when_no_suggestion():
    """Bid không có từ nào để gợi → bỏ qua."""
    bids = [
        _mk_bid("A-1", "x"),
        _mk_bid("A-2", "x"),
        _mk_bid("A-3", "x"),
    ]
    sent: list = []
    with patch.object(bot_commands, "_reply", lambda *a, **kw: sent.append(a)):
        bot_commands._maybe_send_narrow_suggestions(
            _fake_secrets(), 123, "chat|user",
            phrases=["x"], bids=bids, include_closed=False,
        )
    assert sent == []


def test_maybe_send_narrow_populates_state_and_sends_kb():
    bids = [
        _mk_bid("A-1", "Camera giám sát Lâm Đồng"),
        _mk_bid("A-2", "Camera Lâm Đồng công an"),
        _mk_bid("A-3", "Camera Lâm Đồng UBND"),
        _mk_bid("A-4", "Camera Lâm Đồng sở y tế"),
    ]
    captured: list[tuple] = []
    def fake_reply(token, chat_id, text, **kwargs):
        captured.append((chat_id, text, kwargs.get("reply_markup")))

    with patch.object(bot_commands, "_reply", fake_reply):
        bot_commands._maybe_send_narrow_suggestions(
            _fake_secrets(), 123, "chat|user",
            phrases=["camera"], bids=bids, include_closed=False,
        )

    assert len(captured) == 1
    _, text, kb = captured[0]
    assert "Bot gợi ý" in text
    assert kb is not None and "inline_keyboard" in kb

    state = bot_commands._narrow_state.get("chat|user")
    assert state is not None
    assert state["phrases"] == ["camera"]
    assert len(state["bids"]) == 4
    assert state["suggestions"]  # có ít nhất 1 gợi ý


def test_narrow_click_filters_and_re_emits():
    """Click một suggestion → filter in-memory + re-emit bid khớp."""
    bids = [
        _mk_bid("A-1", "Camera Lâm Đồng"),
        _mk_bid("A-2", "Camera Lâm Đồng công an"),
        _mk_bid("A-3", "Camera Hà Nội"),
    ]
    bot_commands._narrow_state["chat|user"] = {
        "phrases": ["camera"],
        "bids": bids,
        "suggestions": [("lam dong", 2), ("ha noi", 1)],
        "include_closed": False,
    }

    sent_messages: list[tuple] = []
    def fake_send(token, cid, body, **kw):
        sent_messages.append((cid, body))
        return True

    reply_messages: list[tuple] = []
    def fake_reply(token, chat_id, text, **kwargs):
        reply_messages.append((chat_id, text))

    with patch.object(bot_commands, "_reply", fake_reply), \
         patch("tracker.telegram.send_message", fake_send):
        bot_commands._execute_narrow_click(_fake_secrets(), 123, "chat|user", 0)

    # Phải emit 2 bid khớp "lam dong" (case-insensitive normalize)
    assert len(sent_messages) == 2
    emitted_codes = [m[1].split('<code>')[1].split('</code>')[0] for m in sent_messages]
    assert "A-1" in emitted_codes
    assert "A-2" in emitted_codes
    # Summary phải có chat reply
    assert any("Hẹp dần" in r[1] for r in reply_messages)


def test_narrow_click_no_match_clears_state():
    bot_commands._narrow_state["chat|user"] = {
        "phrases": ["camera"],
        "bids": [_mk_bid("A-1", "camera")],
        "suggestions": [("xyzkhongco", 0)],
        "include_closed": False,
    }
    replies: list = []
    with patch.object(bot_commands, "_reply", lambda *a, **kw: replies.append(a[2])):
        bot_commands._execute_narrow_click(_fake_secrets(), 123, "chat|user", 0)
    assert "chat|user" not in bot_commands._narrow_state
    assert any("Không còn gói" in r for r in replies)


def test_narrow_click_expired_state():
    """Click nút sau khi state đã clear → message friendly."""
    replies: list = []
    with patch.object(bot_commands, "_reply", lambda *a, **kw: replies.append(a[2])):
        bot_commands._execute_narrow_click(_fake_secrets(), 123, "chat|user", 0)
    assert any("hết hạn" in r for r in replies)


def test_narrow_click_invalid_idx():
    bot_commands._narrow_state["chat|user"] = {
        "phrases": ["x"],
        "bids": [_mk_bid("A-1", "x")],
        "suggestions": [("y", 1)],
        "include_closed": False,
    }
    replies: list = []
    with patch.object(bot_commands, "_reply", lambda *a, **kw: replies.append(a[2])):
        bot_commands._execute_narrow_click(_fake_secrets(), 123, "chat|user", 99)
    assert any("không hợp lệ" in r.lower() for r in replies)
