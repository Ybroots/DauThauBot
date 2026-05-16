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
from .interactive_search import parse_keyword_phrases, parse_search_query, run_interactive_keyword_search
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
    get_group_by_id,
    init_db,
    list_all_groups_raw,
    list_bids_since_hours,
    list_recent_bids,
    list_unsent,
    load_groups_from_db,
    lookup_bid_in_db,
    remove_bid_from_db,
    remove_group,
    remove_keyword_from_group,
    rename_group,
    seed_groups_from_yaml,
    toggle_group_active,
    total_bids_in_db,
)
from .__main__ import run_once

BOT_VERSION = "0.1.0"

POLL_TIMEOUT = 30

# Đợi user gửi từ khóa trong tin tiếp theo (scope theo Chat+User để không lẫn nhau trong nhóm)
_await_keyword: dict[str, bool] = {}
_await_include_closed: dict[str, bool] = {}   # True khi nút "Tìm kể cả đóng" được bấm
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


def _reply(
    token: str,
    chat_id: int | str,
    text: str,
    *,
    parse_html: bool = False,
    reply_markup: Optional[dict] = None,
) -> None:
    payload: dict = {"chat_id": chat_id, "text": text}
    if parse_html:
        payload["parse_mode"] = "HTML"
    if reply_markup:
        payload["reply_markup"] = reply_markup
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


# ── Inline keyboard builders ──────────────────────────────────────────────

def _btn(text: str, data: str) -> dict:
    """Inline keyboard button — callback_data capped at 64 bytes (Telegram limit)."""
    return {"text": text, "callback_data": data.encode()[:64].decode("utf-8", errors="ignore")}


def _kb(rows: list[list[dict]]) -> dict:
    """Build reply_markup with inline_keyboard."""
    return {"inline_keyboard": rows}


def _main_menu_kb() -> dict:
    return _kb([
        [_btn("🔍 Tìm gói mở", "search|open"), _btn("🌐 Tìm kể cả đóng", "search|closed")],
        [_btn("📋 Keyword Groups", "cmd|/groups"), _btn("📊 Thống kê", "cmd|/thongke")],
        [_btn("🕐 Gói hôm nay (24h)", "cmd|/timhom"), _btn("📜 Lịch sử gần đây", "cmd|/lichsu")],
    ])


def _after_search_kb(include_closed: bool = False) -> dict:
    if include_closed:
        row1 = [_btn("🌐 Tìm lại (tất cả)", "search|closed"), _btn("🔍 Chỉ gói mở", "search|open")]
    else:
        row1 = [_btn("🔍 Tìm lại", "search|open"), _btn("🌐 Tìm kể cả đóng", "search|closed")]
    return _kb([row1, [_btn("📋 Groups", "cmd|/groups"), _btn("🏠 Menu", "menu")]])


def _groups_kb(all_raw: list[tuple]) -> dict:
    """Keyboard for /groups list — each group gets a quick-search button."""
    rows: list[list[dict]] = []
    for entry in all_raw[:10]:  # limit to 10 to avoid oversized keyboard
        gid = entry[0]
        name = entry[1]
        active = entry[3]
        icon = "▶" if active else "⏸"
        label = f"{icon} {name}"
        if len(label.encode("utf-8")) > 32:
            label = label[:28] + "…"
        rows.append([_btn(label, f"grp|{gid}")])
    rows.append([_btn("➕ Hướng dẫn thêm group", "hint|addgroup"), _btn("🏠 Menu", "menu")])
    return _kb(rows)


def _suggest_kb(suggestions: list[tuple[str, int]]) -> dict:
    """Keyboard for /goiy suggestions — numbered buttons + taogroup/huy."""
    rows: list[list[dict]] = []
    for i, (term, count) in enumerate(suggestions):
        label = f"{i + 1}. {term} ({count})"
        if len(label.encode("utf-8")) > 50:
            label = f"{i + 1}. {term[:18]}… ({count})"
        rows.append([_btn(label, f"sug|{i}")])  # index-based, no encoding issue
    rows.append([_btn("✅ Tạo group AND", "taogroup"), _btn("❌ Hủy", "huy")])
    return _kb(rows)


def _answer_callback(token: str, callback_query_id: str, text: str = "") -> None:
    """Acknowledge a callback_query (required within 10s or Telegram shows spinner)."""
    try:
        httpx.post(
            f"https://api.telegram.org/bot{token}/answerCallbackQuery",
            json={"callback_query_id": callback_query_id, "text": text},
            timeout=8,
        )
    except Exception:
        pass  # Non-fatal — user sees spinner briefly but bot continues


# ─────────────────────────────────────────────────────────────────────────────


def _execute_search(
    secrets: Secrets,
    phrases: list[str],
    target_chat_id: str | int,
    chat_scope_key: str,
    *,
    mode: str = "any",
    include_closed: bool = False,
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
            reply_markup=_kb([[_btn("🏠 Menu", "menu")]]),
        )
        return
    mode_note = " [AND — tất cả phải khớp]" if mode == "all" else ""
    scope_note = " [tất cả — kể cả đã đóng thầu]" if include_closed else ""
    _reply(
        secrets.telegram_bot_token,
        target_chat_id,
        f"Đang tra trên Muasamcong{mode_note}{scope_note} (Playwright có thể mất 30–90 giây), vui lòng chờ…",
    )
    try:
        sent, total, summary = run_interactive_keyword_search(
            secrets,
            phrases,
            target_chat_id=target_chat_id,
            mode=mode,
            include_closed=include_closed,
        )
        logger.info(
            "interactive_search done chat={} sent={} matched={}",
            target_chat_id,
            sent,
            total,
        )
        _mark_search_done(chat_scope_key)
        _reply(
            secrets.telegram_bot_token,
            target_chat_id,
            summary,
            reply_markup=_after_search_kb(include_closed),
        )
    except BlockedException as e:
        _reply(
            secrets.telegram_bot_token,
            target_chat_id,
            f"Trang chủ thầu tạm chặn hoặc lỗi mạng (HTTP {e.status_code}). Thử sau vài tiếng.",
            reply_markup=_after_search_kb(include_closed),
        )
    except RuntimeError as e:
        msg = str(e)
        if "reCAPTCHA" in msg or "invalid site key" in msg.lower():
            logger.warning("interactive search reCAPTCHA/runtime: {}", msg)
            _reply(
                secrets.telegram_bot_token,
                target_chat_id,
                msg[:900],
                reply_markup=_after_search_kb(include_closed),
            )
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
            reply_markup=_after_search_kb(include_closed),
        )
    except Exception:
        logger.exception("interactive search failed")
        _reply(
            secrets.telegram_bot_token,
            target_chat_id,
            "Lỗi khi cào hoặc gửi Telegram. Xem logs/ trên máy chạy bot.",
            reply_markup=_after_search_kb(include_closed),
        )


def HELP_VI() -> str:
    return (
        "Luồng cron: tracker trên máy + keyword groups trong DB.\n\n"
        "Tra nhanh — một tin là bot chạy:\n"
        "• /tim camera                    — 1 từ khóa (chỉ gói đang mở thầu)\n"
        "• /tim camera | cctv             — OR: bất kỳ khớp\n"
        "• /tim camera & lâm đồng        — AND: tất cả phải khớp\n"
        "• /tim Công an tỉnh Lâm Đồng    — tìm theo tên cơ quan\n"
        "• /tim camera & lâm đồng & giám sát — AND 3 điều kiện\n\n"
        "Tìm kể cả gói đã đóng thầu:\n"
        "• /timtat Công an tỉnh Lâm Đồng — tìm tất cả (mở + đóng)\n"
        "• /timtat camera & lâm đồng     — AND mode, bao gồm đã đóng\n"
        "  Dùng /timtat khi /tim trả về ít kết quả vì thiếu gói mở.\n\n"
        "Chat riêng: gõ thẳng từ khóa (không cần /tim). Hỗ trợ & để AND.\n"
        "Trong nhóm: bắt /tim ... hoặc bật BOT_GROUP_FREEWORD=true.\n\n"
        "Lọc kết quả: mặc định từ đơn phải khớp cả từ (tránh khớp nhầm). Tắt: INTERACTIVE_SEARCH_STRICT_KEYWORDS=false.\n\n"
        "Gợi ý & tạo keyword group từ dữ liệu thực:\n"
        "• /goiy lâm đồng — bot cào cổng, gợi ý từ liên quan\n"
        "  → chọn số để hẹp dần → /taogroup để lưu\n\n"
        "Quản lý keyword groups (AND/OR logic):\n"
        "• /groups — xem tất cả groups (gồm cả group đang tắt)\n"
        "• /addgroup Tên | all | kw1, kw2 — tạo group AND\n"
        "• /addgroup Tên | any | kw1, kw2 — tạo group OR\n"
        "• /removegroup Tên — xóa group\n"
        "• /addkw Tên | keyword — thêm keyword vào group\n"
        "• /removekw Tên | keyword — xóa keyword khỏi group\n"
        "• /renamegroup Tên cũ | Tên mới — đổi tên group\n"
        "• /tatgroup Tên — tắt group (cron bỏ qua, vẫn tra được bằng /timgroup)\n"
        "• /batgroup Tên — bật lại group đã tắt\n"
        "• /timgroup Tên — tìm kiếm ngay theo từ khóa của group\n"
        "• /testkw từ khóa thử — debug group nào match\n\n"
        "Quản lý dữ liệu:\n"
        "• /xem MÃ — tra mã TBMT trong DB (tiêu đề, đã gửi chưa)\n"
        "• /timhom [hours] — gói thấy trong N giờ qua (mặc định 24h)\n"
        "• /xoa MÃ — admin: xóa mã khỏi DB để cron gửi lại\n\n"
        "Lệnh khác: /lenh — danh sách ngắn. /thongke /lichsu /chuagui /id /ping /test /hủy"
    )


def COMMAND_LIST_VI() -> str:
    return (
        "Lệnh bot DauThauBot:\n"
        "Tìm kiếm:\n"
        "• /tim kw — tra gói đang mở (OR/AND với |, &)\n"
        "• /timtat kw — tra kể cả gói đã đóng\n"
        "• /timgroup Tên — tra theo từ khóa của group đã lưu\n"
        "• /goiy kw — gợi ý từ liên quan, hẹp dần → /taogroup\n"
        "\nQuản lý groups:\n"
        "• /groups — xem tất cả groups (gồm cả tắt)\n"
        "• /addgroup Tên | all|any | kw1, kw2\n"
        "• /removegroup /renamegroup /addkw /removekw\n"
        "• /tatgroup Tên — tắt group khỏi cron\n"
        "• /batgroup Tên — bật lại group\n"
        "• /testkw từ khóa — debug group nào match\n"
        "\nDữ liệu:\n"
        "• /xem MÃ_TBMT — tra mã trong DB\n"
        "• /timhom [hours] — gói thấy trong N giờ (mặc định 24h)\n"
        "• /xoa MÃ — admin: xóa khỏi DB để cron gửi lại\n"
        "• /thongke /lichsu [n] /chuagui /stats\n"
        "\nKhác: /id /ping /about /test /help /hủy"
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
        all_groups = list_all_groups_raw()
        if not all_groups:
            return (
                "Chưa có keyword group nào trong DB.\n"
                "Dùng /addgroup để tạo, hoặc khởi động lại tracker để seed từ keywords.yaml."
            )
        active_n = sum(1 for entry in all_groups if entry[3])  # entry[3] = active
        inactive_n = len(all_groups) - active_n
        header = f"📋 Keyword groups ({active_n} đang bật"
        if inactive_n:
            header += f", {inactive_n} tắt"
        header += "):"
        lines = [header]
        for i, (gid, name, require, active, kws) in enumerate(all_groups, 1):
            req = "TẤT CẢ — AND" if require == "all" else "BẤT KỲ — OR"
            sep = " + " if require == "all" else " | "
            kws_str = sep.join(kws) if kws else "(trống)"
            status = "" if active else " [tắt]"
            lines.append(f"{i}. {name}{status} [{req}]\n   {kws_str}")
        lines.append("\nBấm nút bên dưới để tìm ngay theo từng group:")
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

    if cmd in ("/xem", "/lookup"):
        code = rest.strip().upper()
        if not code:
            return (
                "Cú pháp: /xem MÃ_TBMT\n"
                "Tra thông tin gói thầu đã lưu trong DB.\n"
                "Ví dụ: /xem 20240001234-00"
            )
        init_db()
        row = lookup_bid_in_db(code)
        if row is None:
            return (
                f'Không tìm thấy mã "{code}" trong DB.\n'
                "Bot chỉ lưu gói đã qua cron hoặc /tim. Thử /timtat để tìm trên cổng."
            )
        title, seen_at, sent = row
        flag = "Đã gửi Telegram" if sent else "Chưa gửi Telegram"
        short_at = seen_at[:19].replace("T", " ") if len(seen_at) >= 19 else seen_at
        # Construct a minimal detail page link — search portal by notifyNo
        notify_no = code.rsplit("-", 1)[0] if "-" in code else code
        search_link = (
            "https://muasamcong.mpi.gov.vn/web/guest/contractor-selection"
            "?p_p_id=egpportalcontractorselectionv2_WAR_egpportalcontractorselectionv2"
            "&p_p_lifecycle=0&p_p_state=normal&p_p_mode=view"
            "&_egpportalcontractorselectionv2_WAR_egpportalcontractorselectionv2_render=index"
        )
        return (
            f"<b>Mã TBMT:</b> <code>{html.escape(code)}</code>\n"
            f"<b>Tiêu đề:</b> {html.escape(title)}\n"
            f"<b>Trạng thái gửi:</b> {flag}\n"
            f"<b>Thấy lúc:</b> {short_at} UTC\n"
            f'\n🔗 <a href="{search_link}">Tìm trên muasamcong</a> (tìm mã <code>{html.escape(notify_no)}</code>)'
        )

    if cmd in ("/renamegroup",):
        if not _is_privileged(secrets, chat_id=chat_id, user_id=user_id):
            return "Lệnh /renamegroup chỉ dành cho admin."
        parts = [p.strip() for p in rest.split("|", 1)]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            return "Cú pháp: /renamegroup Tên cũ | Tên mới"
        old_name, new_name = parts[0], parts[1]
        init_db()
        ok = rename_group(old_name, new_name)
        if not ok:
            return f'Không tìm thấy group "{old_name}" hoặc tên "{new_name}" đã tồn tại.'
        return f'✅ Đã đổi tên "{old_name}" → "{new_name}"'

    if cmd in ("/tatgroup", "/disablegroup"):
        if not _is_privileged(secrets, chat_id=chat_id, user_id=user_id):
            return "Lệnh /tatgroup chỉ dành cho admin."
        name = rest.strip()
        if not name:
            return "Cú pháp: /tatgroup Tên group\nGroup bị tắt sẽ không dùng trong cron cho đến khi bật lại."
        init_db()
        ok = toggle_group_active(name, False)
        if not ok:
            return f'Không tìm thấy group "{name}". Xem /groups.'
        return (
            f'⏸ Group "{name}" đã tắt — cron bỏ qua group này.\n'
            "Dùng /batgroup để bật lại, /timgroup để vẫn tra thủ công."
        )

    if cmd in ("/batgroup", "/enablegroup"):
        if not _is_privileged(secrets, chat_id=chat_id, user_id=user_id):
            return "Lệnh /batgroup chỉ dành cho admin."
        name = rest.strip()
        if not name:
            return "Cú pháp: /batgroup Tên group"
        init_db()
        ok = toggle_group_active(name, True)
        if not ok:
            return f'Không tìm thấy group "{name}". Xem /groups.'
        return f'▶️ Group "{name}" đã bật — cron sẽ dùng lại group này.'

    if cmd in ("/timhom", "/today"):
        hours = _parse_positive_int(rest, default=24, max_v=168, min_v=1)
        init_db()
        rows = list_bids_since_hours(hours)
        if not rows:
            h_label = f"{hours}h" if hours != 24 else "24h"
            return f"Không thấy gói nào trong {h_label} vừa qua."
        lines = []
        for code, title, seen_at, sent in rows:
            flag = "gửi" if sent else "chưa"
            short_time = seen_at[11:16] if len(seen_at) >= 16 else seen_at
            lines.append(f"• [{flag}] {code} — {_truncate(title, 58)}\n  {short_time} UTC")
        h_label = f"{hours}h" if hours != 24 else "24h"
        return f"Gói thấy trong {h_label} ({len(rows)} gói):\n" + "\n".join(lines)

    if cmd in ("/xoa", "/deletebid"):
        if not _is_privileged(secrets, chat_id=chat_id, user_id=user_id):
            return "Lệnh /xoa chỉ dành cho admin."
        code = rest.strip().upper()
        if not code:
            return (
                "Cú pháp: /xoa MÃ_TBMT\n"
                "Xóa mã khỏi seen.db → cron sẽ gửi lại lần tiếp theo tìm thấy."
            )
        init_db()
        ok = remove_bid_from_db(code)
        if not ok:
            return f'Không tìm thấy mã "{code}" trong DB.'
        return f'🗑 Đã xóa "{code}" khỏi seen.db. Cron sẽ gửi lại khi tìm thấy gói này.'

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
        _await_include_closed.pop(ukey, None)
        _suggest_state.pop(ukey, None)
        _reply(bot_token, chat_id, "Đã hủy.", reply_markup=_main_menu_kb())
        return

    if cmd in ("/goiy", "/suggest"):
        seed = rest.strip()
        if not seed:
            _reply(
                bot_token, chat_id,
                "Cú pháp: /goiy từ_khóa\nVí dụ: /goiy lâm đồng\n\n"
                "Bot cào cổng, gợi ý từ liên quan, bạn bấm nút để hẹp dần, rồi tạo group.",
                reply_markup=_kb([[_btn("🏠 Menu", "menu")]]),
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
            _reply(
                bot_token, chat_id,
                f'Không tìm thấy gói nào cho "{seed}". Thử từ khóa ngắn hơn?',
                reply_markup=_kb([
                    [_btn("🔍 Tìm trực tiếp", "search|open"), _btn("🌐 Tìm cả đóng", "search|closed")],
                    [_btn("🏠 Menu", "menu")],
                ]),
            )
            return

        suggestions = extract_suggestions(bids, accumulated=[seed])
        if not suggestions:
            _reply(
                bot_token, chat_id,
                f'Tìm thấy {len(bids)} gói nhưng kết quả quá đa dạng, không trích được từ gợi ý.\n'
                f'Dùng nút bên dưới để tìm trực tiếp với từ khóa "{seed}".',
                reply_markup=_kb([
                    [_btn("🔍 Tìm gói mở", "search|open"), _btn("🌐 Tìm cả đóng", "search|closed")],
                    [_btn("🏠 Menu", "menu")],
                ]),
            )
            return

        _suggest_state[ukey] = {
            "accumulated": [seed],
            "bids": bids,
            "suggestions": suggestions,
        }
        _reply(
            bot_token,
            chat_id,
            build_suggest_reply(bids, [seed], suggestions),
            reply_markup=_suggest_kb(suggestions),
        )
        return

    if cmd in ("/tim", "/timkiem", "/search"):
        phrases, search_mode = parse_search_query(rest)
        if phrases:
            _await_keyword.pop(ukey, None)
            _execute_search(secrets, phrases, chat_id, cid_s, mode=search_mode)
            return
        _await_keyword[ukey] = True
        hint = (
            "Nhanh nhất: gửi lại một tin /tim kèm từ khóa.\n\n"
            "OR (bất kỳ khớp là đủ) — dùng dấu phẩy hoặc |:\n"
            "  /tim camera, lâm đồng\n"
            "  /tim camera | cctv\n\n"
            "AND (tất cả phải khớp) — dùng dấu &:\n"
            "  /tim công an & lâm đồng\n"
            "  /tim camera & lâm đồng & giám sát\n\n"
            "Hoặc gõ tên cơ quan trực tiếp:\n"
            "  /tim Công an tỉnh Lâm Đồng\n\n"
            "Thoát: /hủy"
        )
        _reply(
            bot_token, chat_id, hint,
            reply_markup=_kb([[_btn("❌ Hủy", "huy")]]),
        )
        return

    if cmd in ("/timtat", "/timall"):
        # Tìm tất cả TBMT kể cả đã đóng thầu — giải quyết vấn đề tìm tên cơ quan cụ thể
        phrases, search_mode = parse_search_query(rest)
        if not phrases:
            _reply(
                bot_token,
                chat_id,
                "Cú pháp: /timtat từ_khóa\n\n"
                "Giống /tim nhưng tìm cả gói đã đóng thầu, hữu ích khi tên cơ quan\n"
                "không còn gói mở thầu nào hiện tại.\n\n"
                "Ví dụ:\n"
                "  /timtat Công an tỉnh Lâm Đồng\n"
                "  /timtat camera & lâm đồng\n"
                "  /timtat camera, cctv",
                reply_markup=_kb([
                    [_btn("🌐 Bấm để tìm tất cả", "search|closed")],
                    [_btn("🏠 Menu", "menu")],
                ]),
            )
            return
        _await_keyword.pop(ukey, None)
        _execute_search(secrets, phrases, chat_id, cid_s, mode=search_mode, include_closed=True)
        return

    if cmd in ("/timgroup",):
        # Tìm kiếm theo từ khóa của một keyword group đã lưu
        name = rest.strip()
        if not name:
            _reply(
                bot_token,
                chat_id,
                "Cú pháp: /timgroup Tên group\n\n"
                "Chạy tìm kiếm ngay bằng từ khóa của group đã lưu.\n"
                "Group tắt (/tatgroup) vẫn có thể dùng lệnh này để tra thử.\n"
                "Bấm nút hoặc xem /groups để biết danh sách:",
                reply_markup=_groups_kb(list_all_groups_raw()),
            )
            return
        init_db()
        all_raw = list_all_groups_raw()  # kể cả inactive
        matched_g = next(
            (g for g in all_raw if g[1] == name),  # g[1] = name (gid is g[0])
            None,
        )
        if matched_g is None:
            # thử tìm case-insensitive
            name_lower = name.lower()
            matched_g = next((g for g in all_raw if g[1].lower() == name_lower), None)
        if matched_g is None:
            _reply(bot_token, chat_id, f'Không tìm thấy group "{name}". Xem /groups.')
            return
        _gid, g_name, g_require, g_active, g_kws = matched_g
        if not g_kws:
            _reply(bot_token, chat_id, f'Group "{g_name}" không có từ khóa nào. Dùng /addkw.')
            return
        inactive_note = " [group đang tắt — chỉ tra thủ công]" if not g_active else ""
        _reply(
            bot_token,
            chat_id,
            f'Đang tra group "{g_name}"{inactive_note} với {len(g_kws)} từ khóa…',
        )
        _execute_search(secrets, g_kws, chat_id, cid_s, mode=g_require)
        return

    routed = handle_slash(
        text.strip(),
        secrets,
        chat_type,
        chat_id=chat_id,
        user_id=int(uid),
    )
    if routed:
        parse_html_cmds = ("/id", "/ma", "/xem", "/lookup")
        kb: Optional[dict] = None
        _groups_cmds = (
            "/groups", "/keywords",
            "/addgroup", "/removegroup", "/addkw", "/removekw",
            "/renamegroup", "/tatgroup", "/disablegroup", "/batgroup", "/enablegroup",
            "/taogroup", "/testkw",
        )
        _stats_cmds = ("/thongke", "/dashboard", "/stats")
        _data_cmds = ("/lichsu", "/recent", "/chuagui", "/unsent", "/timhom", "/today")
        _menu_cmds = (
            "/start", "/help", "/gioithieu", "/lenh", "/commands",
            "/ping", "/about", "/phienban", "/id", "/ma",
            "/xem", "/lookup", "/xoa", "/deletebid", "/test",
        )
        if cmd in _groups_cmds:
            init_db()
            kb = _groups_kb(list_all_groups_raw())
        elif cmd in _stats_cmds:
            kb = _kb([
                [_btn("📜 Lịch sử", "cmd|/lichsu"), _btn("📭 Chưa gửi", "cmd|/chuagui")],
                [_btn("🔍 Tìm TBMT", "search|open"), _btn("🏠 Menu", "menu")],
            ])
        elif cmd in _data_cmds:
            kb = _kb([[_btn("📊 Thống kê", "cmd|/thongke"), _btn("🏠 Menu", "menu")]])
        elif cmd in _menu_cmds:
            kb = _main_menu_kb()
        _reply(
            bot_token,
            chat_id,
            routed,
            parse_html=cmd in parse_html_cmds,
            reply_markup=kb,
        )
        return

    if cmd:
        _reply(
            bot_token, chat_id,
            "Lệnh không rõ. Gõ /help hoặc bấm Menu để xem hướng dẫn.",
            reply_markup=_kb([[_btn("🏠 Menu chính", "menu"), _btn("❓ Trợ giúp", "cmd|/help")]]),
        )
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
                    "Bấm nút hoặc gõ /taogroup để tạo group AND.",
                    reply_markup=_kb([[_btn("✅ Tạo group AND", "taogroup"), _btn("❌ Hủy", "huy")]]),
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
                    f"Còn {len(filtered)} gói khớp. Không còn từ để gợi thêm.",
                    reply_markup=_kb([[_btn("✅ Tạo group AND", "taogroup"), _btn("❌ Hủy", "huy")]]),
                )
                return

            reply = (
                f'✅ Thêm "{chosen_term}". '
                + build_suggest_reply(filtered, accumulated, new_suggestions)
            )
            _reply(bot_token, chat_id, reply, reply_markup=_suggest_kb(new_suggestions))
            return
        # Không phải số → bỏ qua suggest state, xử lý bình thường bên dưới

    if _await_keyword.get(ukey):
        phrases, search_mode = parse_search_query(text.strip())
        if not phrases:
            _reply(bot_token, chat_id, "Chưa có từ khóa. Ví dụ: camera  hoặc  camera & lâm đồng (AND). /hủy để thoát.")
            return
        inc_closed = _await_include_closed.pop(ukey, False)
        _await_keyword.pop(ukey, None)
        _execute_search(secrets, phrases, chat_id, cid_s, mode=search_mode, include_closed=inc_closed)
        return

    stripped = text.strip()
    if not stripped:
        return

    phrases: list[str] = []
    search_mode = "any"
    private = chat_type == "private"
    if private:
        phrases, search_mode = parse_search_query(stripped)
    elif secrets.bot_group_freeword and chat_type in ("group", "supergroup"):
        phrases, search_mode = parse_search_query(stripped)
    elif chat_type in ("group", "supergroup") and secrets.bot_group_reply_hint:
        _reply(
            bot_token,
            chat_id,
            "Tra nhanh trong nhóm: /tim rồi gửi từ khóa, hoặc /tim camera & lâm đồng (AND)",
        )
        return

    if phrases:
        _execute_search(secrets, phrases, chat_id, cid_s, mode=search_mode)


def process_callback_query(secrets: Secrets, cq: dict) -> None:
    """Xử lý inline button callback_query."""
    cq_id = cq.get("id") or ""
    data = (cq.get("data") or "").strip()
    msg = cq.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    frm = cq.get("from") or {}
    uid = frm.get("id")

    if not chat_id or not uid:
        return

    token = secrets.telegram_bot_token
    cid_s = str(chat_id)
    ukey = _state_key(chat_id, int(uid))

    # Acknowledge immediately — Telegram requires answerCallbackQuery within 10s
    _answer_callback(token, cq_id)

    # ── menu ─────────────────────────────────────────────────────────────
    if data == "menu":
        _reply(token, chat_id, "Menu chính:", reply_markup=_main_menu_kb())
        return

    # ── cmd|/lệnh — chạy lệnh và trả kết quả kèm keyboard ───────────────
    if data.startswith("cmd|"):
        cmd_str = data[4:]
        cmd_key = cmd_str.split()[0].lower()
        routed = handle_slash(cmd_str, secrets, chat.get("type"), chat_id=chat_id, user_id=int(uid))
        if routed:
            kb: Optional[dict] = None
            _cq_groups_cmds = (
                "/groups", "/keywords",
                "/addgroup", "/removegroup", "/addkw", "/removekw",
                "/renamegroup", "/tatgroup", "/batgroup", "/taogroup", "/testkw",
            )
            if cmd_key in _cq_groups_cmds:
                init_db()
                kb = _groups_kb(list_all_groups_raw())
            elif cmd_key in ("/thongke", "/dashboard", "/stats"):
                kb = _kb([
                    [_btn("📜 Lịch sử", "cmd|/lichsu"), _btn("📭 Chưa gửi", "cmd|/chuagui")],
                    [_btn("🔍 Tìm TBMT", "search|open"), _btn("🏠 Menu", "menu")],
                ])
            elif cmd_key in ("/lichsu", "/recent", "/chuagui", "/unsent", "/timhom", "/today"):
                kb = _kb([[_btn("📊 Thống kê", "cmd|/thongke"), _btn("🏠 Menu", "menu")]])
            else:
                kb = _main_menu_kb()
            _reply(
                token,
                chat_id,
                routed,
                parse_html=cmd_key in ("/id", "/ma", "/xem", "/lookup"),
                reply_markup=kb,
            )
        return

    # ── search|open / search|closed — đặt trạng thái chờ từ khóa ─────────
    if data.startswith("search|"):
        inc_closed = data[7:] == "closed"
        _await_keyword[ukey] = True
        _await_include_closed[ukey] = inc_closed
        prefix = "[Tìm tất cả — kể cả đã đóng]\n\n" if inc_closed else ""
        _reply(
            token, chat_id,
            f"{prefix}Gõ từ khóa cần tìm:\n\n"
            "  camera\n"
            "  camera & lâm đồng   (AND — tất cả phải khớp)\n"
            "  camera | cctv       (OR — bất kỳ khớp)\n\n"
            "Hoặc tên cơ quan: Công an tỉnh Lâm Đồng\n"
            "/hủy để thoát.",
            reply_markup=_kb([[_btn("❌ Hủy", "huy")]]),
        )
        return

    # ── grp|<db_id> — tra ngay bằng một keyword group ────────────────────
    if data.startswith("grp|"):
        try:
            gid = int(data[4:])
        except ValueError:
            _reply(token, chat_id, "Nút không hợp lệ.")
            return
        init_db()
        row = get_group_by_id(gid)
        if row is None:
            _reply(token, chat_id, "Không tìm thấy group — có thể đã bị xóa. Xem /groups.")
            return
        g_name, g_require, g_kws = row
        if not g_kws:
            _reply(token, chat_id, f'Group "{g_name}" không có từ khóa. Dùng /addkw.')
            return
        _reply(token, chat_id, f'Đang tra group "{g_name}" ({len(g_kws)} từ khóa)…')
        _execute_search(secrets, g_kws, chat_id, cid_s, mode=g_require)
        return

    # ── sug|<idx> — chọn gợi ý trong luồng /goiy ─────────────────────────
    if data.startswith("sug|"):
        if ukey not in _suggest_state:
            _reply(token, chat_id, "Phiên /goiy đã hết hạn. Dùng /goiy để bắt đầu lại.")
            return
        try:
            idx = int(data[4:])
        except ValueError:
            _reply(token, chat_id, "Nút không hợp lệ.")
            return
        state = _suggest_state[ukey]
        suggestions = state.get("suggestions", [])
        if idx < 0 or idx >= len(suggestions):
            _reply(token, chat_id, "Lựa chọn không còn hợp lệ. Dùng /goiy lại.")
            return
        chosen_term, _ = suggestions[idx]
        state["accumulated"].append(chosen_term)
        accumulated = state["accumulated"]
        filtered = filter_bids_by_terms(state["bids"], accumulated)

        if not filtered:
            kw_str = " + ".join(f'"{k}"' for k in accumulated)
            _reply(
                token, chat_id,
                f'✅ Thêm "{chosen_term}". Từ đã chọn: {kw_str}\n\n'
                f"Không còn gói nào khớp đủ {len(accumulated)} điều kiện.",
                reply_markup=_kb([[_btn("✅ Tạo group AND", "taogroup"), _btn("❌ Hủy", "huy")]]),
            )
            state["suggestions"] = []
            return

        new_suggestions = extract_suggestions(filtered, accumulated=accumulated)
        state["bids"] = filtered
        state["suggestions"] = new_suggestions

        if not new_suggestions:
            kw_str = " + ".join(f'"{k}"' for k in accumulated)
            _reply(
                token, chat_id,
                f'✅ Thêm "{chosen_term}". Từ đã chọn: {kw_str}\n\nCòn {len(filtered)} gói khớp. Không còn từ gợi thêm.',
                reply_markup=_kb([[_btn("✅ Tạo group AND", "taogroup"), _btn("❌ Hủy", "huy")]]),
            )
            return

        reply_text = f'✅ Thêm "{chosen_term}". ' + build_suggest_reply(filtered, accumulated, new_suggestions)
        _reply(token, chat_id, reply_text, reply_markup=_suggest_kb(new_suggestions))
        return

    # ── taogroup ─────────────────────────────────────────────────────────
    if data == "taogroup":
        routed = handle_slash("/taogroup", secrets, None, chat_id=chat_id, user_id=int(uid))
        if routed:
            _reply(token, chat_id, routed, reply_markup=_main_menu_kb())
        return

    # ── huy ──────────────────────────────────────────────────────────────
    if data == "huy":
        _await_keyword.pop(ukey, None)
        _await_include_closed.pop(ukey, None)
        _suggest_state.pop(ukey, None)
        _reply(token, chat_id, "Đã hủy.", reply_markup=_main_menu_kb())
        return

    # ── hint|addgroup ─────────────────────────────────────────────────────
    if data == "hint|addgroup":
        _reply(
            token, chat_id,
            "Cú pháp tạo keyword group:\n\n"
            "AND (tất cả phải khớp):\n"
            "  /addgroup Camera LĐ | all | camera, lâm đồng\n\n"
            "OR (bất kỳ khớp):\n"
            "  /addgroup Camera | any | camera, cctv, giám sát\n\n"
            "Hoặc dùng /goiy để gợi ý từ khóa từ dữ liệu thực.",
            reply_markup=_kb([[_btn("🔙 Groups", "cmd|/groups"), _btn("🏠 Menu", "menu")]]),
        )
        return

    logger.debug("Unhandled callback_data: {!r}", data)


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
                if msg := upd.get("message"):
                    try:
                        process_message(secrets, msg)
                    except Exception:
                        logger.exception("Unhandled in process_message")
                elif cq := upd.get("callback_query"):
                    try:
                        process_callback_query(secrets, cq)
                    except Exception:
                        logger.exception("Unhandled in process_callback_query")
        except httpx.HTTPError as e:
            logger.warning("getUpdates: {}", e)
            time.sleep(5)


def main() -> None:
    poll_loop()


if __name__ == "__main__":
    main()
