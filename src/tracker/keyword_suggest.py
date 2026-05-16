"""Gợi ý từ khóa từ dữ liệu thực trên cổng muasamcong.

Flow:
  1. User: /goiy lâm đồng
  2. Bot cào 1 trang ES (50 gói), trích từ phổ biến trong tiêu đề
  3. Bot hiển thị danh sách đánh số
  4. User gõ số → bot thu hẹp và gợi ý thêm vòng kế
  5. User gõ /taogroup → tạo AND group với tất cả từ đã chọn
"""

from __future__ import annotations

import re
from collections import Counter
from typing import TYPE_CHECKING

from .filter import normalize

if TYPE_CHECKING:
    from .models import Bid

# Từ quá chung trong văn bản đấu thầu — không có giá trị phân biệt
_STOP: set[str] = {
    # động từ/giới từ phổ biến
    "va", "cua", "cho", "tai", "voi", "theo", "trong", "tren", "tu", "den",
    "cac", "mot", "hai", "ba", "bon", "nam", "nhieu", "toan", "bo",
    # hành chính
    "ubnd", "tinh", "huyen", "quan", "phuong", "xa", "thi", "tran",
    "so", "phong", "ban", "trung", "tam", "vien", "cuc", "tong",
    # đấu thầu chung
    "mua", "sam", "cung", "cap", "thiet", "bi", "he", "thong",
    "xay", "dung", "sua", "chua", "nang", "cap", "bao", "tri",
    "thi", "cong", "du", "an", "cong", "trinh", "hang", "hoa", "dich", "vu",
    "giai", "doan", "ky", "thuat", "chat", "luong", "gia", "tri",
    "quyet", "dinh", "ke", "hoach", "bao", "cao", "ket", "qua",
    "theo", "nam", "thang", "quy", "dot", "lan",
}


def _tokenize(text: str) -> list[str]:
    """Normalize → split thành token."""
    return [w for w in re.findall(r"[a-z0-9]+", normalize(text)) if len(w) >= 2]


def extract_suggestions(
    bids: list,
    accumulated: list[str],
    *,
    top_n: int = 6,
    min_count: int = 2,
) -> list[tuple[str, int]]:
    """Trích từ/cụm xuất hiện nhiều nhất trong bid titles, loại bỏ từ đã chọn + stopwords.

    Returns list[(term, count)] sắp xếp theo count giảm dần.
    """
    # Tất cả token của các từ đã chọn → loại khỏi gợi ý
    accumulated_tokens: set[str] = set()
    for kw in accumulated:
        accumulated_tokens.update(_tokenize(kw))

    unigram: Counter = Counter()
    bigram: Counter = Counter()

    for bid in bids:
        tokens = _tokenize(bid.title + " " + bid.field + " " + bid.location)
        # Unigrams (bỏ stopword + accumulated)
        for t in tokens:
            if t not in _STOP and t not in accumulated_tokens and len(t) >= 3:
                unigram[t] += 1
        # Bigrams
        for i in range(len(tokens) - 1):
            a, b = tokens[i], tokens[i + 1]
            if a in _STOP or b in _STOP:
                continue
            if a in accumulated_tokens or b in accumulated_tokens:
                continue
            if len(a) >= 2 and len(b) >= 2:
                bigram[f"{a} {b}"] += 1

    # Ưu tiên bigram (cụm từ cụ thể hơn), bổ sung unigram nếu thiếu
    candidates: list[tuple[str, int]] = []
    used: set[str] = set()

    for phrase, cnt in bigram.most_common(top_n * 3):
        if cnt < min_count:
            break
        words = phrase.split()
        if any(w in used for w in words):
            continue
        candidates.append((phrase, cnt))
        used.update(words)
        if len(candidates) >= top_n:
            break

    for word, cnt in unigram.most_common(top_n * 2):
        if len(candidates) >= top_n:
            break
        if cnt < min_count:
            break
        if word not in used:
            candidates.append((word, cnt))
            used.add(word)

    return sorted(candidates, key=lambda x: -x[1])[:top_n]


def filter_bids_by_terms(bids: list, terms: list[str]) -> list:
    """Lọc bids còn chứa TẤT CẢ từ trong terms (AND logic) — dùng sau mỗi lượt chọn."""
    norm_terms = [normalize(t) for t in terms]
    result = []
    for bid in bids:
        haystack = normalize(
            bid.title + " " + bid.investor + " " + bid.field + " " + bid.location
        )
        if all(t in haystack for t in norm_terms):
            result.append(bid)
    return result


def build_suggest_reply(
    bids: list,
    accumulated: list[str],
    suggestions: list[tuple[str, int]],
) -> str:
    """Tạo message gợi ý cho user."""
    kw_display = " + ".join(f'"{k}"' for k in accumulated)
    lines = [
        f"🔍 {len(bids)} gói khớp với {kw_display}",
        "",
        "Từ xuất hiện nhiều nhất (chọn số để hẹp hơn):",
    ]
    for i, (term, count) in enumerate(suggestions, 1):
        lines.append(f"  {i}. {term}  ({count} gói)")
    lines += [
        "",
        "Gõ số (1–6) để thêm vào điều kiện AND",
        "/taogroup — tạo group với từ đã chọn",
        "/huy — huỷ",
    ]
    return "\n".join(lines)
