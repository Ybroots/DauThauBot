# Muasamcong Tracker (DauThauBot)

Repo: [github.com/Ybroots/DauThauBot](https://github.com/Ybroots/DauThauBot)

Tool Python cào thông báo mời thầu (TBMT) từ [muasamcong.mpi.gov.vn](https://muasamcong.mpi.gov.vn/web/guest/contractor-selection), lọc theo từ khóa và gửi lên nhóm Telegram.

## Yêu cầu

- Python 3.9+
- Chromium (cho Playwright — API tìm kiếm cần reCAPTCHA)

## Cài đặt

```bash
cd Tool
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[playwright,dev]"
playwright install chromium
```

Sao chép cấu hình:

```bash
copy .env.example .env
```

Chỉnh `.env`:

- `TELEGRAM_BOT_TOKEN` — token từ [@BotFather](https://t.me/BotFather)
- `TELEGRAM_CHAT_IDS` — ID nhóm (âm), lấy qua [@userinfobot](https://t.me/userinfobot) hoặc forward tin vào bot

Chỉnh từ khóa: sao chép `config/keywords.example.yaml` thành `config/keywords.yaml` rồi sửa (file `keywords.yaml` không đưa lên Git — mỗi máy một bản).

## Chạy

Một lần (thử nhanh):

```bash
python -m tracker
```

Chạy nền theo lịch (mặc định 45 phút ± jitter):

```bash
muasamcong-tracker
# hoặc
python -m tracker.scheduler
```

Lệnh bot (tùy chọn, terminal riêng):

```bash
python -m tracker.bot_commands
```

- `/tim` → bot gợi ý nhập từ khóa; tin tiếp theo (vd `camera, lâm đồng`) sẽ cào và gửi kết quả **ngay** trong chat đó (**không** ghi `seen.db`; khác luồng cron).
- `/tim camera | máy chủ` — tra một lần với các từ OR.
- `/help` — hướng dẫn dài; `/lenh` — danh sách lệnh ngắn.
- `/thongke` — thống kê 24h / 7d / 30d + tổng DB + chưa gửi; `/stats` — gói đã gửi 7 ngày.
- `/lichsu [n]` — n tin gần nhất trong `seen.db` (mặc định 10); `/chuagui` — gói chưa gửi Telegram.
- `/keywords` — từ khóa + bộ lọc cron; `/id` — `chat_id` / `user_id` để điền `.env`.
- `/ping`, `/about` — kiểm tra bot + phiên bản.
- `/test` — chạy một vòng tracker; nếu có `TELEGRAM_ADMIN_CHAT_ID` thì chỉ chat/user khớp mới gọi được.

**Chat riêng:** có thể gõ thẳng một dòng từ khóa không cần `/tim`.

**Trong nhóm:** mặc định chỉ các lệnh trên được xử lý (`/tim`). Muốn mỗi tin thường cũng kích hoạt tra như trong chat riêng, đặt `BOT_GROUP_FREEWORD=true` trong `.env` (dễ tốn tài nguyên và ồn).

Để bot *nhìn thấy* tin không phải lệnh trong nhóm: tắt **Group Privacy** trong [@BotFather](https://t.me/BotFather) (hoặc giữ bot là admin nhóm).

**Lưu ý:** một token bot chỉ nên có **một** process gọi `getUpdates` (chỉ một `bot_commands` chạy). Tracker + scheduler chạy song song vẫn ổn.

Tùy chọn `.env`: `INTERACTIVE_SEARCH_MAX_MESSAGES`, `INTERACTIVE_SEARCH_COOLDOWN_SECONDS`, `BOT_GROUP_REPLY_HINT`.

## Cấu trúc

- `src/tracker/crawler.py` — gọi API `smart/search` (Playwright + reCAPTCHA)
- `data/seen.db` — SQLite khử trùng Mã TBMT
- `logs/` — log hàng ngày

## Tăng số gói cào mỗi lần

Mặc định **cào theo từng từ khóa phía server** (`CRAWL_PER_KEYWORD=true` khi `keywords.yaml` có từ): mỗi từ = một chuỗi `smart/search` (tối đa `CRAWL_MAX_PAGES` trang), kết quả **gộp và loại trùng theo mã TBMT**. Cách này tìm đúng hướng ES hơn là chỉ lấy vài trang “TBMT mới nhất” rồi lọc từ khóa trên máy.

Trong `.env`:

```env
CRAWL_PAGE_SIZE=50    # 10–50 (giống trang web)
CRAWL_MAX_PAGES=5     # 1–10 trang cho mỗi từ khóa (hoặc cho luồng duy nhất nếu tắt per-keyword)
CRAWL_PER_KEYWORD=true
CRAWL_KEYWORD_GAP_MIN_SECONDS=6
CRAWL_KEYWORD_GAP_MAX_SECONDS=14
```

| Cấu hình | Gói/lần | Ghi chú |
|----------|---------|---------|
| `2` × `50` (mặc định) | ~100 | An toàn, đủ cho chu kỳ 45 phút |
| `5` × `50` | ~250 | Cân bằng |
| `10` × `50` | ~500 | Dễ bị rate-limit; chạy chậm hơn (~1 phút/lần) |

Nhiều từ khóa ⇒ nhiều lần Playwright + reCAPTCHA: giảm `CRAWL_MAX_PAGES` hoặc tăng `POLL_INTERVAL_MINUTES` nếu hay bị chặn.

`CRAWL_PER_KEYWORD=false` — quay lại một luồng TBMT mới, lọc từ khóa chỉ trên máy (nhẹ hơn, dễ miss gói).

## Ghi chú kỹ thuật

API tìm kiếm: `POST /o/egp-portal-contractor-selection-v2/services/smart/search?token=<recaptcha>`.

Không dùng Playwright (`USE_PLAYWRIGHT=false`) thường sẽ bị HTTP 400 vì thiếu token.

## Test

```bash
pytest
```
