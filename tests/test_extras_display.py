"""Tests cho helpers hiển thị extras (giá VND, hạn đóng compact)."""

from __future__ import annotations

from tracker.bot_commands import _compact_extras, _human_vnd, _short_closing


def test_human_vnd_handles_none_and_zero():
    assert _human_vnd(None) == ""
    assert _human_vnd(0) == ""


def test_human_vnd_thousand_range_uses_comma():
    assert _human_vnd(500_000) == "500,000"


def test_human_vnd_million_range():
    assert _human_vnd(100_000_000) == "100tr"
    assert _human_vnd(999_999_999) == "1000tr"


def test_human_vnd_billion_range():
    assert _human_vnd(1_000_000_000) == "1tỷ"
    assert _human_vnd(1_500_000_000) == "1.5tỷ"
    assert _human_vnd(12_300_000_000) == "12.3tỷ"


def test_short_closing_iso_round_trip():
    assert _short_closing("2025-12-31T15:00:00+00:00") == "31/12 15:00"


def test_short_closing_iso_with_z():
    assert _short_closing("2025-12-19T03:00:00.000Z") == "19/12 03:00"


def test_short_closing_handles_garbage():
    assert _short_closing("") == ""
    assert _short_closing(None) == ""
    assert _short_closing("not a date") == ""


def test_compact_extras_empty_when_no_data():
    assert _compact_extras({}) == ""
    assert _compact_extras({"budget_vnd": None, "closing_at": None}) == ""


def test_compact_extras_combines_budget_and_closing():
    out = _compact_extras({
        "budget_vnd": 500_000_000,
        "closing_at": "2025-12-19T10:00:00+00:00",
    })
    assert "GG: 500tr" in out
    assert "HĐT: 19/12 10:00" in out
    assert " | " in out


def test_compact_extras_partial_only_budget():
    out = _compact_extras({"budget_vnd": 2_000_000_000})
    assert out == "GG: 2tỷ"


def test_compact_extras_partial_only_closing():
    out = _compact_extras({"closing_at": "2025-06-30T09:00:00Z"})
    assert out == "HĐT: 30/06 09:00"
