from __future__ import annotations

import time

import httpx
from loguru import logger

TELEGRAM_RATE_BATCH = 20
TELEGRAM_BATCH_SLEEP_S = 2.0


def send_message(bot_token: str, chat_id: str, text: str) -> bool:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        r = httpx.post(url, json=payload, timeout=15)
        if r.status_code == 200:
            return True
        logger.error("Telegram error {}: {}", r.status_code, r.text)
        return False
    except httpx.HTTPError as e:
        logger.exception("Telegram send failed: {}", e)
        return False


def send_to_chats(
    bot_token: str,
    chat_ids: list[str],
    text: str,
    *,
    sent_count: int = 0,
) -> int:
    success = 0
    for i, chat_id in enumerate(chat_ids):
        if sent_count + i > 0 and (sent_count + i) % TELEGRAM_RATE_BATCH == 0:
            time.sleep(TELEGRAM_BATCH_SLEEP_S)
        if send_message(bot_token, chat_id, text):
            success += 1
    return success
