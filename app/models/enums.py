"""Domain enumerations for alert types, severities, stock statuses, and movement reasons."""

from __future__ import annotations

from enum import StrEnum


class AlertType(StrEnum):
    """Types of inventory anomalies and threshold breaches detected by the engine."""

    OUT_OF_STOCK = "OUT_OF_STOCK"
    LOW_STOCK = "LOW_STOCK"
    NEGATIVE_STOCK = "NEGATIVE_STOCK"
    OVERSTOCK = "OVERSTOCK"
    SLOW_MOVING = "SLOW_MOVING"
    STOCKOUT_FORECAST = "STOCKOUT_FORECAST"


class AlertSeverity(StrEnum):
    """Severity classification for inventory alerts."""

    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


class StockStatus(StrEnum):
    """Computed operational status of a product's stock at a warehouse."""

    HEALTHY = "HEALTHY"
    LOW_STOCK = "LOW_STOCK"
    OUT_OF_STOCK = "OUT_OF_STOCK"
    OVERSTOCK = "OVERSTOCK"
    ANOMALY = "ANOMALY"


class MovementReason(StrEnum):
    """Standardized reasons for stock level changes."""

    SALE = "SALE"
    RESTOCK = "RESTOCK"
    ADJUSTMENT = "ADJUSTMENT"
    RETURN = "RETURN"
    DAMAGE = "DAMAGE"
    TRANSFER = "TRANSFER"
    INITIAL_SEED = "INITIAL_SEED"
    CSV_IMPORT = "CSV_IMPORT"

