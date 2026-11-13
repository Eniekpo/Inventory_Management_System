"""Custom domain exceptions for the Inventory Monitoring System."""

from __future__ import annotations


class InventoryError(Exception):
    """Base exception for all inventory monitoring domain errors."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        """Initialize domain exception with message and HTTP-friendly status code."""
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class EntityNotFoundError(InventoryError):
    """Raised when a requested entity (Product, Warehouse, Alert, etc.) does not exist."""

    def __init__(self, entity: str, identifier: str | int) -> None:
        """Initialize not found error."""
        super().__init__(f"{entity} '{identifier}' not found.", status_code=404)


class DuplicateEntityError(InventoryError):
    """Raised when attempting to create an entity that violates uniqueness constraints."""

    def __init__(self, entity: str, identifier: str | int) -> None:
        """Initialize duplicate entity error."""
        super().__init__(f"{entity} '{identifier}' already exists.", status_code=409)


class ValidationFailedError(InventoryError):
    """Raised when business or input validation fails."""

    def __init__(self, message: str) -> None:
        """Initialize validation error."""
        super().__init__(message, status_code=422)


class AuthenticationError(InventoryError):
    """Raised when API key authentication fails."""

    def __init__(self, message: str = "Invalid or missing API key.") -> None:
        """Initialize authentication error."""
        super().__init__(message, status_code=401)


class RateLimitExceededError(InventoryError):
    """Raised when a client exceeds the configured API rate limit."""

    def __init__(self, message: str = "Rate limit exceeded. Please try again later.") -> None:
        """Initialize rate limit exceeded error."""
        super().__init__(message, status_code=429)

