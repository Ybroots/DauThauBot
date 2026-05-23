from __future__ import annotations

from .models import Bid
from .parser import extract_bid_extras


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _hashtag(kw: str) -> str:
    tag = kw.replace(" ", "_")
    tag = "".join(c for c in tag if c.isalnum() or c in "_-")
    return f"#{tag}" if tag else ""


def format_bid_message(bid: Bid, matched_keywords: list[str]) -> str:
    closing_date = bid.closing_at.strftime("%d/%m/%Y")
    closing_time = bid.closing_at.strftime("%H:%M")
    posted = bid.posted_at.strftime("%d/%m/%Y - %H:%M")

    msg = (
        f"<b>Mã TBMT:</b> <code>{_esc(bid.tbmt_code)}</code>\n"
        f"<b>Trạng thái:</b> {_esc(bid.status)}\n"
        f"<b>{_esc(bid.title)}</b>\n\n"
        f"<b>Chủ đầu tư:</b> {_esc(bid.investor)}\n"
        f"<b>Ngày đăng:</b> {posted}\n"
        f"<b>Lĩnh vực:</b> {_esc(bid.field)}\n"
        f"<b>Địa điểm:</b> {_esc(bid.location)}\n"
        f"<b>Đóng thầu:</b> {closing_time} {closing_date}\n"
        f"<b>Hình thức:</b> {_esc(bid.bid_method)}\n"
    )
    if bid.budget_vnd:
        msg += f"<b>Giá gói:</b> {bid.budget_vnd:,} VNĐ\n"
    if matched_keywords:
        tags = " ".join(_hashtag(kw) for kw in matched_keywords if _hashtag(kw))
        if tags:
            msg += f"\n{tags}\n"
    msg += f'\n🔗 <a href="{bid.detail_url}">Xem chi tiết</a>'
    return msg


def format_bid_detail(bid: Bid) -> str:
    """Bản đầy đủ: gồm tất cả trường của format_bid_message + extras đọc từ raw API."""
    msg = format_bid_message(bid, matched_keywords=[])
    extras = extract_bid_extras(bid.raw or {})
    if not extras:
        return msg

    lines = ["", "<b>── Chi tiết bổ sung ──</b>"]
    for label, value in extras.items():
        lines.append(f"<b>{_esc(label)}:</b> {_esc(value)}")
    insert_marker = "\n🔗 "
    if insert_marker in msg:
        head, tail = msg.split(insert_marker, 1)
        return head + "\n" + "\n".join(lines) + insert_marker + tail
    return msg + "\n" + "\n".join(lines)
