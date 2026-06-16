from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path
from urllib.parse import parse_qs

from loguru import logger

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# Vercel Functions only guarantee writable storage under /tmp.
os.environ.setdefault("DATA_DIR", "/tmp/dauthau")
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "0")
os.environ.setdefault("CRAWL_MAX_PAGES", "1")
os.environ.setdefault("CRAWL_PAGE_SIZE", "20")
os.environ.setdefault("CRAWL_KEYWORD_GAP_MIN_SECONDS", "0")
os.environ.setdefault("CRAWL_KEYWORD_GAP_MAX_SECONDS", "0")

logger.remove()
logger.add(
    sys.stderr,
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    colorize=False,
    format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}",
)


def _json_response(start_response, status: str, payload: dict):
    body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    start_response(
        status,
        [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Cache-Control", "no-store"),
            ("Content-Length", str(len(body))),
        ],
    )
    return [body]


def _header(environ: dict, name: str) -> str:
    key = "HTTP_" + name.upper().replace("-", "_")
    return str(environ.get(key, ""))


def _authorized(environ: dict) -> bool:
    secret = os.environ.get("VERCEL_CRON_SECRET", "").strip()
    if not secret:
        return True

    query_secret = (parse_qs(str(environ.get("QUERY_STRING", ""))).get("secret") or [""])[0]
    auth_header = _header(environ, "authorization")
    bearer = auth_header.removeprefix("Bearer ").strip()
    user_agent = _header(environ, "user-agent")

    return (
        query_secret == secret
        or _header(environ, "x-cron-secret") == secret
        or bearer == secret
        or user_agent.startswith("vercel-cron/")
    )


def _health(start_response):
    from tracker.crawler import INDEX_URL, site_status

    return _json_response(
        start_response,
        "200 OK",
        {
            "ok": True,
            "service": "DauThauBot",
            "runtime": "vercel-python-wsgi",
            "index_url": INDEX_URL,
            "data_dir": os.environ.get("DATA_DIR"),
            "site_status": site_status(),
        },
    )


def _cron(environ: dict, start_response):
    if environ.get("REQUEST_METHOD") not in ("GET", "POST"):
        return _json_response(
            start_response,
            "405 Method Not Allowed",
            {"ok": False, "error": "method_not_allowed"},
        )
    if not _authorized(environ):
        return _json_response(
            start_response,
            "401 Unauthorized",
            {"ok": False, "error": "unauthorized"},
        )

    started = time.time()
    try:
        from tracker.__main__ import run_once
        from tracker.crawler import BlockedException

        logger.info("vercel_cron: starting run_once")
        run_once()
        return _json_response(
            start_response,
            "200 OK",
            {
                "ok": True,
                "status": "completed",
                "duration_ms": int((time.time() - started) * 1000),
                "data_dir": os.environ.get("DATA_DIR"),
            },
        )
    except BlockedException as exc:
        logger.warning("vercel_cron: blocked HTTP {}", exc.status_code)
        return _json_response(
            start_response,
            "503 Service Unavailable",
            {
                "ok": False,
                "status": "blocked",
                "status_code": exc.status_code,
                "duration_ms": int((time.time() - started) * 1000),
            },
        )
    except Exception as exc:
        logger.exception("vercel_cron: failed")
        return _json_response(
            start_response,
            "500 Internal Server Error",
            {
                "ok": False,
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(limit=5),
                "duration_ms": int((time.time() - started) * 1000),
            },
        )


def app(environ: dict, start_response):
    path = str(environ.get("PATH_INFO") or "/").rstrip("/") or "/"
    if path in ("/", "/api", "/api/health", "/health"):
        return _health(start_response)
    if path in ("/api/cron", "/cron"):
        return _cron(environ, start_response)
    return _json_response(
        start_response,
        "404 Not Found",
        {
            "ok": False,
            "error": "not_found",
            "paths": ["/api/health", "/api/cron"],
        },
    )
