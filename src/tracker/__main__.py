from __future__ import annotations

import random
import time

from loguru import logger

from .config import PROJECT_ROOT, Secrets, load_keywords_yaml
from .crawler import BlockedException, MuasamcongCrawler
from .filter import match_bid
from .formatter import format_bid_message
from .models import Bid
from .storage import init_db, load_groups_from_db, mark_seen, seed_groups_from_yaml, was_sent
from .telegram import chitiet_button, send_to_chats
from .tender_store import (
    log_crawl_finish,
    log_crawl_start,
    upsert_tenders_bulk,
)

_consecutive_empty = 0
_consecutive_blocks = 0


def _tracker_keyword_strings(keywords_cfg) -> list[str]:
    """Trích xuất từ khóa để gửi lên ES server-side.

    Chiến lược giảm số vòng Playwright và tránh phrase-match quá chặt:
    • AND group (require=all): chỉ lấy TỪ KHÓA ĐẦU TIÊN của mỗi group để query ES —
      client-side match_bid() kiểm tra đủ điều kiện AND sau khi gộp kết quả.
      Lý do: querying cả N từ = N vòng Playwright; chỉ cần 1 từ "mồi" để lấy candidates.
    • OR group (require=any): lấy TẤT CẢ từ khóa vì mỗi từ có thể match bộ gói khác nhau.
    • Keyword từ ≥ 3 từ: cắt xuống còn 2 từ đầu — ES matchType all-1 với cụm dài
      rất chặt (phải có đủ từng chữ), truncate giúp ES trả về rộng hơn;
      match_bid() vẫn kiểm tra cụm đầy đủ phía client.
    """
    seen: set[str] = set()
    out: list[str] = []
    for group in keywords_cfg.groups:
        if group.require == "all":
            # AND group: chỉ cần 1 keyword để kéo candidates từ ES
            candidates = [group.keywords[0]] if group.keywords else []
        else:
            # OR group: cần query từng keyword riêng để phủ đủ
            candidates = group.keywords

        for k in candidates:
            k = str(k).strip()
            if not k:
                continue
            # Cắt phrase dài → 2 từ đầu để ES không bị phrase-match quá chặt
            words = k.split()
            es_kw = " ".join(words[:2]) if len(words) > 2 else k
            if es_kw not in seen:
                out.append(es_kw)
                seen.add(es_kw)
    return out


def _collect_bids_crawl(
    crawler: MuasamcongCrawler,
    secrets: Secrets,
    keywords_cfg,
) -> list[Bid]:
    """TBMT: luồng mới toàn cổng, hoặc (mặc định) tra từng từ khóa phía server rồi gộp + dedupe."""
    kws = _tracker_keyword_strings(keywords_cfg)
    if not kws or not secrets.crawl_per_keyword:
        logger.info("Cào: một luồng TBMT mới (lọc từ khóa trên máy nếu có)")
        return crawler.fetch_recent_bids(max_pages=secrets.crawl_max_pages)

    logger.info(
        "Cào: {} từ khóa phía server × tối đa {} trang/gộp dedupe theo mã TBMT",
        len(kws),
        secrets.crawl_max_pages,
    )
    merged: dict[str, Bid] = {}
    for i, kw in enumerate(kws):
        if i > 0 and secrets.crawl_keyword_gap_max_seconds > 0:
            lo = float(secrets.crawl_keyword_gap_min_seconds)
            hi = float(secrets.crawl_keyword_gap_max_seconds)
            gap = random.uniform(lo, hi)
            logger.info("Nghỉ {:.1f}s trước từ khóa tiếp theo…", gap)
            time.sleep(gap)
        logger.info("Từ khóa [{}/{}]: {!r}", i + 1, len(kws), kw)
        chunk = crawler.fetch_recent_bids(
            max_pages=secrets.crawl_max_pages,
            server_keyword=kw,
        )
        for b in chunk:
            merged[b.tbmt_code] = b
    out = list(merged.values())
    logger.info("Gộp xong: {} gói (sau dedupe)", len(out))
    return out


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
    init_db()
    seed_groups_from_yaml(load_keywords_yaml())
    keywords_cfg = load_groups_from_db()

    kw_strings = _tracker_keyword_strings(keywords_cfg)
    log_id = log_crawl_start("cron", keywords=kw_strings)

    crawler = MuasamcongCrawler(
        page_size=secrets.crawl_page_size,
        use_playwright=secrets.use_playwright,
        playwright_headless=secrets.playwright_headless,
        playwright_channel=secrets.playwright_channel,
    )
    sent_total = 0
    total_new = total_updated = total_failed = 0
    crawl_status = "success"
    error_msg: str | None = None

    try:
        logger.info(
            "fetch_start page_size={} crawl_max_pages={} (~{} kết quả mỗi nguồn)",
            secrets.crawl_page_size,
            secrets.crawl_max_pages,
            secrets.crawl_max_bids,
        )
        bids = _collect_bids_crawl(crawler, secrets, keywords_cfg)
        logger.info("Fetched {} bids (sau gộp)", len(bids))

        if len(bids) == 0:
            _consecutive_empty += 1
            if _consecutive_empty >= 3:
                warn = "⚠️ Tracker: 3 lần liên tiếp không lấy được gói thầu — có thể bị chặn hoặc API lỗi."
                logger.warning(warn)
                if secrets.admin_chat_id:
                    send_to_chats(secrets.telegram_bot_token, [secrets.admin_chat_id], warn)
            crawl_status = "partial_failed" if _consecutive_empty >= 3 else "success"
        else:
            _consecutive_empty = 0

        # ── Upsert TẤT CẢ bid vào catalog tenders (DB-first search cho /tim) ──
        try:
            # Build keywords_matched map: bid → danh sách kw matched
            kw_map: dict[str, list[str]] = {}
            for bid in bids:
                matched_check, kw, _ = match_bid(bid, keywords_cfg)
                if matched_check:
                    kw_map[bid.tbmt_code] = kw
            total_new, total_updated = upsert_tenders_bulk(bids, keywords_matched_map=kw_map)
            logger.info(
                "tenders catalog: {} new, {} updated (total {} bids)",
                total_new, total_updated, len(bids),
            )
        except Exception:
            logger.exception("upsert_tenders_bulk failed — continuing")

        msg_batch = 0
        for bid in bids:
            if was_sent(bid.tbmt_code):
                continue

            matched, kw, group_name = match_bid(bid, keywords_cfg)
            if not matched:
                mark_seen(bid.tbmt_code, bid.title, sent=False, bid=bid)
                continue

            text = format_bid_message(bid, kw)
            try:
                success_count = send_to_chats(
                    secrets.telegram_bot_token,
                    secrets.chat_id_list,
                    text,
                    sent_count=msg_batch,
                    reply_markup=chitiet_button(bid.tbmt_code),
                )
            except Exception:
                logger.exception("send_to_chats failed for {}", bid.tbmt_code)
                total_failed += 1
                success_count = 0

            msg_batch += len(secrets.chat_id_list)
            mark_seen(bid.tbmt_code, bid.title, sent=(success_count > 0), bid=bid)
            if success_count > 0:
                sent_total += 1
                logger.info(
                    "telegram_send: {} | group: {} | matched: {}",
                    bid.tbmt_code,
                    group_name,
                    kw,
                )

        if total_failed > 0:
            crawl_status = "partial_failed"
        logger.info("Done. New bids sent: {}", sent_total)
        _consecutive_blocks = 0
    except BlockedException as e:
        _consecutive_blocks += 1
        crawl_status = "failed"
        error_msg = f"BlockedException HTTP {e.status_code}"
        raise
    except Exception as e:
        crawl_status = "failed"
        error_msg = str(e)[:500]
        raise
    finally:
        crawler.close()
        log_crawl_finish(
            log_id,
            status=crawl_status,
            total_found=len(bids) if "bids" in dir() else 0,  # type: ignore[name-defined]
            total_new=total_new,
            total_updated=total_updated,
            total_sent=sent_total,
            total_failed=total_failed,
            error_message=error_msg,
        )


def main() -> None:
    _setup_logging(Secrets().log_level)
    run_once()


if __name__ == "__main__":
    main()
