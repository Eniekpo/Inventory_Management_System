"""MonitoringEngine orchestrator: runs rules, deduplicates open alerts, and auto-resolves."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.core.config import get_settings
from app.core.logging_config import get_logger
from app.models.entities import (
    Alert,
    AlertSetting,
    Product,
    StockLevel,
    StockMovement,
    SystemMetadata,
    utc_now,
)
from app.models.enums import MovementReason
from app.models.schemas import ReplenishmentReport
from app.services.monitoring.forecast import calculate_average_daily_consumption
from app.services.monitoring.replenishment import (
    build_replenishment_report,
    export_replenishment_csv,
)
from app.services.monitoring.rules import (
    AlertCandidate,
    StockSnapshot,
    ThresholdConfig,
    evaluate_snapshot,
)

logger = get_logger("monitoring.engine")


@dataclass
class MonitoringRunResult:
    """Summary of a single MonitoringEngine evaluation cycle."""

    evaluated_count: int = 0
    new_alerts_created: int = 0
    duplicate_alerts_skipped: int = 0
    alerts_auto_resolved: int = 0
    active_alerts_total: int = 0
    new_alerts: list[Alert] = field(default_factory=list)
    resolved_alerts: list[Alert] = field(default_factory=list)


class MonitoringEngine:
    """Core service that evaluates inventory health, manages alerts, and computes replenishment."""

    def __init__(self, session: Session) -> None:
        """Initialize engine with a SQLAlchemy session."""
        self.session = session
        self.settings = get_settings()

    def get_effective_thresholds(
        self,
        product: Product,
        global_setting: AlertSetting | None = None,
        product_setting: AlertSetting | None = None,
    ) -> ThresholdConfig:
        """Resolve effective thresholds for a product using per-product override -> global -> defaults."""
        if global_setting is None:
            global_setting = self.session.scalar(
                select(AlertSetting).where(AlertSetting.product_id.is_(None))
            )
        if product_setting is None and product.id is not None:
            product_setting = self.session.scalar(
                select(AlertSetting).where(AlertSetting.product_id == product.id)
            )

        # Low stock threshold priority:
        # 1. Per-product AlertSetting.low_stock_threshold (if explicitly set)
        # 2. Product.reorder_point (if > 0 or explicitly configured on product)
        # 3. Global AlertSetting.low_stock_threshold
        # 4. Env default
        if product_setting and product_setting.low_stock_threshold is not None:
            low_stock = product_setting.low_stock_threshold
        elif product.reorder_point is not None:
            low_stock = product.reorder_point
        elif global_setting and global_setting.low_stock_threshold is not None:
            low_stock = global_setting.low_stock_threshold
        else:
            low_stock = self.settings.default_low_stock_threshold

        overstock = (
            product_setting.overstock_threshold
            if product_setting is not None
            else (
                global_setting.overstock_threshold
                if global_setting is not None
                else self.settings.default_overstock_threshold
            )
        )
        slow_days = (
            product_setting.slow_moving_days
            if product_setting is not None
            else (
                global_setting.slow_moving_days
                if global_setting is not None
                else self.settings.default_slow_moving_days
            )
        )
        lookback_days = (
            product_setting.forecast_lookback_days
            if product_setting is not None
            else (
                global_setting.forecast_lookback_days
                if global_setting is not None
                else self.settings.default_forecast_lookback_days
            )
        )
        warning_days = (
            product_setting.forecast_warning_days
            if product_setting is not None
            else (
                global_setting.forecast_warning_days
                if global_setting is not None
                else self.settings.default_forecast_warning_days
            )
        )

        return ThresholdConfig(
            low_stock_threshold=int(low_stock),
            overstock_threshold=int(overstock),
            slow_moving_days=int(slow_days),
            forecast_lookback_days=int(lookback_days),
            forecast_warning_days=int(warning_days),
        )

    def build_snapshots(
        self,
        reference_time: datetime | None = None,
        product_id: int | None = None,
        warehouse_id: int | None = None,
    ) -> list[tuple[StockSnapshot, ThresholdConfig]]:
        """Build StockSnapshot and ThresholdConfig pairs for all (or filtered) stock records."""
        now = reference_time or utc_now()

        stmt = (
            select(StockLevel)
            .options(joinedload(StockLevel.product), joinedload(StockLevel.warehouse))
            .order_by(StockLevel.product_id, StockLevel.warehouse_id)
        )
        if product_id is not None:
            stmt = stmt.where(StockLevel.product_id == product_id)
        if warehouse_id is not None:
            stmt = stmt.where(StockLevel.warehouse_id == warehouse_id)

        stock_levels = list(self.session.scalars(stmt).all())
        if not stock_levels:
            return []

        all_settings = list(self.session.scalars(select(AlertSetting)).all())
        global_setting = next((s for s in all_settings if s.product_id is None), None)
        settings_by_product = {
            s.product_id: s for s in all_settings if s.product_id is not None
        }

        # Determine maximum lookback window needed for movement consumption queries
        max_lookback = max(
            [self.settings.default_forecast_lookback_days]
            + [s.forecast_lookback_days for s in all_settings]
        )
        cutoff = now - timedelta(days=max_lookback)

        # Query last movement timestamp per (product_id, warehouse_id)
        last_movement_rows = self.session.execute(
            select(
                StockMovement.product_id,
                StockMovement.warehouse_id,
                func.max(StockMovement.timestamp),
            ).group_by(StockMovement.product_id, StockMovement.warehouse_id)
        ).all()
        last_movement_map: dict[tuple[int, int], datetime] = {
            (row[0], row[1]): row[2] for row in last_movement_rows if row[2] is not None
        }

        # Query recent outbound movements within max_lookback window
        recent_movements = self.session.execute(
            select(
                StockMovement.product_id,
                StockMovement.warehouse_id,
                StockMovement.timestamp,
                StockMovement.change,
                StockMovement.reason,
            ).where(
                StockMovement.timestamp >= cutoff,
                StockMovement.change < 0,
            )
        ).all()

        outbound_by_pair: dict[tuple[int, int], list[tuple[datetime, int]]] = defaultdict(list)
        for p_id, w_id, ts, change, reason in recent_movements:
            if reason != MovementReason.INITIAL_SEED.value:
                outbound_by_pair[(p_id, w_id)].append((ts, change))

        results: list[tuple[StockSnapshot, ThresholdConfig]] = []
        for sl in stock_levels:
            product = sl.product
            warehouse = sl.warehouse
            thresholds = self.get_effective_thresholds(
                product=product,
                global_setting=global_setting,
                product_setting=settings_by_product.get(product.id),
            )
            pair_key = (product.id, warehouse.id)
            avg_daily = calculate_average_daily_consumption(
                movements=outbound_by_pair.get(pair_key, []),
                lookback_days=thresholds.forecast_lookback_days,
                reference_time=now,
            )
            snapshot = StockSnapshot(
                product_id=product.id,
                sku=product.sku,
                product_name=product.name,
                category=product.category,
                supplier=product.supplier,
                unit_cost=float(product.unit_cost),
                reorder_point=int(product.reorder_point),
                reorder_quantity=int(product.reorder_quantity),
                warehouse_id=warehouse.id,
                warehouse_code=warehouse.code,
                warehouse_name=warehouse.name,
                quantity_on_hand=int(sl.quantity_on_hand),
                quantity_reserved=int(sl.quantity_reserved),
                last_movement_at=last_movement_map.get(pair_key),
                avg_daily_consumption=avg_daily,
            )
            results.append((snapshot, thresholds))

        return results

    def run_check(
        self,
        reference_time: datetime | None = None,
        product_id: int | None = None,
        warehouse_id: int | None = None,
    ) -> MonitoringRunResult:
        """Execute monitoring rules, prevent duplicate alerts, and auto-resolve recovered alerts."""
        now = reference_time or utc_now()
        snapshots_with_thresholds = self.build_snapshots(
            reference_time=now,
            product_id=product_id,
            warehouse_id=warehouse_id,
        )

        # Load existing open alerts (unacknowledged and unresolved) for the target scope
        open_alerts_stmt = select(Alert).where(
            Alert.is_active.is_(True),
            Alert.acknowledged_at.is_(None),
            Alert.resolved_at.is_(None),
        )
        if product_id is not None:
            open_alerts_stmt = open_alerts_stmt.where(Alert.product_id == product_id)
        if warehouse_id is not None:
            open_alerts_stmt = open_alerts_stmt.where(Alert.warehouse_id == warehouse_id)

        open_alerts = list(self.session.scalars(open_alerts_stmt).all())
        open_by_key: dict[tuple[int, int, str], list[Alert]] = defaultdict(list)
        for alert in open_alerts:
            open_by_key[(alert.product_id, alert.warehouse_id, str(alert.type))].append(alert)

        result = MonitoringRunResult(evaluated_count=len(snapshots_with_thresholds))
        triggered_keys: set[tuple[int, int, str]] = set()
        evaluated_pairs: set[tuple[int, int]] = set()

        for snapshot, thresholds in snapshots_with_thresholds:
            evaluated_pairs.add((snapshot.product_id, snapshot.warehouse_id))
            candidates: list[AlertCandidate] = evaluate_snapshot(
                snapshot=snapshot,
                thresholds=thresholds,
                reference_time=now,
            )

            for candidate in candidates:
                alert_key = (
                    candidate.product_id,
                    candidate.warehouse_id,
                    candidate.type.value,
                )
                triggered_keys.add(alert_key)

                existing_list = open_by_key.get(alert_key, [])
                if existing_list:
                    # Update message/severity on existing open alert; do NOT create a duplicate
                    primary_open = existing_list[0]
                    primary_open.message = candidate.message
                    primary_open.severity = candidate.severity.value
                    result.duplicate_alerts_skipped += 1
                else:
                    new_alert = Alert(
                        product_id=candidate.product_id,
                        warehouse_id=candidate.warehouse_id,
                        type=candidate.type.value,
                        severity=candidate.severity.value,
                        message=candidate.message,
                        created_at=now,
                        acknowledged_at=None,
                        resolved_at=None,
                        is_active=True,
                    )
                    self.session.add(new_alert)
                    open_by_key[alert_key].append(new_alert)
                    result.new_alerts_created += 1
                    result.new_alerts.append(new_alert)

        # Auto-resolve any open alerts in the evaluated scope whose condition recovered
        for (p_id, w_id, alert_type), alert_list in open_by_key.items():
            if (p_id, w_id) in evaluated_pairs and (p_id, w_id, alert_type) not in triggered_keys:
                for open_alert in alert_list:
                    open_alert.is_active = False
                    open_alert.resolved_at = now
                    open_alert.acknowledged_at = open_alert.acknowledged_at or now
                    result.alerts_auto_resolved += 1
                    result.resolved_alerts.append(open_alert)

        # Record last monitoring run timestamp in SystemMetadata
        meta = self.session.get(SystemMetadata, "last_monitoring_run")
        if meta is None:
            self.session.add(
                SystemMetadata(
                    key="last_monitoring_run",
                    value=now.isoformat(),
                    updated_at=now,
                )
            )
        else:
            meta.value = now.isoformat()
            meta.updated_at = now

        self.session.commit()

        result.active_alerts_total = (
            self.session.scalar(
                select(func.count(Alert.id)).where(
                    Alert.is_active.is_(True),
                    Alert.acknowledged_at.is_(None),
                )
            )
            or 0
        )

        logger.info(
            "Monitoring check complete: evaluated=%d, new_alerts=%d, "
            "duplicates_skipped=%d, auto_resolved=%d, active_total=%d",
            result.evaluated_count,
            result.new_alerts_created,
            result.duplicate_alerts_skipped,
            result.alerts_auto_resolved,
            result.active_alerts_total,
        )
        return result

    def generate_replenishment_report(
        self,
        reference_time: datetime | None = None,
    ) -> ReplenishmentReport:
        """Compute replenishment recommendations grouped by supplier."""
        now = reference_time or utc_now()
        snapshots = self.build_snapshots(reference_time=now)
        return build_replenishment_report(snapshots, reference_time=now)

    def export_replenishment_to_csv(
        self,
        output_path: Path | None = None,
        reference_time: datetime | None = None,
    ) -> str:
        """Generate replenishment report and export it as CSV string (and optional file)."""
        report = self.generate_replenishment_report(reference_time=reference_time)
        return export_replenishment_csv(report, output_path=output_path)

