# Playwright + Chromium có sẵn — phù hợp reCAPTCHA smart/search
FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    TZ=Asia/Ho_Chi_Minh

# tzdata qua pip (requirements.txt) — đủ cho zoneinfo Asia/Ho_Chi_Minh; tránh apt interactive khi build
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY pyproject.toml README.md run_railway.py ./
COPY config ./config
COPY src ./src

# Cài package tracker (src/tracker) vào site-packages — tránh ModuleNotFoundError
RUN pip install --no-cache-dir .

ENV PYTHONPATH=/app/src

# Volume Railway gắn tại /data (SQLite + tuỳ chọn keywords.yaml)
ENV DATA_DIR=/data

CMD ["python", "run_railway.py"]
