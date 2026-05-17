"""Tra cứu TBMT nhanh theo từ khóa từ Telegram — không sửa seen.db."""

from __future__ import annotations

import re
import time

from loguru import logger

from .config import KeywordGroup, KeywordsConfig, Secrets
from .crawler import MuasamcongCrawler
from .filter import match_bid
from .models import Bid
from .formatter import format_bid_message
from .telegram import TELEGRAM_BATCH_SLEEP_S, TELEGRAM_RATE_BATCH, send_message


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
    """Cào theo crawl_max_pages của .env; gửi tối đa N tin HTML tới một chat.

    mode="any"        → OR: bid khớp ít nhất 1 phrase (hành vi mặc định)
    mode="all"        → AND: bid phải chứa TẤT CẢ phrases mới được gửi
    include_closed    → True: tìm cả gói đã đóng thầu (/timtat)
                        False: chỉ gói còn mở (/tim, mặc định)
    field_filter      → Lọc lĩnh vực ES phía server, vd. ["HH", "XL"]. None = tất cả.
    bid_method_filter → 1 = qua mạng, 0 = không qua mạng, None = tất cả.

    Chiến lược ES:
    • OR mode:  query TẤT CẢ phrases lên ES (union), match_bid lọc OR client-side.
    • AND mode: query TẤT CẢ phrases lên ES (union), match_bid lọc AND client-side.
      Union + AND client-side đảm bảo gom đủ candidate từ nhiều field khác nhau
      (vd. "công an" trong investorName, "camera" trong bidName).
    • Filter-only (phrases rỗng): duyệt tất cả TBMT với bộ lọc server, không lọc từ khóa.

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
        filter_note = f" | bộ lọc: {', '.join(filter_parts)}" if filter_parts else ""

        logger.info(
            "interactive_fetch mode={} ({} pages × {}) per phrase, phrases={}, filters={}",
            require,
            secrets.interactive_fetch_max_pages,
            secrets.crawl_page_size,
            uniq,
            filter_parts or "none",
        )

        open_only = not include_closed
        by_code: dict[str, Bid] = {}

        if not uniq:
            # Filter-only mode — browse all TBMT with server-side filters, no keyword
            logger.info("interactive_fetch: filter-only mode (no keywords), browsing all TBMT")
            part = crawler.fetch_recent_bids(
                max_pages=secrets.interactive_fetch_max_pages,
                server_keyword=None,
                open_only=open_only,
                field_filter=field_filter,
                bid_method_filter=bid_method_filter,
            )
            for b in part:
                by_code.setdefault(b.tbmt_code, b)
            bids = list(by_code.values())
            # No client-side keyword filtering needed — all bids are candidates
            matched = [(bid, []) for bid in bids]
        else:
            # Keyword mode — Query từng phrase lên ES, lấy UNION để có đủ candidates
            # Client-side match_bid() sẽ áp dụng AND/OR logic chính xác sau đó
            for phrase in uniq:
                part = crawler.fetch_recent_bids(
                    max_pages=secrets.interactive_fetch_max_pages,
                    server_keyword=phrase,
                    open_only=open_only,
                    field_filter=field_filter,
                    bid_method_filter=bid_method_filter,
                )
                for b in part:
                    by_code.setdefault(b.tbmt_code, b)
            bids = list(by_code.values())

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

        total = len(matched)
        n_from_portal = len(bids)
        max_slots = secrets.interactive_fetch_max_pages * secrets.crawl_page_size * max(1, len(uniq))
        to_emit = matched[:cap]

        for i, (bid, ks) in enumerate(to_emit):
            if i > 0 and i % TELEGRAM_RATE_BATCH == 0:
                time.sleep(TELEGRAM_BATCH_SLEEP_S)
            body = format_bid_message(bid, ks)
            if send_message(token, cid, body):
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
            no_result_hint += "Có thể tăng CRAWL_MAX_PAGES hoặc INTERACTIVE_CRAWL_MAX_PAGES trong .env."
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
                    f"Tìm thấy {total} gói{closed_note}. Đã gửi {sent} tin (giới hạn {cap}/lần)."
                    + filter_suffix
                )
            else:
                summary = (
                    f"Tìm thấy {total} gói [{mode_display}]{closed_note}. Đã gửi {sent} tin (giới hạn {cap}/lần).\n"
                    "Thu hẹp từ khóa hoặc xem chi tiết trên muasamcong để tiếp tục lọc."
                    + filter_suffix
                )
            if n_from_portal > total and not is_filter_only:
                summary += f" (Đã cào tối đa ~{max_slots} ô kết quả; API trả {n_from_portal} gói khác nhau.)"
        else:
            if is_filter_only:
                summary = f"Tìm thấy {total} gói{closed_note}. Đã gửi {sent} tin." + filter_suffix
            else:
                summary = f"Tìm thấy {total} gói [{mode_display}]{closed_note}. Đã gửi {sent} tin." + filter_suffix
                if total == 1 and n_from_portal > 1:
                    summary += (
                        f"\nLưu ý: bot đã gom tối đa {secrets.interactive_fetch_max_pages} trang ES × "
                        f"{secrets.crawl_page_size} gói cho mỗi cụm từ (~{max_slots} ô); "
                        f"API trả {n_from_portal} gói khác nhau, chỉ 1 gói đáp ứng đủ điều kiện lọc."
                    )
                elif total == 1 and n_from_portal == 1:
                    scope_desc = scope_label
                    summary += (
                        f" Trong phạm vi đã cào ({secrets.interactive_fetch_max_pages} trang ES/cụm), "
                        f"cổng chỉ trả đúng 1 gói TBMT ({scope_desc}) khớp tìm kiếm."
                    )

        return sent, total, summary
    finally:
        crawler.close()


__all__ = ["parse_keyword_phrases", "parse_search_query", "run_interactive_keyword_search"]
