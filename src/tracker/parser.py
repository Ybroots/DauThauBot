from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import parse_qs, urlencode, urlparse

from .models import Bid

# Mã TBMT chuẩn cổng: tiền tố chữ + số. notifyNo thường dạng "IB2500579539" hoặc số thuần.
# notifyNoStand = notifyNo + "-" + version (vd. "-00"). Cho phép paste cả 2 dạng.
_TBMT_CODE_RE = re.compile(r"\b([A-Z]{2,4}\d{6,14}|\d{8,14})(?:-(\d{1,3}))?\b", re.IGNORECASE)

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

PROCESS_APPLY_NAMES: dict[str, str] = {
    "LDT": "Luật đấu thầu",
    "NDT": "Nhà đầu tư",
    "ODA": "ODA / vốn vay",
}

BID_MODE_NAMES: dict[str, str] = {
    "1_MTHS": "Một giai đoạn một túi hồ sơ",
    "1_MTHS_2_PT": "Một giai đoạn hai túi hồ sơ",
    "2_MTHS": "Hai giai đoạn một túi hồ sơ",
    "2_MTHS_2_PT": "Hai giai đoạn hai túi hồ sơ",
}

BID_FORM_NAMES: dict[str, str] = {
    "DTRR": "Đấu thầu rộng rãi",
    "DTHC": "Đấu thầu hạn chế",
    "CDT": "Chỉ định thầu",
    "CTH": "Chào hàng cạnh tranh",
    "CHCT": "Chào hàng cạnh tranh",
    "CHCT_RG": "Chào hàng cạnh tranh rút gọn",
    "MSTT": "Mua sắm trực tiếp",
    "TLBT": "Tự thực hiện",
    "DBDT": "Đặc biệt",
}

STATUS_FOR_NOTIFY_NAMES: dict[str, str] = {
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


def _label(table: dict[str, str], code: object) -> str:
    """Trả label nếu có trong table, không thì trả nguyên code (hoặc '')."""
    if code is None:
        return ""
    s = str(code).strip()
    if not s or s.lower() == "undefined":
        return ""
    return table.get(s, s)


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


def parse_tbmt_input(raw: str) -> tuple[str, Optional[str]]:
    """Lấy (notifyNo, version) từ input của người dùng.

    Hỗ trợ:
    - "IB2500579539"                    → ("IB2500579539", None)
    - "ib2500579539-00"                 → ("IB2500579539", "00")
    - URL detail của cổng (đọc notifyNo) → ("IB2500579539", None hoặc version trong URL)

    Trả ("", None) nếu không nhận diện được.
    """
    s = (raw or "").strip()
    if not s:
        return "", None

    if "://" in s or s.lower().startswith("muasamcong"):
        try:
            url = s if "://" in s else f"https://{s}"
            q = parse_qs(urlparse(url).query)
            notify_no = (q.get("notifyNo") or [""])[0].strip().upper()
            if notify_no:
                version = (q.get("notifyVersion") or [""])[0].strip() or None
                return notify_no, version
        except (ValueError, TypeError):
            pass

    m = _TBMT_CODE_RE.search(s)
    if m:
        return m.group(1).upper(), (m.group(2) or None)
    return "", None


def _fmt_vn_datetime(value: Any) -> str:
    """ISO/UTC → 'HH:MM dd/mm/YYYY' giờ VN (+7). '' nếu không parse được."""
    if not value:
        return ""
    try:
        dt = _parse_dt(value)
        # _parse_dt trả tz-aware UTC; cộng 7 giờ cho giờ VN trong hiển thị
        from datetime import timedelta
        vn = dt + timedelta(hours=7)
        return vn.strftime("%H:%M %d/%m/%Y")
    except (ValueError, TypeError):
        return ""


def _safe_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def extract_bid_extras(item: dict[str, Any]) -> dict[str, str]:
    """Trả các trường detail bổ sung (decoded) chưa có trong dataclass Bid.

    Surface tất cả field có ích trong search response — kể cả những field bot
    chưa hiển thị trước đây (bidOpenDate, numBidderTech, numPetition*…)
    để /chitiet giàu thông tin hơn mà không cần gọi detail API riêng.
    """
    if not isinstance(item, dict):
        return {}
    extras: dict[str, str] = {}

    investor = (item.get("investorName") or "").strip()
    procuring = (item.get("procuringEntityName") or "").strip()
    if procuring and procuring != investor:
        extras["Bên mời thầu"] = procuring

    plan_no = (item.get("planNo") or "").strip()
    if plan_no and plan_no.lower() != "undefined":
        extras["Kế hoạch số"] = plan_no

    bid_form = _label(BID_FORM_NAMES, item.get("bidForm"))
    if bid_form:
        extras["Hình thức LCNT"] = bid_form

    bid_mode = _label(BID_MODE_NAMES, item.get("bidMode"))
    if bid_mode:
        extras["Phương thức"] = bid_mode

    process = _label(PROCESS_APPLY_NAMES, item.get("processApply"))
    if process:
        extras["Luật áp dụng"] = process

    # Thời gian mở thầu — KHÁC bidCloseDate (= hạn nộp HSDT). Cổng phân biệt rõ.
    open_dt = _fmt_vn_datetime(item.get("bidOpenDate"))
    if open_dt:
        extras["Mở thầu"] = open_dt

    # Đăng lần đầu — khác publicDate khi gói đã được sửa/cập nhật
    orig = item.get("originalPublicDate")
    pub = item.get("publicDate")
    if orig and orig != pub:
        orig_fmt = _fmt_vn_datetime(orig)
        if orig_fmt:
            extras["Đăng lần đầu"] = orig_fmt

    # Đã có nhà thầu đăng ký HSDT
    n_tech = _safe_int(item.get("numBidderTech"))
    if n_tech and n_tech > 0:
        extras["Nhà thầu đã đăng ký"] = str(n_tech)

    # Yêu cầu làm rõ
    n_clarify = _safe_int(item.get("numClarifyReq"))
    if n_clarify and n_clarify > 0:
        extras["Yêu cầu làm rõ"] = str(n_clarify)

    # Tổng khiếu nại (HSMT + LCNT + KQLCNT + general)
    n_pet = sum(
        v for v in (
            _safe_int(item.get("numPetition")),
            _safe_int(item.get("numPetitionHsmt")),
            _safe_int(item.get("numPetitionLcnt")),
            _safe_int(item.get("numPetitionKqlcnt")),
        ) if v is not None
    )
    if n_pet > 0:
        extras["Khiếu nại / kiến nghị"] = str(n_pet)

    # Tag gói đặc biệt
    tags: list[str] = []
    if item.get("isMedicine") == 1:
        tags.append("Gói thuốc")
    if item.get("isDomestic") == 1:
        tags.append("Đấu thầu trong nước")
    if tags:
        extras["Tag"] = " · ".join(tags)

    estimate = item.get("bidEstimatePrice")
    price = item.get("bidPrice")
    try:
        if estimate is not None and (price is None or int(estimate) != int(price)):
            extras["Giá dự toán"] = f"{int(estimate):,} VNĐ"
    except (TypeError, ValueError):
        pass

    return extras


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
