"""Pure, framework-independent inventory monitoring rules and threshold evaluators."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.models.enums import AlertSeverity, AlertType, StockStatus
from app.services.monitoring.forecast import estimate_days_of_stock_remaining


@dataclass(frozen=True)
class ThresholdConfig:
    """Effective threshold configuration for evaluating a product's stock state."""

    low_stock_threshold: int
    overstock_threshold: int = 500
    slow_moving_days: int = 30
    forecast_lookback_days: int = 30
    forecast_warning_days: int = 7


@dataclass(frozen=True)
class StockSnapshot:
    """Framework-independent snapshot of a product's inventory at a single warehouse."""

    product_id: int
    sku: str
    product_name: str
    category: str
    supplier: str
    unit_cost: float
    reorder_point: int
    reorder_quantity: int
    warehouse_id: int
    warehouse_code: str
    warehouse_name: str
    quantity_on_hand: int
    quantity_reserved: int
    last_movement_at: datetime | None = None
    avg_daily_consumption: float = 0.0

    @property
    def available(self) -> int:
        """Net available stock (quantity_on_hand - quantity_reserved)."""
        return int(self.quantity_on_hand) - int(self.quantity_reserved)

    @property
    def has_data_anomaly(self) -> bool:
        """Return True if stock quantities violate physical integrity invariants."""
        return (
            self.quantity_on_hand < 0
            or self.quantity_reserved < 0
            or self.quantity_reserved > self.quantity_on_hand
        )


@dataclass(frozen=True)
class AlertCandidate:
    """A detected alert candidate produced by a monitoring rule."""

    product_id: int
    warehouse_id: int
    sku: str
    product_name: str
    warehouse_code: str
    type: AlertType
    severity: AlertSeverity
    message: str


def check_negative_stock(snapshot: StockSnapshot) -> AlertCandidate | None:
    """Detect negative stock or reservation data-quality anomalies.

    Triggers when quantity_on_hand < 0, quantity_reserved < 0, or
    quantity_reserved > quantity_on_hand.
    """
    reasons: list[str] = []
    if snapshot.quantity_on_hand < 0:
        reasons.append(f"quantity_on_hand ({snapshot.quantity_on_hand}) is negative")
    if snapshot.quantity_reserved < 0:
        reasons.append(f"quantity_reserved ({snapshot.quantity_reserved}) is negative")
    if snapshot.quantity_reserved > snapshot.quantity_on_hand:
        reasons.append(
            f"quantity_reserved ({snapshot.quantity_reserved}) exceeds "
            f"quantity_on_hand ({snapshot.quantity_on_hand})"
        )

    if not reasons:
        return None

    detail = "; ".join(reasons)
    return AlertCandidate(
        product_id=snapshot.product_id,
        warehouse_id=snapshot.warehouse_id,
        sku=snapshot.sku,
        product_name=snapshot.product_name,
        warehouse_code=snapshot.warehouse_code,
        type=AlertType.NEGATIVE_STOCK,
        severity=AlertSeverity.CRITICAL,
        message=(
            f"Data-quality anomaly for {snapshot.sku} ({snapshot.product_name}) "
            f"at {snapshot.warehouse_code}: {detail} (available={snapshot.available})."
        ),
    )


def check_out_of_stock(snapshot: StockSnapshot) -> AlertCandidate | None:
    """Detect out-of-stock condition (available <= 0) with CRITICAL severity."""
    if snapshot.available > 0:
        return None

    return AlertCandidate(
        product_id=snapshot.product_id,
        warehouse_id=snapshot.warehouse_id,
        sku=snapshot.sku,
        product_name=snapshot.product_name,
        warehouse_code=snapshot.warehouse_code,
        type=AlertType.OUT_OF_STOCK,
        severity=AlertSeverity.CRITICAL,
        message=(
            f"OUT OF STOCK: {snapshot.sku} ({snapshot.product_name}) at "
            f"{snapshot.warehouse_code} has {snapshot.available} available units "
            f"(on_hand={snapshot.quantity_on_hand}, reserved={snapshot.quantity_reserved})."
        ),
    )


def check_low_stock(
    snapshot: StockSnapshot,
    thresholds: ThresholdConfig,
) -> AlertCandidate | None:
    """Detect low-stock condition (0 < available <= low_stock_threshold) with WARNING severity."""
    if snapshot.available <= 0:
        # Handled by OUT_OF_STOCK rule to avoid redundant overlapping alerts
        return None

    if snapshot.available > thresholds.low_stock_threshold:
        return None

    return AlertCandidate(
        product_id=snapshot.product_id,
        warehouse_id=snapshot.warehouse_id,
        sku=snapshot.sku,
        product_name=snapshot.product_name,
        warehouse_code=snapshot.warehouse_code,
        type=AlertType.LOW_STOCK,
        severity=AlertSeverity.WARNING,
        message=(
            f"LOW STOCK: {snapshot.sku} ({snapshot.product_name}) at "
            f"{snapshot.warehouse_code} has {snapshot.available} available units, "
            f"at or below reorder threshold of {thresholds.low_stock_threshold}."
        ),
    )


def check_overstock(
    snapshot: StockSnapshot,
    thresholds: ThresholdConfig,
) -> AlertCandidate | None:
    """Detect overstock condition (available > overstock_threshold) with WARNING severity."""
    if snapshot.available <= thresholds.overstock_threshold:
        return None

    return AlertCandidate(
        product_id=snapshot.product_id,
        warehouse_id=snapshot.warehouse_id,
        sku=snapshot.sku,
        product_name=snapshot.product_name,
        warehouse_code=snapshot.warehouse_code,
        type=AlertType.OVERSTOCK,
        severity=AlertSeverity.WARNING,
        message=(
            f"OVERSTOCK: {snapshot.sku} ({snapshot.product_name}) at "
            f"{snapshot.warehouse_code} has {snapshot.available} available units, "
            f"exceeding maximum threshold of {thresholds.overstock_threshold}."
        ),
    )


def check_slow_moving(
    snapshot: StockSnapshot,
    thresholds: ThresholdConfig,
    reference_time: datetime,
) -> AlertCandidate | None:
    """Detect slow-moving inventory when available > 0 and no movement occurred in N days."""
    if snapshot.available <= 0:
        return None

    if snapshot.last_movement_at is None:
        days_idle: float | None = None
        is_slow = True
    else:
        elapsed_seconds = (reference_time - snapshot.last_movement_at).total_seconds()
        days_idle = round(max(0.0, elapsed_seconds / 86400.0), 1)
        is_slow = days_idle >= float(thresholds.slow_moving_days)

    if not is_slow:
        return None

    idle_desc = (
        f"{days_idle:.1f} days"
        if days_idle is not None
        else f"over {thresholds.slow_moving_days} days (no recorded movements)"
    )
    return AlertCandidate(
        product_id=snapshot.product_id,
        warehouse_id=snapshot.warehouse_id,
        sku=snapshot.sku,
        product_name=snapshot.product_name,
        warehouse_code=snapshot.warehouse_code,
        type=AlertType.SLOW_MOVING,
        severity=AlertSeverity.WARNING,
        message=(
            f"SLOW MOVING: {snapshot.sku} ({snapshot.product_name}) at "
            f"{snapshot.warehouse_code} ({snapshot.available} units available) "
            f"has had no stock movement for {idle_desc} "
            f"(threshold: {thresholds.slow_moving_days} days)."
        ),
    )


def check_stockout_forecast(
    snapshot: StockSnapshot,
    thresholds: ThresholdConfig,
) -> AlertCandidate | None:
    """Detect imminent stockout based on average daily consumption over the lookback window."""
    if snapshot.available <= 0 or snapshot.avg_daily_consumption <= 0.0:
        return None

    days_remaining = estimate_days_of_stock_remaining(
        available=snapshot.available,
        avg_daily_consumption=snapshot.avg_daily_consumption,
    )
    if days_remaining is None or days_remaining > float(thresholds.forecast_warning_days):
        return None

    severity = (
        AlertSeverity.CRITICAL if days_remaining <= 2.0 else AlertSeverity.WARNING
    )
    return AlertCandidate(
        product_id=snapshot.product_id,
        warehouse_id=snapshot.warehouse_id,
        sku=snapshot.sku,
        product_name=snapshot.product_name,
        warehouse_code=snapshot.warehouse_code,
        type=AlertType.STOCKOUT_FORECAST,
        severity=severity,
        message=(
            f"STOCKOUT FORECAST: {snapshot.sku} ({snapshot.product_name}) at "
            f"{snapshot.warehouse_code} has an estimated {days_remaining:.1f} days of stock "
            f"remaining ({snapshot.available} units available at "
            f"{snapshot.avg_daily_consumption:.2f} units/day; warning threshold: "
            f"{thresholds.forecast_warning_days} days)."
        ),
    )


def evaluate_snapshot(
    snapshot: StockSnapshot,
    thresholds: ThresholdConfig,
    reference_time: datetime,
) -> list[AlertCandidate]:
    """Evaluate all 6 monitoring rules against a single StockSnapshot."""
    candidates: list[AlertCandidate] = []

    for candidate in (
        check_negative_stock(snapshot),
        check_out_of_stock(snapshot),
        check_low_stock(snapshot, thresholds),
        check_overstock(snapshot, thresholds),
        check_slow_moving(snapshot, thresholds, reference_time),
        check_stockout_forecast(snapshot, thresholds),
    ):
        if candidate is not None:
            candidates.append(candidate)

    return candidates


def compute_stock_status(
    snapshot: StockSnapshot,
    thresholds: ThresholdConfig,
) -> StockStatus:
    """Classify the primary operational StockStatus for a StockSnapshot."""
    if snapshot.has_data_anomaly:
        return StockStatus.ANOMALY
    if snapshot.available <= 0:
        return StockStatus.OUT_OF_STOCK
    if snapshot.available <= thresholds.low_stock_threshold:
        return StockStatus.LOW_STOCK
    if snapshot.available > thresholds.overstock_threshold:
        return StockStatus.OVERSTOCK
    return StockStatus.HEALTHY

