# PLAN.md — Tool cào gói thầu muasamcong + Telegram Bot

> Tài liệu plan cho Cursor thực thi. Mục tiêu: hoàn thành tool trong **1-2 ngày làm việc**.

---

## 1. Mục tiêu

Xây dựng tool Python chạy nền (daemon hoặc cron) làm các việc sau:

1. **Cào** dữ liệu thông báo mời thầu mới từ cổng `muasamcong.mpi.gov.vn/web/guest/contractor-selection` định kỳ (mặc định 15 phút/lần).
2. **Lọc** theo danh sách từ khoá do user cấu hình trong file `keywords.yaml`.
3. **Khử trùng lặp** theo Mã TBMT — đã gửi 1 lần là không gửi lại.
4. **Gửi** thông báo các gói thầu khớp lên 1 nhóm Telegram (hoặc nhiều nhóm) qua Telegram Bot API.
5. **Log** hoạt động ra file để truy vết khi sai.

---

## 2. Format message mẫu (theo yêu cầu user)

```
Mã TBMT : IB2500579539-00
Chưa đóng thầu
Mua sắm thiết bị công nghệ thông tin phục vụ triển khai thử nghiệm hệ thống phần mềm
Chủ đầu tư : Trung tâm Chuyển đổi số
Ngày đăng tải thông báo : 10/12/2025 - 10:53
Lĩnh vực : Hàng hóa
Địa điểm : Phường Long An - Tỉnh Tây Ninh;
Thời điểm đóng thầu
10:00
19/12/2025
Hình thức dự thầu
Qua mạng
;https://muasamcong.mpi.gov.vn/web/guest/contractor-selection?...
```

Format Telegram: dùng MarkdownV2 hoặc HTML, **bôi đậm Mã TBMT** + tên gói thầu, link clickable, thêm các từ khoá đã match ở cuối.

---

## 3. Tech stack (giữ tối thiểu để build nhanh)

| Hạng mục | Lựa chọn | Lý do |
|---|---|---|
| Ngôn ngữ | Python 3.11+ | Có sẵn typing tốt |
| HTTP client | **httpx** (sync) | Async-ready nếu cần, cleaner hơn requests |
| Fallback browser | **Playwright** (chỉ khi cần) | Chạy headless Chromium parse SPA |
| Parser HTML (fallback) | selectolax | Nhanh hơn BeautifulSoup ~5x |
| Scheduler | **APScheduler** (BlockingScheduler) | Đơn giản, 1 process |
| Database | **SQLite** (sqlite3 stdlib) | Không cần server, 1 file `seen.db` |
| Config | **pydantic v2 + pyyaml + python-dotenv** | Validate config, secret tách file |
| Telegram | HTTP trực tiếp tới `api.telegram.org/bot<TOKEN>/sendMessage` | Không cần lib nặng |
| Logging | **loguru** | Setup 1 dòng, log rotation tự động |
| Đóng gói | `pyproject.toml` + `uv` | `uv pip install -e .` cực nhanh |

> **Không dùng**: Scrapy (overkill cho 1 site), python-telegram-bot library (chỉ cần 1 endpoint), Redis (SQLite đủ), Docker (optional, không bắt buộc Day 1).

---

## 4. Cấu trúc dự án

```
muasamcong-tracker/
├── src/
│   └── tracker/
│       ├── __init__.py
│       ├── __main__.py          # Entry point: python -m tracker
│       ├── config.py            # Pydantic settings + load YAML
│       ├── crawler.py           # Hàm fetch_new_bids() — gọi API/Playwright
│       ├── parser.py            # Parse response JSON → dataclass Bid
│       ├── filter.py            # Logic lọc từ khoá (case-insensitive, có dấu/không dấu)
│       ├── storage.py           # SQLite: seen_tbmt, mark_seen, is_seen
│       ├── telegram.py          # send_message(text, chat_id)
│       ├── formatter.py         # bid → Telegram message text
│       └── scheduler.py         # APScheduler setup
├── tests/
│   ├── test_filter.py
│   ├── test_formatter.py
│   └── test_storage.py
├── config/
│   ├── keywords.yaml            # User cấu hình từ khoá
│   └── keywords.example.yaml
├── data/
│   └── seen.db                  # SQLite (gitignored)
├── logs/                        # Log files (gitignored)
├── .env.example
├── .env                         # gitignored
├── .gitignore
├── pyproject.toml
├── README.md
└── PLAN.md                      # ← file này
```

---

## 5. File cấu hình

### 5.1. `.env` (secret)

```bash
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
TELEGRAM_CHAT_IDS=-1001234567890,-1009876543210  # comma-separated, nhóm có dấu trừ
LOG_LEVEL=INFO

# === Polling — đã hardening anti-block ===
POLL_INTERVAL_MINUTES=45        # khuyến nghị 30-60, KHÔNG dưới 15
POLL_JITTER_SECONDS=600         # ±10 phút ngẫu nhiên quanh interval

# === Quiet hours — không chạy crawler khoảng này ===
# (Format HH:MM 24h, timezone Asia/Ho_Chi_Minh)
# Lý do: chạy 24/7 đều đặn là bot signature rõ. Skip 01-06 sáng giả lập "ngủ".
QUIET_HOURS_START=01:00
QUIET_HOURS_END=06:00

# === Block cooldown — khi server trả 429/403 ===
# Đợi N giờ trước khi crawler thử request lại. Lần block tiếp theo nhân đôi.
BLOCK_COOLDOWN_HOURS=6
BLOCK_COOLDOWN_MAX_HOURS=48
```

### 5.2. `config/keywords.yaml` (user setup)

```yaml
# QUAN TRỌNG: 
# - Mỗi keyword sẽ được search ACROSS ALL FIELDS của gói thầu
#   (title, chủ đầu tư, lĩnh vực, địa điểm, trạng thái, mô tả).
# - Logic OR: gói thầu chỉ cần khớp ÍT NHẤT 1 keyword là được gửi.
# - Match case-insensitive, không phân biệt có dấu / không dấu.
#   ("Lâm Đồng" = "lam dong" = "LÂM ĐỒNG")
# - User CHỈ cần sửa danh sách `keywords` bên dưới, không cần biết
#   keyword đó thuộc location, field hay title.

keywords:
  # Ví dụ thực tế từ khách hàng:
  - "lâm đồng"
  - "truyền thanh thông minh"
  - "camera"
  - "truyền dẫn"
  # Thêm các từ khoá khác:
  # - "công nghệ thông tin"
  # - "phần mềm"
  # - "máy chủ"

# === FILTER BỔ SUNG (optional) ===
# Bỏ trống [] hoặc null = không lọc thêm.
# Khi có giá trị, các filter này áp dụng SAU bước match keywords (AND logic).

locations: []          # vd ["Tây Ninh", "Hồ Chí Minh"] — chỉ nhận tỉnh này
fields: []             # vd ["Hàng hóa"] — chỉ nhận lĩnh vực này
min_budget_vnd: null   # vd 100000000 — chỉ nhận gói ≥ 100tr
```

---

## 6. KẾ HOẠCH 2 NGÀY (chi tiết)

---

### 🟡 DAY 1 — Sáng (3-4h): Reverse-engineer API + Crawler core

#### Bước 1.1 — Thám thính API (1-1.5h)

**Cursor cần làm tay phần này hoặc hỏi user**:

1. Mở Chrome, vào `https://muasamcong.mpi.gov.vn/web/guest/contractor-selection`
2. Mở DevTools (F12) → tab **Network** → filter **Fetch/XHR**
3. Refresh page hoặc bấm Search → quan sát các request JSON
4. Ghi lại:
   - URL endpoint chính (vd `/web/guest/.../paging-search-data-vongchon` hoặc tương tự)
   - HTTP method (chắc là POST)
   - Request payload (body JSON: page, size, filter)
   - Response schema (mảng kết quả, các field)
   - Header bắt buộc nếu có (X-CSRF-Token, Cookie phiên...)

> **Tip cho Cursor**: copy request dưới dạng "Copy as cURL" rồi dán vào tool `curlconverter.com` để ra Python httpx code. Hoặc dùng `mitmproxy` để intercept dài hạn.

**Hai kịch bản kết quả**:

| Kịch bản | Xử lý | Time |
|---|---|---|
| **A. Tìm được API JSON sạch** (90% xác suất) | Đi tiếp Bước 1.2 với httpx | 30 phút |
| **B. Cần token/session phức tạp** | Fallback Playwright headless mở page và `page.wait_for_response()` | +1h |

> Nếu sau 2h vẫn không tìm được API, **dừng lại** chuyển sang nhánh B ngay, đừng cố.

#### Bước 1.2 — Khung dự án (30 phút)

- [ ] `uv init` hoặc `pdm init`, tạo `pyproject.toml`
- [ ] Tạo cấu trúc folder theo §4
- [ ] Cài deps: `uv add httpx pydantic-settings pyyaml python-dotenv apscheduler loguru selectolax`
- [ ] Setup `.gitignore` (`.env`, `data/`, `logs/`, `__pycache__/`, `.venv/`)
- [ ] Khởi tạo `git init` + initial commit

#### Bước 1.3 — Module `config.py` (30 phút)

```python
# src/tracker/config.py
from pathlib import Path
from typing import Optional
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
import yaml

class Secrets(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', extra='ignore')
    
    telegram_bot_token: str
    telegram_chat_ids: str  # comma-separated
    poll_interval_minutes: int = 15
    log_level: str = 'INFO'
    
    @property
    def chat_id_list(self) -> list[str]:
        return [c.strip() for c in self.telegram_chat_ids.split(',') if c.strip()]

class KeywordsConfig(BaseModel):
    keywords: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    fields: list[str] = Field(default_factory=list)
    min_budget_vnd: Optional[int] = None

def load_keywords(path: Path = Path('config/keywords.yaml')) -> KeywordsConfig:
    with open(path, 'r', encoding='utf-8') as f:
        data = yaml.safe_load(f)
    return KeywordsConfig.model_validate(data)
```

#### Bước 1.4 — Module `storage.py` SQLite dedup (30 phút)

```python
# src/tracker/storage.py
import sqlite3
from pathlib import Path
from datetime import datetime

DB_PATH = Path('data/seen.db')

def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS seen_bids (
                tbmt_code TEXT PRIMARY KEY,
                title TEXT,
                seen_at TEXT NOT NULL,
                sent_to_telegram INTEGER DEFAULT 0
            )
        ''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_seen_at ON seen_bids(seen_at)')

def is_seen(tbmt_code: str) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute('SELECT 1 FROM seen_bids WHERE tbmt_code = ?', (tbmt_code,))
        return cur.fetchone() is not None

def mark_seen(tbmt_code: str, title: str, sent: bool = True):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            'INSERT OR REPLACE INTO seen_bids(tbmt_code, title, seen_at, sent_to_telegram) VALUES (?, ?, ?, ?)',
            (tbmt_code, title, datetime.utcnow().isoformat(), 1 if sent else 0)
        )
```

---

### 🟡 DAY 1 — Chiều (4-5h): Crawler + Telegram + Run thử

#### Bước 1.5 — Module `crawler.py` (1.5-2h)

```python
# src/tracker/crawler.py
import httpx
import random
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from loguru import logger

# === ĐIỀU CHỈNH SAU KHI REVERSE-ENGINEER ===
BASE_URL = 'https://muasamcong.mpi.gov.vn'
HOMEPAGE_PATH = '/web/guest/contractor-selection'
SEARCH_ENDPOINT = '/api/...'  # ← FILL khi tìm được
DETAIL_URL_TEMPLATE = '{base}/web/guest/contractor-selection?notice={tbmt}&...'

# Pool UA — chọn 1 cho mỗi lifecycle crawler, không rotate giữa chừng
# (rotation trong cùng session là bot signature rõ ràng).
# Cập nhật phiên bản Chrome mỗi 3-6 tháng để khỏi lạc hậu.
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.6167.85 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0',
]

@dataclass
class Bid:
    tbmt_code: str          # IB2500579539-00
    title: str
    status: str             # "Chưa đóng thầu" / "Đã đóng thầu"
    investor: str           # Chủ đầu tư
    posted_at: datetime
    field: str              # Lĩnh vực: Hàng hóa / Xây lắp / Tư vấn...
    location: str           # Địa điểm
    closing_at: datetime
    bid_method: str         # Qua mạng / Trực tiếp
    detail_url: str
    budget_vnd: Optional[int] = None
    raw: Optional[dict] = None  # giữ JSON gốc để debug

class MuasamcongCrawler:
    def __init__(self, page_size: int = 50, timeout: float = 30.0):
        # Chọn 1 User-Agent cho cả lifecycle (không đổi giữa chừng — bot signature)
        self.user_agent = random.choice(USER_AGENTS)
        
        # http2=True để giả lập Chrome modern (Chrome dùng HTTP/2)
        self.client = httpx.Client(
            base_url=BASE_URL,
            timeout=timeout,
            http2=True,
            follow_redirects=True,
            headers={
                'User-Agent': self.user_agent,
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
                'Accept-Language': 'vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7',
                'Accept-Encoding': 'gzip, deflate, br',
                'Sec-Ch-Ua': '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
                'Sec-Ch-Ua-Mobile': '?0',
                'Sec-Ch-Ua-Platform': '"Windows"',
                'Upgrade-Insecure-Requests': '1',
            },
        )
        self.page_size = page_size
        self._session_warmed = False
        self._last_request_at = 0.0
    
    def _human_delay(self, min_s: float = 2.0, max_s: float = 6.0):
        """Đợi ngẫu nhiên giả lập user 'đọc trang' giữa các action."""
        delay = random.uniform(min_s, max_s)
        elapsed = time.time() - self._last_request_at
        if elapsed < delay:
            time.sleep(delay - elapsed)
        self._last_request_at = time.time()
    
    def _warmup_session(self):
        """GET homepage trước để lấy cookies + session token, giống user thật mở trang."""
        if self._session_warmed:
            return
        logger.debug('Warming up session (GET homepage)...')
        r = self.client.get(
            HOMEPAGE_PATH,
            headers={
                'Sec-Fetch-Dest': 'document',
                'Sec-Fetch-Mode': 'navigate',
                'Sec-Fetch-Site': 'none',
                'Sec-Fetch-User': '?1',
            }
        )
        r.raise_for_status()
        # Đợi 'đọc trang' 3-8s như user thật scan kết quả
        self._human_delay(3.0, 8.0)
        self._session_warmed = True
        logger.debug(f'Session warmed up, cookies: {len(self.client.cookies)} items')
    
    def fetch_recent_bids(self, max_pages: int = 2) -> list[Bid]:
        """Fetch các gói thầu mới đăng tải gần nhất.
        
        max_pages mặc định 2 (hard-cap). Mỗi page 50 bid = 100 bid/lần,
        đủ phủ thông báo trong 15-30 phút gần nhất.
        """
        max_pages = min(max_pages, 2)  # hard cap chống vô tình cào nhiều
        
        self._warmup_session()
        
        bids: list[Bid] = []
        for page in range(1, max_pages + 1):
            try:
                page_bids = self._fetch_page(page)
                if not page_bids:
                    break
                bids.extend(page_bids)
                # Delay giữa các page (giả lập user click "next page")
                if page < max_pages:
                    self._human_delay(4.0, 10.0)
            except httpx.HTTPStatusError as e:
                code = e.response.status_code
                if code in (429, 403):
                    # Bị block/throttle — DỪNG hẳn, raise lên scheduler để cooldown
                    logger.error(f'BLOCKED by server: HTTP {code}. Stopping run.')
                    raise BlockedException(code)
                logger.exception(f'HTTP error page {page}: {e}')
                break
            except httpx.HTTPError as e:
                logger.exception(f'Network error page {page}: {e}')
                break
        return bids
    
    def _fetch_page(self, page: int) -> list[Bid]:
        # Header khác cho API call (XHR) so với navigation
        api_headers = {
            'Accept': 'application/json, text/plain, */*',
            'Referer': f'{BASE_URL}{HOMEPAGE_PATH}',
            'Origin': BASE_URL,
            'X-Requested-With': 'XMLHttpRequest',
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'same-origin',
        }
        payload = {
            'page': page,
            'pageSize': self.page_size,
            'sort': 'postedAt,desc',
            # KHÔNG filter keyword ở server — lọc ở filter.py phía client
        }
        resp = self.client.post(SEARCH_ENDPOINT, json=payload, headers=api_headers)
        resp.raise_for_status()
        data = resp.json()
        return [self._parse_item(item) for item in data.get('items', [])]
    
    def _parse_item(self, item: dict) -> Bid:
        # Mapping field names tùy response thực tế
        return Bid(
            tbmt_code=item['tbmtCode'],
            title=item['name'],
            status=item.get('status', ''),
            investor=item.get('investorName', ''),
            posted_at=datetime.fromisoformat(item['postedAt']),
            field=item.get('field', ''),
            location=item.get('location', ''),
            closing_at=datetime.fromisoformat(item['closingAt']),
            bid_method=item.get('bidMethod', ''),
            detail_url=DETAIL_URL_TEMPLATE.format(base=BASE_URL, tbmt=item['tbmtCode']),
            budget_vnd=item.get('budgetVnd'),
            raw=item,
        )
    
    def close(self):
        self.client.close()


class BlockedException(Exception):
    """Raised khi server trả 429/403 — đã bị block, scheduler sẽ cooldown."""
    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__(f'Server blocked request (HTTP {status_code})')
```

> **Fallback Playwright** nếu API lock cứng:
> ```python
> from playwright.sync_api import sync_playwright
> 
> def fetch_via_playwright(self, max_pages: int = 3) -> list[Bid]:
>     with sync_playwright() as p:
>         browser = p.chromium.launch(headless=True)
>         page = browser.new_page()
>         page.goto(f'{BASE_URL}/web/guest/contractor-selection')
>         # Đợi data load
>         response = page.wait_for_response(lambda r: 'paging-search' in r.url, timeout=30000)
>         data = response.json()
>         # ... parse
> ```

#### Bước 1.6 — Module `filter.py` (45 phút)

```python
# src/tracker/filter.py
import unicodedata
from .crawler import Bid
from .config import KeywordsConfig

def normalize(text: str) -> str:
    """Lowercase + remove diacritics for fuzzy match. 'Hồ Chí Minh' → 'ho chi minh'."""
    nfkd = unicodedata.normalize('NFKD', text.lower())
    return ''.join(c for c in nfkd if not unicodedata.combining(c))

def matches_keywords(bid: Bid, cfg: KeywordsConfig) -> tuple[bool, list[str]]:
    """Trả về (có match không, danh sách keyword đã match).
    
    LOGIC:
    - Match keyword theo OR: chỉ cần 1 keyword khớp là pass.
    - Search ACROSS ALL FIELDS: title, investor, field, location, status,
      và description nếu có. User không cần biết keyword thuộc nhóm nào.
    - Các filter `locations`, `fields`, `min_budget_vnd` là filter BỔ SUNG
      (AND logic): áp dụng SAU bước match keywords.
    """
    # Ghép TẤT CẢ field text vào 1 haystack
    haystack = normalize(' '.join(filter(None, [
        bid.title,
        bid.investor,
        bid.field,
        bid.location,
        bid.status,
        getattr(bid, 'description', '') or '',  # nếu API có field này
    ])))
    
    matched: list[str] = []
    if cfg.keywords:
        for kw in cfg.keywords:
            if normalize(kw) in haystack:
                matched.append(kw)
        if not matched:
            return False, []
    
    # === FILTER BỔ SUNG (optional) ===
    
    # Lọc địa điểm — substring match riêng trên field location
    if cfg.locations:
        loc_hay = normalize(bid.location)
        if not any(normalize(loc) in loc_hay for loc in cfg.locations):
            return False, matched
    
    # Lọc lĩnh vực — exact match
    if cfg.fields:
        if not any(normalize(f) == normalize(bid.field) for f in cfg.fields):
            return False, matched
    
    # Lọc ngân sách tối thiểu
    if cfg.min_budget_vnd and bid.budget_vnd:
        if bid.budget_vnd < cfg.min_budget_vnd:
            return False, matched
    
    return True, matched
```

#### Bước 1.7 — Module `formatter.py` + `telegram.py` (45 phút)

```python
# src/tracker/formatter.py
from .crawler import Bid

def format_bid_message(bid: Bid, matched_keywords: list[str]) -> str:
    """Format theo mẫu user yêu cầu, dùng HTML cho Telegram."""
    def esc(s: str) -> str:
        return (s or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    
    closing_date = bid.closing_at.strftime('%d/%m/%Y')
    closing_time = bid.closing_at.strftime('%H:%M')
    posted = bid.posted_at.strftime('%d/%m/%Y - %H:%M')
    
    msg = (
        f'<b>Mã TBMT:</b> <code>{esc(bid.tbmt_code)}</code>\n'
        f'<b>Trạng thái:</b> {esc(bid.status)}\n'
        f'<b>{esc(bid.title)}</b>\n\n'
        f'<b>Chủ đầu tư:</b> {esc(bid.investor)}\n'
        f'<b>Ngày đăng:</b> {posted}\n'
        f'<b>Lĩnh vực:</b> {esc(bid.field)}\n'
        f'<b>Địa điểm:</b> {esc(bid.location)}\n'
        f'<b>Đóng thầu:</b> {closing_time} {closing_date}\n'
        f'<b>Hình thức:</b> {esc(bid.bid_method)}\n'
    )
    if bid.budget_vnd:
        msg += f'<b>Giá gói:</b> {bid.budget_vnd:,} VNĐ\n'
    if matched_keywords:
        tags = ' '.join(f'#{kw.replace(" ", "_")}' for kw in matched_keywords)
        msg += f'\n{tags}\n'
    msg += f'\n🔗 <a href="{bid.detail_url}">Xem chi tiết</a>'
    return msg
```

```python
# src/tracker/telegram.py
import httpx
from loguru import logger

def send_message(bot_token: str, chat_id: str, text: str) -> bool:
    url = f'https://api.telegram.org/bot{bot_token}/sendMessage'
    payload = {
        'chat_id': chat_id,
        'text': text,
        'parse_mode': 'HTML',
        'disable_web_page_preview': False,
    }
    try:
        r = httpx.post(url, json=payload, timeout=15)
        if r.status_code == 200:
            return True
        logger.error(f'Telegram error {r.status_code}: {r.text}')
        return False
    except httpx.HTTPError as e:
        logger.exception(f'Telegram send failed: {e}')
        return False
```

#### Bước 1.8 — `__main__.py` + chạy thử thủ công (45 phút)

```python
# src/tracker/__main__.py
from loguru import logger
from .config import Secrets, load_keywords
from .crawler import MuasamcongCrawler
from .filter import matches_keywords
from .formatter import format_bid_message
from .telegram import send_message
from .storage import init_db, is_seen, mark_seen

def run_once():
    secrets = Secrets()
    keywords_cfg = load_keywords()
    init_db()
    
    crawler = MuasamcongCrawler()
    try:
        bids = crawler.fetch_recent_bids(max_pages=3)
        logger.info(f'Fetched {len(bids)} bids')
        
        new_count = 0
        for bid in bids:
            if is_seen(bid.tbmt_code):
                continue
            matched, kw = matches_keywords(bid, keywords_cfg)
            if not matched:
                mark_seen(bid.tbmt_code, bid.title, sent=False)
                continue
            
            text = format_bid_message(bid, kw)
            success_count = 0
            for chat_id in secrets.chat_id_list:
                if send_message(secrets.telegram_bot_token, chat_id, text):
                    success_count += 1
            
            mark_seen(bid.tbmt_code, bid.title, sent=(success_count > 0))
            new_count += 1
            logger.info(f'Sent: {bid.tbmt_code} | matched: {kw}')
        
        logger.info(f'Done. New bids sent: {new_count}')
    finally:
        crawler.close()

if __name__ == '__main__':
    logger.add('logs/tracker_{time:YYYYMMDD}.log', rotation='1 day', retention='30 days')
    run_once()
```

**Acceptance Day 1**:
- [ ] Chạy `python -m tracker` trên local thấy gói thầu khớp được gửi lên Telegram đúng format
- [ ] Chạy lần 2 không gửi trùng
- [ ] Sửa `keywords.yaml`, chạy lại, thấy kết quả thay đổi đúng

---

### 🟢 DAY 2 — Sáng (3-4h): Scheduling + Hardening

#### Bước 2.1 — Module `scheduler.py` (1h)

```python
# src/tracker/scheduler.py
from datetime import datetime, time as dtime, timedelta
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger
import pytz
from .__main__ import run_once
from .config import Secrets
from .crawler import BlockedException

# Global state cho cooldown (in-process)
_block_state = {
    'blocked_until': None,    # datetime | None
    'consecutive_blocks': 0,
}

def _in_quiet_hours(now: datetime, start_str: str, end_str: str) -> bool:
    """Kiểm tra now có nằm trong quiet hours không. Hỗ trợ wrap qua midnight."""
    start = dtime.fromisoformat(start_str)
    end = dtime.fromisoformat(end_str)
    cur = now.time()
    if start <= end:
        return start <= cur < end
    # wrap qua nửa đêm, vd 22:00 → 06:00
    return cur >= start or cur < end

def _in_block_cooldown(now: datetime) -> bool:
    until = _block_state['blocked_until']
    if until is None:
        return False
    if now < until:
        return True
    # Hết cooldown, reset state
    _block_state['blocked_until'] = None
    return False

def _trigger_cooldown(secrets: Secrets):
    """Khi bị block, tính cooldown theo exponential backoff."""
    _block_state['consecutive_blocks'] += 1
    n = _block_state['consecutive_blocks']
    hours = min(
        secrets.block_cooldown_hours * (2 ** (n - 1)),
        secrets.block_cooldown_max_hours
    )
    until = datetime.now(pytz.timezone('Asia/Ho_Chi_Minh')) + timedelta(hours=hours)
    _block_state['blocked_until'] = until
    logger.warning(f'BLOCK COOLDOWN activated: pause until {until.isoformat()} ({hours}h, attempt #{n})')

def safe_run():
    """Wrapper an toàn — không crash scheduler khi run_once lỗi."""
    secrets = Secrets()
    tz = pytz.timezone('Asia/Ho_Chi_Minh')
    now = datetime.now(tz)
    
    # 1. Skip nếu trong quiet hours
    if _in_quiet_hours(now, secrets.quiet_hours_start, secrets.quiet_hours_end):
        logger.info(f'Skip run: in quiet hours ({secrets.quiet_hours_start}-{secrets.quiet_hours_end})')
        return
    
    # 2. Skip nếu đang trong block cooldown
    if _in_block_cooldown(now):
        until = _block_state['blocked_until']
        logger.info(f'Skip run: in block cooldown until {until.isoformat()}')
        return
    
    # 3. Chạy
    try:
        run_once()
        # Thành công → reset block counter
        _block_state['consecutive_blocks'] = 0
    except BlockedException as e:
        logger.error(f'Detected block (HTTP {e.status_code}), entering cooldown')
        _trigger_cooldown(secrets)
    except Exception:
        logger.exception('Unhandled error in run_once, but scheduler continues')

def main():
    secrets = Secrets()
    logger.add(
        'logs/tracker_{time:YYYYMMDD}.log',
        rotation='1 day', retention='30 days',
        level=secrets.log_level,
    )
    
    scheduler = BlockingScheduler(timezone='Asia/Ho_Chi_Minh')
    scheduler.add_job(
        safe_run,
        IntervalTrigger(
            minutes=secrets.poll_interval_minutes,
            jitter=secrets.poll_jitter_seconds,  # ±N giây ngẫu nhiên — KHÔNG đều như đồng hồ
        ),
        id='crawl_job',
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )
    
    logger.info(
        f'Scheduler started: interval={secrets.poll_interval_minutes}m '
        f'±{secrets.poll_jitter_seconds}s jitter, '
        f'quiet={secrets.quiet_hours_start}-{secrets.quiet_hours_end}'
    )
    # Chạy ngay 1 lần khi start (sau khi check quiet hours)
    safe_run()
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info('Scheduler stopped')

if __name__ == '__main__':
    main()
```

> Cần bổ sung 4 field mới vào `Secrets` trong `config.py`:
> ```python
> poll_jitter_seconds: int = 600
> quiet_hours_start: str = '01:00'
> quiet_hours_end: str = '06:00'
> block_cooldown_hours: int = 6
> block_cooldown_max_hours: int = 48
> ```
> Và thêm `pytz` vào deps: `uv add pytz`.

Update `pyproject.toml`:
```toml
[project.scripts]
muasamcong-tracker = "tracker.scheduler:main"
```

#### Bước 2.2 — Error handling & retry (1h)

- [ ] Wrap `run_once()` trong try/except, log full traceback, không crash scheduler
- [ ] Crawler retry với exponential backoff (httpx-retries hoặc tenacity)
- [ ] Telegram retry: nếu fail thì lưu `sent_to_telegram=0`, lần chạy sau resend các bid chưa sent
- [ ] Rate limit Telegram: 30 messages/giây/bot — nếu gửi >20 msg/lần thì `time.sleep(2)` giữa các batch
- [ ] Health check: nếu 3 lần liên tiếp fetch 0 bid, gửi alert "⚠️ Tracker có thể bị chặn" lên Telegram

#### Bước 2.3 — Tests (1h)

- [ ] `test_filter.py`: test các case quan trọng:
  - Normalize: "Lâm Đồng" → "lam dong"
  - Match keyword trong title: bid title "Mua camera giám sát" + keyword "camera" → pass
  - Match keyword trong location: bid location "Tỉnh Lâm Đồng" + keyword "lâm đồng" → pass
  - Match keyword trong investor: bid investor "UBND Lâm Đồng" + keyword "lâm đồng" → pass
  - Không phân biệt dấu: keyword "lam dong" vẫn match bid có "Lâm Đồng"
  - Logic OR: bid match 1 trong nhiều keyword → pass, trả về đúng list matched
  - Filter location bổ sung: keyword pass nhưng location không trong list → reject
  - Filter budget: bid pass keyword nhưng budget < min → reject
- [ ] `test_formatter.py`: test format đúng emoji, escape HTML, hashtag có dấu underscore
- [ ] `test_storage.py`: test is_seen, mark_seen, không trùng
- [ ] Fixture mock response từ API (lưu 1 JSON sample trong `tests/fixtures/`)

#### Bước 2.4 — Bot setup commands (1h, optional)

Thêm 1 file `bot_commands.py` listen các lệnh trong nhóm Telegram:
- `/keywords` → in danh sách keyword hiện tại
- `/stats` → số bid đã gửi 7 ngày qua
- `/test` → force chạy 1 lần ngay

Dùng `httpx` polling `getUpdates` (không cần webhook) để giữ đơn giản.

---

### 🟢 DAY 2 — Chiều (2-3h): Deploy + Documentation

#### Bước 2.5 — Deploy options

Chọn 1 trong 3:

**A. systemd service trên VPS Ubuntu** (production-ready)

```ini
# /etc/systemd/system/muasamcong-tracker.service
[Unit]
Description=Muasamcong bid tracker
After=network.target

[Service]
Type=simple
User=tracker
WorkingDirectory=/opt/muasamcong-tracker
EnvironmentFile=/opt/muasamcong-tracker/.env
ExecStart=/opt/muasamcong-tracker/.venv/bin/muasamcong-tracker
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable muasamcong-tracker
sudo systemctl start muasamcong-tracker
sudo journalctl -u muasamcong-tracker -f
```

**B. Docker** (portable)

```dockerfile
FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir uv
COPY pyproject.toml ./
RUN uv pip install --system -e .
COPY src ./src
COPY config ./config
CMD ["python", "-m", "tracker.scheduler"]
```

**C. cron job** (đơn giản nhất)

```cron
*/15 * * * * cd /opt/muasamcong-tracker && /opt/muasamcong-tracker/.venv/bin/python -m tracker >> logs/cron.log 2>&1
```

#### Bước 2.6 — README.md (45 phút)

Có: hướng dẫn install, cài bot Telegram (BotFather), lấy chat_id (forward message tới `@userinfobot`), sửa keywords.yaml, chạy thử.

#### Bước 2.7 — Smoke test end-to-end + bàn giao

- [ ] Chạy thật trên server 1 chu kỳ (15 phút)
- [ ] Xác minh log không error
- [ ] Xác minh nhóm Telegram nhận được tin
- [ ] Sửa `keywords.yaml` qua SSH, xác minh thay đổi áp dụng lần chạy kế

**Acceptance Day 2**:
- [ ] Service chạy nền liên tục 1h không crash
- [ ] Update keywords không cần restart (đọc lại file mỗi lần `run_once`)
- [ ] Log file rotation hoạt động
- [ ] Nhóm Telegram nhận tin format đúng, link click được

---

## 7. Risks và mitigation

| Risk | Mức độ | Cách xử lý |
|---|---|---|
| Không tìm được API JSON sạch | Cao (40%) | Fallback Playwright, mất thêm 2-3h Day 1 |
| Server muasamcong rate limit / chặn IP | **Cao** | Đầy đủ chiến lược ở §7.1 dưới đây |
| Cookie/CSRF token bắt buộc | TB | Dùng `httpx.Client` giữ session, warm-up homepage trước API |
| Telegram block bot do spam | Thấp | Sleep giữa các message, không gửi quá 20 msg/lần |
| Format response thay đổi | Thấp (sau khi xong) | Log raw JSON, có alert khi parse fail |
| Trùng lặp do reset DB | Thấp | Backup `seen.db` daily |

### 7.1. Anti-detection strategy (CHIẾN LƯỢC TRÁNH BỊ CHẶN IP)

Đây là phần **quan trọng nhất** để tool sống được lâu dài. Các site chính phủ Việt Nam (gov.vn) thường có WAF/rate-limiter, IP bị đánh dấu sẽ block từ vài giờ tới vài ngày. Tool phải giả lập browser thật càng giống càng tốt.

**Nguyên lý**: server phân biệt bot vs người qua 5 dimension:

| Dimension | Bot signature | Human signature | Cách giả lập (đã làm trong code §1.5, §2.1) |
|---|---|---|---|
| **Tần suất** | Đều đặn 15p/lần | Bất quy luật, có lúc nghỉ | `POLL_INTERVAL=45m` + jitter `±10m` + quiet hours 01-06 |
| **Header** | Default `python-httpx/X.X` | Chrome full header set | UA pool + Sec-Ch-Ua + Sec-Fetch-* + Accept-Language vi-VN |
| **Pattern request** | API call thẳng | GET homepage → đọc → click search | `_warmup_session()` GET homepage trước, đợi 3-8s, mới POST API |
| **TLS fingerprint** | Python's OpenSSL | Chrome BoringSSL | `http2=True` (giải quyết 80%); upgrade lên `curl_cffi` nếu cần 99% |
| **Timing trong session** | Request bắn liên tiếp | 3-10s giữa các action | `_human_delay()` random 2-10s giữa requests |

**Các biện pháp đã code vào tool**:

1. **Tần suất 45 phút thay vì 15** — đủ để bắt thông báo mới (trang gov đăng ~50-100 bid/ngày, không sợ miss).
2. **Jitter ±10 phút** — không chạy ở phút :00, :15, :30, :45 chính xác. Apscheduler `IntervalTrigger(jitter=600)` xử lý.
3. **Quiet hours 01:00-06:00** — không người nào check thầu lúc 3h sáng. Crawler nghỉ tự nhiên.
4. **Warm-up session** — mỗi run mới: GET homepage trước, đợi 3-8s rồi mới POST API. Lấy session cookie + giảm sống Referer.
5. **Full Chrome header set** — UA, Sec-Ch-Ua-*, Sec-Fetch-*, Accept-Language vi-VN, Origin, Referer.
6. **HTTP/2** — `httpx.Client(http2=True)` + cài `httpx[http2]`. Chrome luôn dùng HTTP/2; nếu app dùng HTTP/1.1 là tín hiệu lạ.
7. **Max 2 page/lần** — hard cap. Không bao giờ cào >100 bid/chu kỳ.
8. **Exponential cooldown khi bị block** — gặp HTTP 429/403 → đợi 6h, lần block tiếp theo đợi 12h, rồi 24h, max 48h.
9. **Random delay giữa các page** — 4-10s, không click liên tục.

**Upgrade path khi vẫn bị block** (làm khi cần, không phải MVP):

- **Nâng cấp 1 — `curl_cffi`** thay `httpx`: spoof TLS fingerprint giống Chrome thật. `pip install curl_cffi`, đổi `httpx.Client()` thành `curl_cffi.requests.Session(impersonate='chrome120')`. Giải quyết case JA3 fingerprinting.
- **Nâng cấp 2 — Playwright headful**: chạy Chrome thật, không phải HTTP client. Server gần như không phân biệt được. Trade-off: ~500MB RAM, ~10s/run.
- **Nâng cấp 3 — Residential proxy rotation**: BrightData/Smartproxy ~$5-15/tháng cho volume nhỏ. Chỉ làm khi cần thật sự nghiêm túc và đã hỏi user OK với chi phí.

**Cảnh báo cho user nếu vẫn bị block dù đã hardening**:

Khi `BlockedException` raise 3 lần liên tiếp trong 24h, gửi 1 tin lên nhóm Telegram quản trị (chat_id riêng, set qua env `TELEGRAM_ADMIN_CHAT_ID`):
```
⚠️ Tracker bị server chặn lần thứ 3/24h
Cooldown hiện tại: {hours}h
Cân nhắc: (a) tăng POLL_INTERVAL_MINUTES, (b) upgrade lên Playwright/curl_cffi, (c) đổi IP/VPS.
```

---

## 8. Tham khảo

- Repo cũ (Scrapy, portal cũ): https://github.com/dinhhh/dauthaubk-spider — tham khảo cấu trúc spider
- n8n workflow tương tự: https://community.n8n.io/t/seeking-assistance-with-data-crawling-from-websites/99300
- Telegram Bot API docs: https://core.telegram.org/bots/api#sendmessage
- APScheduler docs: https://apscheduler.readthedocs.io/

---

## 9. Quy tắc khi Cursor code

1. **Không hardcode** endpoint, chat_id, token — tất cả vào `.env` hoặc `keywords.yaml`.
2. **Log mọi action quan trọng** với loguru: fetch_start, fetch_done, filter_match, telegram_send.
3. **Catch HTTPError cụ thể**, không catch `Exception` blanket trừ ở top-level `run_once`.
4. **Type hints đầy đủ**, chạy `mypy src/tracker` không lỗi.
5. **Format code** với `ruff format` + `ruff check`.
6. **Commit theo Conventional Commits**: `feat(crawler): ...`, `fix(telegram): ...`, `chore: ...`.
7. **Khi gặp việc cần kiến thức ngoài plan** (vd API endpoint cụ thể), **dừng và hỏi user** thay vì đoán bừa.

---

**Phiên bản plan: v1.2 (2026-05-15)** — Có thể hoàn thành trong 1.5-2 ngày làm việc với điều kiện tìm được API JSON. Nếu phải dùng Playwright fallback thì dồn ép vẫn xong trong 2 ngày.

**Changelog**:
- v1.2: Thêm anti-detection hardening đầy đủ — warm-up session, jitter, quiet hours, exponential cooldown khi gặp HTTP 429/403, User-Agent pool, HTTP/2, full Chrome header set. Tăng default interval từ 15m lên 45m ± 10m. Cập nhật pip deps thêm `pytz` và `httpx[http2]`.
- v1.1: Đơn giản hoá UX — keywords search across all fields (title, investor, field, location, status), bỏ ràng buộc user phải phân biệt keyword theo nhóm. Locations/fields/budget chuyển sang filter bổ sung optional.
- v1.0: Initial plan.