"""Abstract base interface and payload definitions for notification channels."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from app.models.enums import AlertSeverity


@dataclass(frozen=True)
class NotificationPayload:
    """Standardized notification payload passed to all notification channels."""

    alert_key: str
    subject: str
    text_body: str
    html_body: str | None = None
    severity: AlertSeverity = AlertSeverity.WARNING
    metadata: dict[str, Any] = field(default_factory=dict)


class NotificationChannel(ABC):
    """Common interface implemented by Email, Telegram, Slack, and Console channels."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique identifier for the channel (e.g., 'email', 'telegram', 'slack', 'console')."""

    @abstractmethod
    def is_enabled(self) -> bool:
        """Return True if this channel is enabled in configuration."""

    @abstractmethod
    def send(self, payload: NotificationPayload) -> None:
        """Deliver a notification payload or raise an exception on failure."""

