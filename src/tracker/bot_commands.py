"""Telegram: lệnh hỗ trợ + tra TBMT nhanh (/tim) qua long polling."""

from __future__ import annotations

import time
from typing import Optional

import httpx
from loguru import logger
from tenacity import RetryError

from .config import Secrets, load_keywords
from .crawler import BlockedException
from .interactive_search import parse_keyword_phrases, run_interactive_keyword_search
from .storage import count_sent_since, init_db
from .__main__ import run_once

POLL_TIMEOUT = 30

# Đợi user gửi từ khóa trong tin tiếp theo (scope theo Chat+User để không lẫn nhau trong nhóm)
_await_keyword: dict[str, bool] = {}
_last_search_ts: dict[str, float] = {}


def _state_key(chat_id: str | int, user_id: int) -> str:
    return f"{chat_id}:{user_id}"


def _get_updates(token: str, offset: int | None) -> list[dict]:
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    params: dict = {"timeout": POLL_TIMEOUT}
    if offset is not None:
        params["offset"] = offset
    r = httpx.get(url, params=params, timeout=POLL_TIMEOUT + 10)
    r.raise_for_status()
    return r.json().get("result") or []


def _reply(token: str, chat_id: int | str, text: str, *, parse_html: bool = False) -> None:
    payload: dict = {"chat_id": chat_id, "text": text}
    if parse_html:
        payload["parse_mode"] = "HTML"
    httpx.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json=payload,
        timeout=120,
    )


def _cooldown_ok(secrets: Secrets, chat_key: str) -> tuple[bool, int]:
    gap = secrets.interactive_search_cooldown_seconds
    if gap <= 0:
        return True, 0
    last = _last_search_ts.get(chat_key, 0.0)
    now = time.time()
    elapsed = now - last
    if elapsed < gap:
        return False, max(1, int(gap - elapsed))
    return True, 0


def _mark_search_done(chat_key: str) -> None:
    _last_search_ts[chat_key] = time.time()


def _cmd_and_rest(text: str) -> tuple[str, str]:
    raw = text.strip()
    if not raw.startswith("/"):
        return "", raw
    chunk = raw.split(maxsplit=1)
    cmd0 = chunk[0]
    at = cmd0.find("@")
    if at > 0:
        cmd0 = cmd0[:at]
    rest = chunk[1] if len(chunk) > 1 else ""
    return cmd0.lower(), rest.strip()


def _execute_search(
    secrets: Secrets,
    phrases: list[str],
    target_chat_id: str | int,
    chat_scope_key: str,
) -> None:
    phrases = [p for p in phrases if p.strip()]
    if not phrases:
        _reply(secrets.telegram_bot_token, target_chat_id, "Chưa có từ khóa. Ví dụ: camera, lâm đồng")
        return
    cd_ok, secs = _cooldown_ok(secrets, chat_scope_key)
    if not cd_ok:
        _reply(
            secrets.telegram_bot_token,
            target_chat_id,
            f"Chờ thêm ~{secs}s rồi tra tiếp (tránh spam cào nhiều lần).",
        )
        return
    _reply(
        secrets.telegram_bot_token,
        target_chat_id,
        "Đang tra trên Muasamcong (Playwright có thể mất 30–90 giây), vui lòng chờ…",
    )
    try:
        sent, total, summary = run_interactive_keyword_search(
            secrets,
            phrases,
            target_chat_id=target_chat_id,
        )
        logger.info(
            "interactive_search done chat={} sent={} matched={}",
            target_chat_id,
            sent,
            total,
        )
        _mark_search_done(chat_scope_key)
        _reply(secrets.telegram_bot_token, target_chat_id, summary)
    except BlockedException as e:
        _reply(
            secrets.telegram_bot_token,
            target_chat_id,
            f"Trang chủ thầu tạm chặn hoặc lỗi mạng (HTTP {e.status_code}). Thử sau vài tiếng.",
        )
    except RuntimeError as e:
        msg = str(e)
        if "reCAPTCHA" in msg or "invalid site key" in msg.lower():
            logger.warning("interactive search reCAPTCHA/runtime: {}", msg)
            _reply(secrets.telegram_bot_token, target_chat_id, msg[:900])
            return
        raise
    except RetryError:
        logger.exception("interactive search failed (hết lần thử tenacity)")
        _reply(
            secrets.telegram_bot_token,
            target_chat_id,
            "Lỗi cào sau vài lần thử (thường do reCAPTCHA / site key). "
            "Cập nhật bản Tool mới nhất; thử PLAYWRIGHT_HEADLESS=false hoặc PLAYWRIGHT_CHANNEL=chrome trong .env. "
            "Chi tiết: file logs/tracker_*.log",
        )
    except Exception:
        logger.exception("interactive search failed")
        _reply(
            secrets.telegram_bot_token,
            target_chat_id,
            "Lỗi khi cào hoặc gửi Telegram. Xem logs/ trên máy chạy bot.",
        )


def HELP_VI() -> str:
    return (
        "Luồng cron: tracker trên máy + config/keywords.yaml.\n\n"
        "Tra nhanh — một tin là bot chạy (không bắt buộc 2 bước):\n"
        "• /tim camera — khuyến nghị trong nhóm\n"
        "• /tim camera | máy chủ — OR nhiều cụm\n"
        "• /tim — nếu gõ trống: bot chờ tin kế tiếp (chat riêng vẫn nên dùng /tim camera cho nhanh)\n\n"
        "Chat riêng: gõ một dòng từ khóa (VD: camera) là tra luôn, không cần /tim.\n"
        "Trong nhóm: bắt /tim ... hoặc bật BOT_GROUP_FREEWORD=true để gõ thẳng như chat riêng.\n\n"
        "Lọc kết quả: mặc định từ đơn phải khớp cả từ (tránh khớp nhầm). Tắt: INTERACTIVE_SEARCH_STRICT_KEYWORDS=false.\n\n"
        "Khác: /keywords  /stats  /test  /hủy"
    )


def handle_slash(full_text: str, secrets: Secrets, _chat_type: Optional[str]) -> Optional[str]:
    """Trả text trả lời (plain) hoặc None nếu đã xử lý không cần gửi thêm."""
    cmd, rest = _cmd_and_rest(full_text)

    if cmd in ("/start", "/help", "/gioithieu"):
        return HELP_VI()

    if cmd == "/keywords":
        cfg = load_keywords()
        lines = "\n".join(f"• {k}" for k in cfg.keywords) or "(trống)"
        return (
            "Từ khóa cron (keywords.yaml):\n"
            + lines
            + "\n\nChỉnh file config/keywords.yaml trên máy host."
        )

    if cmd == "/stats":
        init_db()
        n = count_sent_since(7)
        return f"Đã gửi {n} gói thầu (đánh dấu đã tin) trong 7 ngày qua."

    if cmd == "/test":
        run_once()
        return "Đã chạy 1 chu kỳ tracker (cron). Xem logs/ trên máy host."

    return None


def process_message(secrets: Secrets, msg: dict) -> None:
    chat = msg.get("chat") or {}
    frm = msg.get("from") or {}
    text = msg.get("text") or ""

    chat_id = chat.get("id")
    chat_type = chat.get("type")
    uid = frm.get("id")
    bot_token = secrets.telegram_bot_token

    if chat_id is None or uid is None or not isinstance(text, str):
        return

    cid_s = str(chat_id)
    ukey = _state_key(chat_id, int(uid))

    cmd, rest = _cmd_and_rest(text)

    if cmd in ("/huy", "/hủy", "/cancel"):
        _await_keyword.pop(ukey, None)
        _reply(bot_token, chat_id, "Đã hủy bước nhập từ khóa.")
        return

    if cmd in ("/tim", "/timkiem", "/search"):
        phrases = parse_keyword_phrases(rest)
        if phrases:
            _await_keyword.pop(ukey, None)
            _execute_search(secrets, phrases, chat_id, cid_s)
            return
        _await_keyword[ukey] = True
        hint = (
            "Nhanh nhất: gửi lại một tin /tim kèm từ khóa, ví dụ: /tim camera\n\n"
            "Hoặc gửi tin kế tiếp chỉ có từ khóa (OR):\n"
            "• camera\n"
            "• lâm đồng, công nghệ thông tin\n\nThoát: /hủy"
        )
        _reply(bot_token, chat_id, hint)
        return

    routed = handle_slash(text.strip(), secrets, chat_type)
    if routed:
        _reply(bot_token, chat_id, routed)
        return

    if cmd:
        _reply(bot_token, chat_id, "Lệnh không rõ. Gõ /help để xem hướng dẫn.")
        return

    if _await_keyword.get(ukey):
        phrases = parse_keyword_phrases(text.strip())
        if not phrases:
            _reply(bot_token, chat_id, "Chưa có từ khóa. Ví dụ camera. /hủy để thoát.")
            return
        _await_keyword.pop(ukey, None)
        _execute_search(secrets, phrases, chat_id, cid_s)
        return

    stripped = text.strip()
    if not stripped:
        return

    phrases = []
    private = chat_type == "private"
    if private:
        phrases = parse_keyword_phrases(stripped)
    elif secrets.bot_group_freeword and chat_type in ("group", "supergroup"):
        phrases = parse_keyword_phrases(stripped)
    elif chat_type in ("group", "supergroup") and secrets.bot_group_reply_hint:
        _reply(
            bot_token,
            chat_id,
            "Tra nhanh trong nhóm: /tim rồi gửi từ khóa, hoặc /tim camera, lâm đồng",
        )
        return

    if phrases:
        _execute_search(secrets, phrases, chat_id, cid_s)


def poll_loop() -> None:
    secrets = Secrets()
    init_db()

    offset: int | None = None
    logger.info("Bot Telegram đang lắng nghe (getUpdates). Lệnh: /tim ...")
    logger.warning(
        "Chỉ chạy một process getUpdates/trên một token bot. Scheduler + tracker vẫn chạy bình thường.",
    )

    while True:
        try:
            updates = _get_updates(secrets.telegram_bot_token, offset)
            for upd in updates:
                offset = upd["update_id"] + 1
                msg = upd.get("message")
                if not msg:
                    continue
                try:
                    process_message(secrets, msg)
                except Exception:
                    logger.exception("Unhandled in process_message")
        except httpx.HTTPError as e:
            logger.warning("getUpdates: {}", e)
            time.sleep(5)


def main() -> None:
    poll_loop()


if __name__ == "__main__":
    main()
