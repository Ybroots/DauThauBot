# Playwright + Chromium có sẵn — phù hợp reCAPTCHA smart/search
FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY pyproject.toml README.md ./
COPY config ./config
COPY src ./src

# Volume Railway gắn tại /data (SQLite + tuỳ chọn keywords.yaml)
ENV DATA_DIR=/data

CMD ["python", "-m", "tracker.railway_main"]
