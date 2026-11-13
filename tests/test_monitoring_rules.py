"""Unit tests for pure monitoring rules, edge cases, and forecast calculations."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.models.enums import AlertSeverity, AlertType, StockStatus
from app.services.monitoring.forecast import (
    calculate_average_daily_consumption,
    estimate_days_of_stock_remaining,
)
from app.services.monitoring.rules import (
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


def _make_snapshot(**overrides: object) -> StockSnapshot:
    """Create a baseline StockSnapshot with customizable field overrides."""
    defaults = dict(
        product_id=1,
        sku="TEST-001",
        product_name="Test Sensor",
        category="Electronics",
        supplier="Apex Components",
        unit_cost=25.0,
        reorder_point=10,
        reorder_quantity=50,
        warehouse_id=1,
        warehouse_code="WH-EAST",
        warehouse_name="East Warehouse",
        quantity_on_hand=30,
        quantity_reserved=5,
        last_movement_at=datetime(2026, 6, 10, 12, 0, 0),
        avg_daily_consumption=1.0,
    )
    defaults.update(overrides)
    return StockSnapshot(**defaults)  # type: ignore[arg-type]


class TestOutAndLowStockRules:
    """Tests for OUT_OF_STOCK and LOW_STOCK detection and boundary conditions."""

    def test_zero_available_triggers_out_of_stock_critical(self) -> None:
        snap = _make_snapshot(quantity_on_hand=5, quantity_reserved=5)
        thresholds = ThresholdConfig(low_stock_threshold=10)

        out_alert = check_out_of_stock(snap)
        low_alert = check_low_stock(snap, thresholds)

        assert out_alert is not None
        assert out_alert.type == AlertType.OUT_OF_STOCK
        assert out_alert.severity == AlertSeverity.CRITICAL
        assert low_alert is None

    def test_exact_reorder_point_triggers_low_stock_warning(self) -> None:
        snap = _make_snapshot(quantity_on_hand=12, quantity_reserved=2)  # available == 10
        thresholds = ThresholdConfig(low_stock_threshold=10)

        alert = check_low_stock(snap, thresholds)
        assert alert is not None
        assert alert.type == AlertType.LOW_STOCK
        assert alert.severity == AlertSeverity.WARNING

    def test_one_above_reorder_point_is_healthy(self) -> None:
        snap = _make_snapshot(quantity_on_hand=13, quantity_reserved=2)  # available == 11
        thresholds = ThresholdConfig(low_stock_threshold=10)

        assert check_low_stock(snap, thresholds) is None
        assert check_out_of_stock(snap) is None


class TestNegativeStockAndAnomalies:
    """Tests for NEGATIVE_STOCK and reservation anomaly detection."""

    def test_negative_on_hand_triggers_critical_anomaly(self) -> None:
        snap = _make_snapshot(quantity_on_hand=-4, quantity_reserved=0)
        alert = check_negative_stock(snap)

        assert alert is not None
        assert alert.type == AlertType.NEGATIVE_STOCK
        assert alert.severity == AlertSeverity.CRITICAL
        assert "negative" in alert.message.lower()

    def test_reserved_greater_than_on_hand_triggers_anomaly(self) -> None:
        snap = _make_snapshot(quantity_on_hand=8, quantity_reserved=12)
        alert = check_negative_stock(snap)

        assert alert is not None
        assert alert.type == AlertType.NEGATIVE_STOCK
        assert alert.severity == AlertSeverity.CRITICAL
        assert "exceeds" in alert.message.lower()

    def test_negative_reserved_triggers_anomaly(self) -> None:
        snap = _make_snapshot(quantity_on_hand=10, quantity_reserved=-1)
        alert = check_negative_stock(snap)
        assert alert is not None
        assert alert.type == AlertType.NEGATIVE_STOCK

    def test_valid_quantities_do_not_trigger_anomaly(self) -> None:
        snap = _make_snapshot(quantity_on_hand=10, quantity_reserved=10)
        assert check_negative_stock(snap) is None


class TestOverstockAndSlowMovingRules:
    """Tests for OVERSTOCK and SLOW_MOVING boundaries."""

    def test_overstock_exact_threshold_does_not_trigger(self) -> None:
        snap = _make_snapshot(quantity_on_hand=500, quantity_reserved=0)
        thresholds = ThresholdConfig(low_stock_threshold=10, overstock_threshold=500)
        assert check_overstock(snap, thresholds) is None

    def test_overstock_above_threshold_triggers_warning(self) -> None:
        snap = _make_snapshot(quantity_on_hand=501, quantity_reserved=0)
        thresholds = ThresholdConfig(low_stock_threshold=10, overstock_threshold=500)
        alert = check_overstock(snap, thresholds)

        assert alert is not None
        assert alert.type == AlertType.OVERSTOCK
        assert alert.severity == AlertSeverity.WARNING

    def test_slow_moving_exact_threshold_triggers(self) -> None:
        ref = datetime(2026, 7, 1, 12, 0, 0)
        snap = _make_snapshot(
            quantity_on_hand=25,
            quantity_reserved=0,
            last_movement_at=ref - timedelta(days=30),
        )
        thresholds = ThresholdConfig(low_stock_threshold=10, slow_moving_days=30)
        alert = check_slow_moving(snap, thresholds, reference_time=ref)

        assert alert is not None
        assert alert.type == AlertType.SLOW_MOVING

    def test_slow_moving_recent_movement_does_not_trigger(self) -> None:
        ref = datetime(2026, 7, 1, 12, 0, 0)
        snap = _make_snapshot(
            quantity_on_hand=25,
            quantity_reserved=0,
            last_movement_at=ref - timedelta(days=29, hours=12),
        )
        thresholds = ThresholdConfig(low_stock_threshold=10, slow_moving_days=30)
        assert check_slow_moving(snap, thresholds, reference_time=ref) is None

    def test_slow_moving_ignored_when_out_of_stock(self) -> None:
        ref = datetime(2026, 7, 1, 12, 0, 0)
        snap = _make_snapshot(
            quantity_on_hand=0,
            quantity_reserved=0,
            last_movement_at=None,
        )
        thresholds = ThresholdConfig(low_stock_threshold=10, slow_moving_days=30)
        assert check_slow_moving(snap, thresholds, reference_time=ref) is None


class TestForecastCalculationsAndRule:
    """Tests for consumption forecasting math and STOCKOUT_FORECAST rule."""

    def test_calculate_average_daily_consumption_30_days(self) -> None:
        ref = datetime(2026, 7, 1, 12, 0, 0)
        movements = [
            (ref - timedelta(days=5), -30),
            (ref - timedelta(days=15), -60),
            (ref - timedelta(days=10), +100),  # Inbound restock ignored
            (ref - timedelta(days=45), -90),   # Outside 30d window ignored
        ]
        avg = calculate_average_daily_consumption(movements, lookback_days=30, reference_time=ref)
        assert avg == 3.0

    def test_invalid_lookback_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="lookback_days"):
            calculate_average_daily_consumption([], lookback_days=0)

    def test_estimate_days_of_stock_remaining_cases(self) -> None:
        assert estimate_days_of_stock_remaining(0, 5.0) == 0.0
        assert estimate_days_of_stock_remaining(-3, 5.0) == 0.0
        assert estimate_days_of_stock_remaining(50, 0.0) is None
        assert estimate_days_of_stock_remaining(21, 3.0) == 7.0

    def test_stockout_forecast_warning_and_critical_severities(self) -> None:
        thresholds = ThresholdConfig(low_stock_threshold=10, forecast_warning_days=7)

        # 5 days left -> WARNING
        snap_warn = _make_snapshot(
            quantity_on_hand=25, quantity_reserved=0, avg_daily_consumption=5.0
        )
        alert_warn = check_stockout_forecast(snap_warn, thresholds)
        assert alert_warn is not None
        assert alert_warn.type == AlertType.STOCKOUT_FORECAST
        assert alert_warn.severity == AlertSeverity.WARNING

        # 1.5 days left -> CRITICAL
        snap_crit = _make_snapshot(
            quantity_on_hand=15, quantity_reserved=0, avg_daily_consumption=10.0
        )
        alert_crit = check_stockout_forecast(snap_crit, thresholds)
        assert alert_crit is not None
        assert alert_crit.severity == AlertSeverity.CRITICAL

        # 10 days left -> None
        snap_ok = _make_snapshot(
            quantity_on_hand=50, quantity_reserved=0, avg_daily_consumption=5.0
        )
        assert check_stockout_forecast(snap_ok, thresholds) is None


class TestCompositeStatusAndEvaluate:
    """Tests for compute_stock_status and evaluate_snapshot."""

    def test_compute_stock_status_all_variants(self) -> None:
        thresholds = ThresholdConfig(low_stock_threshold=10, overstock_threshold=100)

        assert (
            compute_stock_status(
                _make_snapshot(quantity_on_hand=-1, quantity_reserved=0), thresholds
            )
            == StockStatus.ANOMALY
        )
        assert (
            compute_stock_status(
                _make_snapshot(quantity_on_hand=0, quantity_reserved=0), thresholds
            )
            == StockStatus.OUT_OF_STOCK
        )
        assert (
            compute_stock_status(
                _make_snapshot(quantity_on_hand=10, quantity_reserved=0), thresholds
            )
            == StockStatus.LOW_STOCK
        )
        assert (
            compute_stock_status(
                _make_snapshot(quantity_on_hand=150, quantity_reserved=0), thresholds
            )
            == StockStatus.OVERSTOCK
        )
        assert (
            compute_stock_status(
                _make_snapshot(quantity_on_hand=50, quantity_reserved=0), thresholds
            )
            == StockStatus.HEALTHY
        )

    def test_evaluate_snapshot_combines_multiple_rules(self) -> None:
        ref = datetime(2026, 7, 1, 12, 0, 0)
        thresholds = ThresholdConfig(
            low_stock_threshold=10,
            overstock_threshold=100,
            slow_moving_days=30,
            forecast_warning_days=7,
        )
        snap = _make_snapshot(
            quantity_on_hand=8,
            quantity_reserved=0,
            last_movement_at=ref - timedelta(days=45),
            avg_daily_consumption=2.0,
        )
        candidates = evaluate_snapshot(snap, thresholds, reference_time=ref)
        triggered_types = {c.type for c in candidates}
        assert AlertType.LOW_STOCK in triggered_types
        assert AlertType.SLOW_MOVING in triggered_types
        assert AlertType.STOCKOUT_FORECAST in triggered_types

