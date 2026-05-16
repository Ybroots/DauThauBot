from __future__ import annotations

import re
import unicodedata
from typing import Any, Optional

from .config import KeywordsConfig
from .models import Bid


def normalize(text: str) -> str:
    s = (text or "").lower().replace("đ", "d")
    s = re.sub(r"\s+", " ", s.strip())
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _raw_search_text(raw: Optional[dict[str, Any]]) -> str:
    """Các trường ES thường dùng để khớp — tránh lệch với lọc client sau tra server-side."""
    if not raw or not isinstance(raw, dict):
        return ""
    parts: list[str] = []
    for key in (
        "notifyNo",
        "notifyNoStand",
        "bidName",
        "investorName",
        "procuringEntityName",
        "planNo",
        "mainWork",
        "name",
    ):
        v = raw.get(key)
        if v is None:
            continue
        if isinstance(v, list):
            parts.extend(str(x) for x in v if x is not None and str(x).strip())
        elif str(v).strip():
            parts.append(str(v))
    return " ".join(parts)


def _keyword_matches_in_haystack(
    haystack_norm: str, kw_norm: str, *, strict_word: bool
) -> bool:
    """strict_word=True: một từ (không có khoảng trắng trong kw) chỉ khớp khi tách biệt, không chỉ là tiền tố của từ dài.

    Ví dụ loại được: cụm tìm cam khớp nhầm trong camera."""
    kw_norm = (kw_norm or "").strip()
    if not kw_norm:
        return False
    if not strict_word:
        return kw_norm in haystack_norm
    if " " in kw_norm:
        return kw_norm in haystack_norm
    return (
        re.search(
            rf"(?<![a-z0-9]){re.escape(kw_norm)}(?![a-z0-9])",
            haystack_norm,
        )
        is not None
    )


def matches_keywords(
    bid: Bid,
    cfg: KeywordsConfig,
    *,
    strict_keywords: bool = False,
) -> tuple[bool, list[str]]:
    haystack = normalize(
        " ".join(
            filter(
                None,
                [
                    bid.tbmt_code,
                    bid.title,
                    bid.investor,
                    bid.field,
                    bid.location,
                    bid.status,
                    bid.description,
                    _raw_search_text(bid.raw),
                ],
            )
        )
    )

    matched: list[str] = []
    if cfg.keywords:
        for kw in cfg.keywords:
            kn = normalize(kw)
            if _keyword_matches_in_haystack(haystack, kn, strict_word=strict_keywords):
                matched.append(kw)
        if not matched:
            return False, []

    if cfg.locations:
        loc_hay = normalize(bid.location)
        if not any(normalize(loc) in loc_hay for loc in cfg.locations):
            return False, matched

    if cfg.fields:
        if not any(normalize(f) == normalize(bid.field) for f in cfg.fields):
            return False, matched

    if cfg.min_budget_vnd and bid.budget_vnd:
        if bid.budget_vnd < cfg.min_budget_vnd:
            return False, matched

    return True, matched
