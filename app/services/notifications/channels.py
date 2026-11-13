"""Concrete notification channels: Email (SMTP + HTML), Telegram, Slack, and Console."""

from __future__ import annotations

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

import requests
from jinja2 import Template

from app.core.config import Settings, get_settings
from app.core.logging_config import get_logger
from app.services.notifications.base import NotificationChannel, NotificationPayload

logger = get_logger("notifications.channels")

ALERT_HTML_TEMPLATE = Template(
    """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; color: #1f2937; margin: 0; padding: 24px; background-color: #f9fafb; }
    .card { max-width: 680px; margin: 0 auto; background: #ffffff; border-radius: 10px; border: 1px solid #e5e7eb; padding: 24px; }
    .header { border-bottom: 2px solid #e5e7eb; padding-bottom: 12px; margin-bottom: 16px; }
    .badge-critical { background: #fee2e2; color: #991b1b; padding: 3px 8px; border-radius: 4px; font-weight: 600; font-size: 12px; }
    .badge-warning { background: #fef3c7; color: #92400e; padding: 3px 8px; border-radius: 4px; font-weight: 600; font-size: 12px; }
    table { width: 100%; border-collapse: collapse; margin-top: 12px; font-size: 14px; }
    th, td { text-align: left; padding: 10px 8px; border-bottom: 1px solid #f3f4f6; }
    th { background-color: #f9fafb; color: #4b5563; font-weight: 600; }
    .footer { margin-top: 20px; font-size: 12px; color: #6b7280; }
  </style>
</head>
<body>
  <div class="card">
    <div class="header">
      <h2 style="margin:0;">{{ subject }}</h2>
    </div>
    <p>{{ intro_text }}</p>
    {% if rows %}
    <table>
      <thead>
        <tr>
          <th>SKU</th>
          <th>Product</th>
          <th>Warehouse</th>
          <th>Type</th>
          <th>Severity</th>
          <th>Details</th>
        </tr>
      </thead>
      <tbody>
        {% for row in rows %}
        <tr>
          <td><strong>{{ row.sku }}</strong></td>
          <td>{{ row.product_name }}</td>
          <td>{{ row.warehouse_code }}</td>
          <td>{{ row.type }}</td>
          <td>
            <span class="{{ 'badge-critical' if row.severity == 'critical' else 'badge-warning' }}">
              {{ row.severity | upper }}
            </span>
          </td>
          <td>{{ row.message }}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
    {% endif %}
    <div class="footer">
      Generated automatically by <strong>Automate Inventory Monitoring with Python</strong>.
    </div>
  </div>
</body>
</html>"""
)


def render_alert_html(
    subject: str,
    intro_text: str,
    rows: list[dict[str, Any]] | None = None,
) -> str:
    """Render an HTML email body using the built-in Jinja2 template."""
    return ALERT_HTML_TEMPLATE.render(
        subject=subject,
        intro_text=intro_text,
        rows=rows or [],
    )


class ConsoleChannel(NotificationChannel):
    """Notification channel that writes formatted alerts to the application log."""

    def __init__(self, settings: Settings | None = None) -> None:
        """Initialize console channel."""
        self.settings = settings or get_settings()

    @property
    def name(self) -> str:
        """Return channel identifier."""
        return "console"

    def is_enabled(self) -> bool:
        """Return True if console notifications are enabled."""
        return self.settings.enable_console_notifications

    def send(self, payload: NotificationPayload) -> None:
        """Log notification details to the configured logger."""
        logger.info(
            "[CONSOLE NOTIFICATION] severity=%s | key=%s | subject=%s | body=%s",
            payload.severity.value,
            payload.alert_key,
            payload.subject,
            payload.text_body,
        )


class EmailChannel(NotificationChannel):
    """SMTP email notification channel supporting plain-text and HTML templates."""

    def __init__(self, settings: Settings | None = None) -> None:
        """Initialize SMTP email channel."""
        self.settings = settings or get_settings()

    @property
    def name(self) -> str:
        """Return channel identifier."""
        return "email"

    def is_enabled(self) -> bool:
        """Return True if email notifications are enabled in settings."""
        return self.settings.enable_email_notifications

    def send(self, payload: NotificationPayload) -> None:
        """Send multipart HTML + text email via SMTP."""
        recipients = self.settings.smtp_to_emails
        if not recipients:
            raise ValueError("No SMTP_TO_EMAILS configured for EmailChannel.")

        msg = MIMEMultipart("alternative")
        msg["Subject"] = payload.subject
        msg["From"] = self.settings.smtp_from_email
        msg["To"] = ", ".join(recipients)

        msg.attach(MIMEText(payload.text_body, "plain", "utf-8"))
        html_content = payload.html_body or render_alert_html(
            subject=payload.subject,
            intro_text=payload.text_body,
            rows=payload.metadata.get("rows"),
        )
        msg.attach(MIMEText(html_content, "html", "utf-8"))

        with smtplib.SMTP(self.settings.smtp_host, self.settings.smtp_port, timeout=10) as server:
            if self.settings.smtp_use_tls:
                server.starttls()
            if self.settings.smtp_username and self.settings.smtp_password:
                server.login(self.settings.smtp_username, self.settings.smtp_password)
            server.sendmail(self.settings.smtp_from_email, recipients, msg.as_string())


class TelegramChannel(NotificationChannel):
    """Telegram Bot notification channel."""

    def __init__(self, settings: Settings | None = None) -> None:
        """Initialize Telegram Bot channel."""
        self.settings = settings or get_settings()

    @property
    def name(self) -> str:
        """Return channel identifier."""
        return "telegram"

    def is_enabled(self) -> bool:
        """Return True if Telegram notifications are enabled."""
        return self.settings.enable_telegram_notifications

    def send(self, payload: NotificationPayload) -> None:
        """Send message via Telegram Bot HTTP API."""
        if not self.settings.telegram_bot_token or not self.settings.telegram_chat_id:
            raise ValueError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be configured.")

        url = f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/sendMessage"
        message_text = f"*{payload.subject}*\n\n{payload.text_body}"
        response = requests.post(
            url,
            json={
                "chat_id": self.settings.telegram_chat_id,
                "text": message_text,
                "parse_mode": "Markdown",
            },
            timeout=10,
        )
        response.raise_for_status()


class SlackChannel(NotificationChannel):
    """Slack Incoming Webhook notification channel."""

    def __init__(self, settings: Settings | None = None) -> None:
        """Initialize Slack webhook channel."""
        self.settings = settings or get_settings()

    @property
    def name(self) -> str:
        """Return channel identifier."""
        return "slack"

    def is_enabled(self) -> bool:
        """Return True if Slack notifications are enabled."""
        return self.settings.enable_slack_notifications

    def send(self, payload: NotificationPayload) -> None:
        """Post formatted message to Slack incoming webhook."""
        if not self.settings.slack_webhook_url:
            raise ValueError("SLACK_WEBHOOK_URL must be configured.")

        icon = ":rotating_light:" if payload.severity.value == "critical" else ":warning:"
        body = {
            "text": f"{icon} *{payload.subject}*\n{payload.text_body}",
        }
        response = requests.post(
            self.settings.slack_webhook_url,
            json=body,
            timeout=10,
        )
        response.raise_for_status()

