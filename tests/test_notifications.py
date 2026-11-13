"""Unit tests for notification channels, throttling, quiet hours, retry backoff, and folder watch."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.entities import NotificationLog
from app.models.enums import AlertSeverity
from app.services.notifications.base import NotificationChannel, NotificationPayload
from app.services.notifications.channels import (
    EmailChannel,
    SlackChannel,
    TelegramChannel,
)
from app.services.notifications.dispatcher import (
    NotificationDispatcher,
    is_within_quiet_hours,
)
from app.sources.csv_watch import CSVFolderWatchSource


class _FlakyChannel(NotificationChannel):
    """Test double that fails a configurable number of times before succeeding."""

    def __init__(self, fail_times: int = 2) -> None:
        self.fail_times = fail_times
        self.calls = 0

    @property
    def name(self) -> str:
        return "flaky_test"

    def is_enabled(self) -> bool:
        return True

    def send(self, payload: NotificationPayload) -> None:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError(f"Simulated network timeout #{self.calls}")


class TestQuietHoursAndThrottling:
    """Tests for quiet hours calculation, throttling cooldown, and retry backoff."""

    def test_is_within_quiet_hours_overnight_and_daytime(self) -> None:
        # Overnight window 22:00 -> 07:00
        assert is_within_quiet_hours(datetime(2026, 6, 1, 23, 30), "22:00", "07:00") is True
        assert is_within_quiet_hours(datetime(2026, 6, 1, 2, 15), "22:00", "07:00") is True
        assert is_within_quiet_hours(datetime(2026, 6, 1, 14, 0), "22:00", "07:00") is False

        # Same-day window 01:00 -> 05:00
        assert is_within_quiet_hours(datetime(2026, 6, 1, 3, 0), "01:00", "05:00") is True
        assert is_within_quiet_hours(datetime(2026, 6, 1, 6, 0), "01:00", "05:00") is False

    def test_throttling_prevents_duplicate_sends(self, db_session: Session) -> None:
        settings = Settings(
            enable_console_notifications=True,
            enable_email_notifications=False,
            enable_telegram_notifications=False,
            enable_slack_notifications=False,
            notification_throttle_minutes=60,
            quiet_hours_enabled=False,
        )
        dispatcher = NotificationDispatcher(db_session, settings=settings)
        t0 = datetime(2026, 6, 10, 12, 0, 0)
        payload = NotificationPayload(
            alert_key="alert:1:1:LOW_STOCK",
            subject="Low Stock SKU-001",
            text_body="Stock is low.",
            severity=AlertSeverity.WARNING,
        )

        first = dispatcher.send_payload(payload, reference_time=t0)
        assert first["console"] == "SENT"

        # 15 minutes later -> throttled
        second = dispatcher.send_payload(payload, reference_time=t0 + timedelta(minutes=15))
        assert second["console"] == "THROTTLED"

        # 65 minutes later -> allowed again
        third = dispatcher.send_payload(payload, reference_time=t0 + timedelta(minutes=65))
        assert third["console"] == "SENT"

    def test_quiet_hours_suppresses_warning_but_allows_critical(
        self,
        db_session: Session,
    ) -> None:
        settings = Settings(
            enable_console_notifications=True,
            quiet_hours_enabled=True,
            quiet_hours_start="22:00",
            quiet_hours_end="07:00",
            quiet_hours_allow_critical=True,
        )
        dispatcher = NotificationDispatcher(db_session, settings=settings)
        night_time = datetime(2026, 6, 10, 23, 15, 0)

        warn_payload = NotificationPayload(
            alert_key="alert:warn:1",
            subject="Warning Alert",
            text_body="Non-critical warning",
            severity=AlertSeverity.WARNING,
        )
        crit_payload = NotificationPayload(
            alert_key="alert:crit:1",
            subject="Critical Alert",
            text_body="Out of stock critical",
            severity=AlertSeverity.CRITICAL,
        )

        out_warn = dispatcher.send_payload(warn_payload, reference_time=night_time)
        out_crit = dispatcher.send_payload(crit_payload, reference_time=night_time)

        assert out_warn["console"] == "QUIET_HOURS_SUPPRESSED"
        assert out_crit["console"] == "SENT"

    def test_retry_with_exponential_backoff_succeeds_on_third_attempt(
        self,
        db_session: Session,
    ) -> None:
        settings = Settings(
            quiet_hours_enabled=False,
            max_notification_retries=3,
            notification_retry_base_seconds=0.5,
        )
        flaky = _FlakyChannel(fail_times=2)
        sleeps: list[float] = []
        dispatcher = NotificationDispatcher(
            db_session,
            settings=settings,
            channels=[flaky],
            sleep_fn=lambda s: sleeps.append(s),
        )
        payload = NotificationPayload(
            alert_key="alert:retry:1",
            subject="Retry Test",
            text_body="Testing retry backoff",
            severity=AlertSeverity.CRITICAL,
        )

        outcomes = dispatcher.send_payload(payload, reference_time=datetime(2026, 6, 10, 12, 0))
        assert outcomes["flaky_test"] == "SENT"
        assert flaky.calls == 3
        assert sleeps == [0.5, 1.0]

        log_row = db_session.scalar(
            select(NotificationLog).where(NotificationLog.alert_key == "alert:retry:1")
        )
        assert log_row is not None
        assert log_row.attempts == 3
        assert log_row.status == "SENT"


class TestExternalChannelsAndFolderWatch:
    """Tests for Email, Telegram, Slack channels (mocked I/O) and CSVFolderWatchSource."""

    def test_email_telegram_slack_channels_with_mocks(self) -> None:
        settings = Settings(
            enable_email_notifications=True,
            smtp_host="smtp.test.local",
            smtp_port=587,
            smtp_username="user",
            smtp_password="secret",
            smtp_to_emails_raw="ops@test.local",
            enable_telegram_notifications=True,
            telegram_bot_token="123:ABC",
            telegram_chat_id="999",
            enable_slack_notifications=True,
            slack_webhook_url="https://hooks.slack.com/services/T1/B1/X1",
        )
        payload = NotificationPayload(
            alert_key="alert:ext:1",
            subject="External Channel Test",
            text_body="Testing external channels",
            severity=AlertSeverity.CRITICAL,
        )

        with patch("smtplib.SMTP") as mock_smtp:
            smtp_inst = MagicMock()
            mock_smtp.return_value.__enter__.return_value = smtp_inst
            EmailChannel(settings).send(payload)
            assert smtp_inst.sendmail.called

        with patch("requests.post") as mock_post:
            mock_post.return_value.raise_for_status.return_value = None
            TelegramChannel(settings).send(payload)
            SlackChannel(settings).send(payload)
            assert mock_post.call_count == 2

    def test_csv_folder_watch_source_syncs_and_archives(
        self,
        db_session: Session,
        sample_entities: dict[str, object],
        tmp_path: Path,
    ) -> None:
        watch_dir = tmp_path / "watch"
        watch_dir.mkdir()

        prod_file = watch_dir / "products_drop.csv"
        prod_file.write_text(
            "sku,name,category,unit_cost,reorder_point,reorder_quantity,supplier\n"
            "SKU-WATCH-1,Watched Valve,Industrial Hardware,32.00,10,30,PrimeLine Industrial\n",
            encoding="utf-8",
        )
        stock_file = watch_dir / "stock_drop.csv"
        stock_file.write_text(
            "sku,warehouse_code,quantity_on_hand,quantity_reserved\n"
            "SKU-WATCH-1,WH-EAST,45,5\n",
            encoding="utf-8",
        )

        source = CSVFolderWatchSource(watch_dir=watch_dir)
        sync_res = source.sync(db_session)

        assert sync_res.files_processed == 2
        assert sync_res.products_created == 1
        assert sync_res.stock_levels_created == 1
        assert len(list((watch_dir / "processed").glob("*.csv"))) == 2

    def test_inventory_scheduler_service_lifecycle(self) -> None:
        from app.services.scheduler import InventorySchedulerService

        sched_svc = InventorySchedulerService()
        sched_svc.start()
        assert sched_svc.is_running is True
        jobs = sched_svc.scheduler.get_jobs()
        job_ids = {j.id for j in jobs}
        assert "monitoring_check_job" in job_ids
        assert "daily_summary_job" in job_ids
        assert "csv_folder_watch_job" in job_ids
        sched_svc.stop()
        assert sched_svc.is_running is False

    def test_dispatch_alerts_and_daily_summary(
        self,
        db_session: Session,
        sample_entities: dict[str, object],
    ) -> None:
        from app.services.monitoring.engine import MonitoringEngine

        settings = Settings(
            enable_console_notifications=True,
            quiet_hours_enabled=False,
        )
        engine = MonitoringEngine(db_session)
        run_res = engine.run_check()
        dispatcher = NotificationDispatcher(db_session, settings=settings)

        alert_outcomes = dispatcher.dispatch_alerts(run_res.new_alerts)
        assert len(alert_outcomes) == len(run_res.new_alerts)

        summary_outcomes = dispatcher.dispatch_daily_summary(
            {
                "total_products": 2,
                "total_stock_units": 50,
                "total_stock_value": 1937.5,
                "out_of_stock_items": 0,
                "low_stock_items": 1,
                "active_alerts_count": len(run_res.new_alerts),
                "alert_rows": [],
            }
        )
        assert summary_outcomes["console"] == "SENT"

