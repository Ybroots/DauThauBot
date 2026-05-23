"""Tra cứu TBMT nhanh theo từ khóa từ Telegram — không sửa seen.db."""

from __future__ import annotations

import re
import threading
import time
from typing import Any

from loguru import logger

from .config import KeywordGroup, KeywordsConfig, Secrets
from .crawler import MuasamcongCrawler
from .filter import _build_haystack, normalize, match_bid
from .models import Bid
from .formatter import format_bid_message
from .telegram import TELEGRAM_BATCH_SLEEP_S, TELEGRAM_RATE_BATCH, chitiet_button, send_message

# ── TTL in-memory result cache (5 min) ───────────────────────────────────────
_CACHE_TTL_S = 300  # seconds
_bid_cache: dict[str, tuple[float, list[Bid]]] = {}
_cache_lock = threading.Lock()


def _make_cache_key(
    phrases: list[str],
    mode: str,
    include_closed: bool,
    field_filter: list[str] | None,
    bid_method_filter: int | None,
    max_pages: int,
) -> str:
    parts = [
        "|".join(sorted(phrases)),
        mode,
        "closed" if include_closed else "open",
        ",".join(sorted(field_filter or [])),
        str(bid_method_filter) if bid_method_filter is not None else "all",
        str(max_pages),
    ]
    return "::".join(parts)


def _cache_get(key: str) -> list[Bid] | None:
    with _cache_lock:
        entry = _bid_cache.get(key)
        if entry is None:
            return None
        ts, bids = entry
        if time.time() - ts > _CACHE_TTL_S:
            del _bid_cache[key]
            return None
        logger.info("interactive_cache: HIT ({} bids, age {:.0f}s)", len(bids), time.time() - ts)
        return bids


def _cache_put(key: str, bids: list[Bid]) -> None:
    with _cache_lock:
        _bid_cache[key] = (time.time(), bids)
        logger.info("interactive_cache: stored {} bids", len(bids))


def parse_keyword_phrases(raw: str) -> list[str]:
    """Tách các cụm từ: dấu phẩy, chấm phẩy, | hoặc xuống dòng."""
    if not raw:
        return []
    raw = raw.strip()
    parts = [p.strip() for p in re.split(r"[,;|]+\s*|\n+", raw, flags=re.MULTILINE) if p.strip()]
    if parts:
        return parts
    return [raw] if raw else []


def parse_search_query(raw: str) -> tuple[list[str], str]:
    """Phân tách input thành (phrases, mode).

    Cú pháp:
    • "camera, lâm đồng"           → OR  (BẤT KỲ khớp là đủ)
    • "camera | cctv"              → OR
    • "camera & lâm đồng"          → AND (TẤT CẢ phải khớp)
    • "công an & lâm đồng & camera" → AND 3 điều kiện

    Dấu & được ưu tiên: nếu có & thì toàn bộ là AND mode.
    """
    raw = (raw or "").strip()
    if "&" in raw:
        parts = [p.strip() for p in raw.split("&") if p.strip()]
        return parts, "all"
    return parse_keyword_phrases(raw), "any"


def _relevance_score(bid: Bid, phrases: list[str], strict: bool) -> tuple[int, int]:
    """Returns (matched_phrase_count, title_match_count) for relevance sorting (higher = better)."""
    haystack = _build_haystack(bid)
    title_norm = normalize(bid.title or "")
    phrase_hits = 0
    title_hits = 0
    for ph in phrases:
        n = normalize(ph)
        if n and n in haystack:
            phrase_hits += 1
            if n in title_norm:
                title_hits += 1
    return phrase_hits, title_hits


def run_interactive_keyword_search(
    secrets: Secrets,
    phrases: list[str],
    *,
    target_chat_id: str | int,
    mode: str = "any",
    include_closed: bool = False,
    field_filter: list[str] | None = None,
    bid_method_filter: int | None = None,
) -> tuple[int, int, str]:
    """Cào theo interactive_fetch_max_pages; gửi tối đa N tin HTML tới một chat.

    mode="any"  → OR: bid khớp ít nhất 1 phrase.
    mode="all"  → AND: bid phải chứa TẤT CẢ phrases.

    Chiến lược tối ưu:
    • AND mode (≥2 phrases): gộp tất cả phrases thành 1 keyword ES → 1 Playwright session.
    • OR mode (1 phrase): fetch_recent_bids thường — 1 session.
    • OR mode (≥2 phrases): fetch_recent_bids_multi (batch) — 1 session thay vì N.
    • Filter-only (phrases rỗng): duyệt tất cả TBMT với bộ lọc server.
    • Cache: kết quả được cache 5 phút — tra lại cùng query gần như tức thì.
    • Relevance sort: matched bids sắp xếp theo số phrase khớp, số match trong title.

    Returns (sent_count, total_matching, summary_plain).
    Không đụng vào SQLite.
    """
    require: str = mode if mode in ("all", "any") else "any"
    cap = secrets.interactive_search_max_messages

    crawler = MuasamcongCrawler(
        page_size=secrets.crawl_page_size,
        use_playwright=secrets.use_playwright,
        playwright_headless=secrets.playwright_headless,
        playwright_channel=secrets.playwright_channel,
    )
    matched: list[tuple] = []
    sent = 0
    token = secrets.telegram_bot_token
    cid = str(target_chat_id)

    try:
        uniq: list[str] = []
        for p in phrases:
            s = (p or "").strip()
            if s and s not in uniq:
                uniq.append(s)

        mode_label = "AND (tất cả phải khớp)" if require == "all" else "OR (bất kỳ khớp)"
        scope_label = "tất cả (kể cả đã đóng thầu)" if include_closed else "đang mở thầu"

        # Build filter description for logs/summary
        filter_parts: list[str] = []
        if field_filter:
            filter_parts.append(f"lĩnh vực={field_filter}")
        if bid_method_filter is not None:
            filter_parts.append(f"hình thức={'qua mạng' if bid_method_filter == 1 else 'không qua mạng'}")

        max_pages = secrets.interactive_fetch_max_pages
        open_only = not include_closed

        logger.info(
            "interactive_fetch mode={} phrases={} pages={} filters={}",
            require, uniq, max_pages, filter_parts or "none",
        )

        # ── Check cache ───────────────────────────────────────────────────────
        cache_key = _make_cache_key(uniq, require, include_closed, field_filter, bid_method_filter, max_pages)
        cached_bids = _cache_get(cache_key)

        by_code: dict[str, Bid] = {}

        if cached_bids is not None:
            logger.info("interactive_fetch: cache hit — {} bids, skip crawling", len(cached_bids))
            for b in cached_bids:
                by_code[b.tbmt_code] = b
            bids = cached_bids
        elif not uniq:
            # ── Filter-only mode ──────────────────────────────────────────────
            logger.info("interactive_fetch: filter-only mode (no keywords)")
            part = crawler.fetch_recent_bids(
                max_pages=max_pages,
                server_keyword=None,
                open_only=open_only,
                field_filter=field_filter,
                bid_method_filter=bid_method_filter,
            )
            for b in part:
                by_code.setdefault(b.tbmt_code, b)
            bids = list(by_code.values())
            _cache_put(cache_key, bids)
        elif require == "all" and len(uniq) >= 2:
            # ── AND optimisation: join all phrases → 1 ES keyword → 1 session ─
            # matchType "any" + combined keyword → ES trả mọi gói có bất kỳ token nào
            # client-side match_bid(require="all") lọc AND chính xác sau đó.
            combined_kw = " ".join(uniq)
            logger.info(
                "interactive_fetch: AND batch — combined keyword='{}' matchType=any (1 session instead of {})",
                combined_kw, len(uniq),
            )
            part = crawler.fetch_recent_bids(
                max_pages=max_pages,
                server_keyword=combined_kw,
                open_only=open_only,
                field_filter=field_filter,
                bid_method_filter=bid_method_filter,
                match_type="any",
            )
            for b in part:
                by_code.setdefault(b.tbmt_code, b)
            bids = list(by_code.values())
            _cache_put(cache_key, bids)
        elif len(uniq) == 1:
            # ── Single phrase — matchType "any" để ES trả nhiều candidate hơn ─
            part = crawler.fetch_recent_bids(
                max_pages=max_pages,
                server_keyword=uniq[0],
                open_only=open_only,
                field_filter=field_filter,
                bid_method_filter=bid_method_filter,
                match_type="any",
            )
            for b in part:
                by_code.setdefault(b.tbmt_code, b)
            bids = list(by_code.values())
            _cache_put(cache_key, bids)
        else:
            # ── OR mode multi-phrase: batch (1 Playwright session) ────────────
            logger.info(
                "interactive_fetch: OR batch — {} phrases → 1 Playwright session (matchType=any)",
                len(uniq),
            )
            phrase_map = crawler.fetch_recent_bids_multi(
                phrases=uniq,
                max_pages=max_pages,
                open_only=open_only,
                field_filter=field_filter,
                bid_method_filter=bid_method_filter,
                match_type="any",
            )
            for bids_for_phrase in phrase_map.values():
                for b in bids_for_phrase:
                    by_code.setdefault(b.tbmt_code, b)
            bids = list(by_code.values())
            _cache_put(cache_key, bids)

        # ── Client-side keyword filtering ─────────────────────────────────────
        if not uniq:
            # Filter-only — all crawled bids qualify
            matched = [(bid, []) for bid in bids]
        else:
            cfg = KeywordsConfig(
                groups=[KeywordGroup(name="Search", require=require, keywords=phrases)]
            )
            for bid in bids:
                ok, ks, _ = match_bid(
                    bid,
                    cfg,
                    strict_keywords=secrets.interactive_search_strict_keywords,
                )
                if ok:
                    matched.append((bid, ks))

            # ── Relevance sort (most relevant first) ──────────────────────────
            if len(matched) > 1 and len(uniq) > 1:
                matched.sort(
                    key=lambda t: _relevance_score(t[0], uniq, secrets.interactive_search_strict_keywords),
                    reverse=True,
                )

        total = len(matched)
        n_from_portal = len(bids)
        max_slots = max_pages * secrets.crawl_page_size * max(1, len(uniq))
        to_emit = matched[:cap]

        for i, (bid, ks) in enumerate(to_emit):
            if i > 0 and i % TELEGRAM_RATE_BATCH == 0:
                time.sleep(TELEGRAM_BATCH_SLEEP_S)
            body = format_bid_message(bid, ks)
            if send_message(token, cid, body, reply_markup=chitiet_button(bid.tbmt_code)):
                sent += 1

        # ── Tạo summary ───────────────────────────────────────────────────────
        is_filter_only = not uniq
        mode_display = "duyệt lọc" if is_filter_only else mode_label
        and_hint = (
            f" Lưu ý: chế độ AND — bid phải chứa đồng thời tất cả {len(phrases)} điều kiện.\n"
            "Nếu kết quả 0, thử bỏ bớt một điều kiện hoặc dùng dấu phẩy (OR mode)."
            if require == "all" and not is_filter_only else ""
        )

        closed_note = " (bao gồm gói đã đóng thầu)" if include_closed else ""
        filter_suffix = f"\nBộ lọc: {', '.join(filter_parts)}" if filter_parts else ""
        cache_note = " ⚡ (từ cache)" if cached_bids is not None else ""

        if total == 0:
            no_result_hint = (
                "\nNếu tra theo tên cơ quan, thử cụm ngắn hơn (vd. Lâm Đồng, Công an). "
            )
            if not include_closed and not is_filter_only:
                no_result_hint += (
                    "Nếu trên cổng có nhưng ở mục đã đóng thầu/rút thì bot sẽ không liệt kê — "
                    "thử /timtat để tìm cả gói đã đóng. "
                )
            if filter_parts:
                no_result_hint += "Thử bỏ bớt bộ lọc để mở rộng kết quả. "
            no_result_hint += "Có thể tăng INTERACTIVE_CRAWL_MAX_PAGES trong .env."
            if is_filter_only:
                summary = (
                    f"Không thấy gói TBMT nào{closed_note} với bộ lọc đã chọn."
                    + no_result_hint + filter_suffix
                )
            else:
                summary = (
                    f"Không thấy gói TBMT nào khớp [{mode_display}]{closed_note} trong các trang đã tìm."
                    + and_hint + no_result_hint + filter_suffix
                )
            if n_from_portal > 0 and not is_filter_only:
                summary += (
                    f"\nLưu ý: đã nhận {n_from_portal} gói từ API nhưng không gói nào qua lọc "
                    f"(strict={secrets.interactive_search_strict_keywords})."
                )
        elif total > sent:
            if is_filter_only:
                summary = (
                    f"Tìm thấy {total} gói{closed_note}{cache_note}. Đã gửi {sent} tin (giới hạn {cap}/lần)."
                    + filter_suffix
                )
            else:
                summary = (
                    f"Tìm thấy {total} gói [{mode_display}]{closed_note}{cache_note}. Đã gửi {sent} tin (giới hạn {cap}/lần).\n"
                    "Thu hẹp từ khóa hoặc xem chi tiết trên muasamcong để tiếp tục lọc."
                    + filter_suffix
                )
            if n_from_portal > total and not is_filter_only:
                summary += f" (Đã cào ~{max_slots} ô; API trả {n_from_portal} gói, lọc còn {total}.)"
        else:
            if is_filter_only:
                summary = f"Tìm thấy {total} gói{closed_note}{cache_note}. Đã gửi {sent} tin." + filter_suffix
            else:
                summary = f"Tìm thấy {total} gói [{mode_display}]{closed_note}{cache_note}. Đã gửi {sent} tin." + filter_suffix
                if total <= 3 and not include_closed and not is_filter_only:
                    # Gợi ý thử /timtat và AND mode khi kết quả ít
                    kw_sample = " ".join(uniq[:2]) if uniq else ""
                    summary += (
                        f"\n💡 Trên cổng thấy nhiều hơn? Bot chỉ tìm gói đang mở thầu — "
                        f"thử /timtat {kw_sample} để tìm cả gói đã đóng."
                    )
                    if len(uniq) == 1 and " " in (uniq[0] if uniq else ""):
                        words = (uniq[0] if uniq else "").split()
                        if len(words) >= 3:
                            # Gợi ý AND mode với từ khóa ngắn hơn
                            short = " & ".join(words[i] for i in [0, -1])
                            summary += f"\nHoặc thử: {short} (tìm rộng hơn theo 2 từ khóa độc lập)."
                if total == 1 and n_from_portal > 1 and not (total <= 3 and not include_closed and not is_filter_only):
                    summary += (
                        f"\nLưu ý: đã cào ~{max_slots} ô; API trả {n_from_portal} gói, "
                        f"chỉ 1 gói đáp ứng đủ điều kiện lọc."
                    )

        return sent, total, summary
    finally:
        crawler.close()


__all__ = [
    "parse_keyword_phrases",
    "parse_search_query",
    "run_interactive_keyword_search",
    "_make_cache_key",
    "_cache_get",
    "_cache_put",
]
