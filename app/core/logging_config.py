"""Centralized logging configuration with rotating file handler and secret scrubbing."""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.core.config import PROJECT_ROOT, get_settings

_SENSITIVE_PATTERNS = [
    re.compile(r"(api[_-]?key\s*[=:]\s*)([^\s,;\"']+)", re.IGNORECASE),
    re.compile(r"(password\s*[=:]\s*)([^\s,;\"']+)", re.IGNORECASE),
    re.compile(r"(token\s*[=:]\s*)([^\s,;\"']+)", re.IGNORECASE),
    re.compile(r"(https://hooks\.slack\.com/services/)([^\s\"']+)", re.IGNORECASE),
]


class SecretScrubbingFilter(logging.Filter):
    """Logging filter that masks sensitive credentials, tokens, and API keys."""

    def __init__(self, explicit_secrets: list[str] | None = None) -> None:
        """Initialize filter with optional explicit secret strings to scrub."""
        super().__init__()
        self._secrets = [s for s in (explicit_secrets or []) if s and len(s) >= 4]

    def filter(self, record: logging.LogRecord) -> bool:
        """Scrub secrets from the log record message and arguments."""
        msg = record.getMessage()
        scrubbed = self._scrub_text(msg)
        if scrubbed != msg:
            record.msg = scrubbed
            record.args = ()
        return True

    def _scrub_text(self, text: str) -> str:
        """Replace explicit secret values and regex matches with [REDACTED]."""
        result = text
        for secret in self._secrets:
            if secret in result:
                result = result.replace(secret, "[REDACTED]")
        for pattern in _SENSITIVE_PATTERNS:
            result = pattern.sub(r"\1[REDACTED]", result)
        return result


_LOGGING_CONFIGURED = False


def configure_logging() -> logging.Logger:
    """Configure root and application loggers with console and rotating file handlers."""
    global _LOGGING_CONFIGURED
    settings = get_settings()
    logger = logging.getLogger("inventory_monitor")

    if _LOGGING_CONFIGURED:
        return logger

    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logger.setLevel(level)
    logger.propagate = False

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s:%(lineno)d | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    secret_filter = SecretScrubbingFilter(
        explicit_secrets=[
            settings.api_key,
            settings.smtp_password,
            settings.telegram_bot_token,
            settings.slack_webhook_url,
        ]
    )

    # Console Handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    console_handler.addFilter(secret_filter)
    logger.addHandler(console_handler)

    # Rotating File Handler
    log_path = Path(settings.log_file_path)
    if not log_path.is_absolute():
        log_path = PROJECT_ROOT / log_path
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            filename=str(log_path),
            maxBytes=settings.log_max_bytes,
            backupCount=settings.log_backup_count,
            encoding="utf-8",
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        file_handler.addFilter(secret_filter)
        logger.addHandler(file_handler)
    except OSError:
        logger.warning("Could not initialize file logger at %s; using console only.", log_path)

    _LOGGING_CONFIGURED = True
    return logger


def get_logger(name: str = "inventory_monitor") -> logging.Logger:
    """Return a configured child logger."""
    configure_logging()
    if name == "inventory_monitor" or name.startswith("inventory_monitor."):
        return logging.getLogger(name)
    return logging.getLogger(f"inventory_monitor.{name}")

