# Deploy DauThauBot tren Vercel

Vercel chi phu hop chay theo HTTP function/cron, khong phu hop process 24/7.
Repo nay them:

- `api/index.py`: WSGI entrypoint chinh cho Vercel
- `/api/health`: kiem tra deploy
- `/api/cron`: chay mot vong `run_once()`
- `vercel.json`: cau hinh Python Function va Vercel Cron moi gio
- `pyproject.toml`: `tool.vercel.entrypoint = "api/index.py"`

## Cach deploy

1. Push commit len GitHub.
2. Tao Vercel project tu repo GitHub nay.
3. Trong Vercel Project Settings, them environment variables:

```env
TELEGRAM_BOT_TOKEN=123456:ABC...
TELEGRAM_CHAT_IDS=-1001234567890
TELEGRAM_ADMIN_CHAT_ID=123456789
LOG_LEVEL=INFO
USE_PLAYWRIGHT=true
CRAWL_MAX_PAGES=1
CRAWL_PAGE_SIZE=20
CRAWL_PER_KEYWORD=true
```

Vercel function mac dinh ghi SQLite vao `/tmp/dauthau`. Thu muc nay khong
dam bao ben vung qua moi deployment/cold start. Neu can chong gui trung tin
lau dai, hay giu Railway/VPS voi volume, hoac thay SQLite bang DB ngoai.

## Playwright tren Vercel

Cong muasamcong can reCAPTCHA token, nen crawler thuong can Playwright.
Python Playwright local tren Vercel co the bi gioi han Chromium/browser binary.
Neu function fail vi khong launch duoc browser, dung browser remote va set mot
trong cac bien:

```env
PLAYWRIGHT_CONNECT_URL=wss://...
# hoac
PLAYWRIGHT_CDP_URL=wss://...
```

Khi co bien nay, crawler se connect browser remote thay vi launch Chromium
trong Vercel function.

## Endpoint

- `GET /api/health`: tra JSON trang thai deploy.
- `GET /api/cron`: chay mot vong crawl va gui Telegram.
- `POST /api/cron`: tuong tu GET, tien cho manual trigger.

Neu muon bao ve manual trigger, set:

```env
VERCEL_CRON_SECRET=mot_chuoi_bi_mat
```

Sau do goi thu cong bang:

```text
https://<project>.vercel.app/api/cron?secret=mot_chuoi_bi_mat
```

Vercel Cron van duoc cho phep bang user-agent `vercel-cron/*`.

## Luu y

- Cron trong `vercel.json` dang la `0 * * * *`, tuc moi gio theo UTC.
- Giam `CRAWL_MAX_PAGES` va `CRAWL_PAGE_SIZE` de tranh qua `maxDuration`.
- Bot Telegram long polling (`python -m tracker.bot_commands`) khong chay 24/7
  tren Vercel. Vercel chi chay crawl theo cron/manual trigger.
