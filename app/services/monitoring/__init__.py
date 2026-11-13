"""Core inventory monitoring engine, pure rule evaluators, forecasting, and replenishment."""

from app.services.monitoring.engine import MonitoringEngine, MonitoringRunResult
from app.services.monitoring.forecast import (
    calculate_average_daily_consumption,
    estimate_days_of_stock_remaining,
)
from app.services.monitoring.replenishment import (
    build_replenishment_report,
    calculate_suggested_reorder_qty,
    export_replenishment_csv,
)
from app.services.monitoring.rules import (
    AlertCandidate,
    StockSnapshot,
    ThresholdConfig,
    check_low_stock,
    check_negative_stock,
    check_out_of_stock,
    check_overstock,
    check_slow_moving,
    check_stockout_forecast,
    compute_stock_status,
    evaluate_snapshot,
)

__all__ = [
    "AlertCandidate",
    "MonitoringEngine",
    "MonitoringRunResult",
    "StockSnapshot",
    "ThresholdConfig",
    "build_replenishment_report",
    "calculate_average_daily_consumption",
    "calculate_suggested_reorder_qty",
    "check_low_stock",
    "check_negative_stock",
    "check_out_of_stock",
    "check_overstock",
    "check_slow_moving",
    "check_stockout_forecast",
    "compute_stock_status",
    "estimate_days_of_stock_remaining",
    "evaluate_snapshot",
    "export_replenishment_csv",
]

