"""Một process: scheduler (blocking) + Telegram bot (daemon thread) — Railway / VPS."""

from __future__ import annotations

import os
import sys
import threading

from loguru import logger

from . import bot_commands
from . import scheduler as sched_module
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

    sched_tz = getattr(sched_module, "SCHEDULER_TZ", None)
    if sched_tz != "UTC":
        raise RuntimeError(
            f"scheduler.py cũ (SCHEDULER_TZ={sched_tz!r}). Redeploy với Clear build cache."
        )

    logger.info("railway_main: scheduler + Telegram bot (1 process) rev={}", DEPLOY_REV)
    bot_thread = threading.Thread(target=bot_commands.main, daemon=True, name="telegram_bot")
    bot_thread.start()
    logger.info("Telegram bot thread started (getUpdates long polling)")

    sched_module.main(secrets=secrets, skip_setup_logging=use_stdout)


if __name__ == "__main__":
    main()
