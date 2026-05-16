#!/usr/bin/env python3
"""Entry Railway — bootstrap đầy đủ; scheduler UTC tại đây (không gọi scheduler.main cũ)."""

from __future__ import annotations

import os
import sys
import threading
import traceback
from pathlib import Path

# v3-inline-scheduler-utc — log phải thấy chuỗi này sau deploy
RUNTIME_REV = "v3-inline-scheduler-utc"

_ROOT = Path(__file__).resolve().parent
_SRC = _ROOT / "src"

os.chdir(_ROOT)
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _configure_logging(level: str) -> None:
    from loguru import logger

    logger.remove()
    use_stdout = bool(os.environ.get("RAILWAY_ENVIRONMENT", "").strip()) or os.environ.get(
        "LOG_TO_STDOUT", ""
    ).lower() in ("1", "true", "yes")
    if use_stdout:
        logger.add(
            sys.stderr,
            level=level.upper(),
            colorize=False,
            format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}",
        )
    else:
        from tracker.__main__ import _setup_logging

        _setup_logging(level)


def _run_scheduler_utc(secrets) -> None:
    """Không import scheduler.main — tránh BlockingScheduler(timezone=TZ) trên image cache cũ."""
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.interval import IntervalTrigger
    from loguru import logger

    from tracker.scheduler import safe_run

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(
        safe_run,
        IntervalTrigger(
            minutes=secrets.poll_interval_minutes,
            jitter=secrets.poll_jitter_seconds,
        ),
        id="crawl_job",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )
    logger.info(
        "Scheduler started: interval={}m ±{}s (tz=UTC), quiet={}-{} (VN) rev={}",
        secrets.poll_interval_minutes,
        secrets.poll_jitter_seconds,
        secrets.quiet_hours_start,
        secrets.quiet_hours_end,
        RUNTIME_REV,
    )
    safe_run()
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped")


def main() -> None:
    from loguru import logger

    from tracker import bot_commands
    from tracker.config import Secrets

    secrets = Secrets()
    _configure_logging(secrets.log_level)

    print(f"[run_railway] {RUNTIME_REV} cwd={os.getcwd()}", file=sys.stderr, flush=True)

    logger.info("railway_main (inline): bot + scheduler rev={}", RUNTIME_REV)
    bot_thread = threading.Thread(target=bot_commands.main, daemon=True, name="telegram_bot")
    bot_thread.start()
    logger.info("Telegram bot thread started (getUpdates long polling)")

    _run_scheduler_utc(secrets)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
