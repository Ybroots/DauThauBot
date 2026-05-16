from __future__ import annotations

from datetime import datetime, time as dtime, timedelta
from typing import Optional

import pytz
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

from .__main__ import _maybe_alert_block, _setup_logging, run_once
from .config import Secrets
from .crawler import BlockedException
_block_state: dict = {
    "blocked_until": None,
    "consecutive_blocks": 0,
}

TZ = pytz.timezone("Asia/Ho_Chi_Minh")


def _in_quiet_hours(now: datetime, start_str: str, end_str: str) -> bool:
    start = dtime.fromisoformat(start_str)
    end = dtime.fromisoformat(end_str)
    cur = now.time()
    if start <= end:
        return start <= cur < end
    return cur >= start or cur < end


def _in_block_cooldown(now: datetime) -> bool:
    until = _block_state["blocked_until"]
    if until is None:
        return False
    if now < until:
        return True
    _block_state["blocked_until"] = None
    return False


def _trigger_cooldown(secrets: Secrets) -> float:
    _block_state["consecutive_blocks"] += 1
    n = _block_state["consecutive_blocks"]
    hours = min(
        secrets.block_cooldown_hours * (2 ** (n - 1)),
        secrets.block_cooldown_max_hours,
    )
    until = datetime.now(TZ) + timedelta(hours=hours)
    _block_state["blocked_until"] = until
    logger.warning(
        "BLOCK COOLDOWN activated: pause until {} ({:.0f}h, attempt #{})",
        until.isoformat(),
        hours,
        n,
    )
    _maybe_alert_block(secrets, hours)
    return hours


def safe_run() -> None:
    secrets = Secrets()
    now = datetime.now(TZ)

    if _in_quiet_hours(now, secrets.quiet_hours_start, secrets.quiet_hours_end):
        logger.info(
            "Skip run: in quiet hours ({}-{})",
            secrets.quiet_hours_start,
            secrets.quiet_hours_end,
        )
        return

    if _in_block_cooldown(now):
        until = _block_state["blocked_until"]
        logger.info("Skip run: in block cooldown until {}", until.isoformat() if until else "?")
        return

    try:
        run_once()
        _block_state["consecutive_blocks"] = 0
    except BlockedException as e:
        logger.error("Detected block (HTTP {}), entering cooldown", e.status_code)
        _trigger_cooldown(secrets)
    except Exception:
        logger.exception("Unhandled error in run_once, but scheduler continues")


def main(secrets: Optional[Secrets] = None, *, skip_setup_logging: bool = False) -> None:
    s = secrets if secrets is not None else Secrets()
    if not skip_setup_logging:
        _setup_logging(s.log_level)

    scheduler = BlockingScheduler(timezone=TZ)
    scheduler.add_job(
        safe_run,
        IntervalTrigger(
            minutes=s.poll_interval_minutes,
            jitter=s.poll_jitter_seconds,
        ),
        id="crawl_job",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )

    logger.info(
        "Scheduler started: interval={}m ±{}s jitter, quiet={}-{}",
        s.poll_interval_minutes,
        s.poll_jitter_seconds,
        s.quiet_hours_start,
        s.quiet_hours_end,
    )
    safe_run()
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped")


if __name__ == "__main__":
    main()
