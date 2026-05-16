# Playwright + Chromium — reCAPTCHA smart/search
FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PYTHONPATH=/app/src \
    DATA_DIR=/data

COPY requirements.txt .
# playwright==1.49.0 phải khớp base image — không playwright install (browser có sẵn trong image)
RUN pip install --no-cache-dir -r requirements.txt \
    && python -c "import playwright; v=playwright.__version__; print('playwright', v); assert v.startswith('1.49')"

# Một lần copy app — BUILD_STAMP buộc invalidate cache khi đổi version
COPY BUILD_STAMP run_railway.py pyproject.toml README.md ./
COPY config ./config
COPY src ./src

# Fail build sớm nếu Railway vẫn dùng file cũ trong context
RUN grep -q 'v3-inline-scheduler-utc' /app/run_railway.py \
 && grep -q 'BlockingScheduler(timezone="UTC")' /app/run_railway.py \
 || (echo "BUILD FAIL: run_railway.py chua phai ban v3. Push Git + clear cache." && exit 1)

CMD ["python", "run_railway.py"]
