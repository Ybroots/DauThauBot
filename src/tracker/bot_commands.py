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
from .formatter import format_bid_detail, format_bid_message
from .interactive_search import parse_keyword_phrases, parse_search_query, run_interactive_keyword_search
from .keyword_suggest import (
    build_suggest_reply,
    extract_suggestions,
    filter_bids_by_terms,
)
from .models import Bid
from .parser import parse_tbmt_input
from .storage import (
    add_group,
    add_keyword_to_group,
    count_sent_since,
    count_sent_since_hours,
    count_unsent_in_db,
    disable_all_groups,
    enable_all_groups,
    get_group_by_id,
    init_db,
    list_all_groups_raw,
    list_bids_since_hours,
    list_recent_bids,
    list_unsent,
    load_groups_from_db,
    lookup_bid_in_db,
    remove_all_groups,
    remove_bid_from_db,
    remove_group,
    remove_keyword_from_group,
    rename_group,
    seed_groups_from_yaml,
    toggle_group_active,
    total_bids_in_db,
)
from .crawler import site_status as _crawler_site_status
from .tender_store import (
    count_tenders,
    get_last_crawl_time,
    list_crawl_logs,
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

# State cho auto-suggest hẹp dần sau /tim — riêng với /goiy vì UX khác:
# /tim narrow click → re-emit bid cụ thể; /goiy click → summary đếm.
# { state_key: { "phrases": [str], "bids": [Bid], "suggestions": [(str,int)],
#                "include_closed": bool, "chat_scope_key": str } }
_narrow_state: dict[str, dict] = {}

# State cho luồng /loc — bộ lọc tìm kiếm nâng cao
# { state_key: { "fields": list[str], "method": int|None, "closed": bool } }
_filter_state: dict[str, dict] = {}

# State cho thao tác nhập liệu với group (addkw)
# { state_key: { "action": str, "gid": int, "name": str } }
_await_group_action: dict[str, dict] = {}

# Lĩnh vực đấu thầu — mã ES và tên hiển thị
FIELD_OPTIONS: list[tuple[str, str]] = [
    ("HH", "Hàng hóa"),
    ("XL", "Xây lắp"),
    ("TV", "Tư vấn"),
    ("PTV", "Phi tư vấn"),
    ("HON_HOP", "Hỗn hợp"),
]

# Tỉnh/TP nhanh — (nhãn nút, từ khóa tìm kiếm ES)
PROVINCE_QUICK: list[tuple[str, str]] = [
    ("Hà Nội", "Hà Nội"),
    ("TP.HCM", "Hồ Chí Minh"),
    ("Đà Nẵng", "Đà Nẵng"),
    ("Hải Phòng", "Hải Phòng"),
    ("Cần Thơ", "Cần Thơ"),
    ("Lâm Đồng", "Lâm Đồng"),
    ("Nghệ An", "Nghệ An"),
    ("Bình Dương", "Bình Dương"),
    ("Đồng Nai", "Đồng Nai"),
    ("Thanh Hóa", "Thanh Hóa"),
    ("Khánh Hòa", "Khánh Hòa"),
    ("Bình Định", "Bình Định"),
]


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


def _human_vnd(amount: int | None) -> str:
    """100_000_000 → '100tr'; 1_500_000_000 → '1.5tỷ'; trả '' nếu None/0."""
    if not amount:
        return ""
    a = int(amount)
    if a >= 1_000_000_000:
        return f"{a / 1_000_000_000:.1f}tỷ".replace(".0tỷ", "tỷ")
    if a >= 1_000_000:
        return f"{a / 1_000_000:.0f}tr"
    return f"{a:,}"


def _short_closing(closing_iso: str | None) -> str:
    """ISO date → 'dd/mm HH:MM'. '' nếu không parse được."""
    if not closing_iso:
        return ""
    s = str(closing_iso).replace("Z", "+00:00")
    try:
        from datetime import datetime as _dt
        dt = _dt.fromisoformat(s)
        return dt.strftime("%d/%m %H:%M")
    except (ValueError, TypeError):
        return ""


def _compact_extras(extras: dict) -> str:
    """Một dòng phụ gọn: 'GG: 100tr | HĐT: 31/12 15:00' — bỏ phần thiếu, '' nếu rỗng."""
    if not extras:
        return ""
    parts: list[str] = []
    gg = _human_vnd(extras.get("budget_vnd"))
    if gg:
        parts.append(f"GG: {gg}")
    hdt = _short_closing(extras.get("closing_at"))
    if hdt:
        parts.append(f"HĐT: {hdt}")
    return " | ".join(parts)


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
        [_btn("🔧 Bộ lọc nâng cao", "cmd|/loc"), _btn("📋 Groups", "cmd|/groups")],
        [_btn("📊 Thống kê", "cmd|/thongke"), _btn("🕐 Hôm nay (24h)", "cmd|/timhom")],
        [_btn("📜 Lịch sử gần đây", "cmd|/lichsu")],
    ])


def _after_search_kb(include_closed: bool = False) -> dict:
    if include_closed:
        row1 = [_btn("🌐 Tìm lại (tất cả)", "search|closed"), _btn("🔍 Chỉ gói mở", "search|open")]
    else:
        row1 = [_btn("🔍 Tìm lại", "search|open"), _btn("🌐 Tìm kể cả đóng", "search|closed")]
    return _kb([
        row1,
        [_btn("🔧 Bộ lọc", "cmd|/loc"), _btn("📋 Groups", "cmd|/groups"), _btn("🏠 Menu", "menu")],
    ])


def _loc_kb(state: dict) -> dict:
    """Keyboard cho /loc — bộ lọc lĩnh vực + hình thức + phạm vi."""
    fields: list[str] = state.get("fields") or []
    method: Optional[int] = state.get("method")   # None=tất cả, 1=qua mạng, 0=không qua mạng
    closed: bool = state.get("closed", False)

    def _f(code: str, label: str) -> dict:
        icon = "✅" if code in fields else "☐"
        return _btn(f"{icon} {label}", f"floc|field|{code}")

    rows: list[list[dict]] = [
        # Lĩnh vực — hàng 3 + hàng 2
        [_f("HH", "Hàng hóa"), _f("XL", "Xây lắp"), _f("TV", "Tư vấn")],
        [_f("PTV", "Phi tư vấn"), _f("HON_HOP", "Hỗn hợp")],
        # Hình thức đấu thầu
        [
            _btn(f"{'✅' if method == 1 else '☐'} Qua mạng", "floc|method|1"),
            _btn(f"{'✅' if method == 0 else '☐'} Không qua mạng", "floc|method|0"),
            _btn(f"{'✅' if method is None else '☐'} Tất cả HT", "floc|method|all"),
        ],
        # Phạm vi tìm kiếm
        [
            _btn(f"{'✅' if not closed else '☐'} Chỉ gói mở", "floc|scope|open"),
            _btn(f"{'✅' if closed else '☐'} Kể cả đóng", "floc|scope|closed"),
        ],
        # Hành động
        [_btn("🔍 Tìm ngay", "floc|run"), _btn("🔄 Reset bộ lọc", "floc|reset"), _btn("🏠 Menu", "menu")],
    ]
    return _kb(rows)


def _show_loc_panel(token: str, chat_id: int | str, fstate: dict) -> None:
    """Gửi bảng lọc với trạng thái hiện tại."""
    fields = fstate.get("fields") or []
    method: Optional[int] = fstate.get("method")
    closed: bool = fstate.get("closed", False)

    fields_str = (
        ", ".join(next((lbl for c, lbl in FIELD_OPTIONS if c == f), f) for f in fields)
        or "Tất cả lĩnh vực"
    )
    method_map = {None: "Tất cả hình thức", 1: "Qua mạng", 0: "Không qua mạng"}
    method_str = method_map.get(method, "Tất cả hình thức")
    scope_str = "Kể cả đã đóng thầu" if closed else "Chỉ gói đang mở"

    _reply(
        token, chat_id,
        "🔧 Bộ lọc nâng cao\n\n"
        f"  Lĩnh vực: {fields_str}\n"
        f"  Hình thức: {method_str}\n"
        f"  Phạm vi:   {scope_str}\n\n"
        "Bấm ✅ để chọn/bỏ chọn, rồi nhấn Tìm ngay.\n"
        "Có thể để trống từ khóa — bot sẽ duyệt toàn bộ TBMT theo bộ lọc.",
        reply_markup=_loc_kb(fstate),
    )


def _groups_kb(all_raw: list[tuple]) -> dict:
    """Keyboard for /groups list — tap group to open detail panel."""
    rows: list[list[dict]] = []
    for entry in all_raw[:12]:
        gid = entry[0]
        name = entry[1]
        active = entry[3]
        icon = "▶" if active else "⏸"
        label = f"{icon} {name}"
        if len(label.encode("utf-8")) > 48:
            label = label[:38] + "…"
        rows.append([_btn(label, f"grp|{gid}")])
    rows.append([_btn("➕ Tạo group mới", "hint|addgroup"), _btn("🏠 Menu", "menu")])
    return _kb(rows)


def _group_detail_kb(gid: int, active: bool) -> dict:
    """Keyboard for a single group's detail panel."""
    tgl_label = "⏸ Tắt group" if active else "▶ Bật group"
    return _kb([
        [_btn("🔍 Tìm gói mở", f"grpsearch|{gid}"), _btn("🌐 Tìm cả đóng", f"grpsearchclosed|{gid}")],
        [_btn(tgl_label, f"grptgl|{gid}"), _btn("➕ Thêm từ khóa", f"grpadkw|{gid}")],
        [_btn("🗑 Xóa group", f"grpdel|{gid}"), _btn("🔙 Danh sách groups", "cmd|/groups")],
    ])


def _search_prompt_kb(include_closed: bool) -> dict:
    """Province quick-pick keyboard shown alongside search keyword prompt."""
    scope = "c" if include_closed else "o"
    rows: list[list[dict]] = []
    row: list[dict] = []
    for i, (name, _) in enumerate(PROVINCE_QUICK):
        row.append(_btn(name, f"qs|{i}|{scope}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([_btn("🔧 Bộ lọc nâng cao", "cmd|/loc"), _btn("❌ Hủy", "huy")])
    return _kb(rows)


def _timhom_kb() -> dict:
    """Time-range picker for /timhom."""
    return _kb([
        [_btn("1h", "timhom|1"), _btn("3h", "timhom|3"), _btn("6h", "timhom|6"), _btn("12h", "timhom|12")],
        [_btn("24h", "timhom|24"), _btn("48h", "timhom|48"), _btn("72h", "timhom|72"), _btn("7 ngày", "timhom|168")],
        [_btn("📊 Thống kê", "cmd|/thongke"), _btn("🔍 Tìm TBMT", "search|open"), _btn("🏠 Menu", "menu")],
    ])


def _show_group_detail(token: str, chat_id: int | str, gid: int) -> None:
    """Gửi chi tiết một keyword group kèm nút quản lý."""
    init_db()
    row = get_group_by_id(gid)
    if row is None:
        _reply(token, chat_id, "Không tìm thấy group — có thể đã bị xóa.",
               reply_markup=_kb([[_btn("📋 Groups", "cmd|/groups"), _btn("🏠 Menu", "menu")]]))
        return
    g_name, g_require, g_kws = row
    all_groups = list_all_groups_raw()
    g_active = next((g[3] for g in all_groups if g[0] == gid), True)
    req_label = "AND — tất cả phải khớp" if g_require == "all" else "OR — bất kỳ khớp"
    sep = " + " if g_require == "all" else " | "
    kws_str = sep.join(g_kws) if g_kws else "(trống — dùng nút Thêm từ khóa)"
    status = "▶ Đang bật" if g_active else "⏸ Đang tắt (cron bỏ qua)"
    text = (
        f"📋 {g_name}\n"
        f"Kiểu khớp: {req_label}\n"
        f"Trạng thái: {status}\n\n"
        f"Từ khóa ({len(g_kws)}):\n"
        f"  {kws_str}\n\n"
        "Chọn thao tác:"
    )
    _reply(token, chat_id, text, reply_markup=_group_detail_kb(gid, bool(g_active)))


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


def _narrow_kb(suggestions: list[tuple[str, int]]) -> dict:
    """Keyboard auto-suggest sau /tim — click để hẹp dần kết quả (re-emit bid)."""
    rows: list[list[dict]] = []
    # 2 nút mỗi dòng để gọn
    pair: list[dict] = []
    for i, (term, count) in enumerate(suggestions):
        label = f"+ {term} ({count})"
        if len(label.encode("utf-8")) > 30:
            label = f"+ {term[:14]}… ({count})"
        pair.append(_btn(label, f"nrw|{i}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([_btn("✖ Bỏ gợi ý", "nrw|cancel"), _btn("🏠 Menu", "menu")])
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
    field_filter: Optional[list[str]] = None,
    bid_method_filter: Optional[int] = None,
) -> None:
    phrases = [p for p in phrases if p.strip()]
    has_filter = bool(field_filter) or bid_method_filter is not None
    if not phrases and not has_filter:
        _reply(secrets.telegram_bot_token, target_chat_id,
               "Chưa có từ khóa hoặc bộ lọc. Ví dụ: camera, lâm đồng\nHoặc dùng /loc để đặt bộ lọc.")
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
    mode_note = " [AND — tất cả phải khớp]" if mode == "all" and phrases else ""
    scope_note = " [tất cả — kể cả đã đóng thầu]" if include_closed else ""
    filter_notes: list[str] = []
    if field_filter:
        field_labels = [next((lbl for c, lbl in FIELD_OPTIONS if c == f), f) for f in field_filter]
        filter_notes.append(f"Lĩnh vực: {', '.join(field_labels)}")
    if bid_method_filter is not None:
        filter_notes.append("Qua mạng" if bid_method_filter == 1 else "Không qua mạng")
    filter_note = f" | {'; '.join(filter_notes)}" if filter_notes else ""
    kw_note = f'"{", ".join(phrases)}"' if phrases else "duyệt tất cả"
    _reply(
        secrets.telegram_bot_token,
        target_chat_id,
        f"Đang tra Muasamcong — {kw_note}{mode_note}{scope_note}{filter_note}\n(Playwright có thể mất 30–90 giây)…",
    )
    try:
        sent, total, summary, matched_bids = run_interactive_keyword_search(
            secrets,
            phrases,
            target_chat_id=target_chat_id,
            mode=mode,
            include_closed=include_closed,
            field_filter=field_filter,
            bid_method_filter=bid_method_filter,
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
        # Auto-suggest hẹp dần — chỉ khi có ≥ 3 bid matched & có gợi ý hữu ích
        _maybe_send_narrow_suggestions(
            secrets,
            target_chat_id,
            chat_scope_key,
            phrases=phrases,
            bids=matched_bids,
            include_closed=include_closed,
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
    except Exception as _exc:
        logger.exception("interactive search failed")
        _exc_str = f"{type(_exc).__name__} {_exc}".lower()
        _is_network = any(k in _exc_str for k in (
            "timeout", "connecttimeout", "connecterror", "connectionerror",
            "network", "connection failed", "timed out", "connection refused",
        ))
        if _is_network:
            _err_msg = (
                "Không thể kết nối đến cổng muasamcong.\n"
                "IP có thể đang bị throttle hoặc cổng tạm thời bảo trì.\n"
                "Thử lại sau vài phút. Dùng /crawllogs để xem lịch sử cào."
            )
        else:
            _err_msg = (
                "Lỗi khi cào cổng. Dùng /crawllogs để xem chi tiết lỗi.\n"
                "Nếu lỗi liên tục: thử PLAYWRIGHT_HEADLESS=false hoặc đổi IP."
            )
        _reply(
            secrets.telegram_bot_token,
            target_chat_id,
            _err_msg,
            reply_markup=_after_search_kb(include_closed),
        )


def _maybe_send_narrow_suggestions(
    secrets: Secrets,
    target_chat_id: str | int,
    chat_scope_key: str,
    *,
    phrases: list[str],
    bids: list,
    include_closed: bool,
) -> None:
    """Sau /tim, gửi keyboard 'Hẹp dần thêm với:' nếu có ≥ 3 bid + có suggestion."""
    # Ngưỡng: dưới 3 bid không cần hẹp; trên N rất nhiều cũng có ích — luôn show
    if not bids or len(bids) < 3:
        return
    suggestions = extract_suggestions(bids, accumulated=list(phrases))
    if not suggestions:
        return

    ukey = chat_scope_key  # cùng scope với cooldown
    _narrow_state[ukey] = {
        "phrases": list(phrases),
        "bids": list(bids),
        "suggestions": suggestions,
        "include_closed": include_closed,
    }
    kw_summary = " + ".join(f'"{p}"' for p in phrases) if phrases else "(lọc)"
    text = (
        f"🔍 Bot gợi ý hẹp tiếp từ {len(bids)} gói khớp {kw_summary}.\n"
        "Click một từ để lọc nhanh (không cào lại):"
    )
    _reply(
        secrets.telegram_bot_token,
        target_chat_id,
        text,
        reply_markup=_narrow_kb(suggestions),
    )


def _execute_narrow_click(
    secrets: Secrets,
    target_chat_id: str | int,
    chat_scope_key: str,
    chosen_idx: int,
) -> None:
    """Xử lý click nút nrw|<idx>: filter in-memory + re-emit bid + show new suggestions."""
    state = _narrow_state.get(chat_scope_key)
    if not state:
        _reply(
            secrets.telegram_bot_token,
            target_chat_id,
            "Gợi ý đã hết hạn. Gõ /tim để tra lại.",
            reply_markup=_kb([[_btn("🔍 Tìm", "search|open"), _btn("🏠 Menu", "menu")]]),
        )
        return

    suggestions = state["suggestions"]
    if chosen_idx < 0 or chosen_idx >= len(suggestions):
        _reply(secrets.telegram_bot_token, target_chat_id, "Nút không hợp lệ.")
        return

    chosen_term, _ = suggestions[chosen_idx]
    accumulated = state["phrases"] + [chosen_term]
    filtered = filter_bids_by_terms(state["bids"], accumulated)

    token = secrets.telegram_bot_token
    cid = str(target_chat_id)
    kw_summary = " + ".join(f'"{p}"' for p in accumulated)

    if not filtered:
        _narrow_state.pop(chat_scope_key, None)
        _reply(
            token, target_chat_id,
            f"❌ Không còn gói khớp {kw_summary}. Thử từ khác hoặc /tim lại.",
            reply_markup=_after_search_kb(state.get("include_closed", False)),
        )
        return

    # Re-emit filtered bids (cap như /tim)
    cap = secrets.interactive_search_max_messages
    to_emit = filtered[:cap]
    sent = 0
    from .telegram import TELEGRAM_BATCH_SLEEP_S, TELEGRAM_RATE_BATCH, chitiet_button, send_message
    for i, bid in enumerate(to_emit):
        if i > 0 and i % TELEGRAM_RATE_BATCH == 0:
            time.sleep(TELEGRAM_BATCH_SLEEP_S)
        body = format_bid_message(bid, accumulated)
        if send_message(token, cid, body, reply_markup=chitiet_button(bid.tbmt_code)):
            sent += 1

    # New suggestions từ tập đã hẹp
    new_suggestions = extract_suggestions(filtered, accumulated=accumulated)
    capped_note = (
        f" (đã gửi {sent}/{len(filtered)}, giới hạn {cap})" if len(filtered) > cap else f" (đã gửi {sent})"
    )
    summary = f"🔎 Hẹp dần với {kw_summary} → còn {len(filtered)} gói{capped_note}."

    if new_suggestions and len(filtered) >= 3:
        _narrow_state[chat_scope_key] = {
            **state,
            "phrases": accumulated,
            "bids": filtered,
            "suggestions": new_suggestions,
        }
        _reply(
            token, target_chat_id,
            summary + "\nClick để hẹp thêm:",
            reply_markup=_narrow_kb(new_suggestions),
        )
    else:
        _narrow_state.pop(chat_scope_key, None)
        _reply(token, target_chat_id, summary,
               reply_markup=_after_search_kb(state.get("include_closed", False)))


def _execute_detail_fetch(
    secrets: Secrets,
    raw_input: str,
    target_chat_id: str | int,
    chat_scope_key: str,
) -> None:
    """/chitiet: tra một mã TBMT trên cổng, gửi message detail HTML."""
    notify_no, version = parse_tbmt_input(raw_input)
    token = secrets.telegram_bot_token
    if not notify_no:
        _reply(
            token,
            target_chat_id,
            "Cú pháp: /chitiet MÃ_TBMT\n"
            "Ví dụ:\n"
            "  /chitiet IB2500579539\n"
            "  /chitiet IB2500579539-00\n"
            "  /chitiet <dán URL chi tiết của muasamcong>",
            reply_markup=_kb([[_btn("🏠 Menu", "menu")]]),
        )
        return

    cd_ok, secs = _cooldown_ok(secrets, chat_scope_key)
    if not cd_ok:
        _reply(
            token,
            target_chat_id,
            f"Chờ thêm ~{secs}s rồi tra tiếp (tránh spam cào).",
            reply_markup=_kb([[_btn("🏠 Menu", "menu")]]),
        )
        return

    code_label = f"{notify_no}-{version}" if version else notify_no
    _reply(
        token,
        target_chat_id,
        f"Đang đọc chi tiết mã {code_label} trên Muasamcong (Playwright ~30–90s)…",
    )

    crawler = MuasamcongCrawler(
        page_size=secrets.crawl_page_size,
        use_playwright=secrets.use_playwright,
        playwright_headless=secrets.playwright_headless,
        playwright_channel=secrets.playwright_channel,
    )
    try:
        bid = crawler.fetch_bid_by_code(notify_no, version=version, include_closed=True)
    except BlockedException as e:
        _reply(
            token,
            target_chat_id,
            f"Cổng tạm chặn (HTTP {e.status_code}). Thử sau vài tiếng.",
            reply_markup=_kb([[_btn("🏠 Menu", "menu")]]),
        )
        return
    except RuntimeError as e:
        msg = str(e)
        if "reCAPTCHA" in msg or "invalid site key" in msg.lower():
            _reply(token, target_chat_id, msg[:900],
                   reply_markup=_kb([[_btn("🏠 Menu", "menu")]]))
            return
        logger.exception("/chitiet runtime")
        _reply(token, target_chat_id, "Lỗi cào cổng. Xem logs/.",
               reply_markup=_kb([[_btn("🏠 Menu", "menu")]]))
        return
    except RetryError:
        logger.exception("/chitiet retry exhausted")
        _reply(
            token,
            target_chat_id,
            "Hết lần thử (thường do reCAPTCHA). Chạy lại sau ít phút, hoặc thử PLAYWRIGHT_HEADLESS=false.",
            reply_markup=_kb([[_btn("🏠 Menu", "menu")]]),
        )
        return
    except Exception as _exc:
        logger.exception("/chitiet failed")
        _exc_s = f"{type(_exc).__name__} {_exc}".lower()
        if any(k in _exc_s for k in ("timeout", "connect", "network", "timed out")):
            _emsg = "Không thể kết nối cổng muasamcong. Thử lại sau vài phút."
        else:
            _emsg = "Lỗi khi đọc chi tiết. Dùng /crawllogs để debug."
        _reply(token, target_chat_id, _emsg,
               reply_markup=_kb([[_btn("🏠 Menu", "menu")]]))
        return
    finally:
        crawler.close()

    _mark_search_done(chat_scope_key)

    if bid is None:
        _reply(
            token,
            target_chat_id,
            f'Không tìm thấy gói "{code_label}" trên cổng.\n'
            "Kiểm tra lại mã, hoặc gói có thể đã bị huỷ TBMT.",
            reply_markup=_kb([[_btn("🏠 Menu", "menu")]]),
        )
        return

    body = format_bid_detail(bid)
    _reply(
        token,
        target_chat_id,
        body,
        parse_html=True,
        reply_markup=_kb([
            [_btn("🔍 Tìm thêm", "search|open"), _btn("🌐 Tìm cả đóng", "search|closed")],
            [_btn("🏠 Menu", "menu")],
        ]),
    )


def HELP_VI() -> str:
    return (
        "Luồng cron: tracker trên máy + keyword groups trong DB.\n\n"
        "Tra nhanh — một tin là bot chạy:\n"
        "• /tim camera                    — 1 từ khóa (chỉ gói đang mở thầu)\n"
        "• /tim camera | cctv             — OR: bất kỳ khớp\n"
        "• /tim camera & lâm đồng        — AND: tất cả phải khớp\n"
        "• /tim Công an tỉnh Lâm Đồng    — tìm theo tên cơ quan\n"
        "• /tim camera & lâm đồng & giám sát — AND 3 điều kiện\n"
        "  💡 Sau khi /tim, bot gợi ý nút 'Hẹp dần thêm' — click để lọc nhanh.\n\n"
        "Tìm kể cả gói đã đóng thầu:\n"
        "• /timtat Công an tỉnh Lâm Đồng — tìm tất cả (mở + đóng)\n"
        "• /timtat camera & lâm đồng     — AND mode, bao gồm đã đóng\n"
        "  Dùng /timtat khi /tim trả về ít kết quả vì thiếu gói mở.\n\n"
        "Lọc nâng cao (lĩnh vực, hình thức, phạm vi):\n"
        "• /loc — mở bảng lọc với nút bấm:\n"
        "    Lĩnh vực: Hàng hóa | Xây lắp | Tư vấn | Phi tư vấn | Hỗn hợp\n"
        "    Hình thức: Qua mạng | Không qua mạng\n"
        "    Phạm vi: Chỉ gói mở | Kể cả đóng\n"
        "    → Chọn xong, bấm Tìm ngay rồi gõ từ khóa.\n"
        "    → Để trống từ khóa (gửi -) = duyệt tất cả TBMT theo bộ lọc.\n\n"
        "Chat riêng: gõ thẳng từ khóa (không cần /tim). Hỗ trợ & để AND.\n"
        "Trong nhóm: bắt /tim ... hoặc bật BOT_GROUP_FREEWORD=true.\n\n"
        "Lọc kết quả: mặc định từ đơn phải khớp cả từ (tránh khớp nhầm). Tắt: INTERACTIVE_SEARCH_STRICT_KEYWORDS=false.\n\n"
        "Gợi ý & tạo keyword group từ dữ liệu thực:\n"
        "• /goiy lâm đồng — bot cào cổng, gợi ý từ liên quan\n"
        "  → chọn số để hẹp dần → /taogroup để lưu\n\n"
        "Quản lý keyword groups (AND/OR logic):\n"
        "• /trangthai — xem tổng quan: groups + cron + stats 24h\n"
        "• /groups — xem tất cả groups (gồm cả group đang tắt)\n"
        "• /addgroup Tên | all | kw1, kw2 — tạo group AND\n"
        "• /addgroup Tên | any | kw1, kw2 — tạo group OR\n"
        "• /removegroup Tên — xóa group\n"
        "• /addkw Tên | keyword — thêm keyword vào group\n"
        "• /removekw Tên | keyword — xóa keyword khỏi group\n"
        "• /renamegroup Tên cũ | Tên mới — đổi tên group\n"
        "• /tatgroup Tên — tắt group (cron bỏ qua, vẫn tra được bằng /timgroup)\n"
        "• /batgroup Tên — bật lại group đã tắt\n"
        "• /tatallgroup — tắt TẤT CẢ tạm thời (cron rảnh)\n"
        "• /batallgroup — bật lại tất cả\n"
        "• /huyhetgroup — XÓA HẾT group (reset, có confirm)\n"
        "• /timgroup Tên — tìm kiếm ngay theo từ khóa của group\n"
        "• /testkw từ khóa thử — debug group nào match\n\n"
        "Quản lý dữ liệu:\n"
        "• /chitiet MÃ — đọc chi tiết gói trên cổng (auto fill data)\n"
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
        "• /loc — bộ lọc nâng cao (lĩnh vực, hình thức, phạm vi)\n"
        "\nQuản lý groups:\n"
        "• /trangthai — tổng quan groups + cron + stats\n"
        "• /groups — xem tất cả groups (gồm cả tắt)\n"
        "• /addgroup Tên | all|any | kw1, kw2\n"
        "• /removegroup /renamegroup /addkw /removekw\n"
        "• /tatgroup /batgroup Tên — tắt/bật một group\n"
        "• /tatallgroup /batallgroup — tắt/bật tất cả\n"
        "• /huyhetgroup — xóa hết (reset, có confirm)\n"
        "• /testkw từ khóa — debug group nào match\n"
        "\nDữ liệu:\n"
        "• /chitiet MÃ — đọc chi tiết gói trên cổng (auto fill)\n"
        "• /xem MÃ_TBMT — tra mã trong DB\n"
        "• /timhom [hours] — gói thấy trong N giờ (mặc định 24h)\n"
        "• /xoa MÃ — admin: xóa khỏi DB để cron gửi lại\n"
        "• /thongke /lichsu [n] /chuagui /stats\n"
        "• /crawllogs [n] — n lần cào gần nhất (mặc định 5)\n"
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

    if cmd == "/start":
        return (
            "Chào mừng đến với DauThauBot!\n\n"
            "Bot tự động theo dõi đấu thầu trên muasamcong.mpi.gov.vn.\n"
            "Dùng nút Menu bên dưới để bắt đầu, hoặc /help xem hướng dẫn đầy đủ."
        )

    if cmd in ("/help", "/gioithieu"):
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
        title, seen_at, sent, extras = row
        flag = "Đã gửi Telegram" if sent else "Chưa gửi Telegram"
        short_at = seen_at[:19].replace("T", " ") if len(seen_at) >= 19 else seen_at
        notify_no = code.rsplit("-", 1)[0] if "-" in code else code
        search_link = (
            "https://muasamcong.mpi.gov.vn/web/guest/contractor-selection"
            "?p_p_id=egpportalcontractorselectionv2_WAR_egpportalcontractorselectionv2"
            "&p_p_lifecycle=0&p_p_state=normal&p_p_mode=view"
            "&_egpportalcontractorselectionv2_WAR_egpportalcontractorselectionv2_render=index"
        )

        extras_lines: list[str] = []
        if extras.get("investor"):
            extras_lines.append(f"<b>Chủ đầu tư:</b> {html.escape(extras['investor'])}")
        if extras.get("location"):
            extras_lines.append(f"<b>Địa điểm:</b> {html.escape(extras['location'])}")
        gg = _human_vnd(extras.get("budget_vnd"))
        if gg:
            extras_lines.append(f"<b>Giá gói:</b> {gg} (~{extras['budget_vnd']:,} VNĐ)")
        hdt = _short_closing(extras.get("closing_at"))
        if hdt:
            extras_lines.append(f"<b>Đóng thầu:</b> {hdt}")
        if extras.get("bid_form"):
            extras_lines.append(f"<b>Hình thức LCNT:</b> {html.escape(extras['bid_form'])}")
        if extras.get("bid_mode"):
            extras_lines.append(f"<b>Phương thức:</b> {html.escape(extras['bid_mode'])}")

        head = (
            f"<b>Mã TBMT:</b> <code>{html.escape(code)}</code>\n"
            f"<b>Tiêu đề:</b> {html.escape(title)}\n"
            f"<b>Trạng thái gửi:</b> {flag}\n"
            f"<b>Thấy lúc:</b> {short_at} UTC"
        )
        body = ("\n" + "\n".join(extras_lines)) if extras_lines else ""
        footer = (
            f'\n\n🔗 <a href="{search_link}">Tìm trên muasamcong</a> '
            f"(tìm mã <code>{html.escape(notify_no)}</code>)\n"
            "Gõ /chitiet để đọc đầy đủ từ cổng."
        )
        return head + body + footer

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

    if cmd in ("/trangthai", "/status"):
        init_db()
        all_groups = list_all_groups_raw()
        active = [g for g in all_groups if g[3]]
        inactive = [g for g in all_groups if not g[3]]
        total_active_kws = sum(len(g[4]) for g in active)

        from datetime import datetime as _dt, timezone as _tz
        utc_now = _dt.now(_tz.utc).strftime("%d/%m %H:%M")
        h24 = count_sent_since_hours(24)
        d7 = count_sent_since(7)
        tot_db = total_bids_in_db()
        unsent_db = count_unsent_in_db()

        lines = ["📊 <b>Trạng thái DauThauBot</b>", ""]

        # Section 1: Keyword groups
        lines.append("<b>1. Keyword groups (cron đang theo dõi)</b>")
        if not all_groups:
            lines.append("  ⚠️ Chưa có group nào — cron sẽ chạy nhưng không lọc gì.")
            lines.append("  Dùng /addgroup để tạo, hoặc /goiy để gợi ý từ data thực.")
        else:
            lines.append(
                f"  ▶ Đang bật: <b>{len(active)}</b> group ({total_active_kws} từ khóa)"
            )
            if inactive:
                lines.append(f"  ⏸ Tạm tắt: {len(inactive)} group")
            for g in active[:10]:
                gid, name, require, _, kws = g
                req_lbl = "AND" if require == "all" else "OR"
                kw_preview = ", ".join(kws[:3]) + (f"… +{len(kws)-3}" if len(kws) > 3 else "")
                name_esc = html.escape(name)
                kw_esc = html.escape(kw_preview)
                lines.append(f"   • <b>{name_esc}</b> [{req_lbl}] — {kw_esc}")
            if len(active) > 10:
                lines.append(f"   … và {len(active)-10} group nữa (xem /groups)")

        # Section 2: Cron config
        lines += [
            "",
            "<b>2. Cron / thiết lập cào</b>",
            f"  • Chu kỳ: <b>{secrets.poll_interval_minutes} phút</b> (±{secrets.poll_jitter_seconds}s jitter)",
            f"  • Giờ yên (VN): {secrets.quiet_hours_start}–{secrets.quiet_hours_end}",
            f"  • Mỗi lần cào: {secrets.crawl_max_pages} trang × {secrets.crawl_page_size} gói",
            f"  • Per-keyword: {'bật' if secrets.crawl_per_keyword else 'tắt'}",
            f"  • Playwright: {'on' if secrets.use_playwright else 'off'} "
            f"(headless={'on' if secrets.playwright_headless else 'off'})",
        ]

        # Section 3: Stats + Catalog
        try:
            tender_total = count_tenders()
            tender_open = count_tenders(open_only=True)
            last_crawl = get_last_crawl_time()
            if last_crawl:
                from datetime import datetime as _dt2, timezone as _tz2
                delta_min = int((_dt2.now(_tz2.utc) - last_crawl).total_seconds() / 60)
                if delta_min < 60:
                    last_crawl_str = f"{delta_min} phút trước"
                else:
                    last_crawl_str = f"{delta_min // 60}h{delta_min % 60:02d}m trước"
            else:
                last_crawl_str = "Chưa có dữ liệu"
        except Exception:
            tender_total = tender_open = 0
            last_crawl_str = "?"

        db_mode = "bật ⚡" if getattr(secrets, "db_search_enabled", True) else "tắt"
        lines += [
            "",
            "<b>3. Hoạt động</b>",
            f"  • Đã gửi 24h: <b>{h24}</b> | 7 ngày: {d7}",
            f"  • seen.db: {tot_db} gói (chưa gửi: {unsent_db})",
            f"  • Catalog tenders: <b>{tender_total}</b> gói ({tender_open} đang mở)",
            f"  • Cập nhật catalog lần cuối: {last_crawl_str}",
            f"  • DB-search (/tim): {db_mode}",
            f"  • Site circuit breaker: {_crawler_site_status()}",
            f"  • Bây giờ (UTC): {utc_now}",
        ]

        # Section 4: Lệnh quản lý
        lines += [
            "",
            "<b>4. Quản lý nhanh</b>",
            "  /tatallgroup — tạm tắt tất cả (cron rảnh)",
            "  /batallgroup — bật lại tất cả",
            "  /huyhetgroup — xóa hết group (reset, cần confirm)",
            "  /crawllogs — lịch sử crawl gần nhất",
            "  /addgroup, /addkw — thêm group/từ khóa",
        ]
        return "\n".join(lines)

    if cmd in ("/crawllogs", "/lshcrawl"):
        init_db()
        n = _parse_positive_int(rest, default=5, max_v=20)
        logs = list_crawl_logs(n)
        if not logs:
            return "Chưa có log cào nào. Chạy /test hoặc đợi cron tiếp theo."
        lines = [f"📋 <b>{n} lần cào gần nhất</b>"]
        for lg in logs:
            started = (lg.get("started_at") or "")[:16].replace("T", " ")
            dur = lg.get("duration_ms")
            dur_s = f"{dur/1000:.0f}s" if dur else "?"
            status = lg.get("status") or "?"
            status_icon = "✅" if status == "success" else ("⚠️" if status == "partial_failed" else "❌")
            found = lg.get("total_found", 0)
            new_ = lg.get("total_new", 0)
            sent_ = lg.get("total_sent", 0)
            job = lg.get("job_type", "cron")
            kws = lg.get("keywords") or ""
            kw_short = (kws[:40] + "…") if len(kws) > 40 else kws
            line = (
                f"{status_icon} <code>{started}</code> [{job}] {dur_s}\n"
                f"   found={found} new={new_} sent={sent_}"
            )
            if kw_short:
                line += f"\n   kw: {html.escape(kw_short)}"
            err = lg.get("error_message")
            if err:
                line += f"\n   ⚠ {html.escape(str(err)[:80])}"
            lines.append(line)
        return "\n".join(lines)

    if cmd in ("/tatallgroup", "/disableall"):
        if not _is_privileged(secrets, chat_id=chat_id, user_id=user_id):
            return "Lệnh /tatallgroup chỉ dành cho admin."
        init_db()
        n = disable_all_groups()
        if n == 0:
            return "Không có group nào đang bật — không cần tắt."
        return (
            f"⏸ Đã tắt {n} group. Cron sẽ KHÔNG dùng từ khóa nào nữa "
            "(vẫn cào TBMT mới nhưng không lọc).\n"
            "/batallgroup để bật lại, /groups để xem."
        )

    if cmd in ("/batallgroup", "/enableall"):
        if not _is_privileged(secrets, chat_id=chat_id, user_id=user_id):
            return "Lệnh /batallgroup chỉ dành cho admin."
        init_db()
        n = enable_all_groups()
        if n == 0:
            return "Tất cả group đã bật sẵn."
        return f"▶️ Đã bật lại {n} group. Cron sẽ dùng lại các từ khóa."

    if cmd in ("/timhom", "/today"):
        hours = _parse_positive_int(rest, default=24, max_v=168, min_v=1)
        init_db()
        rows = list_bids_since_hours(hours)
        if not rows:
            h_label = f"{hours}h" if hours != 24 else "24h"
            return f"Không thấy gói nào trong {h_label} vừa qua."
        lines = []
        for code, title, seen_at, sent, extras in rows:
            flag = "gửi" if sent else "chưa"
            short_time = seen_at[11:16] if len(seen_at) >= 16 else seen_at
            extras_line = _compact_extras(extras)
            tail = f"\n  {short_time} UTC"
            if extras_line:
                tail += f"  ·  {extras_line}"
            lines.append(f"• [{flag}] {code} — {_truncate(title, 58)}{tail}")
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
        for code, title, seen_at, sent, extras in rows:
            flag = "đã gửi" if sent else "chưa gửi"
            short_at = seen_at[:19].replace("T", " ") if len(seen_at) >= 19 else seen_at
            extras_line = _compact_extras(extras)
            tail = f"\n  {short_at} UTC"
            if extras_line:
                tail += f"  ·  {extras_line}"
            lines.append(f"• [{flag}] {code} — {_truncate(title, 72)}{tail}")
        return f"{len(rows)} tin gần nhất:\n" + "\n".join(lines)

    if cmd in ("/chuagui", "/unsent"):
        init_db()
        rows = list_unsent()
        if not rows:
            return "Không có gói chưa gửi (sent_to_telegram=0)."
        cap = 15
        lines: list[str] = []
        for c, t, extras in rows[:cap]:
            extras_line = _compact_extras(extras)
            tail = f"  ·  {extras_line}" if extras_line else ""
            lines.append(f"• {c} — {_truncate(t, 80)}{tail}")
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
        _filter_state.pop(ukey, None)
        _await_group_action.pop(ukey, None)
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

    if cmd in ("/chitiet", "/detail"):
        _execute_detail_fetch(secrets, rest, chat_id, cid_s)
        return

    if cmd in ("/huyhetgroup", "/wipegroups", "/resetgroups"):
        if not _is_privileged(secrets, chat_id=chat_id, user_id=int(uid)):
            _reply(bot_token, chat_id, "Lệnh /huyhetgroup chỉ dành cho admin.")
            return
        init_db()
        all_groups = list_all_groups_raw()
        if not all_groups:
            _reply(bot_token, chat_id, "Không có group nào để xóa — DB trống rồi.")
            return
        total_kws = sum(len(g[4]) for g in all_groups)
        _reply(
            bot_token,
            chat_id,
            (
                f"⚠️ <b>XÓA TẤT CẢ KEYWORD GROUPS</b>\n\n"
                f"Sẽ xóa: <b>{len(all_groups)}</b> group ({total_kws} từ khóa).\n"
                "Không đụng vào seen.db (lịch sử bid vẫn còn).\n\n"
                "Bấm XÁC NHẬN để xóa, hoặc Hủy để giữ nguyên."
            ),
            parse_html=True,
            reply_markup=_kb([
                [_btn("✅ XÁC NHẬN XÓA HẾT", "wipeall|confirm")],
                [_btn("❌ Hủy", "wipeall|cancel"), _btn("🏠 Menu", "menu")],
            ]),
        )
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

    if cmd == "/loc":
        fstate = _filter_state.setdefault(ukey, {"fields": [], "method": None, "closed": False})
        _show_loc_panel(bot_token, chat_id, fstate)
        return

    routed = handle_slash(
        text.strip(),
        secrets,
        chat_type,
        chat_id=chat_id,
        user_id=int(uid),
    )
    if routed:
        parse_html_cmds = ("/id", "/ma", "/xem", "/lookup", "/trangthai", "/status", "/crawllogs", "/lshcrawl")
        kb: Optional[dict] = None
        _groups_cmds = (
            "/groups", "/keywords",
            "/addgroup", "/removegroup", "/addkw", "/removekw",
            "/renamegroup", "/tatgroup", "/disablegroup", "/batgroup", "/enablegroup",
            "/tatallgroup", "/disableall", "/batallgroup", "/enableall",
            "/trangthai", "/status",
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
            if cmd in ("/timhom", "/today"):
                kb = _timhom_kb()
            else:
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

    # ── xử lý nhập liệu cho thao tác group (addkw) ──────────────────────────
    if ukey in _await_group_action:
        gact = _await_group_action.pop(ukey)
        keyword_input = text.strip()
        if not keyword_input or keyword_input == "/hủy":
            _reply(bot_token, chat_id, "Đã hủy.", reply_markup=_main_menu_kb())
            return
        if gact.get("action") == "addkw":
            gid_act = gact["gid"]
            gname_act = gact["name"]
            init_db()
            ok = add_keyword_to_group(gname_act, keyword_input)
            if ok:
                _reply(bot_token, chat_id, f'✅ Đã thêm "{keyword_input}" vào group "{gname_act}"')
            else:
                _reply(bot_token, chat_id,
                       f'Không thêm được "{keyword_input}" — từ khóa đã tồn tại hoặc group không tìm thấy.')
            _show_group_detail(bot_token, chat_id, gid_act)
        return

    if _await_keyword.get(ukey):
        raw_text = text.strip()
        fstate = _filter_state.pop(ukey, {})
        inc_closed = _await_include_closed.pop(ukey, False)
        # Override inc_closed from filter state if it was set via /loc
        if fstate:
            inc_closed = fstate.get("closed", inc_closed)
        field_f: Optional[list[str]] = fstate.get("fields") or None
        method_f: Optional[int] = fstate.get("method")  # None=all, 0 or 1

        # "-" gửi một dấu gạch ngang → tìm không cần từ khóa (chỉ bộ lọc)
        if raw_text == "-" and fstate:
            _await_keyword.pop(ukey, None)
            _execute_search(
                secrets, [], chat_id, cid_s,
                mode="any",
                include_closed=inc_closed,
                field_filter=field_f,
                bid_method_filter=method_f,
            )
            return

        phrases, search_mode = parse_search_query(raw_text)
        if not phrases and not fstate:
            _reply(bot_token, chat_id,
                   "Chưa có từ khóa. Ví dụ: camera  hoặc  camera & lâm đồng (AND).\n"
                   "Gửi - (dấu gạch ngang) để tìm chỉ theo bộ lọc đã chọn. /hủy để thoát.")
            return
        _await_keyword.pop(ukey, None)
        _execute_search(
            secrets, phrases, chat_id, cid_s,
            mode=search_mode,
            include_closed=inc_closed,
            field_filter=field_f,
            bid_method_filter=method_f,
        )
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

    # ── cmd|/loc — mở bảng lọc nâng cao (stateful — không qua handle_slash) ──
    if data == "cmd|/loc":
        fstate = _filter_state.setdefault(ukey, {"fields": [], "method": None, "closed": False})
        _show_loc_panel(token, chat_id, fstate)
        return

    # ── cmd|/timhom — hiện gói 24h + bộ chọn khung giờ ──────────────────
    if data == "cmd|/timhom":
        init_db()
        rows = list_bids_since_hours(24)
        if not rows:
            _reply(token, chat_id, "Không thấy gói nào trong 24 giờ vừa qua.", reply_markup=_timhom_kb())
        else:
            lines = []
            for code, title, seen_at, sent in rows:
                flag = "✅" if sent else "⏳"
                short_time = seen_at[11:16] if len(seen_at) >= 16 else ""
                lines.append(f"{flag} {short_time}  {_truncate(title, 52)}\n   {code}")
            _reply(
                token, chat_id,
                f"Gói thấy trong 24 giờ ({len(rows)} gói):\n\n" + "\n".join(lines),
                reply_markup=_timhom_kb(),
            )
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

    # ── search|open / search|closed — prompt với tỉnh/TP nhanh ─────────────
    if data.startswith("search|"):
        inc_closed = data[7:] == "closed"
        _await_keyword[ukey] = True
        _await_include_closed[ukey] = inc_closed
        scope_tag = "[Tìm tất cả — kể cả đã đóng]\n\n" if inc_closed else ""
        _reply(
            token, chat_id,
            f"{scope_tag}Gõ từ khóa hoặc chọn nhanh tỉnh/TP bên dưới:\n\n"
            "  camera\n"
            "  camera & lâm đồng   (AND — tất cả phải khớp)\n"
            "  camera | cctv       (OR — bất kỳ khớp)\n"
            "  Công an tỉnh Lâm Đồng   (tên cơ quan)\n",
            reply_markup=_search_prompt_kb(inc_closed),
        )
        return

    # ── grp|<db_id> — mở chi tiết group ─────────────────────────────────
    if data.startswith("grp|") and "|" not in data[4:]:
        try:
            gid = int(data[4:])
        except ValueError:
            _reply(token, chat_id, "Nút không hợp lệ.")
            return
        _show_group_detail(token, chat_id, gid)
        return

    # ── grpsearch|<gid> — tra ngay group (gói mở) ────────────────────────
    if data.startswith("grpsearch|") and not data.startswith("grpsearchclosed|"):
        try:
            gid = int(data[10:])
        except ValueError:
            return
        init_db()
        row = get_group_by_id(gid)
        if row is None:
            _reply(token, chat_id, "Không tìm thấy group.")
            return
        g_name, g_require, g_kws = row
        if not g_kws:
            _reply(token, chat_id, f'Group "{g_name}" chưa có từ khóa. Dùng nút Thêm từ khóa.',
                   reply_markup=_group_detail_kb(gid, True))
            return
        _reply(token, chat_id, f'Đang tra group "{g_name}" — {len(g_kws)} từ khóa (gói mở)…')
        _execute_search(secrets, g_kws, chat_id, cid_s, mode=g_require)
        return

    # ── grpsearchclosed|<gid> — tra group kể cả đã đóng ─────────────────
    if data.startswith("grpsearchclosed|"):
        try:
            gid = int(data[16:])
        except ValueError:
            return
        init_db()
        row = get_group_by_id(gid)
        if row is None:
            _reply(token, chat_id, "Không tìm thấy group.")
            return
        g_name, g_require, g_kws = row
        if not g_kws:
            _reply(token, chat_id, f'Group "{g_name}" chưa có từ khóa.')
            return
        _reply(token, chat_id, f'Đang tra group "{g_name}" — {len(g_kws)} từ khóa (kể cả đóng)…')
        _execute_search(secrets, g_kws, chat_id, cid_s, mode=g_require, include_closed=True)
        return

    # ── grptgl|<gid> — bật/tắt group ────────────────────────────────────
    if data.startswith("grptgl|"):
        try:
            gid = int(data[7:])
        except ValueError:
            return
        init_db()
        row = get_group_by_id(gid)
        if row is None:
            _reply(token, chat_id, "Không tìm thấy group.")
            return
        g_name, _, _ = row
        all_groups = list_all_groups_raw()
        g_active = next((g[3] for g in all_groups if g[0] == gid), True)
        new_active = not bool(g_active)
        toggle_group_active(g_name, new_active)
        status_msg = "▶ Đã bật" if new_active else "⏸ Đã tắt"
        _answer_callback(token, cq_id, f'{status_msg} group "{g_name}"')
        _show_group_detail(token, chat_id, gid)
        return

    # ── grpdel|<gid> — xác nhận xóa group ───────────────────────────────
    if data.startswith("grpdel|") and not data.startswith("grpdelok|"):
        try:
            gid = int(data[7:])
        except ValueError:
            return
        init_db()
        row = get_group_by_id(gid)
        if row is None:
            _reply(token, chat_id, "Group không còn tồn tại.")
            return
        g_name = row[0]
        _reply(
            token, chat_id,
            f'❗ Xác nhận xóa group "{g_name}"?\n\nThao tác này không thể hoàn tác.',
            reply_markup=_kb([
                [_btn("✅ Xóa", f"grpdelok|{gid}"), _btn("❌ Hủy", f"grp|{gid}")],
            ]),
        )
        return

    # ── grpdelok|<gid> — thực hiện xóa group ────────────────────────────
    if data.startswith("grpdelok|"):
        try:
            gid = int(data[9:])
        except ValueError:
            return
        init_db()
        row = get_group_by_id(gid)
        g_name = row[0] if row else f"#{gid}"
        ok = remove_group(g_name)
        if ok:
            _reply(
                token, chat_id,
                f'🗑 Đã xóa group "{g_name}".',
                reply_markup=_kb([[_btn("📋 Xem groups", "cmd|/groups"), _btn("🏠 Menu", "menu")]]),
            )
        else:
            _reply(token, chat_id, f'Không tìm thấy group "{g_name}" để xóa.',
                   reply_markup=_kb([[_btn("📋 Groups", "cmd|/groups"), _btn("🏠 Menu", "menu")]]))
        return

    # ── grpadkw|<gid> — nhập từ khóa mới để thêm vào group ──────────────
    if data.startswith("grpadkw|"):
        try:
            gid = int(data[8:])
        except ValueError:
            return
        init_db()
        row = get_group_by_id(gid)
        if row is None:
            _reply(token, chat_id, "Không tìm thấy group.")
            return
        g_name = row[0]
        _await_group_action[ukey] = {"action": "addkw", "gid": gid, "name": g_name}
        _reply(
            token, chat_id,
            f'Gõ từ khóa muốn thêm vào group "{g_name}":',
            reply_markup=_kb([[_btn("❌ Hủy", "huy")]]),
        )
        return

    # ── qs|<idx>|<scope> — tìm nhanh theo tỉnh/TP ───────────────────────
    if data.startswith("qs|"):
        parts = data.split("|")
        if len(parts) < 3:
            return
        try:
            idx = int(parts[1])
        except ValueError:
            return
        scope = parts[2]
        include_closed = scope == "c"
        if 0 <= idx < len(PROVINCE_QUICK):
            _name, kw = PROVINCE_QUICK[idx]
            _await_keyword.pop(ukey, None)
            _await_include_closed.pop(ukey, None)
            _filter_state.pop(ukey, None)
            _execute_search(secrets, [kw], chat_id, cid_s, include_closed=include_closed)
        else:
            _reply(token, chat_id, "Lựa chọn không hợp lệ.")
        return

    # ── timhom|<hours> — xem gói theo khoảng giờ ────────────────────────
    if data.startswith("timhom|"):
        try:
            hours = int(data[7:])
        except ValueError:
            hours = 24
        hours = max(1, min(hours, 720))
        init_db()
        rows = list_bids_since_hours(hours)
        if hours < 24:
            h_label = f"{hours} giờ"
        elif hours == 24:
            h_label = "24 giờ"
        elif hours % 24 == 0:
            h_label = f"{hours // 24} ngày"
        else:
            h_label = f"{hours} giờ"
        if not rows:
            _reply(token, chat_id, f"Không thấy gói nào trong {h_label} vừa qua.",
                   reply_markup=_timhom_kb())
            return
        lines = []
        for code, title, seen_at, sent in rows:
            flag = "✅" if sent else "⏳"
            short_time = seen_at[11:16] if len(seen_at) >= 16 else ""
            lines.append(f"{flag} {short_time}  {_truncate(title, 52)}\n   {code}")
        _reply(
            token, chat_id,
            f"Gói thấy trong {h_label} ({len(rows)} gói):\n\n" + "\n".join(lines),
            reply_markup=_timhom_kb(),
        )
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
        _filter_state.pop(ukey, None)
        _await_group_action.pop(ukey, None)
        _reply(token, chat_id, "Đã hủy.", reply_markup=_main_menu_kb())
        return

    # ── ct|<mã TBMT> — đọc chi tiết từ nút trên message bid ───────────────
    if data.startswith("ct|"):
        code = data[3:].strip()
        if not code:
            _reply(token, chat_id, "Nút Chi tiết không có mã.")
            return
        _execute_detail_fetch(secrets, code, chat_id, cid_s)
        return

    # ── wipeall|confirm hoặc wipeall|cancel — xóa hết keyword groups ────
    if data.startswith("wipeall|"):
        if not _is_privileged(secrets, chat_id=chat_id, user_id=int(uid)):
            _reply(token, chat_id, "Chỉ admin được dùng.")
            return
        choice = data[len("wipeall|"):]
        if choice == "cancel":
            _reply(token, chat_id, "❎ Đã hủy. Group giữ nguyên.",
                   reply_markup=_main_menu_kb())
            return
        if choice == "confirm":
            init_db()
            n = remove_all_groups()
            _reply(
                token, chat_id,
                f"🗑 Đã xóa {n} group. seen.db không bị ảnh hưởng.\n"
                "Cron sẽ chạy nhưng không lọc cho đến khi bạn /addgroup hoặc /goiy.",
                reply_markup=_main_menu_kb(),
            )
            return

    # ── nrw|<idx> hoặc nrw|cancel — auto-suggest hẹp dần sau /tim ────────
    if data.startswith("nrw|"):
        arg = data[4:].strip()
        if arg == "cancel":
            _narrow_state.pop(cid_s, None)
            _reply(token, chat_id, "Đã tắt gợi ý.", reply_markup=_main_menu_kb())
            return
        try:
            idx = int(arg)
        except ValueError:
            _reply(token, chat_id, "Nút không hợp lệ.")
            return
        _execute_narrow_click(secrets, chat_id, cid_s, idx)
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

    # ── floc|* — cập nhật bộ lọc /loc ────────────────────────────────────
    if data.startswith("floc|"):
        parts = data.split("|")
        action = parts[1] if len(parts) > 1 else ""
        value = parts[2] if len(parts) > 2 else ""

        fstate = _filter_state.setdefault(ukey, {"fields": [], "method": None, "closed": False})

        if action == "field":
            cur_fields: list[str] = fstate.setdefault("fields", [])
            if value in cur_fields:
                cur_fields.remove(value)  # toggle off
            else:
                cur_fields.append(value)  # toggle on

        elif action == "method":
            if value == "all":
                fstate["method"] = None
            else:
                try:
                    fstate["method"] = int(value)
                except ValueError:
                    fstate["method"] = None

        elif action == "scope":
            fstate["closed"] = value == "closed"

        elif action == "reset":
            fstate.clear()
            fstate.update({"fields": [], "method": None, "closed": False})

        elif action == "run":
            # Đặt trạng thái chờ từ khóa — filter đã lưu trong _filter_state
            _await_keyword[ukey] = True
            _await_include_closed[ukey] = fstate.get("closed", False)

            # Tóm tắt bộ lọc đã chọn
            cur_fields = fstate.get("fields") or []
            cur_method: Optional[int] = fstate.get("method")
            cur_closed: bool = fstate.get("closed", False)
            f_str = (
                ", ".join(next((lbl for c, lbl in FIELD_OPTIONS if c == f), f) for f in cur_fields)
                or "Tất cả lĩnh vực"
            )
            m_str = {None: "Tất cả hình thức", 1: "Qua mạng", 0: "Không qua mạng"}.get(cur_method, "Tất cả")
            s_str = "Kể cả đã đóng" if cur_closed else "Chỉ gói mở"

            has_filter = bool(cur_fields) or cur_method is not None
            extra = ""
            if has_filter or cur_closed:
                extra = (
                    f"\nBộ lọc đã đặt:\n"
                    f"  Lĩnh vực: {f_str}\n"
                    f"  Hình thức: {m_str}\n"
                    f"  Phạm vi:   {s_str}\n"
                )

            _reply(
                token, chat_id,
                f"{extra}\nGõ từ khóa cần tìm (hoặc gửi - để tìm chỉ theo bộ lọc):\n\n"
                "  camera\n"
                "  camera & lâm đồng   (AND — tất cả phải khớp)\n"
                "  camera | cctv       (OR — bất kỳ khớp)\n\n"
                "/hủy để thoát.",
                reply_markup=_kb([[_btn("❌ Hủy", "huy")]]),
            )
            return

        # Cập nhật panel sau khi thay đổi field/method/scope/reset
        _show_loc_panel(token, chat_id, fstate)
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
