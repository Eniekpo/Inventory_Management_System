"""Notification dispatcher with throttling, quiet hours, exponential backoff retry, and audit logging."""

from __future__ import annotations

import time
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.logging_config import get_logger
from app.models.entities import Alert, NotificationLog, utc_now
from app.models.enums import AlertSeverity
from app.services.notifications.base import NotificationChannel, NotificationPayload
from app.services.notifications.channels import (
    ConsoleChannel,
    EmailChannel,
    SlackChannel,
    TelegramChannel,
    render_alert_html,
)

logger = get_logger("notifications.dispatcher")


def is_within_quiet_hours(
    current_time: datetime,
    quiet_start_hhmm: str,
    quiet_end_hhmm: str,
) -> bool:
    """Return True if current_time falls within the [quiet_start_hhmm, quiet_end_hhmm) window.

    Handles both same-day windows (e.g. 01:00 to 06:00) and overnight windows
    spanning midnight (e.g. 22:00 to 07:00).
    """
    sh, sm = (int(x) for x in quiet_start_hhmm.split(":"))
    eh, em = (int(x) for x in quiet_end_hhmm.split(":"))

    current_minutes = current_time.hour * 60 + current_time.minute
    start_minutes = sh * 60 + sm
    end_minutes = eh * 60 + em

    if start_minutes == end_minutes:
        return False
    if start_minutes < end_minutes:
        return start_minutes <= current_minutes < end_minutes
    # Overnight window (e.g. 22:00 -> 07:00)
    return current_minutes >= start_minutes or current_minutes < end_minutes


class NotificationDispatcher:
    """Orchestrates sending notifications across enabled channels with resilience policies."""

    def __init__(
        self,
        session: Session,
        settings: Settings | None = None,
        channels: Sequence[NotificationChannel] | None = None,
        sleep_fn: Any = time.sleep,
    ) -> None:
        """Initialize dispatcher with database session, settings, and channel implementations."""
        self.session = session
        self.settings = settings or get_settings()
        self.sleep_fn = sleep_fn
        if channels is not None:
            self.channels = list(channels)
        else:
            self.channels = [
                ConsoleChannel(self.settings),
                EmailChannel(self.settings),
                TelegramChannel(self.settings),
                SlackChannel(self.settings),
            ]

    def _is_throttled(
        self,
        channel_name: str,
        alert_key: str,
        reference_time: datetime,
    ) -> bool:
        """Check if a notification with the same channel and alert_key was sent recently."""
        if self.settings.notification_throttle_minutes <= 0:
            return False

        cutoff = reference_time - timedelta(minutes=self.settings.notification_throttle_minutes)
        recent = self.session.scalar(
            select(NotificationLog.id)
            .where(
                NotificationLog.channel == channel_name,
                NotificationLog.alert_key == alert_key,
                NotificationLog.status == "SENT",
                NotificationLog.created_at >= cutoff,
            )
            .limit(1)
        )
        return recent is not None

    def _should_suppress_for_quiet_hours(
        self,
        severity: AlertSeverity,
        reference_time: datetime,
    ) -> bool:
        """Determine whether a notification should be suppressed due to quiet hours."""
        if not self.settings.quiet_hours_enabled:
            return False
        if (
            severity == AlertSeverity.CRITICAL
            and self.settings.quiet_hours_allow_critical
        ):
            return False
        return is_within_quiet_hours(
            current_time=reference_time,
            quiet_start_hhmm=self.settings.quiet_hours_start,
            quiet_end_hhmm=self.settings.quiet_hours_end,
        )

    def send_payload(
        self,
        payload: NotificationPayload,
        reference_time: datetime | None = None,
    ) -> dict[str, str]:
        """Deliver a NotificationPayload across all enabled channels.

        Applies quiet-hours filtering, per-key throttling, exponential backoff retry,
        and records every outcome in NotificationLog.

        Returns:
            Mapping of {channel_name: status_string}.
        """
        now = reference_time or utc_now()
        outcomes: dict[str, str] = {}

        for channel in self.channels:
            if not channel.is_enabled():
                continue

            # 1. Quiet hours check
            if self._should_suppress_for_quiet_hours(payload.severity, now):
                logger.info(
                    "Notification suppressed during quiet hours: channel=%s key=%s severity=%s",
                    channel.name,
                    payload.alert_key,
                    payload.severity.value,
                )
                self.session.add(
                    NotificationLog(
                        channel=channel.name,
                        alert_key=payload.alert_key,
                        subject=payload.subject,
                        status="QUIET_HOURS_SUPPRESSED",
                        attempts=0,
                        error_message="Suppressed during configured quiet hours.",
                        created_at=now,
                    )
                )
                outcomes[channel.name] = "QUIET_HOURS_SUPPRESSED"
                continue

            # 2. Throttling check
            if self._is_throttled(channel.name, payload.alert_key, now):
                logger.info(
                    "Notification throttled (within %d min cooldown): channel=%s key=%s",
                    self.settings.notification_throttle_minutes,
                    channel.name,
                    payload.alert_key,
                )
                self.session.add(
                    NotificationLog(
                        channel=channel.name,
                        alert_key=payload.alert_key,
                        subject=payload.subject,
                        status="THROTTLED",
                        attempts=0,
                        error_message=(
                            f"Throttled within {self.settings.notification_throttle_minutes}m window."
                        ),
                        created_at=now,
                    )
                )
                outcomes[channel.name] = "THROTTLED"
                continue

            # 3. Send with exponential backoff retry
            max_attempts = max(1, self.settings.max_notification_retries)
            base_delay = max(0.0, self.settings.notification_retry_base_seconds)
            last_error: str | None = None
            sent_ok = False
            attempts_used = 0

            for attempt in range(1, max_attempts + 1):
                attempts_used = attempt
                try:
                    logger.info(
                        "Sending notification attempt %d/%d via channel=%s key=%s",
                        attempt,
                        max_attempts,
                        channel.name,
                        payload.alert_key,
                    )
                    channel.send(payload)
                    sent_ok = True
                    last_error = None
                    break
                except Exception as exc:
                    last_error = str(exc)
                    logger.warning(
                        "Notification send attempt %d/%d failed on channel=%s key=%s: %s",
                        attempt,
                        max_attempts,
                        channel.name,
                        payload.alert_key,
                        last_error,
                    )
                    if attempt < max_attempts and base_delay > 0:
                        backoff = base_delay * (2 ** (attempt - 1))
                        self.sleep_fn(backoff)

            status = "SENT" if sent_ok else "FAILED"
            self.session.add(
                NotificationLog(
                    channel=channel.name,
                    alert_key=payload.alert_key,
                    subject=payload.subject,
                    status=status,
                    attempts=attempts_used,
                    error_message=last_error,
                    created_at=now,
                )
            )
            outcomes[channel.name] = status

        self.session.commit()
        return outcomes

    def dispatch_alerts(
        self,
        alerts: Sequence[Alert],
        reference_time: datetime | None = None,
    ) -> list[dict[str, str]]:
        """Dispatch notifications for a list of newly generated Alert entities."""
        results: list[dict[str, str]] = []
        for alert in alerts:
            sku = alert.product.sku if alert.product else f"PROD-{alert.product_id}"
            prod_name = alert.product.name if alert.product else f"Product #{alert.product_id}"
            wh_code = alert.warehouse.code if alert.warehouse else f"WH-{alert.warehouse_id}"
            sev_str = str(alert.severity).lower()
            severity_enum = (
                AlertSeverity.CRITICAL
                if sev_str == AlertSeverity.CRITICAL.value
                else (
                    AlertSeverity.INFO
                    if sev_str == AlertSeverity.INFO.value
                    else AlertSeverity.WARNING
                )
            )

            alert_key = f"alert:{alert.product_id}:{alert.warehouse_id}:{alert.type}"
            subject = f"[{sev_str.upper()}] {alert.type} - {sku} @ {wh_code}"
            row_dict = {
                "sku": sku,
                "product_name": prod_name,
                "warehouse_code": wh_code,
                "type": str(alert.type),
                "severity": sev_str,
                "message": alert.message,
            }
            html_body = render_alert_html(
                subject=subject,
                intro_text=alert.message,
                rows=[row_dict],
            )
            payload = NotificationPayload(
                alert_key=alert_key,
                subject=subject,
                text_body=alert.message,
                html_body=html_body,
                severity=severity_enum,
                metadata={"rows": [row_dict], "alert_id": alert.id},
            )
            outcomes = self.send_payload(payload, reference_time=reference_time)
            results.append(outcomes)
        return results

    def dispatch_daily_summary(
        self,
        summary_data: dict[str, Any],
        reference_time: datetime | None = None,
    ) -> dict[str, str]:
        """Dispatch a daily inventory health summary report across enabled channels."""
        now = reference_time or utc_now()
        date_str = now.strftime("%Y-%m-%d")
        subject = f"Daily Inventory Health Summary ({date_str})"
        text_body = (
            f"Daily Inventory Summary for {date_str}:\n"
            f"- Total Products: {summary_data.get('total_products', 0)}\n"
            f"- Total Stock Units: {summary_data.get('total_stock_units', 0):,}\n"
            f"- Total Stock Value: ${summary_data.get('total_stock_value', 0.0):,.2f}\n"
            f"- Out of Stock Items: {summary_data.get('out_of_stock_items', 0)}\n"
            f"- Low Stock Items: {summary_data.get('low_stock_items', 0)}\n"
            f"- Open Active Alerts: {summary_data.get('active_alerts_count', 0)}"
        )
        html_body = render_alert_html(
            subject=subject,
            intro_text=text_body.replace("\n", "<br>"),
            rows=summary_data.get("alert_rows", []),
        )
        payload = NotificationPayload(
            alert_key=f"daily_summary:{date_str}",
            subject=subject,
            text_body=text_body,
            html_body=html_body,
            severity=AlertSeverity.INFO,
            metadata=summary_data,
        )
        return self.send_payload(payload, reference_time=now)

