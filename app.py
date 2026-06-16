from __future__ import annotations

import os
import sys
import time
import traceback
from pathlib import Path

from flask import Flask, jsonify, request
from loguru import logger

ROOT = Path(__file__).resolve().parent
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

app = Flask(__name__)


def _authorized() -> bool:
    secret = os.environ.get("VERCEL_CRON_SECRET", "").strip()
    if not secret:
        return True

    auth_header = request.headers.get("authorization", "")
    bearer = auth_header.removeprefix("Bearer ").strip()
    user_agent = request.headers.get("user-agent", "")

    return (
        request.args.get("secret", "") == secret
        or request.headers.get("x-cron-secret", "") == secret
        or bearer == secret
        or user_agent.startswith("vercel-cron/")
    )


@app.get("/")
@app.get("/health")
@app.get("/api/health")
def health():
    from tracker.crawler import INDEX_URL, site_status

    return jsonify(
        {
            "ok": True,
            "service": "DauThauBot",
            "runtime": "vercel-python-flask",
            "index_url": INDEX_URL,
            "data_dir": os.environ.get("DATA_DIR"),
            "site_status": site_status(),
        }
    )


@app.route("/cron", methods=["GET", "POST"])
@app.route("/api/cron", methods=["GET", "POST"])
def cron():
    if not _authorized():
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    started = time.time()
    try:
        from tracker.__main__ import run_once
        from tracker.crawler import BlockedException

        logger.info("vercel_cron: starting run_once")
        run_once()
        return jsonify(
            {
                "ok": True,
                "status": "completed",
                "duration_ms": int((time.time() - started) * 1000),
                "data_dir": os.environ.get("DATA_DIR"),
            }
        )
    except BlockedException as exc:
        logger.warning("vercel_cron: blocked HTTP {}", exc.status_code)
        return (
            jsonify(
                {
                    "ok": False,
                    "status": "blocked",
                    "status_code": exc.status_code,
                    "duration_ms": int((time.time() - started) * 1000),
                }
            ),
            503,
        )
    except Exception as exc:
        logger.exception("vercel_cron: failed")
        return (
            jsonify(
                {
                    "ok": False,
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc(limit=5),
                    "duration_ms": int((time.time() - started) * 1000),
                }
            ),
            500,
        )
