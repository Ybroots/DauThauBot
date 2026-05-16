"""Tra cứu TBMT nhanh theo từ khóa từ Telegram — không sửa seen.db."""

from __future__ import annotations

import re
import time

from loguru import logger

from .config import KeywordsConfig, Secrets
from .crawler import MuasamcongCrawler
from .filter import matches_keywords
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


def run_interactive_keyword_search(
    secrets: Secrets,
    phrases: list[str],
    *,
    target_chat_id: str | int,
) -> tuple[int, int, str]:
    """Cào theo crawl_max_pages của .env; gửi tối đa N tin HTML tới một chat.

    Returns (sent_count, total_matching, summary_plain).
    Không đụng vào SQLite."""
    cfg = KeywordsConfig(keywords=phrases, locations=[], fields=[], min_budget_vnd=None)
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

        logger.info(
            "interactive_fetch ({} pages × {}) per phrase, server keyWord, phrases={}",
            secrets.interactive_fetch_max_pages,
            secrets.crawl_page_size,
            uniq,
        )

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
            ok, ks = matches_keywords(
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

        if total == 0:
            summary = (
                "Không thấy gói TBMT nào (chưa hết thời đóng thầu) khớp từ khóa trong các trang đã tìm. "
                "Nếu tra theo tên cơ quan, thử cụm ngắn hơn (vd. Lâm Đồng, Công an). "
                "Nếu trên cổng có nhưng ở mục đã đóng thầu/rút thì bot sẽ không liệt kê. "
                "Có thể tăng CRAWL_MAX_PAGES hoặc INTERACTIVE_CRAWL_MAX_PAGES trong .env."
            )
            if n_from_portal > 0:
                summary += (
                    f" Lưu ý: đã nhận {n_from_portal} gói từ API nhưng không gói nào khớp đủ từ khóa sau lọc bot "
                    f"(strict={secrets.interactive_search_strict_keywords})."
                )
        elif total > sent:
            summary = (
                f"Tìm thấy {total} gói khớp. Đã gửi {sent} tin (giới hạn {cap}/lần). "
                "Thu hẹp từ khóa hoặc xem chi tiết trên muasamcong để tiếp tục lọc."
            )
            if n_from_portal > total:
                summary += f" (Đã cào tối đa ~{max_slots} ô kết quả; API trả {n_from_portal} gói khác nhau.)"
        else:
            summary = f"Tìm thấy {total} gói khớp. Đã gửi {sent} tin."
            if total == 1 and n_from_portal > 1:
                summary += (
                    " Lưu ý: \"1\" ở đây là một gói thầu khớp từ khóa sau lọc, không phải bot chỉ cào một trang. "
                    f"Bot đã gom tối đa {secrets.interactive_fetch_max_pages} trang ES × {secrets.crawl_page_size} "
                    f"gói cho mỗi cụm từ (tối đa ~{max_slots} ô kết quả); API trả {n_from_portal} gói khác nhau, "
                    "chỉ 1 gói có đủ cụm từ trong tên/chủ đầu tư (TBMT chưa đóng thầu). "
                    "Thử OR: /tim Công an | Lâm Đồng hoặc INTERACTIVE_SEARCH_STRICT_KEYWORDS=false trong .env nếu muốn lỏng hơn."
                )
            elif total == 1 and n_from_portal == 1:
                summary += (
                    f" Trong phạm vi đã cào ({secrets.interactive_fetch_max_pages} trang ES/cụm), "
                    "cổng chỉ trả đúng 1 gói TBMT mở thầu khớp tìm kiếm — có thể mở rộng từ khóa hoặc tăng số trang trong .env."
                )

        return sent, total, summary
    finally:
        crawler.close()


__all__ = ["parse_keyword_phrases", "run_interactive_keyword_search"]
