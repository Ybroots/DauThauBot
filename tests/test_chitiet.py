"""Tests cho tính năng /chitiet — auto fill dữ liệu đọc từ mã TBMT."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from tracker.formatter import format_bid_detail
from tracker.models import Bid
from tracker.parser import extract_bid_extras, parse_search_response, parse_tbmt_input

FIXTURE = Path(__file__).parent / "fixtures" / "search_sample.json"


def test_parse_tbmt_input_plain_code():
    code, ver = parse_tbmt_input("IB2500579539")
    assert code == "IB2500579539"
    assert ver is None


def test_parse_tbmt_input_with_version():
    code, ver = parse_tbmt_input("ib2500579539-00")
    assert code == "IB2500579539"
    assert ver == "00"


def test_parse_tbmt_input_whitespace_and_caps():
    code, ver = parse_tbmt_input("  iB2500579539-03  ")
    assert code == "IB2500579539"
    assert ver == "03"


def test_parse_tbmt_input_from_url():
    url = (
        "https://muasamcong.mpi.gov.vn/web/guest/contractor-selection"
        "?notifyNo=IB2500579539&notifyVersion=00&id=abc"
    )
    code, ver = parse_tbmt_input(url)
    assert code == "IB2500579539"
    assert ver == "00"


def test_parse_tbmt_input_empty():
    assert parse_tbmt_input("") == ("", None)
    assert parse_tbmt_input("   ") == ("", None)
    assert parse_tbmt_input("không phải mã") == ("", None)


def test_extract_bid_extras_from_fixture():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    item = data["page"]["content"][0]
    extras = extract_bid_extras(item)
    assert extras.get("Bên mời thầu") == "Sở Tài chính Lâm Đồng"
    assert extras.get("Hình thức LCNT") == "Đấu thầu rộng rãi"
    assert extras.get("Phương thức") == "Một giai đoạn một túi hồ sơ"
    assert extras.get("Luật áp dụng") == "Luật đấu thầu"
    assert extras.get("Kế hoạch số") == "PL2500003498"


def test_extract_bid_extras_drops_undefined():
    extras = extract_bid_extras({
        "investorName": "X",
        "procuringEntityName": "X",
        "planNo": "undefined",
        "bidForm": "",
    })
    assert "Bên mời thầu" not in extras
    assert "Kế hoạch số" not in extras


def test_extract_bid_extras_includes_open_date():
    extras = extract_bid_extras({"bidOpenDate": "2026-06-02T08:00:00.000Z"})
    # UTC 08:00 + 7h = 15:00 VN
    assert extras.get("Mở thầu") == "15:00 02/06/2026"


def test_extract_bid_extras_original_public_date_only_if_different():
    same = extract_bid_extras({
        "publicDate": "2026-05-22T08:00:00Z",
        "originalPublicDate": "2026-05-22T08:00:00Z",
    })
    assert "Đăng lần đầu" not in same

    diff = extract_bid_extras({
        "publicDate": "2026-05-22T08:00:00Z",
        "originalPublicDate": "2026-05-20T08:00:00Z",
    })
    assert diff.get("Đăng lần đầu") == "15:00 20/05/2026"


def test_extract_bid_extras_zero_counts_hidden():
    extras = extract_bid_extras({
        "numBidderTech": 0,
        "numClarifyReq": 0,
        "numPetition": 0,
    })
    assert "Nhà thầu đã đăng ký" not in extras
    assert "Yêu cầu làm rõ" not in extras
    assert "Khiếu nại / kiến nghị" not in extras


def test_extract_bid_extras_aggregates_petition_counts():
    extras = extract_bid_extras({
        "numPetition": 1,
        "numPetitionHsmt": 2,
        "numPetitionLcnt": 0,
        "numPetitionKqlcnt": 1,
    })
    assert extras.get("Khiếu nại / kiến nghị") == "4"


def test_extract_bid_extras_shows_bidder_and_clarify_counts():
    extras = extract_bid_extras({"numBidderTech": 5, "numClarifyReq": 2})
    assert extras["Nhà thầu đã đăng ký"] == "5"
    assert extras["Yêu cầu làm rõ"] == "2"


def test_extract_bid_extras_tags_medicine_and_domestic():
    extras = extract_bid_extras({"isMedicine": 1, "isDomestic": 1})
    tag = extras.get("Tag", "")
    assert "Gói thuốc" in tag
    assert "Đấu thầu trong nước" in tag


def test_extract_bid_extras_no_tag_when_neither_flag():
    extras = extract_bid_extras({"isMedicine": 0, "isDomestic": 0})
    assert "Tag" not in extras


def test_extract_bid_extras_handles_invalid_counts_gracefully():
    extras = extract_bid_extras({
        "numBidderTech": "không phải số",
        "numPetition": None,
    })
    # Không crash; không có entry
    assert "Nhà thầu đã đăng ký" not in extras


def test_extract_bid_extras_handles_non_dict():
    assert extract_bid_extras(None) == {}
    assert extract_bid_extras("not a dict") == {}


def test_format_bid_detail_has_extras_block():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    bids = parse_search_response(data)
    bid = bids[0]

    msg = format_bid_detail(bid)
    assert "<b>Mã TBMT:</b>" in msg
    assert "<b>── Chi tiết bổ sung ──</b>" in msg
    assert "Đấu thầu rộng rãi" in msg
    assert "🔗 <a" in msg
    # Khối extras phải đứng trước link footer
    assert msg.index("Chi tiết bổ sung") < msg.index("🔗 <a")


def test_format_bid_detail_no_extras_when_raw_missing():
    bid = Bid(
        tbmt_code="X-00",
        title="t",
        status="s",
        investor="i",
        posted_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        field="",
        location="",
        closing_at=datetime(2025, 1, 2, tzinfo=timezone.utc),
        bid_method="",
        detail_url="https://x",
    )
    msg = format_bid_detail(bid)
    assert "── Chi tiết bổ sung ──" not in msg


def test_chitiet_button_callback_data_fits_64_bytes():
    from tracker.telegram import chitiet_button

    kb = chitiet_button("IB2500579539-00")
    cb = kb["inline_keyboard"][0][0]["callback_data"]
    assert cb == "ct|IB2500579539-00"
    assert len(cb.encode("utf-8")) <= 64


def test_chitiet_button_truncates_long_code():
    from tracker.telegram import chitiet_button

    kb = chitiet_button("A" * 200)
    cb = kb["inline_keyboard"][0][0]["callback_data"]
    assert len(cb.encode("utf-8")) <= 64


def test_execute_detail_fetch_invalid_code_sends_usage():
    """Mã không hợp lệ → bot trả message hướng dẫn cú pháp."""
    from tracker import bot_commands

    sent: list[tuple] = []

    def fake_reply(token, chat_id, text, **kwargs):
        sent.append((chat_id, text))

    # Bỏ qua Secrets thật bằng SimpleNamespace để tránh phụ thuộc .env
    from types import SimpleNamespace

    secrets = SimpleNamespace(
        telegram_bot_token="x",
        interactive_search_cooldown_seconds=0,
        crawl_page_size=50,
        use_playwright=True,
        playwright_headless=True,
        playwright_channel=None,
    )

    with patch.object(bot_commands, "_reply", fake_reply):
        bot_commands._execute_detail_fetch(secrets, "không phải mã", 123, "chat|user")

    assert sent, "phải gửi tin hướng dẫn"
    assert "/chitiet" in sent[0][1]
