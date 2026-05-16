from datetime import datetime, timezone

from tracker.config import KeywordsConfig
from tracker.filter import matches_keywords, normalize
from tracker.models import Bid


def _bid(**kwargs) -> Bid:
    defaults = dict(
        tbmt_code="IB2500579539-00",
        title="Mua sắm camera giám sát",
        status="Chưa đóng thầu",
        investor="UBND Lâm Đồng",
        posted_at=datetime(2025, 12, 10, 10, 53, tzinfo=timezone.utc),
        field="Hàng hóa",
        location="Phường Long An - Tỉnh Tây Ninh;",
        closing_at=datetime(2025, 12, 19, 10, 0, tzinfo=timezone.utc),
        bid_method="Qua mạng",
        detail_url="https://example.com",
        budget_vnd=500_000_000,
    )
    defaults.update(kwargs)
    return Bid(**defaults)


def test_normalize_diacritics():
    assert normalize("Lâm Đồng") == "lam dong"


def test_normalize_collapses_whitespace():
    assert normalize("Lâm   Đồng") == "lam dong"


def test_match_keyword_in_title():
    cfg = KeywordsConfig(keywords=["camera"])
    ok, matched = matches_keywords(_bid(), cfg)
    assert ok is True
    assert "camera" in matched


def test_strict_word_rejects_prefix_inside_longer_token():
    cfg = KeywordsConfig(keywords=["cam"])
    b = _bid(title="Mua camera an ninh")
    ok_loose, _ = matches_keywords(b, cfg, strict_keywords=False)
    ok_strict, _ = matches_keywords(b, cfg, strict_keywords=True)
    assert ok_loose is True
    assert ok_strict is False


def test_strict_word_accepts_full_word():
    cfg = KeywordsConfig(keywords=["camera"])
    b = _bid(title="Trang bị camera giám sát")
    ok, matched = matches_keywords(b, cfg, strict_keywords=True)
    assert ok is True
    assert matched == ["camera"]


def test_match_keyword_in_location():
    cfg = KeywordsConfig(keywords=["lâm đồng"])
    ok, matched = matches_keywords(
        _bid(investor="Sở Y tế", location="Tỉnh Lâm Đồng"),
        cfg,
    )
    assert ok is True


def test_match_keyword_in_investor():
    cfg = KeywordsConfig(keywords=["lâm đồng"])
    ok, _ = matches_keywords(_bid(location="Hà Nội"), cfg)
    assert ok is True


def test_match_keyword_from_raw_when_title_differs():
    """ES có thể khớp cụm ở trường raw; haystack client phải đọc raw."""
    cfg = KeywordsConfig(keywords=["camera"])
    b = _bid(title="Mua sắm thiết bị", raw={"bidName": ["Hệ thống camera giám sát"]})
    ok, matched = matches_keywords(b, cfg)
    assert ok is True
    assert "camera" in matched


def test_match_without_diacritics():
    cfg = KeywordsConfig(keywords=["lam dong"])
    ok, _ = matches_keywords(_bid(investor="UBND tỉnh Lâm Đồng"), cfg)
    assert ok is True


def test_or_logic_multiple_keywords():
    cfg = KeywordsConfig(keywords=["camera", "máy chủ"])
    ok, matched = matches_keywords(_bid(), cfg)
    assert ok is True
    assert matched == ["camera"]


def test_reject_location_filter():
    cfg = KeywordsConfig(keywords=["camera"], locations=["Tây Ninh"])
    ok, _ = matches_keywords(_bid(location="Hồ Chí Minh"), cfg)
    assert ok is False


def test_reject_budget_filter():
    cfg = KeywordsConfig(keywords=["camera"], min_budget_vnd=1_000_000_000)
    ok, _ = matches_keywords(_bid(budget_vnd=500_000_000), cfg)
    assert ok is False
