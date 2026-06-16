from __future__ import annotations

import json
import os
import sys
import time
import traceback
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from loguru import logger

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# Vercel functions should write mutable data under /tmp. These defaults can be
# overridden in Vercel Project Settings when a persistent external store is added.
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


def _write_json(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _authorized(handler: BaseHTTPRequestHandler) -> bool:
    secret = os.environ.get("VERCEL_CRON_SECRET", "").strip()
    if not secret:
        return True

    parsed = urlparse(handler.path)
    query_secret = (parse_qs(parsed.query).get("secret") or [""])[0]
    header_secret = handler.headers.get("x-cron-secret", "")
    auth_header = handler.headers.get("authorization", "")
    bearer = auth_header.removeprefix("Bearer ").strip()
    user_agent = handler.headers.get("user-agent", "")

    return (
        query_secret == secret
        or header_secret == secret
        or bearer == secret
        or user_agent.startswith("vercel-cron/")
    )


class handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self._run()

    def do_POST(self) -> None:
        self._run()

    def _run(self) -> None:
        started = time.time()
        if not _authorized(self):
            _write_json(self, 401, {"ok": False, "error": "unauthorized"})
            return

        try:
            from tracker.__main__ import run_once
            from tracker.crawler import BlockedException

            logger.info("vercel_cron: starting run_once")
            run_once()
            _write_json(
                self,
                200,
                {
                    "ok": True,
                    "status": "completed",
                    "duration_ms": int((time.time() - started) * 1000),
                    "data_dir": os.environ.get("DATA_DIR"),
                },
            )
        except BlockedException as exc:
            logger.warning("vercel_cron: blocked HTTP {}", exc.status_code)
            _write_json(
                self,
                503,
                {
                    "ok": False,
                    "status": "blocked",
                    "status_code": exc.status_code,
                    "duration_ms": int((time.time() - started) * 1000),
                },
            )
        except Exception as exc:
            logger.exception("vercel_cron: failed")
            _write_json(
                self,
                500,
                {
                    "ok": False,
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc(limit=5),
                    "duration_ms": int((time.time() - started) * 1000),
                },
            )
