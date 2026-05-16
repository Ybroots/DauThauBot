"""Telegram: lệnh hỗ trợ + tra TBMT nhanh (/tim) qua long polling."""

from __future__ import annotations

import html
import time
from typing import Optional

import httpx
from loguru import logger
from tenacity import RetryError

from .config import Secrets, load_keywords_yaml
from .crawler import BlockedException, MuasamcongCrawler
from .filter import explain_match, match_bid
from .interactive_search import parse_keyword_phrases, run_interactive_keyword_search
from .keyword_suggest import (
    build_suggest_reply,
    extract_suggestions,
    filter_bids_by_terms,
)
from .models import Bid
from .storage import (
    add_group,
    add_keyword_to_group,
    count_sent_since,
    count_sent_since_hours,
    count_unsent_in_db,
    init_db,
    list_recent_bids,
    list_unsent,
    load_groups_from_db,
    remove_group,
    remove_keyword_from_group,
    seed_groups_from_yaml,
    total_bids_in_db,
)
from .__main__ import run_once

BOT_VERSION = "0.1.0"

POLL_TIMEOUT = 30

# Đợi user gửi từ khóa trong tin tiếp theo (scope theo Chat+User để không lẫn nhau trong nhóm)
_await_keyword: dict[str, bool] = {}
_last_search_ts: dict[str, float] = {}

# State cho luồng /goiy — gợi ý từ khóa có hướng dẫn
# { state_key: { "accumulated": [str], "bids": [Bid], "suggestions": [(str,int)] } }
_suggest_state: dict[str, dict] = {}


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


def _is_privileged(secrets: Secrets, *, chat_id: int | str, user_id: int) -> bool:
    """Cho /test khi TELEGRAM_ADMIN_CHAT_ID trùng chat hoặc user (thường là user id cá nhân)."""
    admin = (secrets.admin_chat_id or "").strip()
    if not admin:
        return True
    return str(chat_id).strip() == admin or str(user_id).strip() == admin


def _parse_positive_int(rest: str, *, default: int, max_v: int, min_v: int = 1) -> int:
    s = rest.strip()
    if not s:
        return default
    try:
        n = int(s.split()[0])
        return max(min_v, min(n, max_v))
    except ValueError:
        return default


def _truncate(s: str, max_len: int) -> str:
    s = s.replace("\n", " ").strip()
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"


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
        "Luồng cron: tracker trên máy + keyword groups trong DB.\n\n"
        "Tra nhanh — một tin là bot chạy (không bắt buộc 2 bước):\n"
        "• /tim camera — khuyến nghị trong nhóm\n"
        "• /tim camera | máy chủ — OR nhiều cụm\n"
        "• /tim — nếu gõ trống: bot chờ tin kế tiếp (chat riêng vẫn nên dùng /tim camera cho nhanh)\n\n"
        "Chat riêng: gõ một dòng từ khóa (VD: camera) là tra luôn, không cần /tim.\n"
        "Trong nhóm: bắt /tim ... hoặc bật BOT_GROUP_FREEWORD=true để gõ thẳng như chat riêng.\n\n"
        "Lọc kết quả: mặc định từ đơn phải khớp cả từ (tránh khớp nhầm). Tắt: INTERACTIVE_SEARCH_STRICT_KEYWORDS=false.\n\n"
        "Gợi ý & tạo keyword group từ dữ liệu thực:\n"
        "• /goiy lâm đồng — bot cào cổng, gợi ý từ liên quan\n"
        "  → chọn số để hẹp dần → /taogroup để lưu\n\n"
        "Quản lý keyword groups (AND/OR logic):\n"
        "• /groups — xem tất cả groups\n"
        "• /addgroup Tên | all | kw1, kw2 — tạo group AND\n"
        "• /addgroup Tên | any | kw1, kw2 — tạo group OR\n"
        "• /removegroup Tên — xóa group\n"
        "• /addkw Tên | keyword — thêm keyword vào group\n"
        "• /removekw Tên | keyword — xóa keyword khỏi group\n"
        "• /testkw từ khóa thử — debug group nào match\n\n"
        "Lệnh khác: /lenh — danh sách ngắn. /thongke /lichsu /chuagui /id /ping /test /hủy"
    )


def COMMAND_LIST_VI() -> str:
    return (
        "Lệnh bot DauThauBot:\n"
        "• /tim — tra TBMT theo từ khóa (xem /help)\n"
        "• /goiy từ_khóa — gợi ý từ liên quan, hẹp dần → /taogroup\n"
        "• /groups — xem keyword groups (AND/OR logic)\n"
        "• /addgroup Tên | all|any | kw1, kw2 — tạo group mới\n"
        "• /removegroup Tên — xóa group\n"
        "• /addkw Tên | keyword — thêm keyword vào group\n"
        "• /removekw Tên | keyword — xóa keyword khỏi group\n"
        "• /testkw từ khóa — debug group nào match\n"
        "• /thongke — gửi tin 24h / 7 ngày / 30 ngày + tổng DB + chưa gửi\n"
        "• /lichsu [n] — n tin gần nhất trong DB (mặc định 10, tối đa 25)\n"
        "• /chuagui — các gói đã thấy nhưng chưa gửi Telegram (tối đa 15 dòng)\n"
        "• /stats — tóm tắt 7 ngày (giữ tương thích)\n"
        "• /id — chat_id & user_id (điền .env)\n"
        "• /ping /about — bot sống + phiên bản\n"
        "• /test — chạy 1 vòng tracker (cần quyền admin nếu có TELEGRAM_ADMIN_CHAT_ID)\n"
        "• /help /gioithieu — hướng dẫn dài\n"
        "• /hủy — thoát bước chờ từ khóa sau /tim"
    )


def handle_slash(
    full_text: str,
    secrets: Secrets,
    _chat_type: Optional[str],
    *,
    chat_id: int | str,
    user_id: int,
) -> Optional[str]:
    """Trả text trả lời (plain) hoặc None nếu đã xử lý không cần gửi thêm."""
    cmd, rest = _cmd_and_rest(full_text)

    if cmd in ("/start", "/help", "/gioithieu"):
        return HELP_VI()

    if cmd in ("/lenh", "/commands"):
        return COMMAND_LIST_VI()

    if cmd == "/ping":
        return f"pong — DauThauBot v{BOT_VERSION}"

    if cmd in ("/about", "/phienban"):
        return (
            f"DauThauBot v{BOT_VERSION}\n"
            "Nguồn: https://github.com/Ybroots/DauThauBot\n"
            "Tracker: muasamcong.mpi.gov.vn → SQLite → Telegram"
        )

    if cmd in ("/id", "/ma"):
        cid = html.escape(str(chat_id), quote=False)
        uid_s = html.escape(str(user_id), quote=False)
        return (
            "<b>Định danh Telegram</b>\n"
            f"<code>{cid}</code> — chat_id\n"
            f"<code>{uid_s}</code> — user_id\n"
            "Gán vào .env: TELEGRAM_CHAT_IDS / TELEGRAM_ADMIN_CHAT_ID."
        )

    if cmd in ("/keywords", "/groups"):
        init_db()
        cfg = load_groups_from_db()
        if not cfg.groups:
            return (
                "Chưa có keyword group nào trong DB.\n"
                "Dùng /addgroup để tạo, hoặc khởi động lại tracker để seed từ keywords.yaml."
            )
        lines = [f"📋 Keyword groups ({len(cfg.groups)} groups):"]
        for i, g in enumerate(cfg.groups, 1):
            req = "TẤT CẢ — AND" if g.require == "all" else "BẤT KỲ — OR"
            sep = " + " if g.require == "all" else " | "
            kws_str = sep.join(g.keywords) if g.keywords else "(trống)"
            lines.append(f"{i}. {g.name} [{req}]\n   {kws_str}")
        lines.append("\nDùng /addgroup, /removegroup, /addkw, /removekw để quản lý.")
        return "\n".join(lines)

    if cmd == "/addgroup":
        if not _is_privileged(secrets, chat_id=chat_id, user_id=user_id):
            return "Lệnh /addgroup chỉ dành cho admin."
        parts = [p.strip() for p in rest.split("|")]
        if len(parts) < 3:
            return (
                "Cú pháp: /addgroup Tên | all|any | kw1, kw2\n"
                "Ví dụ: /addgroup Camera LĐ | all | camera, lâm đồng"
            )
        name = parts[0]
        require = parts[1].strip().lower()
        if require not in ("all", "any"):
            return "Chế độ phải là 'all' (AND) hoặc 'any' (OR)."
        kws = [k.strip() for k in parts[2].split(",") if k.strip()]
        if not name:
            return "Tên group không được trống."
        init_db()
        ok = add_group(name, require, kws)
        if not ok:
            return f'Group "{name}" đã tồn tại. Dùng /addkw để thêm từ khóa.'
        req_label = "TẤT CẢ phải khớp" if require == "all" else "BẤT KỲ khớp là đủ"
        return f'✅ Đã thêm group "{name}" [{req_label}]\n   Keywords: {", ".join(kws)}'

    if cmd == "/removegroup":
        if not _is_privileged(secrets, chat_id=chat_id, user_id=user_id):
            return "Lệnh /removegroup chỉ dành cho admin."
        name = rest.strip()
        if not name:
            return "Cú pháp: /removegroup Tên group"
        init_db()
        ok = remove_group(name)
        if not ok:
            return f'Không tìm thấy group "{name}".'
        return f'✅ Đã xóa group "{name}"'

    if cmd == "/addkw":
        if not _is_privileged(secrets, chat_id=chat_id, user_id=user_id):
            return "Lệnh /addkw chỉ dành cho admin."
        parts = [p.strip() for p in rest.split("|", 1)]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            return "Cú pháp: /addkw Tên group | keyword"
        group_name, keyword = parts[0], parts[1]
        init_db()
        ok = add_keyword_to_group(group_name, keyword)
        if not ok:
            return f'Không tìm thấy group "{group_name}" hoặc keyword đã tồn tại.'
        return f'✅ Đã thêm "{keyword}" vào group "{group_name}"'

    if cmd == "/removekw":
        if not _is_privileged(secrets, chat_id=chat_id, user_id=user_id):
            return "Lệnh /removekw chỉ dành cho admin."
        parts = [p.strip() for p in rest.split("|", 1)]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            return "Cú pháp: /removekw Tên group | keyword"
        group_name, keyword = parts[0], parts[1]
        init_db()
        ok = remove_keyword_from_group(group_name, keyword)
        if not ok:
            return f'Không tìm thấy group "{group_name}" hoặc keyword "{keyword}".'
        return f'✅ Đã xóa "{keyword}" khỏi group "{group_name}"'

    if cmd == "/testkw":
        query = rest.strip()
        if not query:
            return "Cú pháp: /testkw từ khóa thử nghiệm\nVí dụ: /testkw camera lâm đồng"
        from datetime import datetime, timezone
        _now = datetime.now(timezone.utc)
        bid = Bid(
            tbmt_code="TEST",
            title=query,
            status="",
            investor="",
            posted_at=_now,
            closing_at=_now,
            field="",
            location="",
            bid_method="",
            detail_url="",
        )
        init_db()
        cfg = load_groups_from_db()
        return explain_match(bid, cfg)

    if cmd == "/taogroup":
        ukey = _state_key(chat_id, user_id)
        state = _suggest_state.pop(ukey, None)
        if not state:
            return (
                "Không có phiên /goiy nào đang mở.\n"
                "Dùng /goiy từ_khóa để bắt đầu gợi ý."
            )
        accumulated = state["accumulated"]
        if len(accumulated) < 1:
            return "Chưa chọn từ khóa nào để tạo group."
        name = " ".join(accumulated[:3])  # tên tự sinh từ 3 từ đầu
        init_db()
        ok = add_group(name, "all", accumulated)
        if not ok:
            # Tên bị trùng → thêm hậu tố
            import time as _time
            name = f"{name} {int(_time.time()) % 10000}"
            ok = add_group(name, "all", accumulated)
        if not ok:
            return "Lỗi khi tạo group. Thử /addgroup thủ công."
        kw_str = " + ".join(f'"{k}"' for k in accumulated)
        return (
            f'✅ Đã tạo group "{name}" [TẤT CẢ — AND]\n'
            f"   Keywords: {kw_str}\n\n"
            f"Dùng /groups để xem, /addkw để thêm từ."
        )

    if cmd == "/stats":
        init_db()
        n = count_sent_since(7)
        return f"Đã gửi {n} gói thầu (đánh dấu đã tin) trong 7 ngày qua."

    if cmd in ("/thongke", "/dashboard"):
        init_db()
        h24 = count_sent_since_hours(24)
        d7 = count_sent_since(7)
        d30 = count_sent_since(30)
        tot = total_bids_in_db()
        uns = count_unsent_in_db()
        return (
            "Thống kê (SQLite seen.db):\n"
            f"• Đã gửi Telegram — 24h: {h24} | 7 ngày: {d7} | 30 ngày: {d30}\n"
            f"• Tổng dòng trong DB: {tot}\n"
            f"• Đã thấy nhưng chưa gửi: {uns}"
        )

    if cmd in ("/lichsu", "/recent"):
        init_db()
        n = _parse_positive_int(rest, default=10, max_v=25)
        rows = list_recent_bids(n)
        if not rows:
            return "Chưa có dữ liệu trong DB (chạy tracker hoặc /test)."
        lines = []
        for code, title, seen_at, sent in rows:
            flag = "đã gửi" if sent else "chưa gửi"
            short_at = seen_at[:19].replace("T", " ") if len(seen_at) >= 19 else seen_at
            lines.append(f"• [{flag}] {code} — {_truncate(title, 72)}\n  {short_at} UTC")
        return f"{len(rows)} tin gần nhất:\n" + "\n".join(lines)

    if cmd in ("/chuagui", "/unsent"):
        init_db()
        rows = list_unsent()
        if not rows:
            return "Không có gói chưa gửi (sent_to_telegram=0)."
        cap = 15
        lines = [f"• {c} — {_truncate(t, 80)}" for c, t in rows[:cap]]
        more = f"\n… và {len(rows) - cap} gói nữa." if len(rows) > cap else ""
        return f"Chưa gửi Telegram ({len(rows)} gói):\n" + "\n".join(lines) + more

    if cmd == "/test":
        if not _is_privileged(secrets, chat_id=chat_id, user_id=user_id):
            return (
                "Lệnh /test chỉ dành cho admin.\n"
                "Đặt TELEGRAM_ADMIN_CHAT_ID = chat_id hoặc user_id của bạn (dùng /id)."
            )
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
        _suggest_state.pop(ukey, None)
        _reply(bot_token, chat_id, "Đã hủy.")
        return

    if cmd in ("/goiy", "/suggest"):
        seed = rest.strip()
        if not seed:
            _reply(
                bot_token, chat_id,
                "Cú pháp: /goiy từ_khóa\nVí dụ: /goiy lâm đồng\n\n"
                "Bot cào cổng, gợi ý từ liên quan, bạn chọn số để hẹp dần, rồi /taogroup.",
            )
            return
        _suggest_state.pop(ukey, None)  # reset phiên cũ nếu có
        _reply(
            bot_token, chat_id,
            f'🔍 Đang cào cổng với từ khóa "{seed}" (Playwright ~30–90s)…',
        )
        crawler = MuasamcongCrawler(
            page_size=50,
            use_playwright=secrets.use_playwright,
            playwright_headless=secrets.playwright_headless,
            playwright_channel=secrets.playwright_channel,
        )
        try:
            bids = crawler.fetch_recent_bids(max_pages=1, server_keyword=seed)
        except BlockedException as e:
            _reply(bot_token, chat_id, f"Cổng tạm chặn (HTTP {e.status_code}). Thử lại sau.")
            return
        except Exception:
            logger.exception("/goiy crawler error")
            _reply(bot_token, chat_id, "Lỗi khi cào cổng. Xem logs/ để biết chi tiết.")
            return
        finally:
            crawler.close()

        if not bids:
            _reply(bot_token, chat_id, f'Không tìm thấy gói nào cho "{seed}". Thử từ khóa khác?')
            return

        suggestions = extract_suggestions(bids, accumulated=[seed])
        if not suggestions:
            _reply(
                bot_token, chat_id,
                f'Tìm thấy {len(bids)} gói nhưng kết quả quá đa dạng, không trích được từ gợi ý.\n'
                f'Thử /tim {seed} để xem trực tiếp.',
            )
            return

        _suggest_state[ukey] = {
            "accumulated": [seed],
            "bids": bids,
            "suggestions": suggestions,
        }
        _reply(bot_token, chat_id, build_suggest_reply(bids, [seed], suggestions))
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

    routed = handle_slash(
        text.strip(),
        secrets,
        chat_type,
        chat_id=chat_id,
        user_id=int(uid),
    )
    if routed:
        _reply(bot_token, chat_id, routed, parse_html=cmd in ("/id", "/ma"))
        return

    if cmd:
        _reply(bot_token, chat_id, "Lệnh không rõ. Gõ /help để xem hướng dẫn.")
        return

    # ── /goiy state: user gửi số để chọn gợi ý ──────────────────────────────
    if ukey in _suggest_state:
        stripped_text = text.strip()
        if stripped_text.isdigit():
            idx = int(stripped_text) - 1
            state = _suggest_state[ukey]
            suggestions = state["suggestions"]
            if idx < 0 or idx >= len(suggestions):
                _reply(
                    bot_token, chat_id,
                    f"Số không hợp lệ. Chọn từ 1 đến {len(suggestions)}, "
                    "hoặc /taogroup để tạo group, /hủy để thoát.",
                )
                return
            chosen_term, _ = suggestions[idx]
            state["accumulated"].append(chosen_term)
            accumulated = state["accumulated"]

            # Lọc bids client-side với tất cả từ đã chọn
            filtered = filter_bids_by_terms(state["bids"], accumulated)

            if not filtered:
                # Không còn gói nào khớp → gợi ý tạo group ngay
                kw_str = " + ".join(f'"{k}"' for k in accumulated)
                _reply(
                    bot_token, chat_id,
                    f'✅ Thêm "{chosen_term}". Từ đã chọn: {kw_str}\n\n'
                    f"Không còn gói nào khớp đủ {len(accumulated)} điều kiện trong dữ liệu đã cào.\n"
                    "Gõ /taogroup để tạo group AND ngay, hoặc /hủy.",
                )
                state["suggestions"] = []
                return

            new_suggestions = extract_suggestions(filtered, accumulated=accumulated)
            state["bids"] = filtered
            state["suggestions"] = new_suggestions

            if not new_suggestions:
                kw_str = " + ".join(f'"{k}"' for k in accumulated)
                _reply(
                    bot_token, chat_id,
                    f'✅ Thêm "{chosen_term}". Từ đã chọn: {kw_str}\n\n'
                    f"Còn {len(filtered)} gói khớp. Không còn từ để gợi thêm.\n"
                    "Gõ /taogroup để tạo group AND, hoặc /hủy.",
                )
                return

            reply = (
                f'✅ Thêm "{chosen_term}". '
                + build_suggest_reply(filtered, accumulated, new_suggestions)
            )
            _reply(bot_token, chat_id, reply)
            return
        # Không phải số → bỏ qua suggest state, xử lý bình thường bên dưới

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
    logger.info(
        "Bot Telegram đang lắng nghe (getUpdates). Lệnh: /tim /lenh /thongke /lichsu …"
    )
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
