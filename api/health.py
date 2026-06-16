from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

os.environ.setdefault("DATA_DIR", "/tmp/dauthau")
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "0")


def _write_json(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        from tracker.crawler import INDEX_URL, site_status

        _write_json(
            self,
            200,
            {
                "ok": True,
                "service": "DauThauBot",
                "runtime": "vercel-python",
                "index_url": INDEX_URL,
                "data_dir": os.environ.get("DATA_DIR"),
                "site_status": site_status(),
            },
        )
