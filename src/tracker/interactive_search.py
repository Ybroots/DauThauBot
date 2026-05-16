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
) -> tuple[int, int, str]:
    """Cào theo crawl_max_pages của .env; gửi tối đa N tin HTML tới một chat.

    mode="any"  → OR: bid khớp ít nhất 1 phrase (hành vi mặc định)
    mode="all"  → AND: bid phải chứa TẤT CẢ phrases mới được gửi

    Chiến lược ES:
    • OR mode:  query TẤT CẢ phrases lên ES (union), match_bid lọc OR client-side.
    • AND mode: query TẤT CẢ phrases lên ES (union), match_bid lọc AND client-side.
      Union + AND client-side đảm bảo gom đủ candidate từ nhiều field khác nhau
      (vd. "công an" trong investorName, "camera" trong bidName).

    Returns (sent_count, total_matching, summary_plain).
    Không đụng vào SQLite.
    """
    require: str = mode if mode in ("all", "any") else "any"
    cfg = KeywordsConfig(
        groups=[KeywordGroup(name="Search", require=require, keywords=phrases)]
    )
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
        logger.info(
            "interactive_fetch mode={} ({} pages × {}) per phrase, phrases={}",
            require,
            secrets.interactive_fetch_max_pages,
            secrets.crawl_page_size,
            uniq,
        )

        # Query từng phrase lên ES — lấy UNION để có đủ candidates
        # Client-side match_bid() sẽ áp dụng AND/OR logic chính xác sau đó
        by_code: dict[str, Bid] = {}
        for phrase in uniq:
            part = crawler.fetch_recent_bids(
                max_pages=secrets.interactive_fetch_max_pages,
                server_keyword=phrase,
            )
            for b in part:
                by_code.setdefault(b.tbmt_code, b)
        bids = list(by_code.values())

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
        and_hint = (
            f" Lưu ý: chế độ AND — bid phải chứa đồng thời tất cả {len(phrases)} điều kiện.\n"
            "Nếu kết quả 0, thử bỏ bớt một điều kiện hoặc dùng dấu phẩy (OR mode)."
            if require == "all" else ""
        )

        if total == 0:
            summary = (
                f"Không thấy gói TBMT nào khớp [{mode_label}] trong các trang đã tìm."
                + and_hint
                + "\nNếu tra theo tên cơ quan, thử cụm ngắn hơn (vd. Lâm Đồng, Công an). "
                "Nếu trên cổng có nhưng ở mục đã đóng thầu/rút thì bot sẽ không liệt kê. "
                "Có thể tăng CRAWL_MAX_PAGES hoặc INTERACTIVE_CRAWL_MAX_PAGES trong .env."
            )
            if n_from_portal > 0:
                summary += (
                    f"\nLưu ý: đã nhận {n_from_portal} gói từ API nhưng không gói nào qua lọc "
                    f"(strict={secrets.interactive_search_strict_keywords})."
                )
        elif total > sent:
            summary = (
                f"Tìm thấy {total} gói [{mode_label}]. Đã gửi {sent} tin (giới hạn {cap}/lần).\n"
                "Thu hẹp từ khóa hoặc xem chi tiết trên muasamcong để tiếp tục lọc."
            )
            if n_from_portal > total:
                summary += f" (Đã cào tối đa ~{max_slots} ô kết quả; API trả {n_from_portal} gói khác nhau.)"
        else:
            summary = f"Tìm thấy {total} gói [{mode_label}]. Đã gửi {sent} tin."
            if total == 1 and n_from_portal > 1:
                summary += (
                    f"\nLưu ý: bot đã gom tối đa {secrets.interactive_fetch_max_pages} trang ES × "
                    f"{secrets.crawl_page_size} gói cho mỗi cụm từ (~{max_slots} ô); "
                    f"API trả {n_from_portal} gói khác nhau, chỉ 1 gói đáp ứng đủ điều kiện lọc."
                )
            elif total == 1 and n_from_portal == 1:
                summary += (
                    f" Trong phạm vi đã cào ({secrets.interactive_fetch_max_pages} trang ES/cụm), "
                    "cổng chỉ trả đúng 1 gói TBMT mở thầu khớp tìm kiếm."
                )

        return sent, total, summary
    finally:
        crawler.close()


__all__ = ["parse_keyword_phrases", "parse_search_query", "run_interactive_keyword_search"]
