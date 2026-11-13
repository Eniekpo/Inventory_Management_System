"""High-level domain service for products, stock levels, movements, alerts, thresholds, and KPIs."""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import desc, func, or_, select
from sqlalchemy.orm import Session, joinedload

from app import __version__
from app.core.config import get_settings
from app.core.exceptions import (
    DuplicateEntityError,
    EntityNotFoundError,
    ValidationFailedError,
)
from app.core.logging_config import get_logger
from app.models.entities import (
    Alert,
    AlertSetting,
    Product,
    StockLevel,
    StockMovement,
    SystemMetadata,
    Warehouse,
    utc_now,
)
from app.models.enums import StockStatus
from app.models.schemas import (
    AlertResponse,
    CategoryStockBreakdown,
    DashboardSummaryResponse,
    HealthResponse,
    PaginatedResponse,
    ProductCreate,
    ProductResponse,
    StockLevelResponse,
    StockMovementCreate,
    StockMovementResponse,
    ThresholdSettingResponse,
    ThresholdSettingUpdate,
    WarehouseResponse,
    WarehouseStockBreakdown,
)
from app.services.monitoring.engine import MonitoringEngine
from app.services.monitoring.forecast import estimate_days_of_stock_remaining
from app.services.monitoring.rules import compute_stock_status

logger = get_logger("inventory_service")


def _paginate_list(
    items: list[Any],
    page: int,
    page_size: int,
) -> PaginatedResponse[Any]:
    """Slice an in-memory sequence into a PaginatedResponse."""
    total = len(items)
    total_pages = math.ceil(total / page_size) if page_size > 0 else 0
    start = (page - 1) * page_size
    end = start + page_size
    return PaginatedResponse(
        items=items[start:end],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )


class InventoryService:
    """Application service encapsulating all inventory queries and state transitions."""

    def __init__(self, session: Session) -> None:
        """Initialize service with a SQLAlchemy session."""
        self.session = session
        self.settings = get_settings()
        self.engine = MonitoringEngine(session)

    # --- Products & Warehouses ---

    def list_products(
        self,
        category: str | None = None,
        supplier: str | None = None,
        search: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> PaginatedResponse[ProductResponse]:
        """Return a paginated, filtered list of catalog products."""
        stmt = select(Product).order_by(Product.sku)
        if category:
            stmt = stmt.where(func.lower(Product.category) == category.strip().lower())
        if supplier:
            stmt = stmt.where(func.lower(Product.supplier) == supplier.strip().lower())
        if search:
            pattern = f"%{search.strip()}%"
            stmt = stmt.where(
                or_(
                    Product.sku.ilike(pattern),
                    Product.name.ilike(pattern),
                    Product.category.ilike(pattern),
                    Product.supplier.ilike(pattern),
                )
            )

        all_products = [
            ProductResponse.model_validate(p)
            for p in self.session.scalars(stmt).all()
        ]
        return _paginate_list(all_products, page=page, page_size=page_size)

    def create_product(
        self,
        payload: ProductCreate,
        initialize_warehouses: bool = True,
    ) -> ProductResponse:
        """Create a new product and optionally initialize 0-balance StockLevels at all warehouses."""
        existing = self.session.scalar(
            select(Product).where(func.lower(Product.sku) == payload.sku.lower())
        )
        if existing is not None:
            raise DuplicateEntityError("Product", payload.sku)

        now = utc_now()
        product = Product(
            sku=payload.sku,
            name=payload.name,
            category=payload.category,
            unit_cost=payload.unit_cost,
            reorder_point=payload.reorder_point,
            reorder_quantity=payload.reorder_quantity,
            supplier=payload.supplier,
            created_at=now,
            updated_at=now,
        )
        self.session.add(product)
        self.session.flush()

        if initialize_warehouses:
            warehouses = list(self.session.scalars(select(Warehouse)).all())
            for wh in warehouses:
                self.session.add(
                    StockLevel(
                        product_id=product.id,
                        warehouse_id=wh.id,
                        quantity_on_hand=0,
                        quantity_reserved=0,
                        updated_at=now,
                    )
                )

        self.session.commit()
        self.session.refresh(product)
        return ProductResponse.model_validate(product)

    def list_warehouses(self) -> list[WarehouseResponse]:
        """Return all configured warehouses."""
        warehouses = self.session.scalars(select(Warehouse).order_by(Warehouse.code)).all()
        return [WarehouseResponse.model_validate(w) for w in warehouses]

    # --- Stock Levels ---

    def list_stock_levels(
        self,
        warehouse: str | None = None,
        category: str | None = None,
        status: str | None = None,
        search: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> PaginatedResponse[StockLevelResponse]:
        """Return enriched, filtered, and paginated warehouse stock levels."""
        snapshots_with_thresholds = self.engine.build_snapshots()

        # Map (product_id, warehouse_id) -> StockLevel ORM row for id & updated_at
        sl_rows = self.session.scalars(select(StockLevel)).all()
        sl_by_pair = {(sl.product_id, sl.warehouse_id): sl for sl in sl_rows}

        enriched: list[StockLevelResponse] = []
        for snap, thresholds in snapshots_with_thresholds:
            if warehouse:
                w_filter = warehouse.strip().lower()
                if (
                    snap.warehouse_code.lower() != w_filter
                    and str(snap.warehouse_id) != w_filter
                    and w_filter not in snap.warehouse_name.lower()
                ):
                    continue
            if category and snap.category.lower() != category.strip().lower():
                continue
            if search:
                q = search.strip().lower()
                if (
                    q not in snap.sku.lower()
                    and q not in snap.product_name.lower()
                    and q not in snap.supplier.lower()
                    and q not in snap.category.lower()
                ):
                    continue

            computed_status = compute_stock_status(snap, thresholds)
            if status and computed_status.value.lower() != status.strip().lower():
                continue

            days_rem = estimate_days_of_stock_remaining(
                available=snap.available,
                avg_daily_consumption=snap.avg_daily_consumption,
            )
            sl_entity = sl_by_pair.get((snap.product_id, snap.warehouse_id))
            stock_val = round(max(0, snap.quantity_on_hand) * snap.unit_cost, 2)

            enriched.append(
                StockLevelResponse(
                    id=sl_entity.id if sl_entity else 0,
                    product_id=snap.product_id,
                    sku=snap.sku,
                    product_name=snap.product_name,
                    category=snap.category,
                    supplier=snap.supplier,
                    unit_cost=round(snap.unit_cost, 2),
                    reorder_point=thresholds.low_stock_threshold,
                    reorder_quantity=snap.reorder_quantity,
                    warehouse_id=snap.warehouse_id,
                    warehouse_code=snap.warehouse_code,
                    warehouse_name=snap.warehouse_name,
                    quantity_on_hand=snap.quantity_on_hand,
                    quantity_reserved=snap.quantity_reserved,
                    available=snap.available,
                    stock_value=stock_val,
                    status=computed_status,
                    days_of_stock_remaining=days_rem,
                    updated_at=sl_entity.updated_at if sl_entity else utc_now(),
                )
            )

        return _paginate_list(enriched, page=page, page_size=page_size)

    # --- Stock Movements ---

    def record_stock_movement(
        self,
        payload: StockMovementCreate,
        trigger_monitoring: bool = True,
    ) -> StockMovementResponse:
        """Record an inbound or outbound stock movement, update StockLevel, and evaluate alerts."""
        product: Product | None = None
        if payload.product_id is not None:
            product = self.session.get(Product, payload.product_id)
            if product is None:
                raise EntityNotFoundError("Product", payload.product_id)
        elif payload.sku:
            product = self.session.scalar(
                select(Product).where(func.lower(Product.sku) == payload.sku.strip().lower())
            )
            if product is None:
                raise EntityNotFoundError("Product SKU", payload.sku)
        else:
            raise ValidationFailedError("Either product_id or sku must be provided.")

        warehouse: Warehouse | None = None
        if payload.warehouse_id is not None:
            warehouse = self.session.get(Warehouse, payload.warehouse_id)
            if warehouse is None:
                raise EntityNotFoundError("Warehouse", payload.warehouse_id)
        elif payload.warehouse_code:
            warehouse = self.session.scalar(
                select(Warehouse).where(
                    func.lower(Warehouse.code) == payload.warehouse_code.strip().lower()
                )
            )
            if warehouse is None:
                raise EntityNotFoundError("Warehouse code", payload.warehouse_code)
        else:
            raise ValidationFailedError("Either warehouse_id or warehouse_code must be provided.")

        now = utc_now()
        stock = self.session.scalar(
            select(StockLevel).where(
                StockLevel.product_id == product.id,
                StockLevel.warehouse_id == warehouse.id,
            )
        )
        if stock is None:
            stock = StockLevel(
                product_id=product.id,
                warehouse_id=warehouse.id,
                quantity_on_hand=payload.change,
                quantity_reserved=0,
                updated_at=now,
            )
            self.session.add(stock)
        else:
            stock.quantity_on_hand = int(stock.quantity_on_hand) + int(payload.change)
            stock.updated_at = now

        movement = StockMovement(
            product_id=product.id,
            warehouse_id=warehouse.id,
            change=payload.change,
            reason=payload.reason.strip().upper(),
            timestamp=now,
            user=payload.user.strip(),
        )
        self.session.add(movement)
        self.session.commit()
        self.session.refresh(movement)
        self.session.refresh(stock)

        if trigger_monitoring:
            self.engine.run_check(
                reference_time=now,
                product_id=product.id,
                warehouse_id=warehouse.id,
            )

        return StockMovementResponse(
            id=movement.id,
            product_id=product.id,
            sku=product.sku,
            product_name=product.name,
            warehouse_id=warehouse.id,
            warehouse_code=warehouse.code,
            warehouse_name=warehouse.name,
            change=movement.change,
            reason=movement.reason,
            timestamp=movement.timestamp,
            user=movement.user,
            new_quantity_on_hand=stock.quantity_on_hand,
            new_available=stock.available,
        )

    def list_stock_movements(
        self,
        product_id: int | None = None,
        warehouse_id: int | None = None,
        reason: str | None = None,
        days: int | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> PaginatedResponse[StockMovementResponse]:
        """Return paginated stock movement ledger entries."""
        stmt = (
            select(StockMovement)
            .options(
                joinedload(StockMovement.product),
                joinedload(StockMovement.warehouse),
            )
            .order_by(desc(StockMovement.timestamp), desc(StockMovement.id))
        )
        if product_id is not None:
            stmt = stmt.where(StockMovement.product_id == product_id)
        if warehouse_id is not None:
            stmt = stmt.where(StockMovement.warehouse_id == warehouse_id)
        if reason:
            stmt = stmt.where(func.upper(StockMovement.reason) == reason.strip().upper())
        if days is not None and days > 0:
            cutoff = utc_now() - timedelta(days=days)
            stmt = stmt.where(StockMovement.timestamp >= cutoff)

        rows = self.session.scalars(stmt).all()
        responses = [
            StockMovementResponse(
                id=m.id,
                product_id=m.product_id,
                sku=m.product.sku if m.product else "",
                product_name=m.product.name if m.product else "",
                warehouse_id=m.warehouse_id,
                warehouse_code=m.warehouse.code if m.warehouse else "",
                warehouse_name=m.warehouse.name if m.warehouse else "",
                change=m.change,
                reason=m.reason,
                timestamp=m.timestamp,
                user=m.user,
            )
            for m in rows
        ]
        return _paginate_list(responses, page=page, page_size=page_size)

    # --- Alerts ---

    def list_alerts(
        self,
        severity: str | None = None,
        alert_type: str | None = None,
        warehouse_id: int | None = None,
        active_only: bool | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> PaginatedResponse[AlertResponse]:
        """Return paginated and filtered monitoring alerts."""
        stmt = (
            select(Alert)
            .options(joinedload(Alert.product), joinedload(Alert.warehouse))
            .order_by(desc(Alert.is_active), desc(Alert.created_at), desc(Alert.id))
        )
        if severity:
            stmt = stmt.where(func.lower(Alert.severity) == severity.strip().lower())
        if alert_type:
            stmt = stmt.where(func.upper(Alert.type) == alert_type.strip().upper())
        if warehouse_id is not None:
            stmt = stmt.where(Alert.warehouse_id == warehouse_id)
        if active_only is True:
            stmt = stmt.where(Alert.is_active.is_(True), Alert.acknowledged_at.is_(None))
        elif active_only is False:
            stmt = stmt.where(
                or_(Alert.is_active.is_(False), Alert.acknowledged_at.is_not(None))
            )

        rows = self.session.scalars(stmt).all()
        items = [
            AlertResponse(
                id=a.id,
                product_id=a.product_id,
                sku=a.product.sku if a.product else "",
                product_name=a.product.name if a.product else "",
                category=a.product.category if a.product else "",
                warehouse_id=a.warehouse_id,
                warehouse_code=a.warehouse.code if a.warehouse else "",
                warehouse_name=a.warehouse.name if a.warehouse else "",
                type=a.type,
                severity=a.severity,
                message=a.message,
                created_at=a.created_at,
                acknowledged_at=a.acknowledged_at,
                resolved_at=a.resolved_at,
                is_active=a.is_active,
            )
            for a in rows
        ]
        return _paginate_list(items, page=page, page_size=page_size)

    def acknowledge_alert(
        self,
        alert_id: int,
        acknowledged_by: str = "operator",
    ) -> AlertResponse:
        """Mark a specific alert as acknowledged."""
        alert = self.session.scalar(
            select(Alert)
            .options(joinedload(Alert.product), joinedload(Alert.warehouse))
            .where(Alert.id == alert_id)
        )
        if alert is None:
            raise EntityNotFoundError("Alert", alert_id)

        now = utc_now()
        if alert.acknowledged_at is None:
            alert.acknowledged_at = now
        alert.is_active = False
        self.session.commit()
        self.session.refresh(alert)

        logger.info("Alert id=%d acknowledged by %s", alert_id, acknowledged_by)
        return AlertResponse(
            id=alert.id,
            product_id=alert.product_id,
            sku=alert.product.sku if alert.product else "",
            product_name=alert.product.name if alert.product else "",
            category=alert.product.category if alert.product else "",
            warehouse_id=alert.warehouse_id,
            warehouse_code=alert.warehouse.code if alert.warehouse else "",
            warehouse_name=alert.warehouse.name if alert.warehouse else "",
            type=alert.type,
            severity=alert.severity,
            message=alert.message,
            created_at=alert.created_at,
            acknowledged_at=alert.acknowledged_at,
            resolved_at=alert.resolved_at,
            is_active=alert.is_active,
        )

    def acknowledge_all_open_alerts(self, acknowledged_by: str = "operator") -> int:
        """Acknowledge all currently open alerts and return count acknowledged."""
        now = utc_now()
        open_alerts = list(
            self.session.scalars(
                select(Alert).where(
                    Alert.is_active.is_(True),
                    Alert.acknowledged_at.is_(None),
                )
            ).all()
        )
        for alert in open_alerts:
            alert.acknowledged_at = now
            alert.is_active = False
        self.session.commit()
        logger.info("Bulk acknowledged %d open alerts by %s", len(open_alerts), acknowledged_by)
        return len(open_alerts)

    # --- Threshold Settings ---

    def get_threshold_settings(self) -> list[ThresholdSettingResponse]:
        """Return global and per-product threshold settings."""
        rows = list(
            self.session.scalars(
                select(AlertSetting)
                .options(joinedload(AlertSetting.product))
                .order_by(AlertSetting.product_id.is_not(None), AlertSetting.product_id)
            ).all()
        )
        if not any(r.product_id is None for r in rows):
            # Ensure global default record exists
            global_default = AlertSetting(
                product_id=None,
                low_stock_threshold=None,
                overstock_threshold=self.settings.default_overstock_threshold,
                slow_moving_days=self.settings.default_slow_moving_days,
                forecast_lookback_days=self.settings.default_forecast_lookback_days,
                forecast_warning_days=self.settings.default_forecast_warning_days,
                updated_at=utc_now(),
            )
            self.session.add(global_default)
            self.session.commit()
            self.session.refresh(global_default)
            rows.insert(0, global_default)

        return [
            ThresholdSettingResponse(
                id=r.id,
                product_id=r.product_id,
                sku=r.product.sku if r.product else None,
                product_name=r.product.name if r.product else "Global Default Thresholds",
                low_stock_threshold=r.low_stock_threshold,
                overstock_threshold=r.overstock_threshold,
                slow_moving_days=r.slow_moving_days,
                forecast_lookback_days=r.forecast_lookback_days,
                forecast_warning_days=r.forecast_warning_days,
                updated_at=r.updated_at,
            )
            for r in rows
        ]

    def upsert_threshold_setting(
        self,
        payload: ThresholdSettingUpdate,
    ) -> ThresholdSettingResponse:
        """Create or update global (product_id=None) or per-product threshold settings."""
        product: Product | None = None
        if payload.product_id is not None:
            product = self.session.get(Product, payload.product_id)
            if product is None:
                raise EntityNotFoundError("Product", payload.product_id)
            setting = self.session.scalar(
                select(AlertSetting).where(AlertSetting.product_id == payload.product_id)
            )
        else:
            setting = self.session.scalar(
                select(AlertSetting).where(AlertSetting.product_id.is_(None))
            )

        now = utc_now()
        if setting is None:
            setting = AlertSetting(
                product_id=payload.product_id,
                low_stock_threshold=payload.low_stock_threshold,
                overstock_threshold=payload.overstock_threshold,
                slow_moving_days=payload.slow_moving_days,
                forecast_lookback_days=payload.forecast_lookback_days,
                forecast_warning_days=payload.forecast_warning_days,
                updated_at=now,
            )
            self.session.add(setting)
        else:
            setting.low_stock_threshold = payload.low_stock_threshold
            setting.overstock_threshold = payload.overstock_threshold
            setting.slow_moving_days = payload.slow_moving_days
            setting.forecast_lookback_days = payload.forecast_lookback_days
            setting.forecast_warning_days = payload.forecast_warning_days
            setting.updated_at = now

        self.session.commit()
        self.session.refresh(setting)

        return ThresholdSettingResponse(
            id=setting.id,
            product_id=setting.product_id,
            sku=product.sku if product else None,
            product_name=product.name if product else "Global Default Thresholds",
            low_stock_threshold=setting.low_stock_threshold,
            overstock_threshold=setting.overstock_threshold,
            slow_moving_days=setting.slow_moving_days,
            forecast_lookback_days=setting.forecast_lookback_days,
            forecast_warning_days=setting.forecast_warning_days,
            updated_at=setting.updated_at,
        )

    # --- Dashboard Summary & Analytics ---

    def get_dashboard_summary(self) -> DashboardSummaryResponse:
        """Compute KPI summary metrics and chart breakdowns across warehouses and categories."""
        total_products = self.session.scalar(select(func.count(Product.id))) or 0
        total_warehouses = self.session.scalar(select(func.count(Warehouse.id))) or 0

        snapshots_with_thresholds = self.engine.build_snapshots()

        total_stock_units = 0
        total_available_units = 0
        low_stock_items = 0
        out_of_stock_items = 0
        overstock_items = 0
        anomaly_items = 0
        total_stock_value = 0.0

        wh_agg: dict[int, dict[str, Any]] = {}
        cat_agg: dict[str, dict[str, Any]] = {}
        cat_products: dict[str, set[int]] = defaultdict(set)

        for snap, thresholds in snapshots_with_thresholds:
            on_hand = max(0, snap.quantity_on_hand)
            avail = snap.available
            val = round(on_hand * snap.unit_cost, 2)

            total_stock_units += on_hand
            total_available_units += max(0, avail)
            total_stock_value += val

            status = compute_stock_status(snap, thresholds)
            if status == StockStatus.ANOMALY:
                anomaly_items += 1
            if snap.available <= 0:
                out_of_stock_items += 1
            elif snap.available <= thresholds.low_stock_threshold:
                low_stock_items += 1
            elif snap.available > thresholds.overstock_threshold:
                overstock_items += 1

            # Warehouse breakdown
            if snap.warehouse_id not in wh_agg:
                wh_agg[snap.warehouse_id] = {
                    "warehouse_id": snap.warehouse_id,
                    "warehouse_code": snap.warehouse_code,
                    "warehouse_name": snap.warehouse_name,
                    "total_on_hand": 0,
                    "total_reserved": 0,
                    "total_available": 0,
                    "total_value": 0.0,
                }
            wh_agg[snap.warehouse_id]["total_on_hand"] += on_hand
            wh_agg[snap.warehouse_id]["total_reserved"] += max(0, snap.quantity_reserved)
            wh_agg[snap.warehouse_id]["total_available"] += max(0, avail)
            wh_agg[snap.warehouse_id]["total_value"] = round(
                wh_agg[snap.warehouse_id]["total_value"] + val, 2
            )

            # Category breakdown
            cat_products[snap.category].add(snap.product_id)
            if snap.category not in cat_agg:
                cat_agg[snap.category] = {
                    "category": snap.category,
                    "product_count": 0,
                    "total_on_hand": 0,
                    "total_available": 0,
                    "total_value": 0.0,
                }
            cat_agg[snap.category]["total_on_hand"] += on_hand
            cat_agg[snap.category]["total_available"] += max(0, avail)
            cat_agg[snap.category]["total_value"] = round(
                cat_agg[snap.category]["total_value"] + val, 2
            )

        for cat_name, prod_ids in cat_products.items():
            if cat_name in cat_agg:
                cat_agg[cat_name]["product_count"] = len(prod_ids)

        open_alerts = list(
            self.session.scalars(
                select(Alert).where(
                    Alert.is_active.is_(True),
                    Alert.acknowledged_at.is_(None),
                )
            ).all()
        )
        critical_count = sum(1 for a in open_alerts if str(a.severity).lower() == "critical")
        warning_count = sum(1 for a in open_alerts if str(a.severity).lower() == "warning")

        return DashboardSummaryResponse(
            total_products=total_products,
            total_warehouses=total_warehouses,
            total_stock_units=total_stock_units,
            total_available_units=total_available_units,
            low_stock_items=low_stock_items,
            out_of_stock_items=out_of_stock_items,
            overstock_items=overstock_items,
            anomaly_items=anomaly_items,
            active_alerts_count=len(open_alerts),
            critical_alerts_count=critical_count,
            warning_alerts_count=warning_count,
            total_stock_value=round(total_stock_value, 2),
            by_warehouse=[
                WarehouseStockBreakdown(**w)
                for w in sorted(wh_agg.values(), key=lambda x: x["warehouse_code"])
            ],
            by_category=[
                CategoryStockBreakdown(**c)
                for c in sorted(cat_agg.values(), key=lambda x: x["total_on_hand"], reverse=True)
            ],
        )

    def get_stock_trend_series(
        self,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        days: int = 30,
    ) -> list[dict[str, Any]]:
        """Reconstruct daily total stock units over the requested date range from StockMovement history."""
        now = end_date or utc_now()
        start = start_date or (now - timedelta(days=days))

        movements = list(
            self.session.scalars(
                select(StockMovement)
                .where(StockMovement.timestamp <= now)
                .order_by(StockMovement.timestamp)
            ).all()
        )

        # Compute cumulative balance prior to `start`
        running_balance = 0
        daily_deltas: dict[str, int] = defaultdict(int)

        for mov in movements:
            if mov.timestamp < start:
                running_balance += int(mov.change)
            else:
                day_key = mov.timestamp.strftime("%Y-%m-%d")
                daily_deltas[day_key] += int(mov.change)

        series: list[dict[str, Any]] = []
        cursor = datetime(start.year, start.month, start.day)
        end_day = datetime(now.year, now.month, now.day)

        while cursor <= end_day:
            day_str = cursor.strftime("%Y-%m-%d")
            running_balance += daily_deltas.get(day_str, 0)
            series.append(
                {
                    "date": day_str,
                    "total_stock_units": max(0, running_balance),
                    "net_change": daily_deltas.get(day_str, 0),
                }
            )
            cursor += timedelta(days=1)

        return series

    def get_system_health(self, scheduler_running: bool | None = None) -> HealthResponse:
        """Return database connectivity, last monitoring check, last sync time, and scheduler status."""
        db_ok = True
        try:
            self.session.execute(select(1))
        except Exception:
            db_ok = False

        last_mon = self.session.get(SystemMetadata, "last_monitoring_run")
        last_sync = self.session.get(SystemMetadata, "last_source_sync")
        sched_meta = self.session.get(SystemMetadata, "scheduler_status")

        if scheduler_running is True:
            sched_status = "RUNNING"
        elif scheduler_running is False:
            sched_status = sched_meta.value if sched_meta else "STOPPED"
        else:
            sched_status = sched_meta.value if sched_meta else "READY"

        return HealthResponse(
            status="healthy" if db_ok else "degraded",
            database_connected=db_ok,
            last_monitoring_run=last_mon.value if last_mon else None,
            last_source_sync=last_sync.value if last_sync else None,
            scheduler_status=sched_status,
            timestamp=utc_now(),
            version=__version__,
        )

