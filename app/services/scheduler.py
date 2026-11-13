"""APScheduler automation service for periodic monitoring checks, folder sync, and daily reports."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app.core.config import Settings, get_settings
from app.core.database import SessionLocal
from app.core.logging_config import get_logger
from app.models.entities import Alert, SystemMetadata, utc_now
from app.services.inventory_service import InventoryService
from app.services.monitoring.engine import MonitoringEngine, MonitoringRunResult
from app.services.notifications.dispatcher import NotificationDispatcher
from app.sources.csv_watch import CSVFolderWatchSource

logger = get_logger("scheduler")

try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    HAS_APSCHEDULER = True
except ImportError:
    HAS_APSCHEDULER = False

    @dataclass
    class IntervalTrigger:  # type: ignore[no-redef]
        """Compatible interval trigger fallback when APScheduler is not installed."""

        minutes: int = 15
        seconds: int = 0

        def next_fire_time(self, now: datetime) -> datetime:
            """Return next fire datetime from now."""
            return now + timedelta(minutes=self.minutes, seconds=self.seconds)

    @dataclass
    class CronTrigger:  # type: ignore[no-redef]
        """Compatible daily cron trigger fallback when APScheduler is not installed."""

        hour: int = 8
        minute: int = 0

        def next_fire_time(self, now: datetime) -> datetime:
            """Return next daily occurrence of hour:minute."""
            candidate = now.replace(hour=self.hour, minute=self.minute, second=0, microsecond=0)
            if candidate <= now:
                candidate += timedelta(days=1)
            return candidate

    @dataclass
    class _ScheduledJob:
        """Internal job descriptor for fallback BackgroundScheduler."""

        id: str
        name: str
        func: Callable[[], Any]
        trigger: IntervalTrigger | CronTrigger
        next_run_time: datetime

    class BackgroundScheduler:  # type: ignore[no-redef]
        """Thread-safe APScheduler-compatible BackgroundScheduler fallback."""

        def __init__(self) -> None:
            """Initialize background scheduler."""
            self._jobs: dict[str, _ScheduledJob] = {}
            self._stop_event = threading.Event()
            self._thread: threading.Thread | None = None
            self.running: bool = False

        def add_job(
            self,
            func: Callable[[], Any],
            trigger: IntervalTrigger | CronTrigger,
            id: str,
            name: str | None = None,
            replace_existing: bool = True,
            **_kwargs: Any,
        ) -> _ScheduledJob:
            """Register a periodic or cron job."""
            if id in self._jobs and not replace_existing:
                raise ValueError(f"Job '{id}' already exists")
            job = _ScheduledJob(
                id=id,
                name=name or id,
                func=func,
                trigger=trigger,
                next_run_time=trigger.next_fire_time(utc_now()),
            )
            self._jobs[id] = job
            return job

        def get_jobs(self) -> list[_ScheduledJob]:
            """Return all registered jobs."""
            return list(self._jobs.values())

        def get_job(self, job_id: str) -> _ScheduledJob | None:
            """Return job by ID or None."""
            return self._jobs.get(job_id)

        def start(self) -> None:
            """Start the background scheduling loop thread."""
            if self.running:
                return
            self._stop_event.clear()
            self.running = True
            self._thread = threading.Thread(
                target=self._run_loop, name="InventorySchedulerThread", daemon=True
            )
            self._thread.start()

        def shutdown(self, wait: bool = False) -> None:
            """Stop the background scheduling loop."""
            self._stop_event.set()
            self.running = False
            if wait and self._thread and self._thread.is_alive():
                self._thread.join(timeout=2.0)

        def _run_loop(self) -> None:
            """Poll scheduled jobs every second until stopped."""
            while not self._stop_event.is_set():
                now = utc_now()
                for job in list(self._jobs.values()):
                    if now >= job.next_run_time:
                        try:
                            job.func()
                        except Exception as exc:
                            logger.error("Scheduled job '%s' failed: %s", job.id, exc)
                        job.next_run_time = job.trigger.next_fire_time(utc_now())
                self._stop_event.wait(1.0)


def run_monitoring_job(session_factory: Any = SessionLocal) -> MonitoringRunResult:
    """Execute a scheduled monitoring check and dispatch notifications for new alerts."""
    with session_factory() as session:
        engine = MonitoringEngine(session)
        result = engine.run_check()

        if result.new_alerts:
            alert_ids = [a.id for a in result.new_alerts if a.id is not None]
            hydrated_alerts = list(
                session.scalars(
                    select(Alert)
                    .options(joinedload(Alert.product), joinedload(Alert.warehouse))
                    .where(Alert.id.in_(alert_ids))
                ).all()
            )
            dispatcher = NotificationDispatcher(session)
            dispatcher.dispatch_alerts(hydrated_alerts)

        return result


def run_daily_summary_job(session_factory: Any = SessionLocal) -> dict[str, str]:
    """Generate and dispatch the daily inventory health summary report."""
    with session_factory() as session:
        service = InventoryService(session)
        summary = service.get_dashboard_summary()
        open_alerts_page = service.list_alerts(active_only=True, page=1, page_size=20)

        alert_rows = [
            {
                "sku": a.sku,
                "product_name": a.product_name,
                "warehouse_code": a.warehouse_code,
                "type": str(a.type),
                "severity": str(a.severity),
                "message": a.message,
            }
            for a in open_alerts_page.items
        ]

        summary_payload = summary.model_dump()
        summary_payload["alert_rows"] = alert_rows

        dispatcher = NotificationDispatcher(session)
        outcomes = dispatcher.dispatch_daily_summary(summary_payload)
        logger.info("Daily summary job completed with outcomes: %s", outcomes)
        return outcomes


def run_folder_watch_sync_job(session_factory: Any = SessionLocal) -> int:
    """Sync dropped CSV files from the configured watch directory."""
    with session_factory() as session:
        source = CSVFolderWatchSource()
        sync_result = source.sync(session)
        if sync_result.files_processed > 0:
            # Immediately run monitoring check after new CSV data is synced
            MonitoringEngine(session).run_check()
        return sync_result.files_processed


class InventorySchedulerService:
    """Manages APScheduler lifecycle for monitoring, CSV folder sync, and daily reports."""

    def __init__(
        self,
        settings: Settings | None = None,
        session_factory: Any = SessionLocal,
    ) -> None:
        """Initialize scheduler service."""
        self.settings = settings or get_settings()
        self.session_factory = session_factory
        self.scheduler = BackgroundScheduler()

    def configure_jobs(self) -> None:
        """Register periodic monitoring, CSV folder-watch, and daily summary jobs."""
        self.scheduler.add_job(
            func=lambda: run_monitoring_job(self.session_factory),
            trigger=IntervalTrigger(minutes=self.settings.monitor_interval_minutes),
            id="monitoring_check_job",
            name=f"Inventory Monitoring Check (every {self.settings.monitor_interval_minutes}m)",
            replace_existing=True,
        )

        hour_str, minute_str = self.settings.daily_summary_time.split(":")
        self.scheduler.add_job(
            func=lambda: run_daily_summary_job(self.session_factory),
            trigger=CronTrigger(hour=int(hour_str), minute=int(minute_str)),
            id="daily_summary_job",
            name=f"Daily Summary Report ({self.settings.daily_summary_time})",
            replace_existing=True,
        )

        self.scheduler.add_job(
            func=lambda: run_folder_watch_sync_job(self.session_factory),
            trigger=IntervalTrigger(minutes=self.settings.watch_interval_minutes),
            id="csv_folder_watch_job",
            name=f"CSV Folder Watch Sync (every {self.settings.watch_interval_minutes}m)",
            replace_existing=True,
        )

    def start(self) -> None:
        """Configure jobs, start the background scheduler, and record RUNNING state."""
        self.configure_jobs()
        self.scheduler.start()
        self._update_status_metadata("RUNNING")
        logger.info(
            "InventorySchedulerService started (monitor=%dm, summary=%s, watch=%dm)",
            self.settings.monitor_interval_minutes,
            self.settings.daily_summary_time,
            self.settings.watch_interval_minutes,
        )

    def stop(self) -> None:
        """Stop the background scheduler and record STOPPED state."""
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        self._update_status_metadata("STOPPED")
        logger.info("InventorySchedulerService stopped.")

    @property
    def is_running(self) -> bool:
        """Return True if the scheduler is actively running."""
        return bool(self.scheduler.running)

    def _update_status_metadata(self, status_value: str) -> None:
        """Persist scheduler operational state in SystemMetadata."""
        try:
            with self.session_factory() as session:
                now = utc_now()
                meta = session.get(SystemMetadata, "scheduler_status")
                if meta is None:
                    session.add(
                        SystemMetadata(
                            key="scheduler_status",
                            value=status_value,
                            updated_at=now,
                        )
                    )
                else:
                    meta.value = status_value
                    meta.updated_at = now
                session.commit()
        except Exception as exc:
            logger.warning("Could not persist scheduler_status metadata: %s", exc)

