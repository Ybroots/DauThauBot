"""Một process: scheduler (blocking) + Telegram bot (daemon thread) — Railway / VPS."""

from __future__ import annotations

import os
import sys
import threading

from loguru import logger

from . import bot_commands
from .config import Secrets

# Đổi khi deploy quan trọng — log giúp biết Railway đã chạy image mới
DEPLOY_REV = "20260516-scheduler-utc-v2"


def _use_stdout_logging() -> bool:
    return bool(os.environ.get("RAILWAY_ENVIRONMENT", "").strip()) or os.environ.get(
        "LOG_TO_STDOUT", ""
    ).lower() in ("1", "true", "yes")


def _configure_stdout_logging(level: str) -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        level=level.upper(),
        colorize=False,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}",
    )


def main() -> None:
    secrets = Secrets()
    use_stdout = _use_stdout_logging()
    if use_stdout:
        _configure_stdout_logging(secrets.log_level)
    else:
        from .__main__ import _setup_logging

        _setup_logging(secrets.log_level)

    logger.info("railway_main: scheduler + Telegram bot (1 process) rev={}", DEPLOY_REV)
    bot_thread = threading.Thread(target=bot_commands.main, daemon=True, name="telegram_bot")
    bot_thread.start()
    logger.info("Telegram bot thread started (getUpdates long polling)")

    # UTC trực tiếp — không gọi scheduler.main (tránh timezone=TZ / zoneinfo trên Docker)
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.interval import IntervalTrigger

    from .scheduler import safe_run

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
        "Scheduler started: interval={}m ±{}s (tz=UTC), quiet={}-{} (VN)",
        secrets.poll_interval_minutes,
        secrets.poll_jitter_seconds,
        secrets.quiet_hours_start,
        secrets.quiet_hours_end,
    )
    safe_run()
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped")


if __name__ == "__main__":
    main()
