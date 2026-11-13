"""Standalone daemon script for running the APScheduler inventory monitoring service."""

from __future__ import annotations

import argparse
import signal
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.database import init_db
from app.core.logging_config import get_logger
from app.services.scheduler import (
    InventorySchedulerService,
    run_daily_summary_job,
    run_folder_watch_sync_job,
    run_monitoring_job,
)

logger = get_logger("run_scheduler")


def main() -> None:
    """Entrypoint for starting the standalone scheduler service or running a single cycle."""
    parser = argparse.ArgumentParser(description="Run Inventory Monitoring Scheduler.")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run folder sync, monitoring check, and daily summary once, then exit.",
    )
    args = parser.parse_args()

    init_db()

    if args.once:
        run_folder_watch_sync_job()
        result = run_monitoring_job()
        summary_outcomes = run_daily_summary_job()
        print(
            f"Single cycle completed: evaluated={result.evaluated_count}, "
            f"new_alerts={result.new_alerts_created}, "
            f"active_total={result.active_alerts_total}, "
            f"summary={summary_outcomes}"
        )
        return

    scheduler_service = InventorySchedulerService()
    scheduler_service.start()
    # Run an immediate initial check on startup
    run_folder_watch_sync_job()
    run_monitoring_job()

    stop_requested = False

    def _handle_sig(_signum: int, _frame: object) -> None:
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGINT, _handle_sig)
    signal.signal(signal.SIGTERM, _handle_sig)

    try:
        while not stop_requested:
            time.sleep(1.0)
    finally:
        scheduler_service.stop()


if __name__ == "__main__":
    main()

