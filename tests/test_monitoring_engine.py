"""Integration tests for MonitoringEngine: deduplication, auto-resolve, and replenishment."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Alert, AlertSetting, Product, StockLevel, StockMovement, utc_now
from app.models.enums import AlertType, MovementReason
from app.services.monitoring.engine import MonitoringEngine
from app.services.monitoring.replenishment import calculate_suggested_reorder_qty


class TestMonitoringEngineLifecycle:
    """Tests for alert creation, duplicate prevention, and auto-resolution."""

    def test_duplicate_alert_prevention(
        self,
        db_session: Session,
        sample_entities: dict[str, object],
    ) -> None:
        now = utc_now()
        prod_a: Product = sample_entities["prod_a"]  # type: ignore[assignment]
        prod_b: Product = sample_entities["prod_b"]  # type: ignore[assignment]
        wh_east = sample_entities["wh_east"]

        # Record recent movements so items are not flagged as SLOW_MOVING
        db_session.add_all(
            [
                StockMovement(
                    product_id=prod_a.id,
                    warehouse_id=wh_east.id,  # type: ignore[attr-defined]
                    change=35,
                    reason=MovementReason.RESTOCK.value,
                    timestamp=now,
                    user="tester",
                ),
                StockMovement(
                    product_id=prod_b.id,
                    warehouse_id=wh_east.id,  # type: ignore[attr-defined]
                    change=15,
                    reason=MovementReason.RESTOCK.value,
                    timestamp=now,
                    user="tester",
                ),
            ]
        )
        db_session.commit()

        engine = MonitoringEngine(db_session)
        # First run: prod_b has available=15 <= reorder_point=20 -> creates 1 LOW_STOCK alert
        run1 = engine.run_check(reference_time=now)
        assert run1.new_alerts_created == 1
        assert run1.duplicate_alerts_skipped == 0

        # Second run: no state change -> 0 new alerts, 1 duplicate skipped
        run2 = engine.run_check(reference_time=now)
        assert run2.new_alerts_created == 0
        assert run2.duplicate_alerts_skipped == 1

        open_alerts = list(
            db_session.scalars(
                select(Alert).where(Alert.is_active.is_(True))
            ).all()
        )
        assert len(open_alerts) == 1
        assert open_alerts[0].type == AlertType.LOW_STOCK.value

    def test_auto_resolve_when_stock_recovers(
        self,
        db_session: Session,
        sample_entities: dict[str, object],
    ) -> None:
        now = utc_now()
        prod_b: Product = sample_entities["prod_b"]  # type: ignore[assignment]
        sl_b: StockLevel = sample_entities["sl_b"]  # type: ignore[assignment]
        wh_east = sample_entities["wh_east"]

        # Drop prod_b to 0 available (out of stock)
        sl_b.quantity_on_hand = 0
        db_session.add(
            StockMovement(
                product_id=prod_b.id,
                warehouse_id=wh_east.id,  # type: ignore[attr-defined]
                change=-15,
                reason=MovementReason.SALE.value,
                timestamp=now,
                user="tester",
            )
        )
        db_session.commit()

        engine = MonitoringEngine(db_session)
        res_out = engine.run_check(reference_time=now, product_id=prod_b.id)
        assert res_out.new_alerts_created >= 1

        open_oos = db_session.scalar(
            select(Alert).where(
                Alert.product_id == prod_b.id,
                Alert.type == AlertType.OUT_OF_STOCK.value,
                Alert.is_active.is_(True),
            )
        )
        assert open_oos is not None

        # Now replenish prod_b above reorder_point (e.g., to 100 units)
        sl_b.quantity_on_hand = 100
        db_session.commit()

        res_recovered = engine.run_check(reference_time=now, product_id=prod_b.id)
        assert res_recovered.alerts_auto_resolved >= 1

        db_session.refresh(open_oos)
        assert open_oos.is_active is False
        assert open_oos.resolved_at is not None
        assert open_oos.acknowledged_at is not None

    def test_per_product_threshold_override(
        self,
        db_session: Session,
        sample_entities: dict[str, object],
    ) -> None:
        prod_a: Product = sample_entities["prod_a"]  # type: ignore[assignment]
        # prod_a has reorder_point=10 and available=30; add override setting low_stock_threshold=40
        db_session.add(
            AlertSetting(
                product_id=prod_a.id,
                low_stock_threshold=40,
                overstock_threshold=200,
                slow_moving_days=15,
                forecast_lookback_days=30,
                forecast_warning_days=7,
            )
        )
        db_session.commit()

        engine = MonitoringEngine(db_session)
        thresholds = engine.get_effective_thresholds(prod_a)
        assert thresholds.low_stock_threshold == 40
        assert thresholds.overstock_threshold == 200
        assert thresholds.slow_moving_days == 15


class TestReplenishmentAndCSVExport:
    """Tests for replenishment quantity calculation, supplier grouping, and CSV export."""

    def test_calculate_suggested_reorder_qty_cases(self) -> None:
        # Healthy stock & low consumption -> 0
        assert (
            calculate_suggested_reorder_qty(
                available=50,
                reorder_point=15,
                reorder_quantity=40,
                avg_daily_consumption=1.0,
            )
            == 0
        )

        # Low stock (available=5, reorder_point=15, reorder_quantity=40) -> deficit(10) + 40 = 50
        qty = calculate_suggested_reorder_qty(
            available=5,
            reorder_point=15,
            reorder_quantity=40,
            avg_daily_consumption=1.0,
        )
        assert qty == 50

    def test_replenishment_report_and_csv_export(
        self,
        db_session: Session,
        sample_entities: dict[str, object],
        tmp_path: Path,
    ) -> None:
        sl_a: StockLevel = sample_entities["sl_a"]  # type: ignore[assignment]
        sl_a.quantity_on_hand = 2
        sl_a.quantity_reserved = 2  # available == 0 -> CRITICAL urgency
        db_session.commit()

        engine = MonitoringEngine(db_session)
        report = engine.generate_replenishment_report()

        assert report.total_suppliers == 2
        assert report.total_line_items == 2
        assert report.total_units_to_order > 0
        assert report.total_estimated_cost > 0

        csv_file = tmp_path / "replenishment_out.csv"
        csv_text = engine.export_replenishment_to_csv(output_path=csv_file)

        assert csv_file.is_file()
        assert "supplier,urgency,sku,product_name" in csv_text
        assert "SKU-001" in csv_text
        assert "SKU-002" in csv_text

