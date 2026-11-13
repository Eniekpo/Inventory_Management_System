"""Consumption analysis and stockout forecasting calculations."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta

from app.models.entities import utc_now


def calculate_average_daily_consumption(
    movements: Iterable[tuple[datetime, int]],
    lookback_days: int = 30,
    reference_time: datetime | None = None,
) -> float:
    """Calculate average daily outbound consumption over the specified lookback window.

    Args:
        movements: Iterable of (timestamp, change) tuples. Negative change values
            represent outbound consumption (sales, usage, damage, outbound transfers).
        lookback_days: Number of days to average over (must be > 0).
        reference_time: Reference UTC timestamp; defaults to current UTC time.

    Returns:
        Average units consumed per day (float >= 0.0).
    """
    if lookback_days <= 0:
        raise ValueError("lookback_days must be greater than 0")

    now = reference_time or utc_now()
    cutoff = now - timedelta(days=lookback_days)

    total_outbound = 0
    for ts, change in movements:
        if change < 0 and cutoff <= ts <= now:
            total_outbound += abs(int(change))

    return round(total_outbound / float(lookback_days), 4)


def estimate_days_of_stock_remaining(
    available: int,
    avg_daily_consumption: float,
) -> float | None:
    """Estimate how many days current available stock will last at the given consumption rate.

    Args:
        available: Net available units (on_hand - reserved).
        avg_daily_consumption: Average daily outbound units consumed.

    Returns:
        0.0 if available <= 0; None if avg_daily_consumption <= 0 (infinite/no consumption);
        otherwise available / avg_daily_consumption rounded to 2 decimal places.
    """
    if available <= 0:
        return 0.0
    if avg_daily_consumption <= 0.0:
        return None
    return round(float(available) / float(avg_daily_consumption), 2)

