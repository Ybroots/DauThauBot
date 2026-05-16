from __future__ import annotations

import json
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
from loguru import logger
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from .parser import INVEST_FIELD_NAMES, parse_search_response

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


def _retry_search_if_transient(exc: BaseException) -> bool:
    """Không retry khi site key sai — lặp lại không có ích."""
    msg = str(exc).lower()
    if "invalid site key" in msg or "invalid key type" in msg:
        return False
    if "grecaptcha_execute_missing" in msg:
        return False
    return True

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


def build_tbmt_payload(page_number: int = 0, page_size: int = 50) -> list[dict[str, Any]]:
    """TBMT mới, chưa đóng — không lọc từ server (cron)."""
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
                    "filters": _open_tbmt_filters(),
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
) -> list[dict[str, Any]]:
    """Tra TBMT theo từ khóa — mặc định gồm cả chủ đầu tư/BMT (giống cổng khi tích tìm theo cơ quan)."""
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
                    "filters": _open_tbmt_filters(),
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
        try:
            self.client = httpx.Client(
                base_url=BASE_URL,
                timeout=timeout,
                http2=True,
                follow_redirects=True,
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
                headers={"User-Agent": self.user_agent},
            )

    def _human_delay(self, min_s: float = 2.0, max_s: float = 6.0) -> None:
        delay = random.uniform(min_s, max_s)
        elapsed = time.time() - self._last_request_at
        if elapsed < delay:
            time.sleep(delay - elapsed)
        self._last_request_at = time.time()

    def _warmup_session(self) -> None:
        if self._session_warmed:
            return
        logger.debug("Warming up session (GET homepage)...")
        r = self.client.get(
            HOMEPAGE_PATH,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
            },
        )
        r.raise_for_status()
        self._human_delay(3.0, 8.0)
        self._session_warmed = True
        logger.debug("Session warmed up, cookies: {}", len(self.client.cookies))

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
                page_bids = self._fetch_page(page, server_keyword=sk)
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

    def _fetch_page(self, page: int, server_keyword: Optional[str] = None) -> list:
        from .models import Bid

        if server_keyword:
            payload = build_tbmt_keyword_payload(
                page_number=page,
                page_size=self.page_size,
                keyword=server_keyword,
            )
        else:
            payload = build_tbmt_payload(page_number=page, page_size=self.page_size)
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

        def _ctx_destroyed(exc: BaseException) -> bool:
            msg = str(exc).lower()
            return "execution context was destroyed" in msg or "context was destroyed" in msg

        def _stabilize_after_goto(page, nav_try: int) -> None:
            """Chờ reCAPTCHA + mạng nguôi — không gọi evaluate dài (dễ vỡ khi SPA redirect)."""
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

        def _extract_recaptcha_site_key_from_dom(page) -> Optional[str]:
            """Khớp ?render= trên <script src=\"...recaptcha/api.js?render=...\"> — tránh Invalid site key."""
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

        def _recaptcha_token_via_polling(page, site_key: str) -> str:
            """Đăng ký token: async IIFE (evaluate trả về ngay) + Promise.race timeout — tránh treo vô hạn."""
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

        last_err: Optional[BaseException] = None
        for session_try in range(3):
            logger.info("Playwright: mở phiên trình duyệt {}/3…", session_try + 1)
            with sync_playwright() as p:
                launch_kw: dict[str, Any] = {
                    "headless": self.playwright_headless,
                    "args": [
                        "--disable-blink-features=AutomationControlled",
                        "--disable-dev-shm-usage",
                        "--no-sandbox",
                    ],
                }
                if self.playwright_channel:
                    launch_kw["channel"] = self.playwright_channel
                browser = p.chromium.launch(**launch_kw)
                logger.info(
                    "Playwright: Chromium headless={} channel={}",
                    self.playwright_headless,
                    self.playwright_channel or "bundled",
                )
                try:
                    context = browser.new_context(
                        user_agent=self.user_agent,
                        locale="vi-VN",
                        viewport={"width": 1365, "height": 900},
                    )
                    page = context.new_page()

                    def _pw_console(msg) -> None:
                        try:
                            if msg.type in ("error", "warning"):
                                logger.info("Playwright console[{}]: {}", msg.type, msg.text[:800])
                        except Exception:
                            pass

                    page.on("console", _pw_console)
                    exhausted_context = False
                    for nav_try in range(3):
                        try:
                            if nav_try > 0:
                                try:
                                    page.close()
                                except PlaywrightError:
                                    pass
                                page = context.new_page()

                            page.goto(
                                INDEX_URL,
                                wait_until="load",
                                timeout=120000,
                            )
                            logger.info("Playwright: đã tải xong trang index cổng")
                            _stabilize_after_goto(page, nav_try)

                            render_key = _extract_recaptcha_site_key_from_dom(page)
                            site_key = (render_key or "").strip() or RECAPTCHA_SITE_KEY
                            if render_key:
                                logger.info(
                                    "Playwright: site key lấy từ trang (render=) — {}…",
                                    site_key[:14],
                                )
                            else:
                                logger.warning(
                                    "Playwright: không đọc được render= trên DOM — dùng RECAPTCHA_SITE_KEY trong code",
                                )

                            token = _recaptcha_token_via_polling(page, site_key)

                            logger.info("Playwright: đang POST smart/search…")
                            url = f"{BASE_URL}{SEARCH_ENDPOINT}?token={token}"
                            headers = {
                                **self._api_headers(),
                                "Content-Type": "application/json",
                            }
                            resp = context.request.post(
                                url,
                                headers=headers,
                                data=json.dumps(payload),
                                timeout=120000,
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
                            return data
                        except PlaywrightError as e:
                            last_err = e
                            if _ctx_destroyed(e):
                                logger.warning(
                                    "Playwright context lost (session {} nav {}): {}",
                                    session_try,
                                    nav_try,
                                    e,
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

    def close(self) -> None:
        self.client.close()
