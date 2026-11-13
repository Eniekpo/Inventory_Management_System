"""Notification channel abstractions, concrete providers, and resilient dispatcher."""

from app.services.notifications.base import NotificationChannel, NotificationPayload
from app.services.notifications.channels import (
    ConsoleChannel,
    EmailChannel,
    SlackChannel,
    TelegramChannel,
    render_alert_html,
)
from app.services.notifications.dispatcher import (
    NotificationDispatcher,
    is_within_quiet_hours,
)

__all__ = [
    "ConsoleChannel",
    "EmailChannel",
    "NotificationChannel",
    "NotificationDispatcher",
    "NotificationPayload",
    "SlackChannel",
    "TelegramChannel",
    "is_within_quiet_hours",
    "render_alert_html",
]

