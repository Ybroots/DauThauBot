# Hướng dẫn deploy Railway — từng bước (bot + tracker 24/7)

Tài liệu này mô tả **từng thao tác** để chạy **DauThauBot** trên [Railway](https://railway.app): một service chạy **`railway_main`** = **cron cào TBMT** + **bot Telegram** (long polling) liên tục.

**Thời gian dự kiến:** 45–90 phút lần đầu (gồm chờ build Docker).

**Bạn cần sẵn:**

- Tài khoản [GitHub](https://github.com) và repo **đã push code** có `Dockerfile`, `requirements.txt`, `src/tracker/railway_main.py`.
- Tài khoản [Railway](https://railway.app) (đăng nhập bằng GitHub là tiện nhất).
- Một bot Telegram (token từ [@BotFather](https://t.me/BotFather)) và **ít nhất một** nhóm/kênh hoặc chat để nhận tin.

---

## Phần A — Hiểu trước khi bấm (2 phút)

### A1. Process chạy gì?

Sau khi deploy thành công, container chạy:

```bash
python -m tracker.railway_main
```

- **Luồng phụ (daemon):** bot lệnh `/tim`, `/ping`, … — gọi API Telegram `getUpdates` liên tục.
- **Luồng chính:** `scheduler` — gọi `run_once()` theo chu kỳ (mặc định ~45 phút ± jitter) để cào Muasamcong và gửi TBMT khớp từ khóa.

### A2. Vì sao cần Volume `/data`?

SQLite (`seen.db`) lưu **mã TBMT đã xử lý**. Nếu không có volume, mỗi lần redeploy file nằm trên disk tạm → **mất DB** → có thể gửi trùng tin. Volume gắn vào **`/data`** và biến **`DATA_DIR=/data`** để DB bền.

### A3. Từ khóa cron đọc từ đâu?

Thứ tự ưu tiên (trong code):

1. Biến môi trường **`KEYWORDS_YAML_PATH`** (đường dẫn tuyệt đối tới file `.yaml`), hoặc  
2. File **`/data/keywords.yaml`** (trên volume) nếu tồn tại, hoặc  
3. **`config/keywords.yaml`** trong image (chỉ có nếu bạn commit file này vào Git), hoặc  
4. Fallback: **`config/keywords.example.yaml`** trong repo.

---

## Phần B — Chuẩn bị Telegram (10–15 phút)

### B1. Tạo bot (nếu chưa có)

1. Mở Telegram → tìm **@BotFather** → Start.  
2. Gửi `/newbot` → làm theo hướng dẫn đặt tên → **sao chép token** dạng `123456789:AAH...` — đây là **`TELEGRAM_BOT_TOKEN`**.  
3. (Tuỳ chọn) `/setjoingroup` hoặc thêm bot vào nhóm/supergroup bạn muốn nhận TBMT.

### B2. Lấy `chat_id` nhóm (cho `TELEGRAM_CHAT_IDS`)

1. Thêm bot vào **nhóm** (supergroup khuyến dùng).  
2. Gửi một tin bất kỳ trong nhóm (có thể chỉ là “test”).  
3. Mở trình duyệt (khi đã có token):

   `https://api.telegram.org/bot<TOKEN>/getUpdates`

   Thay `<TOKEN>` bằng token thật (không có dấu ngoặc).

   **Cảnh báo:** URL chứa token — chỉ làm trong cửa sổ ẩn danh / xóa lịch sử sau khi xong, không chia sẻ link.

4. Trong JSON, tìm `"chat":{"id":-100xxxxxxxxxx,...}` — số **`id`** (thường âm với nhóm) là **chat_id**.  
5. Ghi lại: **`TELEGRAM_CHAT_IDS=-100xxxxxxxxxx`** (nếu nhiều nhóm: cách nhau bằng dấu phẩy, không khoảng trắng thừa).

**Cách khác:** chạy bot ở máy local, vào nhóm gõ `/id` (nếu bot đã hỗ trợ lệnh này) hoặc dùng bot như @userinfobot theo hướng dẫn trong README.

### B3. Lấy `user_id` của bạn (cho `TELEGRAM_ADMIN_CHAT_ID` — khuyến nghị)

1. Chat riêng với [@userinfobot](https://t.me/userinfobot) hoặc bot tương tự → xem **Your user ID** (số dương).  
2. Dùng cho **`TELEGRAM_ADMIN_CHAT_ID`** để nhận cảnh báo và (nếu cấu hình) dùng lệnh **`/test`** trên server.

### B4. Nhóm: bot phải “thấy” tin nhắn

- Vào **@BotFather** → chọn bot → **Bot Settings** → **Group Privacy** → chọn **Turn off** (Disable) nếu bạn muốn bot xử lý lệnh trong nhóm mà không cần slash ở mọi tin (tùy cấu hình `BOT_GROUP_*` trong code).  
- Với lệnh dạng `/tim`, thường vẫn hoạt động; nếu bot không trả lời trong nhóm, kiểm tra lại bước này và quyền admin bot trong nhóm.

### B5. Chỉ một nơi gọi `getUpdates`

**Quan trọng:** cùng một **`TELEGRAM_BOT_TOKEN`** chỉ được **một process** long polling. Trước khi deploy Railway:

- Tắt máy local đang chạy `python -m tracker.bot_commands` hoặc `railway_main` với token đó.  
- Không chạy song song hai Railway service cùng token.

---

## Phần C — Đưa code lên GitHub (5 phút)

1. Trên máy, trong thư mục project: `git status` — đảm bảo đã commit đủ file deploy (`Dockerfile`, `railway_main.py`, …).  
2. `git push origin main` (hoặc nhánh bạn nối với Railway).  
3. Trên GitHub, mở repo → tab **Code** — xác nhận có **`Dockerfile`** ở **thư mục gốc** repo (cùng cấp với `README.md`).

---

## Phần D — Tạo project Railway (10 phút)

### D1. Đăng nhập

1. Vào [https://railway.app](https://railway.app).  
2. **Login** → chọn **Login with GitHub** → cấp quyền đọc repo khi được hỏi.

### D2. Tạo project từ GitHub

1. Dashboard Railway → **New Project**.  
2. Chọn **Deploy from GitHub repo** (hoặc wording tương đương).  
3. Lần đầu có thể phải **Configure GitHub App** / chọn tổ chức cá nhân → tick repo **DauThauBot** (hoặc tên repo của bạn) → **Install**.  
4. Chọn đúng repo → Railway tạo **một service** mặc định.

### D3. Bắt buộc dùng Dockerfile (khuyến nghị)

1. Click vào **service** vừa tạo (thường tên trùng repo).  
2. Vào tab **Settings** (hoặc biểu tượng bánh răng).  
3. Mục **Build** / **Builder**:  
   - Chọn **Dockerfile** (nếu Railway có dropdown **Docker** vs **Nixpacks**, chọn build kiểu Docker).  
4. **Dockerfile path:** để trống hoặc `Dockerfile` nếu file nằm ở root.  
5. **Root directory:** để trống (trừ khi repo là monorepo và code nằm thư mục con — khi đó chỉ đúng thư mục chứa `Dockerfile`).

### D4. Start command (quan trọng — sửa nếu log báo `No module named 'tracker'`)

Trong **Settings → Deploy** → **Custom Start Command**:

- Gõ **`python run_railway.py`** (file ở root repo), **hoặc để trống** nếu build **Dockerfile** (image đã `CMD python run_railway.py`).
- **Không** dùng `python -m tracker.railway_main` trừ khi đã `pip install .` trong image — dễ lỗi trên Nixpacks.

Nếu log vẫn hiện `/usr/bin/python` + `ModuleNotFoundError`: Railway đang **không** dùng Dockerfile → chuyển **Builder = Dockerfile** (mục D3) rồi redeploy.

### D5. Redeploy (xóa cache build cũ)

1. **Save** mọi thay đổi Settings.  
2. Tab **Deployments** → menu **⋯** trên deployment mới nhất → **Redeploy** (nếu có **Clear build cache** / **Rebuild without cache** thì bật).  
3. Mở tab **Build logs** (không phải Deploy logs): phải thấy bước `FROM mcr.microsoft.com/playwright/python` — nếu không thấy dòng này thì vẫn đang Nixpacks.  
4. Đợi **Success** (Docker Playwright thường **5–15 phút**).  
5. Tab **Deploy logs** — tìm `[run_railway] cwd=...` và `railway_main: scheduler + Telegram bot`.

---

## Phần E — Gắn Volume lưu SQLite (5 phút)

> Làm trên **cùng service** đang chạy `railway_main`, không tạo service rỗng khác.

1. Vào service → tab **Volumes** (hoặc **Storage**).  
2. **Add volume** / **New volume**.  
3. **Mount path** nhập **chính xác:** `/data`  
   - Không dùng `data` hay `./data` — phải là **`/data`** để khớp hướng dẫn và `Dockerfile` mẫu.  
4. Dung lượng: **1 GB** là đủ dùng lâu.  
5. **Create** → Railway có thể **restart** container — bình thường.

---

## Phần F — Biến môi trường (Variables) (15 phút)

1. Vào service → tab **Variables**.  
2. Thêm từng biến (hoặc dùng **RAW Editor** nếu muốn paste cả khối).

### F1. Bộ tối thiểu (bắt buộc để chạy)

| Tên biến | Giá trị ví dụ | Ý nghĩa |
|-----------|----------------|---------|
| `TELEGRAM_BOT_TOKEN` | `123456:ABC...` | Token từ BotFather |
| `TELEGRAM_CHAT_IDS` | `-1001234567890` | Một hoặc nhiều `chat_id`, phẩy cách |
| `DATA_DIR` | `/data` | Thư mục volume — SQLite `seen.db` nằm đây |

**Lưu ý:** pydantic-settings đọc tên **IN HOA** giống `.env` mẫu trong repo.

### F2. Bộ khuyến nghị (nên có)

| Tên biến | Ví dụ | Ý nghĩa |
|-----------|--------|---------|
| `TELEGRAM_ADMIN_CHAT_ID` | `123456789` | `user_id` của bạn — cảnh báo + quyền `/test` |
| `LOG_LEVEL` | `INFO` | Mức log |
| `USE_PLAYWRIGHT` | `true` | Bắt buộc `true` cho Muasamcong (reCAPTCHA) |
| `PLAYWRIGHT_HEADLESS` | `true` | Headless trên server |
| `POLL_INTERVAL_MINUTES` | `45` | Chu kỳ cron |
| `POLL_JITTER_SECONDS` | `600` | Jitter ± giây |
| `QUIET_HOURS_START` | `01:00` | Giờ VN không chạy cron |
| `QUIET_HOURS_END` | `06:00` | Hết quiet hours |
| `CRAWL_PER_KEYWORD` | `true` | Cào theo từng từ khóa (code hiện tại) |
| `CRAWL_MAX_PAGES` | `3` | Giảm tải khi nhiều từ khóa trên cloud |
| `CRAWL_PAGE_SIZE` | `50` | Tối đa 50 theo code |

### F3. Từ khóa TBMT (chọn **một** cách)

**Cách 1 — Dùng ví dụ có sẵn trong image (nhanh nhất để thử deploy)**  
- Không thêm biến gì thêm.  
- Nếu không có `config/keywords.yaml` trong Git, app sẽ đọc **`config/keywords.example.yaml`**.

**Cách 2 — Ghi file trên volume** `/data/keywords.yaml`  

- Nội dung file giống `config/keywords.example.yaml` (khóa `keywords`, `locations`, …).  
- Cách tạo file: dùng [Railway CLI](https://docs.railway.app/develop/cli) `railway shell` rồi `nano`/`vi`, hoặc one-off container — tùy quen; đảm bảo sau khi tạo, file nằm đúng **`/data/keywords.yaml`**.

**Cách 3 — Biến `KEYWORDS_YAML_PATH`**  

- Ví dụ: `/data/keywords.yaml` sau khi bạn đã tạo file ở Cách 2.

### F4. Lưu biến

1. **Add** / **Update** từng biến.  
2. Railway thường **tự redeploy** sau khi đổi Variables — đợi deployment xanh.

---

## Phần G — Kiểm tra sau deploy (10 phút)

### G1. Log container

1. Service → tab **Logs** / **View logs**.  
2. Tìm các dòng (có thể lệch vài từ nhưng cùng ý):  
   - `railway_main: scheduler + Telegram bot`  
   - `Telegram bot thread started`  
   - `Scheduler started: interval=...`

Nếu không thấy → xem **Phần H**.

### G2. Kiểm tra Telegram

1. Mở **chat riêng** với bot hoặc **nhóm** đã cấu hình `TELEGRAM_CHAT_IDS`.  
2. Gửi lần lượt:  
   - `/ping` → mong đợi trả lời `pong` + phiên bản.  
   - `/lenh` → danh sách lệnh.  
   - `/tim camera` (hoặc từ khóa khác) → **chờ 30–90 giây** (Playwright + reCAPTCHA).

### G3. Kiểm tra cron (tùy chọn)

- Đợi đủ một khoảng `POLL_INTERVAL_MINUTES`, hoặc  
- Nếu đã set `TELEGRAM_ADMIN_CHAT_ID` trùng `user_id` của bạn: gửi **`/test`** trong chat bot có quyền — bot báo đã chạy một vòng tracker (xem thêm log Railway).

---

## Phần H — Gỡ lỗi thường gặp

| Hiện tượng | Việc nên làm |
|-------------|----------------|
| Build fail / timeout | Build Playwright lớn — thử deploy lại; kiểm tra Dockerfile ở root; xem log dòng `error`. |
| `Permission denied` khi ghi DB | Volume chưa mount **`/data`** hoặc `DATA_DIR` khác mount path. |
| Mỗi lần deploy mất hết “đã gửi” | Chưa có volume hoặc `DATA_DIR` không trỏ `/data`. |
| Bot không trả lời | Token sai; hoặc vẫn chạy **một process khác** `getUpdates` cùng token; hoặc Group Privacy / bot không vào đúng nhóm. |
| `/tim` lỗi reCAPTCHA / timeout | Thử giảm tần suất gọi; tăng `POLL_INTERVAL_MINUTES`; kiểm tra log chi tiết; trên cloud đôi khi cần thử `PLAYWRIGHT_HEADLESS=false` (không phải lúc nào cũng khả thi trên Railway). |
| `ModuleNotFoundError: No module named 'tracker'` | Start command: **`python run_railway.py`**. Build: **Dockerfile** (không Nixpacks). Root Directory để trống. |
| Log có `/usr/lib/python3.10` + lỗi `importlib` | Đang chạy **Nixpacks**, không phải image Docker — đổi Builder → **DockerFILE**, Redeploy **Clear build cache**. |
| `[run_railway] THIEU package: httpx` | Build chưa `pip install -r requirements.txt` — dùng Dockerfile hoặc xem log tab **Build**. |
| `ZoneInfoNotFoundError: Asia/Ho_Chi_Minh` / `No module named 'tzdata'` | Repo đã thêm package **`tzdata`** trong `requirements.txt`; **Redeploy + Clear build cache** để layer `pip install` chạy lại. |
| **Build image failed** sau khi sửa Dockerfile | Thường do `apt-get install tzdata` hỏi timezone — image hiện chỉ dùng **`pip install tzdata`**, không apt. Xem tab **Build Logs** dòng đỏ cuối. |
| Playwright `Executable doesn't exist` / cần `v1.59.0-jammy` | `pip install playwright` mới hơn browser trong image — repo pin **`playwright==1.49.0`** + image **`v1.49.0-jammy`**. Redeploy clear cache. |
| `DH_KEY_TOO_SMALL` SSL | Đã xử lý trong crawler (SECLEVEL=1); redeploy bản mới. |
| `ModuleNotFoundError` (khác) | `requirements.txt` / image thiếu package — so với repo. |

---

## Phần I — Cập nhật code sau này

1. Sửa code trên máy → `git commit` → `git push origin main`.  
2. Railway tự tạo deployment mới.  
3. **Không** commit file `.env`; chỉnh secrets trên tab **Variables**.

---

## Phần J — Chi phí

Worker 24/7 + Playwright tốn RAM/CPU hơn script nhỏ. Tham khảo billing trên Railway (trial credit / gói Hobby ~\$5/tháng tùy thời điểm).

---

## Tóm tắt checklist

- [ ] Repo GitHub có `Dockerfile` ở root, đã push.  
- [ ] Railway: project từ GitHub, build **Dockerfile**.  
- [ ] Volume mount **`/data`**.  
- [ ] Variables: **`TELEGRAM_BOT_TOKEN`**, **`TELEGRAM_CHAT_IDS`**, **`DATA_DIR=/data`**.  
- [ ] (Khuyến nghị) `TELEGRAM_ADMIN_CHAT_ID`, crawl/poll/playwright như Phần F.  
- [ ] Logs có dòng `railway_main` + bot thread + scheduler.  
- [ ] Telegram: `/ping` OK, `/tim ...` chạy được.  
- [ ] Chỉ **một** chỗ dùng token bot (không chạy song song bot local).

---

*Tài liệu bám sát code: `src/tracker/railway_main.py`, `Dockerfile`, `DATA_DIR`, `load_keywords()` trong `config.py`.*
