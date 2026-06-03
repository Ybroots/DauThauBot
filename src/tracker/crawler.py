from __future__ import annotations

import json
import random
import ssl
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
from loguru import logger
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from .parser import INVEST_FIELD_NAMES, parse_search_response, parse_search_item

BASE_URL = "https://muasamcong.mpi.gov.vn"
HOMEPAGE_PATH = "/web/guest/contractor-selection"
SEARCH_ENDPOINT = "/o/egp-portal-contractor-selection-v2/services/smart/search"
CATEGORY_ENDPOINT = "/o/egp-portal-contractor-selection-v2/services/get/category"
INDEX_URL = (
    f"{BASE_URL}/web/guest/contractor-selection"
    "?p_p_id=egpportalcontractorselectionv2_WAR_egpportalcontractorselectionv2"
    "&p_p_lifecycle=0&p_p_state=normal&p_p_mode=view"
    "&_egpportalcontractorselectionv2_WAR_egpportalcontractorselectionv2_render=index"
)
RECAPTCHA_SITE_KEY = "6LfCo9gpAAAAAL1u9qzvWYSrZuYkFsFEjpVruyd5"


def _httpx_ssl_verify() -> ssl.SSLContext | bool:
    """OpenSSL 3 (Docker/Railway) hay báo DH_KEY_TOO_SMALL với muasamcong — hạ SECLEVEL."""
    try:
        ctx = ssl.create_default_context()
        ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
        return ctx
    except ssl.SSLError:
        return True


def _retry_search_if_transient(exc: BaseException) -> bool:
    """Quyết định có nên retry hay không.

    Không retry khi:
    - site key sai / invalid: lặp lại không giải quyết được
    - timeout (Playwright page.goto hoặc httpx connect): site không khả dụng,
      retry ngay sẽ cũng timeout → chỉ kéo dài thời gian block Telegram thread.
      Trường hợp IP bị chặn: fail-fast rồi để scheduler thử lại sau 45 phút.
    """
    msg = str(exc).lower()
    exc_name = type(exc).__name__.lower()

    if "invalid site key" in msg or "invalid key type" in msg:
        return False
    if "grecaptcha_execute_missing" in msg:
        return False
    # TimeoutError từ Playwright (page.goto timeout) hoặc httpx ConnectTimeout
    if "timeout" in msg or "timeout" in exc_name:
        return False
    # Connection reset / site unreachable
    if "connection" in exc_name and ("reset" in msg or "refused" in msg or "failed" in msg):
        return False
    return True

# ── Site circuit breaker ─────────────────────────────────────────────────────
# Theo dõi các lần site không khả dụng liên tiếp. Sau N lần → set cooldown,
# không mở Playwright thêm cho đến khi cooldown hết (giảm số session zombie).

import threading as _threading
from datetime import datetime as _dt, timezone as _tz_utc, timedelta as _td

_site_cb_lock = _threading.Lock()
_site_cb_failures = 0          # số lần timeout/unreachable liên tiếp
_site_cb_cooldown_until: "_dt | None" = None
_SITE_CB_THRESHOLD = 3         # Sau bao nhiêu lỗi liên tiếp thì set cooldown
_SITE_CB_COOLDOWN_MIN = 10     # Cooldown bao nhiêu phút


def _site_cb_record_failure() -> None:
    """Ghi nhận 1 lần site không khả dụng."""
    global _site_cb_failures, _site_cb_cooldown_until
    with _site_cb_lock:
        _site_cb_failures += 1
        if _site_cb_failures >= _SITE_CB_THRESHOLD:
            _site_cb_cooldown_until = _dt.now(_tz_utc()) + _td(minutes=_SITE_CB_COOLDOWN_MIN)
            logger.warning(
                "circuit_breaker: {} consecutive failures → site cooldown {}min until {}",
                _site_cb_failures,
                _SITE_CB_COOLDOWN_MIN,
                _site_cb_cooldown_until.strftime("%H:%M:%S"),
            )


def _site_cb_record_success() -> None:
    """Reset circuit breaker sau khi site phản hồi thành công."""
    global _site_cb_failures, _site_cb_cooldown_until
    with _site_cb_lock:
        if _site_cb_failures > 0:
            logger.info("circuit_breaker: reset after success (was {} failures)", _site_cb_failures)
        _site_cb_failures = 0
        _site_cb_cooldown_until = None


def _site_cb_is_open() -> bool:
    """True nếu đang trong cooldown (không nên thử Playwright)."""
    global _site_cb_cooldown_until
    with _site_cb_lock:
        if _site_cb_cooldown_until is None:
            return False
        if _dt.now(_tz_utc()) >= _site_cb_cooldown_until:
            _site_cb_cooldown_until = None
            _site_cb_failures = 0
            logger.info("circuit_breaker: cooldown expired — will retry site")
            return False
        return True


def site_status() -> str:
    """Trả chuỗi mô tả trạng thái circuit breaker — cho /trangthai."""
    with _site_cb_lock:
        if _site_cb_cooldown_until and _dt.now(_tz_utc()) < _site_cb_cooldown_until:
            mins = int((_site_cb_cooldown_until - _dt.now(_tz_utc())).total_seconds() / 60) + 1
            return f"⚠️ site cooldown {mins}min ({_site_cb_failures} lỗi liên tiếp)"
        return f"✅ ok (failures={_site_cb_failures})"


USER_AGENTS = [
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/121.0.6167.85 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0",
]


class BlockedException(Exception):
    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__(f"Server blocked request (HTTP {status_code})")


def _portal_open_bid_close_from_iso() -> str:
    """Giống cổng (convertFilters tab mở): push7HoursToDate(new Date()).toISOString().

    Cổng cộng 7 giờ vào mốc UTC rồi format Z — khác với datetime VN +07:00.
    Lệch format `from` khiến ES lọc bidCloseDate sai, dễ ra 0 dòng."""
    shifted = datetime.now(timezone.utc) + timedelta(hours=7)
    ms = shifted.microsecond // 1000
    return shifted.strftime("%Y-%m-%dT%H:%M:%S") + f".{ms:03d}Z"


def _open_tbmt_filters() -> list[dict[str, Any]]:
    """TBMT chưa đóng — giống bộ lọc trang chủ cổng."""
    return [
        {
            "fieldName": "type",
            "searchType": "in",
            "fieldValues": ["es-notify-contractor"],
        },
        {
            "fieldName": "caseKHKQ",
            "searchType": "not_in",
            "fieldValues": ["1"],
        },
        {
            "fieldName": "bidCloseDate",
            "searchType": "range",
            "from": _portal_open_bid_close_from_iso(),
            "to": None,
        },
    ]


def _all_tbmt_filters() -> list[dict[str, Any]]:
    """Tất cả TBMT — không lọc theo ngày đóng thầu (dùng cho /timtat)."""
    return [
        {
            "fieldName": "type",
            "searchType": "in",
            "fieldValues": ["es-notify-contractor"],
        },
        {
            "fieldName": "caseKHKQ",
            "searchType": "not_in",
            "fieldValues": ["1"],
        },
    ]


def build_tbmt_payload(
    page_number: int = 0,
    page_size: int = 50,
    *,
    open_only: bool = True,
    field_filter: Optional[list[str]] = None,
    bid_method_filter: Optional[int] = None,
) -> list[dict[str, Any]]:
    """TBMT mới — không lọc từ server (cron).

    open_only=True (mặc định): chỉ gói chưa đóng thầu.
    open_only=False: tất cả gói kể cả đã đóng.
    field_filter: danh sách mã lĩnh vực ES (vd. ["HH", "XL"]) — None = tất cả.
    bid_method_filter: 1 = qua mạng, 0 = không qua mạng, None = tất cả.
    """
    filters = _open_tbmt_filters() if open_only else _all_tbmt_filters()
    if field_filter:
        filters.append({"fieldName": "investField", "searchType": "in", "fieldValues": list(field_filter)})
    if bid_method_filter is not None:
        filters.append({"fieldName": "isInternet", "searchType": "in", "fieldValues": [bid_method_filter]})
    return [
        {
            "pageSize": page_size,
            "pageNumber": page_number,
            "query": [
                {
                    "index": "es-contractor-selection",
                    "keyWord": "",
                    "matchType": "all-1",
                    "matchFields": ["notifyNo", "bidName"],
                    "filters": filters,
                }
            ],
        }
    ]


def build_tbmt_keyword_payload(
    page_number: int,
    page_size: int,
    keyword: str,
    *,
    match_type: str = "all-1",
    include_investor_fields: bool = True,
    open_only: bool = True,
    field_filter: Optional[list[str]] = None,
    bid_method_filter: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Tra TBMT theo từ khóa — mặc định gồm cả chủ đầu tư/BMT (giống cổng khi tích tìm theo cơ quan).

    open_only=True  → chỉ gói chưa đóng thầu (mặc định, dùng cho cron + /tim)
    open_only=False → tất cả gói kể cả đã đóng (dùng cho /timtat)
    field_filter: danh sách mã lĩnh vực ES (vd. ["HH", "XL"]) — None = tất cả.
    bid_method_filter: 1 = qua mạng, 0 = không qua mạng, None = tất cả.
    """
    kw = (keyword or "").strip()
    fields: list[str] = ["notifyNo", "bidName"]
    if include_investor_fields:
        fields.extend(
            [
                "investorName",
                "investorCode",
                "procuringEntityName",
                "procuringEntityCode",
            ]
        )
    filters = _open_tbmt_filters() if open_only else _all_tbmt_filters()
    if field_filter:
        filters.append({"fieldName": "investField", "searchType": "in", "fieldValues": list(field_filter)})
    if bid_method_filter is not None:
        filters.append({"fieldName": "isInternet", "searchType": "in", "fieldValues": [bid_method_filter]})
    return [
        {
            "pageSize": page_size,
            "pageNumber": page_number,
            "query": [
                {
                    "index": "es-contractor-selection",
                    "keyWord": kw,
                    "matchType": match_type,
                    "matchFields": fields,
                    "filters": filters,
                }
            ],
        }
    ]


class MuasamcongCrawler:
    def __init__(
        self,
        page_size: int = 50,
        timeout: float = 30.0,
        use_playwright: bool = True,
        *,
        playwright_headless: bool = True,
        playwright_channel: Optional[str] = None,
    ):
        self.user_agent = random.choice(USER_AGENTS)
        self.page_size = page_size
        self.use_playwright = use_playwright
        self.playwright_headless = playwright_headless
        self.playwright_channel = (playwright_channel or "").strip() or None
        self._session_warmed = False
        self._last_request_at = 0.0
        self._field_names: dict[str, str] = dict(INVEST_FIELD_NAMES)
        _verify = _httpx_ssl_verify()
        try:
            self.client = httpx.Client(
                base_url=BASE_URL,
                timeout=timeout,
                http2=True,
                follow_redirects=True,
                verify=_verify,
                headers={
                    "User-Agent": self.user_agent,
                    "Accept": "application/json, text/plain, */*",
                    "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
                    "Accept-Encoding": "gzip, deflate, br",
                    "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
                    "Sec-Ch-Ua-Mobile": "?0",
                    "Sec-Ch-Ua-Platform": '"Windows"',
                },
            )
        except Exception:
            self.client = httpx.Client(
                base_url=BASE_URL,
                timeout=timeout,
                follow_redirects=True,
                verify=_verify,
                headers={"User-Agent": self.user_agent},
            )

    def _human_delay(self, min_s: float = 2.0, max_s: float = 6.0) -> None:
        delay = random.uniform(min_s, max_s)
        elapsed = time.time() - self._last_request_at
        if elapsed < delay:
            time.sleep(delay - elapsed)
        self._last_request_at = time.time()

    def _warmup_session(self) -> None:
        """GET homepage để lấy cookies trước Playwright.

        Non-fatal: nếu HTTP warmup lỗi (timeout, 404, network) thì log WARNING
        và tiếp tục — Playwright sẽ tự navigate trang chủ khi cần.
        Đặc biệt quan trọng khi Railway IP bị throttle hoặc cổng bảo trì ngắn.
        """
        if self._session_warmed:
            return
        logger.debug("Warming up session (GET homepage)...")
        try:
            r = self.client.get(
                HOMEPAGE_PATH,
                timeout=15.0,  # Short — fail fast, Playwright takes over
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "none",
                    "Sec-Fetch-User": "?1",
                },
            )
            r.raise_for_status()
            self._human_delay(2.0, 5.0)
            logger.debug("Session warmed up, cookies: {}", len(self.client.cookies))
        except Exception as e:
            # Không để warmup crash toàn bộ crawl — Playwright vẫn hoạt động.
            logger.warning(
                "HTTP warmup skipped ({}: {}) — Playwright will navigate directly",
                type(e).__name__, str(e)[:150],
            )
        finally:
            # Mark warmed dù fail hay thành công, tránh retry vô ích mỗi page.
            self._session_warmed = True

    def _load_field_names(self) -> None:
        try:
            r = self.client.post(
                CATEGORY_ENDPOINT,
                json={"categoryTypeCodeLst": ["DM_LVLCNT"]},
                headers=self._api_headers(),
            )
            if r.status_code != 200:
                return
            body = r.json()
            categories = body.get("categories") if isinstance(body, dict) else {}
            for cat in categories.get("DM_LVLCNT") or []:
                if not isinstance(cat, dict):
                    continue
                code = cat.get("code")
                name = cat.get("name")
                if code and name:
                    self._field_names[str(code)] = str(name)
        except httpx.HTTPError as e:
            logger.debug("Category load skipped: {}", e)

    def _api_headers(self) -> dict[str, str]:
        return {
            "Referer": f"{BASE_URL}{HOMEPAGE_PATH}",
            "Origin": BASE_URL,
            "X-Requested-With": "XMLHttpRequest",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }

    def fetch_recent_bids(
        self,
        max_pages: int = 2,
        *,
        max_pages_cap: int = 10,
        server_keyword: Optional[str] = None,
        open_only: bool = True,
        field_filter: Optional[list[str]] = None,
        bid_method_filter: Optional[int] = None,
        match_type: str = "all-1",
    ) -> list:
        from .models import Bid

        max_pages = max(1, min(max_pages, max_pages_cap))
        self._warmup_session()
        self._load_field_names()

        sk = server_keyword.strip() if server_keyword else None
        if sk:
            logger.info("Fetching with server-side keyWord=\"{}\"", sk)

        bids: list[Bid] = []
        for page in range(max_pages):
            try:
                logger.info(
                    "Đang lấy trang dữ liệu {}/{} (mỗi trang: Playwright + reCAPTCHA, thường 20–90s)…",
                    page + 1,
                    max_pages,
                )
                page_bids = self._fetch_page(
                    page,
                    server_keyword=sk,
                    open_only=open_only,
                    field_filter=field_filter,
                    bid_method_filter=bid_method_filter,
                    match_type=match_type,
                )
                if not page_bids:
                    break
                bids.extend(page_bids)
                if page < max_pages - 1:
                    self._human_delay(4.0, 10.0)
            except BlockedException:
                raise
            except httpx.HTTPStatusError as e:
                code = e.response.status_code
                if code in (429, 403):
                    logger.error("BLOCKED by server: HTTP {}. Stopping run.", code)
                    raise BlockedException(code) from e
                logger.exception("HTTP error page {}: {}", page, e)
                break
            except httpx.HTTPError as e:
                logger.exception("Network error page {}: {}", page, e)
                break
        logger.info("fetch_done: {} bids", len(bids))
        return bids

    def _fetch_page(
        self,
        page: int,
        server_keyword: Optional[str] = None,
        *,
        open_only: bool = True,
        field_filter: Optional[list[str]] = None,
        bid_method_filter: Optional[int] = None,
        match_type: str = "all-1",
    ) -> list:
        from .models import Bid

        if server_keyword:
            payload = build_tbmt_keyword_payload(
                page_number=page,
                page_size=self.page_size,
                keyword=server_keyword,
                match_type=match_type,
                open_only=open_only,
                field_filter=field_filter,
                bid_method_filter=bid_method_filter,
            )
        else:
            payload = build_tbmt_payload(
                page_number=page,
                page_size=self.page_size,
                open_only=open_only,
                field_filter=field_filter,
                bid_method_filter=bid_method_filter,
            )
        data = self._search(payload)
        if isinstance(data, (int, float)):
            raise BlockedException(429)
        return parse_search_response(data, self._field_names)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        retry=retry_if_exception(_retry_search_if_transient),
    )
    def _search(self, payload: list[dict[str, Any]]) -> dict[str, Any]:
        if self.use_playwright:
            return self._search_playwright(payload)
        return self._search_httpx(payload, token=None)

    def _search_httpx(self, payload: list[dict[str, Any]], token: Optional[str]) -> dict[str, Any]:
        url = SEARCH_ENDPOINT
        if token:
            url = f"{SEARCH_ENDPOINT}?token={token}"
        resp = self.client.post(url, json=payload, headers=self._api_headers())
        if resp.status_code in (429, 403):
            raise BlockedException(resp.status_code)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, (int, float)):
            raise BlockedException(429)
        return data

    # ── Playwright helper methods (shared by _search_playwright + _search_playwright_batch) ──

    @staticmethod
    def _pw_ctx_destroyed(exc: BaseException) -> bool:
        msg = str(exc).lower()
        return "execution context was destroyed" in msg or "context was destroyed" in msg

    def _pw_stabilize(self, page: Any, nav_try: int) -> None:
        """Chờ reCAPTCHA + mạng nguôi — không gọi evaluate dài (dễ vỡ khi SPA redirect)."""
        try:
            from playwright.sync_api import Error as PlaywrightError
        except ImportError:
            PlaywrightError = Exception  # type: ignore[misc,assignment]
        logger.info("Playwright: chờ script reCAPTCHA tải (tối đa 90s, log mỗi ~10s)…")
        g_deadline = time.time() + 90.0
        g_start = time.time()
        g_last_log = g_start
        while time.time() < g_deadline:
            if page.evaluate("() => typeof grecaptcha !== 'undefined'"):
                logger.info(
                    "Playwright: reCAPTCHA đã có trên trang sau {:.0f}s",
                    time.time() - g_start,
                )
                break
            now = time.time()
            if now - g_last_log >= 10.0:
                logger.info("Playwright: vẫn chờ grecaptcha… {:.0f}s", now - g_start)
                g_last_log = now
            time.sleep(1.0)
        else:
            raise RuntimeError("reCAPTCHA: không thấy grecaptcha sau 90s")

        nw_ms = 22000 if nav_try == 0 else 18000
        logger.info("Playwright: chờ mạng nguôi (tối đa {}s, có thể bỏ qua sớm)…", nw_ms // 1000)
        try:
            page.wait_for_load_state("networkidle", timeout=nw_ms)
            logger.info("Playwright: mạng đã nguôi")
        except PlaywrightError:
            logger.info("Playwright: hết thời gian chờ networkidle — tiếp tục (SPA vẫn chạy ngầm)")
        time.sleep(2.8 if nav_try == 0 else 2.0)

    @staticmethod
    def _pw_extract_site_key(page: Any) -> Optional[str]:
        """Khớp ?render= trên <script src="...recaptcha/api.js?render=..."> — tránh Invalid site key."""
        return page.evaluate(
            """() => {
                const scripts = Array.from(
                    document.querySelectorAll('script[src*="recaptcha/api.js"]')
                );
                for (const sc of scripts) {
                    try {
                        const src = sc.getAttribute("src") || "";
                        const u = new URL(src, window.location.origin);
                        const r = u.searchParams.get("render");
                        if (r) return r.trim();
                    } catch (e) {}
                }
                return null;
            }"""
        )

    @staticmethod
    def _pw_get_token(page: Any, site_key: str) -> str:
        """Đăng ký token: async IIFE + Promise.race timeout — tránh treo vô hạn."""
        logger.info("Playwright: đăng ký lấy token reCAPTCHA v3…")
        page.evaluate(
            """(siteKey) => {
                window.__egpTkDone = false;
                window.__egpTk = null;
                window.__egpTkErr = null;
                const fail = (msg) => {
                    window.__egpTkErr = msg;
                    window.__egpTkDone = true;
                };
                if (typeof grecaptcha === 'undefined' || typeof grecaptcha.execute !== 'function') {
                    fail('grecaptcha_execute_missing');
                    return;
                }
                (async () => {
                    try {
                        // ready() đôi khi không gọi callback (hai script recaptcha / SPA) → timeout rồi vẫn thử execute
                        try {
                            await Promise.race([
                                new Promise((resolve, reject) => {
                                    try {
                                        if (typeof grecaptcha.ready === 'function') {
                                            grecaptcha.ready(() => resolve());
                                        } else {
                                            resolve();
                                        }
                                    } catch (e) {
                                        reject(e);
                                    }
                                }),
                                new Promise((_, rej) =>
                                    setTimeout(
                                        () => rej(new Error('grecaptcha_ready_timeout_12s')),
                                        12000
                                    )
                                ),
                            ]);
                        } catch (_) {
                            /* bỏ qua — chạy execute trực tiếp */
                        }
                        const t = await Promise.race([
                            grecaptcha.execute(siteKey, { action: 'submit' }),
                            new Promise((_, rej) =>
                                setTimeout(
                                    () => rej(new Error('recaptcha_execute_timeout_45s')),
                                    45000
                                )
                            ),
                        ]);
                        if (!t || typeof t !== 'string') {
                            fail('empty_token_from_google');
                            return;
                        }
                        window.__egpTk = t;
                        window.__egpTkDone = true;
                    } catch (e) {
                        fail(String((e && e.message) || e));
                    }
                })();
            }""",
            site_key,
        )
        deadline = time.time() + 62.0
        poll_s = 1.5
        start = time.time()
        last_log = start
        while time.time() < deadline:
            done = page.evaluate("() => window.__egpTkDone === true")
            if done:
                logger.info("Playwright: reCAPTCHA xong sau {:.0f}s", time.time() - start)
                break
            now = time.time()
            if now - last_log >= 10.0:
                logger.info(
                    "Playwright: vẫn đang chờ token reCAPTCHA… {:.0f}s — (thường 3–20s; nếu >50s kiểm tra mạng tới google.com)",
                    now - start,
                )
                last_log = now
            time.sleep(poll_s)
        else:
            raise RuntimeError(
                "reCAPTCHA: hết thời gian ~62s (token không về). "
                "Thử PLAYWRIGHT_HEADLESS=false hoặc PLAYWRIGHT_CHANNEL=chrome trong .env; "
                "kiểm tra firewall/VPN chặn www.google.com."
            )

        err = page.evaluate("() => window.__egpTkErr")
        token = page.evaluate("() => window.__egpTk")
        if err:
            raise RuntimeError(f"reCAPTCHA: {err}")
        if not token or not isinstance(token, str):
            raise RuntimeError("empty reCAPTCHA token")
        return token

    def _pw_launch_browser(self, p: Any) -> Any:
        """Launch Chromium với các flag chống detect automation.

        Timeout 20s để fail fast nếu OS không spawn được subprocess
        (đã từng hang 8h khi không có timeout — Railway resource exhaustion).
        """
        launch_kw: dict[str, Any] = {
            "headless": self.playwright_headless,
            "timeout": 20_000,  # 20s — không để mãi
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        }
        if self.playwright_channel:
            launch_kw["channel"] = self.playwright_channel
        try:
            browser = p.chromium.launch(**launch_kw)
        except Exception as e:
            logger.error("Playwright launch FAILED ({}): {}", type(e).__name__, str(e)[:200])
            raise
        logger.info(
            "Playwright: Chromium headless={} channel={}",
            self.playwright_headless,
            self.playwright_channel or "bundled",
        )
        return browser

    def _pw_new_page(self, browser: Any) -> Any:
        context = browser.new_context(
            user_agent=self.user_agent,
            locale="vi-VN",
            viewport={"width": 1365, "height": 900},
        )
        page = context.new_page()

        def _pw_console(msg: Any) -> None:
            try:
                if msg.type in ("error", "warning"):
                    logger.info("Playwright console[{}]: {}", msg.type, msg.text[:800])
            except Exception:
                pass

        page.on("console", _pw_console)
        return context, page

    def _pw_navigate_and_prepare(self, context: Any, page: Any, nav_try: int) -> tuple[Any, str]:
        """Navigate to index, stabilize, extract site key. Returns (page, site_key)."""
        try:
            from playwright.sync_api import Error as PlaywrightError
        except ImportError:
            PlaywrightError = Exception  # type: ignore[misc,assignment]

        if nav_try > 0:
            try:
                page.close()
            except PlaywrightError:
                pass
            _, page = self._pw_new_page(context)

        page.goto(INDEX_URL, wait_until="load", timeout=30_000)
        logger.info("Playwright: đã tải xong trang index cổng")
        self._pw_stabilize(page, nav_try)

        render_key = self._pw_extract_site_key(page)
        site_key = (render_key or "").strip() or RECAPTCHA_SITE_KEY
        if render_key:
            logger.info("Playwright: site key lấy từ trang (render=) — {}…", site_key[:14])
        else:
            logger.warning("Playwright: không đọc được render= trên DOM — dùng RECAPTCHA_SITE_KEY trong code")
        return page, site_key

    # ── Single-payload search (used by _search → cron path) ──────────────────

    def _search_playwright(self, payload: list[dict[str, Any]]) -> dict[str, Any]:
        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            logger.warning("Playwright not installed, falling back to httpx (may fail): {}", e)
            return self._search_httpx(payload, token=None)

        logger.info(
            "Playwright: bắt đầu trang ES {} (smart/search)…",
            payload[0].get("pageNumber"),
        )

        # Circuit breaker: nếu site đã xác nhận unreachable → fail fast, đừng mở browser
        if _site_cb_is_open():
            raise BlockedException(503)

        last_err: Optional[BaseException] = None
        for session_try in range(3):
            logger.info("Playwright: mở phiên trình duyệt {}/3…", session_try + 1)
            with sync_playwright() as p:
                browser = self._pw_launch_browser(p)
                try:
                    context, page = self._pw_new_page(browser)
                    exhausted_context = False
                    for nav_try in range(3):
                        try:
                            page, site_key = self._pw_navigate_and_prepare(context, page, nav_try)
                            token = self._pw_get_token(page, site_key)

                            logger.info("Playwright: đang POST smart/search…")
                            url = f"{BASE_URL}{SEARCH_ENDPOINT}?token={token}"
                            headers = {**self._api_headers(), "Content-Type": "application/json"}
                            resp = context.request.post(
                                url,
                                headers=headers,
                                data=json.dumps(payload),
                                timeout=60_000,
                            )
                            if resp.status in (429, 403):
                                raise BlockedException(resp.status)
                            if not resp.ok:
                                body = resp.text()
                                raise httpx.HTTPStatusError(
                                    f"Search failed: {body[:500]}",
                                    request=httpx.Request("POST", SEARCH_ENDPOINT),
                                    response=httpx.Response(resp.status),
                                )
                            data = resp.json()
                            if isinstance(data, (int, float)):
                                raise BlockedException(429)
                            n = 0
                            if isinstance(data, dict):
                                page_obj = data.get("page") or {}
                                content = page_obj.get("content")
                                if isinstance(content, list):
                                    n = len(content)
                            logger.info("Playwright: smart/search xong — {} dòng trong trang", n)
                            _site_cb_record_success()
                            return data
                        except PlaywrightError as e:
                            last_err = e
                            err_lower = str(e).lower()
                            # Timeout = site unreachable → ghi circuit breaker, đừng retry
                            if "timeout" in err_lower:
                                _site_cb_record_failure()
                                logger.warning(
                                    "Playwright timeout (session {} nav {}) — site unreachable, fail fast",
                                    session_try, nav_try,
                                )
                                raise
                            if self._pw_ctx_destroyed(e):
                                logger.warning(
                                    "Playwright context lost (session {} nav {}): {}",
                                    session_try, nav_try, e,
                                )
                                if nav_try < 2:
                                    time.sleep(1.5 * (nav_try + 1))
                                    continue
                                exhausted_context = True
                                break
                            raise
                    if exhausted_context and session_try < 2:
                        logger.warning(
                            "Starting new browser session after repeated context loss ({}/2)",
                            session_try + 1,
                        )
                        time.sleep(2.0 * (session_try + 1))
                        continue
                    if exhausted_context and last_err is not None:
                        raise last_err
                finally:
                    browser.close()

        raise RuntimeError("Playwright search exhausted sessions without returning data")

    # ── Batch-payload search (interactive: N phrases × M pages = 1 session) ──

    def _search_playwright_batch(
        self, payloads: list[list[dict[str, Any]]]
    ) -> list[dict[str, Any]]:
        """Gửi nhiều payloads trong MỘT Playwright session — ONE reCAPTCHA acquisition.

        Returns list of response dicts (same order as payloads).
        4 payloads trước → 4 browser sessions (2-6 phút).
        Với batch → 1 session, token dùng lại ~120s giữa các POST.
        """
        if not payloads:
            return []
        if not self.use_playwright:
            return [self._search_httpx(p, token=None) for p in payloads]

        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            logger.warning("Playwright not installed, falling back to httpx: {}", e)
            return [self._search_httpx(p, token=None) for p in payloads]

        logger.info(
            "Playwright batch: {} payloads → 1 phiên trình duyệt (thay vì {} phiên)",
            len(payloads),
            len(payloads),
        )

        last_err: Optional[BaseException] = None
        for session_try in range(3):
            logger.info("Playwright batch: mở phiên {}/3…", session_try + 1)
            with sync_playwright() as p:
                browser = self._pw_launch_browser(p)
                try:
                    context, page = self._pw_new_page(browser)
                    exhausted_context = False
                    for nav_try in range(3):
                        try:
                            page, site_key = self._pw_navigate_and_prepare(context, page, nav_try)
                            # Acquire token once; reuse for all payloads (valid ~120s)
                            token = self._pw_get_token(page, site_key)
                            token_acquired_at = time.time()

                            results: list[dict[str, Any]] = []
                            for idx, payload in enumerate(payloads):
                                # Refresh token if > 100s have elapsed (safety margin)
                                if idx > 0 and time.time() - token_acquired_at > 100:
                                    logger.info(
                                        "Playwright batch: token cũ ({:.0f}s) — lấy token mới cho payload {}/{}…",
                                        time.time() - token_acquired_at,
                                        idx + 1,
                                        len(payloads),
                                    )
                                    token = self._pw_get_token(page, site_key)
                                    token_acquired_at = time.time()

                                logger.info(
                                    "Playwright batch: POST payload {}/{} (page={})…",
                                    idx + 1,
                                    len(payloads),
                                    payload[0].get("pageNumber"),
                                )
                                url = f"{BASE_URL}{SEARCH_ENDPOINT}?token={token}"
                                headers = {**self._api_headers(), "Content-Type": "application/json"}
                                resp = context.request.post(
                                    url,
                                    headers=headers,
                                    data=json.dumps(payload),
                                    timeout=60_000,
                                )
                                if resp.status in (429, 403):
                                    raise BlockedException(resp.status)
                                if not resp.ok:
                                    body = resp.text()
                                    raise httpx.HTTPStatusError(
                                        f"Search failed: {body[:500]}",
                                        request=httpx.Request("POST", SEARCH_ENDPOINT),
                                        response=httpx.Response(resp.status),
                                    )
                                data = resp.json()
                                if isinstance(data, (int, float)):
                                    raise BlockedException(429)
                                n = 0
                                if isinstance(data, dict):
                                    page_obj = data.get("page") or {}
                                    content = page_obj.get("content")
                                    if isinstance(content, list):
                                        n = len(content)
                                logger.info(
                                    "Playwright batch: payload {}/{} xong — {} dòng",
                                    idx + 1, len(payloads), n,
                                )
                                results.append(data)
                                if idx < len(payloads) - 1:
                                    time.sleep(1.2)  # brief inter-request pause
                            return results

                        except PlaywrightError as e:
                            last_err = e
                            if self._pw_ctx_destroyed(e):
                                logger.warning(
                                    "Playwright batch: context lost (session {} nav {}): {}",
                                    session_try, nav_try, e,
                                )
                                if nav_try < 2:
                                    time.sleep(1.5 * (nav_try + 1))
                                    continue
                                exhausted_context = True
                                break
                            raise
                    if exhausted_context and session_try < 2:
                        logger.warning(
                            "Playwright batch: new session after context loss ({}/2)",
                            session_try + 1,
                        )
                        time.sleep(2.0 * (session_try + 1))
                        continue
                    if exhausted_context and last_err is not None:
                        raise last_err
                finally:
                    browser.close()

        raise RuntimeError("Playwright batch: exhausted sessions without returning data")

    def fetch_recent_bids_multi(
        self,
        phrases: list[str],
        max_pages: int = 2,
        *,
        max_pages_cap: int = 10,
        open_only: bool = True,
        field_filter: Optional[list[str]] = None,
        bid_method_filter: Optional[int] = None,
        match_type: str = "any",
    ) -> dict[str, list]:
        """Cào nhiều phrase trong 1 Playwright session (batch) — thay vì N session riêng lẻ.

        Returns dict: phrase → list[Bid].
        OR mode: 2 phrases × 2 pages = 4 payloads → 1 browser session (tiết kiệm 3 session).
        """
        from .models import Bid

        if not phrases:
            return {}

        max_pages = max(1, min(max_pages, max_pages_cap))
        self._warmup_session()
        self._load_field_names()

        # Build all payloads upfront: phrases × pages in interleaved order
        # (interleave by page number so early pages of all phrases come first)
        all_payloads: list[list[dict[str, Any]]] = []
        payload_keys: list[str] = []  # phrase for each payload slot

        for pg in range(max_pages):
            for phrase in phrases:
                sk = phrase.strip()
                if sk:
                    pl = build_tbmt_keyword_payload(
                        page_number=pg,
                        page_size=self.page_size,
                        keyword=sk,
                        match_type=match_type,
                        open_only=open_only,
                        field_filter=field_filter,
                        bid_method_filter=bid_method_filter,
                    )
                else:
                    pl = build_tbmt_payload(
                        page_number=pg,
                        page_size=self.page_size,
                        open_only=open_only,
                        field_filter=field_filter,
                        bid_method_filter=bid_method_filter,
                    )
                all_payloads.append(pl)
                payload_keys.append(sk)

        logger.info(
            "fetch_recent_bids_multi: {} phrases × {} pages = {} payloads → 1 Playwright session",
            len(phrases),
            max_pages,
            len(all_payloads),
        )

        if self.use_playwright:
            raw_results = self._search_playwright_batch(all_payloads)
        else:
            raw_results = [self._search_httpx(pl, token=None) for pl in all_payloads]

        # Aggregate results per phrase
        result: dict[str, list] = {phrase.strip(): [] for phrase in phrases}
        for phrase_key, data in zip(payload_keys, raw_results):
            bids = parse_search_response(data, self._field_names)
            if phrase_key in result:
                result[phrase_key].extend(bids)

        for phrase, bids in result.items():
            logger.info("fetch_recent_bids_multi: '{}' → {} bids", phrase, len(bids))

        return result

    def fetch_bid_by_code(
        self,
        notify_no: str,
        *,
        version: Optional[str] = None,
        include_closed: bool = True,
    ):
        """Tra một gói thầu theo mã notifyNo (kèm version tùy chọn).

        Gọi smart/search với keyWord = notifyNo, chỉ matchFields=['notifyNo'] để chính xác.
        Quét nhiều trang vì cổng đôi khi trả thêm version cũ. Trả về Bid khớp chính xác,
        hoặc None nếu không tìm thấy. include_closed=True (mặc định) để vẫn tìm được
        gói đã đóng thầu.
        """
        from .models import Bid

        code = (notify_no or "").strip().upper()
        if not code:
            return None

        ver = (version or "").strip()
        target_stand = f"{code}-{ver}" if ver else None

        payload_pages: list[Bid] = []
        max_pages = 2

        self._warmup_session()
        self._load_field_names()

        for page in range(max_pages):
            payload = build_tbmt_keyword_payload(
                page_number=page,
                page_size=self.page_size,
                keyword=code,
                include_investor_fields=False,
                open_only=not include_closed,
            )
            data = self._search(payload)
            if isinstance(data, (int, float)):
                raise BlockedException(429)
            content = (data.get("page") or {}).get("content") or []
            if not content:
                break

            for item in content:
                if not isinstance(item, dict):
                    continue
                item_no = (item.get("notifyNo") or "").strip().upper()
                item_stand = (item.get("notifyNoStand") or "").strip().upper()
                if target_stand and item_stand == target_stand:
                    return parse_search_item(item, self._field_names)
                if item_no == code:
                    payload_pages.append(parse_search_item(item, self._field_names))

            if len(content) < self.page_size:
                break
            if page < max_pages - 1:
                self._human_delay(2.0, 5.0)

        if not payload_pages:
            return None
        # Nhiều version → ưu tiên version cao nhất (mới nhất) để hiển thị bản hiện hành.
        payload_pages.sort(key=lambda b: b.tbmt_code, reverse=True)
        return payload_pages[0]

    def close(self) -> None:
        self.client.close()
