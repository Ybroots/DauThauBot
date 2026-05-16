from __future__ import annotations

from loguru import logger

from .config import PROJECT_ROOT, Secrets, load_keywords
from .crawler import BlockedException, MuasamcongCrawler
from .filter import matches_keywords
from .formatter import format_bid_message
from .storage import init_db, mark_seen, was_sent
from .telegram import send_to_chats

_consecutive_empty = 0
_consecutive_blocks = 0


def _setup_logging(level: str) -> None:
    log_dir = PROJECT_ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    logger.add(
        str(log_dir / "tracker_{time:YYYYMMDD}.log"),
        rotation="1 day",
        retention="30 days",
        level=level,
    )


def _maybe_alert_block(secrets: Secrets, hours: float) -> None:
    global _consecutive_blocks
    if _consecutive_blocks < 3:
        return
    admin = secrets.admin_chat_id
    if not admin:
        return
    text = (
        f"⚠️ Tracker bị server chặn lần thứ {_consecutive_blocks}/24h\n"
        f"Cooldown hiện tại: {hours:.0f}h\n"
        "Cân nhắc: (a) tăng POLL_INTERVAL_MINUTES, "
        "(b) cài playwright, (c) đổi IP/VPS."
    )
    send_to_chats(secrets.telegram_bot_token, [admin], text)


def run_once() -> None:
    global _consecutive_empty, _consecutive_blocks

    secrets = Secrets()
    keywords_cfg = load_keywords()
    init_db()

    crawler = MuasamcongCrawler(
        page_size=secrets.crawl_page_size,
        use_playwright=secrets.use_playwright,
        playwright_headless=secrets.playwright_headless,
        playwright_channel=secrets.playwright_channel,
    )
    sent_total = 0
    try:
        logger.info(
            "fetch_start (max {} pages × {} = ~{} bids)",
            secrets.crawl_max_pages,
            secrets.crawl_page_size,
            secrets.crawl_max_bids,
        )
        bids = crawler.fetch_recent_bids(max_pages=secrets.crawl_max_pages)
        logger.info("Fetched {} bids", len(bids))

        if len(bids) == 0:
            _consecutive_empty += 1
            if _consecutive_empty >= 3:
                warn = "⚠️ Tracker: 3 lần liên tiếp không lấy được gói thầu — có thể bị chặn hoặc API lỗi."
                logger.warning(warn)
                if secrets.admin_chat_id:
                    send_to_chats(secrets.telegram_bot_token, [secrets.admin_chat_id], warn)
        else:
            _consecutive_empty = 0

        msg_batch = 0
        for bid in bids:
            if was_sent(bid.tbmt_code):
                continue

            matched, kw = matches_keywords(bid, keywords_cfg)
            if not matched:
                mark_seen(bid.tbmt_code, bid.title, sent=False)
                continue

            text = format_bid_message(bid, kw)
            success_count = send_to_chats(
                secrets.telegram_bot_token,
                secrets.chat_id_list,
                text,
                sent_count=msg_batch,
            )
            msg_batch += len(secrets.chat_id_list)

            mark_seen(bid.tbmt_code, bid.title, sent=(success_count > 0))
            if success_count > 0:
                sent_total += 1
                logger.info("telegram_send: {} | matched: {}", bid.tbmt_code, kw)

        logger.info("Done. New bids sent: {}", sent_total)
        _consecutive_blocks = 0
    except BlockedException:
        _consecutive_blocks += 1
        raise
    finally:
        crawler.close()


def main() -> None:
    secrets = Secrets()
    _setup_logging(secrets.log_level)
    run_once()


if __name__ == "__main__":
    main()
