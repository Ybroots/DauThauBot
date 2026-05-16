#!/usr/bin/env python3
"""Entry Railway/Nixpacks: thêm src/ vào path rồi chạy tracker (không cần pip install -e)."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from tracker.railway_main import main  # noqa: E402

if __name__ == "__main__":
    main()
