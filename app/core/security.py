"""API key authentication and sliding-window rate limiting utilities."""

from __future__ import annotations

import secrets
import threading
import time
from collections import defaultdict, deque

from fastapi import Header, Request

from app.core.config import get_settings
from app.core.exceptions import AuthenticationError, RateLimitExceededError


class SlidingWindowRateLimiter:
    """Thread-safe in-memory sliding-window rate limiter keyed by client identifier."""

    def __init__(self, max_requests_per_minute: int = 120) -> None:
        """Initialize rate limiter with maximum allowed requests per 60-second window."""
        self.max_requests = max_requests_per_minute
        self._window_seconds = 60.0
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check_or_raise(self, client_key: str) -> None:
        """Record a request for client_key or raise RateLimitExceededError if over limit."""
        if self.max_requests <= 0:
            return
        now = time.monotonic()
        cutoff = now - self._window_seconds

        with self._lock:
            q = self._hits[client_key]
            while q and q[0] < cutoff:
                q.popleft()
            if len(q) >= self.max_requests:
                raise RateLimitExceededError(
                    f"Rate limit of {self.max_requests} requests/minute exceeded."
                )
            q.append(now)

    def reset(self) -> None:
        """Clear all recorded rate-limit counters (useful in test suites)."""
        with self._lock:
            self._hits.clear()


rate_limiter = SlidingWindowRateLimiter(
    max_requests_per_minute=get_settings().rate_limit_per_minute
)


def verify_api_key(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> str:
    """Validate the X-API-Key request header and enforce rate limiting."""
    settings = get_settings()
    if not x_api_key or not secrets.compare_digest(x_api_key, settings.api_key):
        raise AuthenticationError("Invalid or missing X-API-Key header.")

    client_host = request.client.host if request.client else "local"
    rate_limiter.check_or_raise(f"{client_host}:{x_api_key[:8]}")
    return x_api_key

