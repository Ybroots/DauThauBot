#!/usr/bin/env python3
"""Entry Railway: luôn chạy từ thư mục repo, thêm src/ vào PYTHONPATH."""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_SRC = _ROOT / "src"

os.chdir(_ROOT)
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _preflight() -> None:
    """In chẩn đoán ra stderr (Railway Logs) trước khi import tracker."""
    print(f"[run_railway] cwd={os.getcwd()}", file=sys.stderr, flush=True)
    print(f"[run_railway] python={sys.executable}", file=sys.stderr, flush=True)
    print(f"[run_railway] src_exists={_SRC.is_dir()}", file=sys.stderr, flush=True)
    tracker_pkg = _SRC / "tracker"
    print(f"[run_railway] tracker_pkg={tracker_pkg.is_dir()}", file=sys.stderr, flush=True)
    for mod in ("httpx", "pydantic", "pydantic_settings", "yaml", "apscheduler", "loguru"):
        try:
            __import__(mod)
        except ImportError as e:
            print(f"[run_railway] THIEU package: {mod} — {e}", file=sys.stderr, flush=True)
            print(
                "[run_railway] Build phai cai requirements.txt hoac dung Dockerfile.",
                file=sys.stderr,
                flush=True,
            )
            sys.exit(1)


if __name__ == "__main__":
    try:
        _preflight()
        from tracker.railway_main import main  # noqa: E402

        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
