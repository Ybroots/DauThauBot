# Playwright + Chromium — tag phải khớp bản playwright nhúng trong image (không pip install playwright)
FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PYTHONPATH=/app/src \
    DATA_DIR=/data

COPY requirements-docker.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY BUILD_STAMP run_railway.py pyproject.toml README.md ./
COPY config ./config
COPY src ./src

RUN grep -q 'v3-inline-scheduler-utc' /app/run_railway.py

CMD ["python", "run_railway.py"]
