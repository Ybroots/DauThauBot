from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlencode

from .models import Bid

BASE_URL = "https://muasamcong.mpi.gov.vn"
DETAIL_V2_BASE = (
    f"{BASE_URL}/web/guest/contractor-selection"
    "?p_p_id=egpportalcontractorselectionv2_WAR_egpportalcontractorselectionv2"
    "&p_p_lifecycle=0&p_p_state=normal&p_p_mode=view"
    "&_egpportalcontractorselectionv2_WAR_egpportalcontractorselectionv2_render=detail-v2"
)

INVEST_FIELD_NAMES: dict[str, str] = {
    "HH": "Hàng hóa",
    "XL": "Xây lắp",
    "TV": "Tư vấn",
    "PTV": "Phi tư vấn",
    "HON_HOP": "Hỗn hợp",
}


def _parse_dt(value: Any) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return datetime.now(timezone.utc)


def _first_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return str(value[0]) if value else ""
    return str(value)


def _format_location(locations: Any) -> str:
    if not locations or not isinstance(locations, list):
        return ""
    parts: list[str] = []
    for lc in locations:
        if not isinstance(lc, dict):
            continue
        district = lc.get("districtName") or ""
        prov = lc.get("provName") or ""
        segment = f"{district} - {prov}".strip(" -")
        if segment:
            parts.append(segment + ";")
    return " ".join(parts)


def _status_label(item: dict[str, Any]) -> str:
    code = item.get("statusForNotify")
    labels = {
        "DHTBMT": "Đã hủy TBMT",
        "KCNTTT": "Không có nhà thầu trúng thầu",
        "CNTTT": "Có nhà thầu trúng thầu",
        "DHT": "Đã huỷ thầu",
        "DHKQLCNT": "Đã huỷ KQLCNT",
        "DXT": "Đang xét thầu",
        "VHH": "Tuyên bố vô hiệu quyết định về KQLCNT",
        "KCN": "Không công nhận KQLCNT",
        "DC": "Đình chỉ cuộc thầu",
    }
    if code in labels:
        return labels[code]

    close = _parse_dt(item.get("bidCloseDate"))
    now = datetime.now(timezone.utc)
    if close.tzinfo is None:
        close = close.replace(tzinfo=timezone.utc)
    if close > now:
        return "Chưa đóng thầu"
    if item.get("isInternet") == 0:
        return "Đang xét thầu"
    return "Chưa mở thầu"


def _invest_field_label(item: dict[str, Any]) -> str:
    raw = item.get("investField") or item.get("bidField")
    if isinstance(raw, list) and raw:
        code = str(raw[0])
    elif raw:
        code = str(raw)
    else:
        return ""
    return INVEST_FIELD_NAMES.get(code, code)


def _detail_url(item: dict[str, Any]) -> str:
    params = {
        "type": item.get("type") or "es-notify-contractor",
        "stepCode": item.get("stepCode") or "",
        "id": item.get("id") or "",
        "notifyId": item.get("notifyId") or item.get("id") or "",
        "inputResultId": item.get("inputResultId") or "undefined",
        "bidOpenId": item.get("bidOpenId") or "undefined",
        "techReqId": item.get("techReqId") or "undefined",
        "bidPreNotifyResultId": item.get("bidPreNotifyResultId") or "undefined",
        "bidPreOpenId": item.get("bidPreOpenId") or "undefined",
        "processApply": item.get("processApply") or "undefined",
        "bidMode": item.get("bidMode") or "undefined",
        "notifyNo": item.get("notifyNo") or "",
        "planNo": item.get("planNo") or "undefined",
        "pno": item.get("pno") or "undefined",
        "step": "tbmt",
        "isInternet": item.get("isInternet", "undefined"),
        "caseKHKQ": item.get("caseKHKQ") or "undefined",
        "bidForm": item.get("bidForm") or "undefined",
    }
    return f"{DETAIL_V2_BASE}&{urlencode(params)}"


def parse_search_item(item: dict[str, Any], field_names: Optional[dict[str, str]] = None) -> Bid:
    names = field_names or INVEST_FIELD_NAMES
    notify_no = item.get("notifyNo") or ""
    version = item.get("notifyVersion") or "00"
    tbmt = item.get("notifyNoStand") or f"{notify_no}-{version}"

    investor = item.get("investorName") or item.get("procuringEntityName") or ""
    title = _first_text(item.get("bidName"))

    raw_field = item.get("investField") or item.get("bidField")
    if isinstance(raw_field, list) and raw_field:
        code = str(raw_field[0])
    elif raw_field:
        code = str(raw_field)
    else:
        code = ""
    field_label = names.get(code, code) if code else _invest_field_label(item)

    budget = item.get("bidPrice") or item.get("bidEstimatePrice")
    budget_vnd: Optional[int] = None
    if budget is not None:
        try:
            budget_vnd = int(budget)
        except (TypeError, ValueError):
            budget_vnd = None

    is_internet = item.get("isInternet")
    bid_method = "Qua mạng" if is_internet == 1 else "Không qua mạng"

    return Bid(
        tbmt_code=tbmt,
        title=title,
        status=_status_label(item),
        investor=investor,
        posted_at=_parse_dt(item.get("publicDate")),
        field=field_label,
        location=_format_location(item.get("locations")),
        closing_at=_parse_dt(item.get("bidCloseDate")),
        bid_method=bid_method,
        detail_url=_detail_url(item),
        budget_vnd=budget_vnd,
        description=_first_text(item.get("bidName")),
        raw=item,
    )


def parse_search_response(
    data: dict[str, Any],
    field_names: Optional[dict[str, str]] = None,
) -> list[Bid]:
    page = data.get("page") or {}
    content = page.get("content") or []
    bids: list[Bid] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") not in (None, "es-notify-contractor"):
            continue
        bids.append(parse_search_item(item, field_names))
    return bids
