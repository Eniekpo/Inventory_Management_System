"""Application configuration management loaded via python-dotenv and Pydantic v2."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator

# Load .env file if present
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(PROJECT_ROOT / ".env", override=False)


def _env_bool(key: str, default: bool) -> bool:
    """Parse a boolean environment variable safely."""
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class Settings(BaseModel):
    """Typed application settings backed by environment variables."""

    # Application & Logging
    app_name: str = Field(
        default_factory=lambda: os.getenv("APP_NAME", "Automate Inventory Monitoring with Python")
    )
    app_env: str = Field(default_factory=lambda: os.getenv("APP_ENV", "development"))
    log_level: str = Field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))
    log_file_path: str = Field(
        default_factory=lambda: os.getenv("LOG_FILE_PATH", "logs/inventory_monitor.log")
    )
    log_max_bytes: int = Field(
        default_factory=lambda: int(os.getenv("LOG_MAX_BYTES", str(5 * 1024 * 1024)))
    )
    log_backup_count: int = Field(
        default_factory=lambda: int(os.getenv("LOG_BACKUP_COUNT", "5"))
    )

    # Database
    database_url: str = Field(
        default_factory=lambda: os.getenv("DATABASE_URL", "sqlite:///./inventory.db")
    )

    # REST API & Security
    api_host: str = Field(default_factory=lambda: os.getenv("API_HOST", "0.0.0.0"))
    api_port: int = Field(default_factory=lambda: int(os.getenv("API_PORT", "8000")))
    api_key: str = Field(
        default_factory=lambda: os.getenv("API_KEY", "inv-dev-secret-api-key-2025")
    )
    cors_origins_raw: str = Field(
        default_factory=lambda: os.getenv(
            "CORS_ORIGINS",
            "http://localhost:8501,http://127.0.0.1:8501,http://localhost:3000",
        )
    )
    rate_limit_per_minute: int = Field(
        default_factory=lambda: int(os.getenv("RATE_LIMIT_PER_MINUTE", "120"))
    )

    # Default Alert & Monitoring Thresholds
    default_low_stock_threshold: int = Field(
        default_factory=lambda: int(os.getenv("DEFAULT_LOW_STOCK_THRESHOLD", "15"))
    )
    default_overstock_threshold: int = Field(
        default_factory=lambda: int(os.getenv("DEFAULT_OVERSTOCK_THRESHOLD", "500"))
    )
    default_slow_moving_days: int = Field(
        default_factory=lambda: int(os.getenv("DEFAULT_SLOW_MOVING_DAYS", "30"))
    )
    default_forecast_lookback_days: int = Field(
        default_factory=lambda: int(os.getenv("DEFAULT_FORECAST_LOOKBACK_DAYS", "30"))
    )
    default_forecast_warning_days: int = Field(
        default_factory=lambda: int(os.getenv("DEFAULT_FORECAST_WARNING_DAYS", "7"))
    )

    # Automation & Scheduler
    monitor_interval_minutes: int = Field(
        default_factory=lambda: int(os.getenv("MONITOR_INTERVAL_MINUTES", "15"))
    )
    daily_summary_time: str = Field(
        default_factory=lambda: os.getenv("DAILY_SUMMARY_TIME", "08:00")
    )
    watch_folder_path: str = Field(
        default_factory=lambda: os.getenv("WATCH_FOLDER_PATH", "data/watch")
    )
    watch_interval_minutes: int = Field(
        default_factory=lambda: int(os.getenv("WATCH_INTERVAL_MINUTES", "5"))
    )

    # Notification Channels
    enable_console_notifications: bool = Field(
        default_factory=lambda: _env_bool("ENABLE_CONSOLE_NOTIFICATIONS", True)
    )
    enable_email_notifications: bool = Field(
        default_factory=lambda: _env_bool("ENABLE_EMAIL_NOTIFICATIONS", False)
    )
    enable_telegram_notifications: bool = Field(
        default_factory=lambda: _env_bool("ENABLE_TELEGRAM_NOTIFICATIONS", False)
    )
    enable_slack_notifications: bool = Field(
        default_factory=lambda: _env_bool("ENABLE_SLACK_NOTIFICATIONS", False)
    )

    # Notification Throttling, Quiet Hours & Retries
    notification_throttle_minutes: int = Field(
        default_factory=lambda: int(os.getenv("NOTIFICATION_THROTTLE_MINUTES", "60"))
    )
    quiet_hours_enabled: bool = Field(
        default_factory=lambda: _env_bool("QUIET_HOURS_ENABLED", True)
    )
    quiet_hours_start: str = Field(
        default_factory=lambda: os.getenv("QUIET_HOURS_START", "22:00")
    )
    quiet_hours_end: str = Field(
        default_factory=lambda: os.getenv("QUIET_HOURS_END", "07:00")
    )
    quiet_hours_allow_critical: bool = Field(
        default_factory=lambda: _env_bool("QUIET_HOURS_ALLOW_CRITICAL", True)
    )
    max_notification_retries: int = Field(
        default_factory=lambda: int(os.getenv("MAX_NOTIFICATION_RETRIES", "3"))
    )
    notification_retry_base_seconds: float = Field(
        default_factory=lambda: float(os.getenv("NOTIFICATION_RETRY_BASE_SECONDS", "1.0"))
    )

    # SMTP Settings
    smtp_host: str = Field(default_factory=lambda: os.getenv("SMTP_HOST", "smtp.example.com"))
    smtp_port: int = Field(default_factory=lambda: int(os.getenv("SMTP_PORT", "587")))
    smtp_username: str = Field(default_factory=lambda: os.getenv("SMTP_USERNAME", ""))
    smtp_password: str = Field(default_factory=lambda: os.getenv("SMTP_PASSWORD", ""))
    smtp_from_email: str = Field(
        default_factory=lambda: os.getenv(
            "SMTP_FROM_EMAIL", "Inventory Monitor <alerts@example.com>"
        )
    )
    smtp_to_emails_raw: str = Field(
        default_factory=lambda: os.getenv("SMTP_TO_EMAILS", "ops-team@example.com")
    )
    smtp_use_tls: bool = Field(default_factory=lambda: _env_bool("SMTP_USE_TLS", True))

    # Telegram Settings
    telegram_bot_token: str = Field(
        default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", "")
    )
    telegram_chat_id: str = Field(
        default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", "")
    )

    # Slack Settings
    slack_webhook_url: str = Field(
        default_factory=lambda: os.getenv("SLACK_WEBHOOK_URL", "")
    )

    @field_validator("daily_summary_time", "quiet_hours_start", "quiet_hours_end")
    @classmethod
    def validate_hhmm(cls, value: str) -> str:
        """Ensure time strings are in valid HH:MM 24-hour format."""
        parts = value.strip().split(":")
        if len(parts) != 2:
            raise ValueError(f"Expected HH:MM time format, got '{value}'")
        hour, minute = int(parts[0]), int(parts[1])
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError(f"Invalid hour/minute in '{value}'")
        return f"{hour:02d}:{minute:02d}"

    @property
    def cors_origins(self) -> list[str]:
        """Return parsed list of allowed CORS origins."""
        return [o.strip() for o in self.cors_origins_raw.split(",") if o.strip()]

    @property
    def smtp_to_emails(self) -> list[str]:
        """Return parsed list of recipient email addresses."""
        return [e.strip() for e in self.smtp_to_emails_raw.split(",") if e.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached application settings instance."""
    return Settings()

