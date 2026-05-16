from datetime import datetime, timezone

import pytest

from tracker.config import KeywordGroup, KeywordsConfig
from tracker.filter import explain_match, group_matches, match_bid, normalize
from tracker.models import Bid


def _bid(**kwargs) -> Bid:
    defaults = dict(
        tbmt_code="IB2500579539-00",
        title="Mua sắm camera giám sát",
        status="Chưa đóng thầu",
        investor="UBND Lâm Đồng",
        posted_at=datetime(2025, 12, 10, 10, 53, tzinfo=timezone.utc),
        closing_at=datetime(2025, 12, 19, 10, 0, tzinfo=timezone.utc),
        field="Hàng hóa",
        location="Phường Long An - Tỉnh Tây Ninh;",
        bid_method="Qua mạng",
        detail_url="https://example.com",
        budget_vnd=500_000_000,
    )
    defaults.update(kwargs)
    return Bid(**defaults)


def _cfg(*keywords, require="any", name="Test") -> KeywordsConfig:
    return KeywordsConfig(
        groups=[KeywordGroup(name=name, require=require, keywords=list(keywords))]
    )


# ── normalize ────────────────────────────────────────────────────────────────

def test_normalize_diacritics():
    assert normalize("Lâm Đồng") == "lam dong"


def test_normalize_collapses_whitespace():
    assert normalize("Lâm   Đồng") == "lam dong"


# ── basic keyword matching ────────────────────────────────────────────────────

def test_match_keyword_in_title():
    ok, matched, _ = match_bid(_bid(), _cfg("camera"))
    assert ok is True
    assert "camera" in matched


def test_match_keyword_in_location():
    ok, _, _ = match_bid(
        _bid(investor="Sở Y tế", location="Tỉnh Lâm Đồng"),
        _cfg("lâm đồng"),
    )
    assert ok is True


def test_match_keyword_in_investor():
    ok, _, _ = match_bid(_bid(location="Hà Nội"), _cfg("lâm đồng"))
    assert ok is True


def test_match_keyword_from_raw_when_title_differs():
    b = _bid(title="Mua sắm thiết bị", raw={"bidName": ["Hệ thống camera giám sát"]})
    ok, matched, _ = match_bid(b, _cfg("camera"))
    assert ok is True
    assert "camera" in matched


def test_match_without_diacritics():
    ok, _, _ = match_bid(_bid(investor="UBND tỉnh Lâm Đồng"), _cfg("lam dong"))
    assert ok is True


def test_or_logic_multiple_keywords():
    ok, matched, _ = match_bid(_bid(), _cfg("camera", "máy chủ", require="any"))
    assert ok is True
    assert "camera" in matched
    assert "máy chủ" not in matched


# ── strict word matching ──────────────────────────────────────────────────────

def test_strict_word_rejects_prefix_inside_longer_token():
    b = _bid(title="Mua camera an ninh")
    ok_loose, _, _ = match_bid(b, _cfg("cam"), strict_keywords=False)
    ok_strict, _, _ = match_bid(b, _cfg("cam"), strict_keywords=True)
    assert ok_loose is True
    assert ok_strict is False


def test_strict_word_accepts_full_word():
    b = _bid(title="Trang bị camera giám sát")
    ok, matched, _ = match_bid(b, _cfg("camera"), strict_keywords=True)
    assert ok is True
    assert "camera" in matched


# ── Acceptance test 1: AND match đúng ────────────────────────────────────────

def test_and_match_both_keywords_present():
    """Camera + Lâm Đồng → MATCH khi cả hai đều có trong bid."""
    bid = _bid(
        title="Mua camera giám sát hành lang",
        investor="UBND Tỉnh Lâm Đồng",
        location="Tỉnh Lâm Đồng",
    )
    cfg = _cfg("camera", "lâm đồng", require="all", name="Camera Lâm Đồng")
    ok, matched, group_name = match_bid(bid, cfg)
    assert ok is True
    assert "camera" in matched
    assert "lâm đồng" in matched
    assert group_name == "Camera Lâm Đồng"


# ── Acceptance test 2: AND không match vì thiếu 1 keyword ───────────────────

def test_and_no_match_missing_one_keyword():
    """Lâm Đồng có nhưng camera không có → không match."""
    bid = _bid(
        title="Sửa chữa đường giao thông nông thôn",
        investor="UBND Huyện Đức Trọng",
        location="Tỉnh Lâm Đồng",
    )
    cfg = KeywordsConfig(
        groups=[
            KeywordGroup(name="Camera Lâm Đồng", require="all", keywords=["camera", "lâm đồng"]),
            KeywordGroup(name="Truyền thanh Lâm Đồng", require="all", keywords=["truyền thanh thông minh", "lâm đồng"]),
            KeywordGroup(name="Camera toàn quốc", require="any", keywords=["camera", "cctv", "giám sát hình ảnh"]),
        ]
    )
    ok, _, _ = match_bid(bid, cfg)
    assert ok is False


# ── Acceptance test 3: OR match ──────────────────────────────────────────────

def test_or_match_one_of_three_keywords():
    """CCTV match OR group → MATCH dù không có camera hay giám sát."""
    bid = _bid(
        title="Mua thiết bị CCTV cho trụ sở UBND",
        investor="UBND Tỉnh Bắc Ninh",
        location="Tỉnh Bắc Ninh",
    )
    cfg = KeywordsConfig(
        groups=[
            KeywordGroup(name="Camera Lâm Đồng", require="all", keywords=["camera", "lâm đồng"]),
            KeywordGroup(name="Camera toàn quốc", require="any", keywords=["camera", "cctv", "giám sát hình ảnh"]),
        ]
    )
    ok, matched, group_name = match_bid(bid, cfg)
    assert ok is True
    assert "cctv" in matched
    assert group_name == "Camera toàn quốc"


# ── Acceptance test 4: single location keyword must not standalone match ─────

def test_standalone_location_does_not_match_unrelated_bid():
    """'Lâm Đồng' đứng một mình trong AND group → không trigger khi thiếu từ kia."""
    bid = _bid(
        title="Mua bàn ghế văn phòng",
        location="Tỉnh Lâm Đồng",
    )
    cfg = _cfg("camera", "lâm đồng", require="all")
    ok, _, _ = match_bid(bid, cfg)
    assert ok is False


# ── KeywordsConfig.from_dict backward compat ─────────────────────────────────

def test_from_dict_new_format():
    data = {
        "groups": [
            {"name": "G1", "require": "all", "keywords": ["camera", "lâm đồng"]}
        ]
    }
    cfg = KeywordsConfig.from_dict(data)
    assert len(cfg.groups) == 1
    assert cfg.groups[0].require == "all"


def test_from_dict_legacy_flat_keywords():
    data = {"keywords": ["camera", "cctv"]}
    cfg = KeywordsConfig.from_dict(data)
    assert len(cfg.groups) == 1
    assert cfg.groups[0].require == "any"
    assert "camera" in cfg.groups[0].keywords


def test_from_dict_empty():
    cfg = KeywordsConfig.from_dict({})
    assert cfg.groups == []


# ── explain_match ─────────────────────────────────────────────────────────────

def test_explain_match_output():
    bid = _bid(title="camera lâm đồng", location="Tỉnh Lâm Đồng")
    cfg = KeywordsConfig(
        groups=[
            KeywordGroup(name="Camera LĐ", require="all", keywords=["camera", "lâm đồng"]),
        ]
    )
    text = explain_match(bid, cfg)
    assert "Camera LĐ" in text
    assert "✅" in text
    assert "✓" in text


# ── group_matches helper ──────────────────────────────────────────────────────

def test_group_matches_all_true():
    g = KeywordGroup(name="G", require="all", keywords=["camera", "lam dong"])
    assert group_matches(g, "mua camera tai lam dong") is True


def test_group_matches_all_false_partial():
    g = KeywordGroup(name="G", require="all", keywords=["camera", "lam dong"])
    assert group_matches(g, "mua camera tai ha noi") is False


def test_group_matches_any_true():
    g = KeywordGroup(name="G", require="any", keywords=["camera", "cctv"])
    assert group_matches(g, "thiet bi cctv") is True


def test_group_matches_empty_keywords():
    g = KeywordGroup(name="G", require="any", keywords=[])
    assert group_matches(g, "anything") is False
