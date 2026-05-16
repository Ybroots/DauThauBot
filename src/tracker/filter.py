from __future__ import annotations

import re
import unicodedata
from typing import Any, Optional

from .config import KeywordGroup, KeywordsConfig
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


def _build_haystack(bid: Bid) -> str:
    return normalize(
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


def group_matches(
    group: KeywordGroup, haystack: str, *, strict_keywords: bool = False
) -> bool:
    nkws = [normalize(k) for k in group.keywords if k.strip()]
    if not nkws:
        return False
    if group.require == "all":
        return all(
            _keyword_matches_in_haystack(haystack, k, strict_word=strict_keywords)
            for k in nkws
        )
    return any(
        _keyword_matches_in_haystack(haystack, k, strict_word=strict_keywords)
        for k in nkws
    )


def match_bid(
    bid: Bid, cfg: KeywordsConfig, *, strict_keywords: bool = False
) -> tuple[bool, list[str], str]:
    """Returns (matched, matched_keywords, group_name)."""
    haystack = _build_haystack(bid)
    for group in cfg.groups:
        if group_matches(group, haystack, strict_keywords=strict_keywords):
            matched_kws = [
                k
                for k in group.keywords
                if _keyword_matches_in_haystack(
                    haystack, normalize(k), strict_word=strict_keywords
                )
            ]
            return True, matched_kws, group.name
    return False, [], ""


def explain_match(bid: Bid, cfg: KeywordsConfig) -> str:
    """Human-readable breakdown of which groups match — used by /test bot command."""
    haystack = _build_haystack(bid)
    lines = [f'🔬 Test với bid: "{bid.title}"']
    for group in cfg.groups:
        req_label = "TẤT CẢ" if group.require == "all" else "BẤT KỲ"
        nkws = [(k, normalize(k)) for k in group.keywords if k.strip()]
        ok_count = sum(1 for _, nk in nkws if nk in haystack)
        if group.require == "all":
            group_ok = ok_count == len(nkws) and len(nkws) > 0
        else:
            group_ok = ok_count > 0
        icon = "✅" if group_ok else "❌"
        lines.append(
            f'{icon} Group "{group.name}" ({req_label}): {ok_count}/{len(nkws)} khớp'
        )
        for k, nk in nkws:
            mark = "✓" if nk in haystack else "✗"
            lines.append(f'   {mark} "{k}"')
    return "\n".join(lines)
