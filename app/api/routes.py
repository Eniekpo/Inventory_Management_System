"""FastAPI router defining all REST API endpoints for the Inventory Monitoring System."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, File, Form, Query, Response, UploadFile, status

from app.api.deps import ApiKeyDep, DbSessionDep, InventoryServiceDep
from app.core.exceptions import ValidationFailedError
from app.models.schemas import (
    AlertAcknowledgeRequest,
    AlertResponse,
    CSVImportReport,
    DashboardSummaryResponse,
    HealthResponse,
    PaginatedResponse,
    ProductCreate,
    ProductResponse,
    ReplenishmentReport,
    StockLevelResponse,
    StockMovementCreate,
    StockMovementResponse,
    ThresholdSettingResponse,
    ThresholdSettingUpdate,
)
from app.services.importer import import_products_csv, import_stock_levels_csv
from app.services.monitoring.engine import MonitoringEngine

router = APIRouter()


@router.get(
    "/health",
    response_model=HealthResponse,
    tags=["System"],
    summary="Check database connectivity, last sync time, and scheduler status",
)
def get_health(service: InventoryServiceDep) -> HealthResponse:
    """Return system operational health, DB status, last monitoring run, and last sync time."""
    return service.get_system_health()


@router.get(
    "/products",
    response_model=PaginatedResponse[ProductResponse],
    tags=["Products"],
    summary="List products with filtering and pagination",
)
def get_products(
    _api_key: ApiKeyDep,
    service: InventoryServiceDep,
    category: str | None = Query(default=None, description="Filter by category"),
    supplier: str | None = Query(default=None, description="Filter by supplier"),
    search: str | None = Query(default=None, description="Search SKU, name, category, or supplier"),
    page: int = Query(default=1, ge=1, description="Page number (1-indexed)"),
    page_size: int = Query(default=50, ge=1, le=500, description="Items per page"),
) -> PaginatedResponse[ProductResponse]:
    """Return a paginated list of catalog products."""
    return service.list_products(
        category=category,
        supplier=supplier,
        search=search,
        page=page,
        page_size=page_size,
    )


@router.post(
    "/products",
    response_model=ProductResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["Products"],
    summary="Create a new catalog product",
)
def create_product(
    _api_key: ApiKeyDep,
    service: InventoryServiceDep,
    payload: ProductCreate,
) -> ProductResponse:
    """Create a new product and initialize stock records across warehouses."""
    return service.create_product(payload)


@router.get(
    "/stock",
    response_model=PaginatedResponse[StockLevelResponse],
    tags=["Stock"],
    summary="List warehouse stock levels filtered by warehouse, category, or status",
)
def get_stock(
    _api_key: ApiKeyDep,
    service: InventoryServiceDep,
    warehouse: str | None = Query(default=None, description="Warehouse code, ID, or name"),
    category: str | None = Query(default=None, description="Product category"),
    status: str | None = Query(
        default=None,
        description="Stock status (HEALTHY, LOW_STOCK, OUT_OF_STOCK, OVERSTOCK, ANOMALY)",
    ),
    search: str | None = Query(default=None, description="Search SKU, name, or supplier"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
) -> PaginatedResponse[StockLevelResponse]:
    """Return enriched warehouse stock levels with computed operational status and forecast."""
    return service.list_stock_levels(
        warehouse=warehouse,
        category=category,
        status=status,
        search=search,
        page=page,
        page_size=page_size,
    )


@router.post(
    "/stock/movements",
    response_model=StockMovementResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["Stock"],
    summary="Record an inbound or outbound stock movement",
)
def create_stock_movement(
    _api_key: ApiKeyDep,
    service: InventoryServiceDep,
    payload: StockMovementCreate,
) -> StockMovementResponse:
    """Apply a stock movement, update StockLevel, and run real-time alert evaluation."""
    return service.record_stock_movement(payload, trigger_monitoring=True)


@router.get(
    "/stock/movements",
    response_model=PaginatedResponse[StockMovementResponse],
    tags=["Stock"],
    summary="List historical stock movements",
)
def get_stock_movements(
    _api_key: ApiKeyDep,
    service: InventoryServiceDep,
    product_id: int | None = Query(default=None, ge=1),
    warehouse_id: int | None = Query(default=None, ge=1),
    reason: str | None = Query(default=None),
    days: int | None = Query(default=None, ge=1, le=365),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
) -> PaginatedResponse[StockMovementResponse]:
    """Return paginated stock movement ledger entries."""
    return service.list_stock_movements(
        product_id=product_id,
        warehouse_id=warehouse_id,
        reason=reason,
        days=days,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/alerts",
    response_model=PaginatedResponse[AlertResponse],
    tags=["Alerts"],
    summary="List inventory alerts with filtering and pagination",
)
def get_alerts(
    _api_key: ApiKeyDep,
    service: InventoryServiceDep,
    severity: str | None = Query(default=None, description="Filter by severity (critical, warning, info)"),
    type: str | None = Query(default=None, description="Filter by AlertType"),
    warehouse_id: int | None = Query(default=None, ge=1),
    active_only: bool | None = Query(default=None, description="True for open alerts only"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
) -> PaginatedResponse[AlertResponse]:
    """Return paginated inventory alerts."""
    return service.list_alerts(
        severity=severity,
        alert_type=type,
        warehouse_id=warehouse_id,
        active_only=active_only,
        page=page,
        page_size=page_size,
    )


@router.post(
    "/alerts/{alert_id}/acknowledge",
    response_model=AlertResponse,
    tags=["Alerts"],
    summary="Acknowledge an open alert by ID",
)
def acknowledge_alert(
    alert_id: int,
    _api_key: ApiKeyDep,
    service: InventoryServiceDep,
    payload: AlertAcknowledgeRequest | None = None,
) -> AlertResponse:
    """Acknowledge a specific alert and mark it inactive."""
    user = payload.acknowledged_by if payload else "api_operator"
    return service.acknowledge_alert(alert_id=alert_id, acknowledged_by=user)


@router.get(
    "/settings/thresholds",
    response_model=list[ThresholdSettingResponse],
    tags=["Settings"],
    summary="Get global and per-product alert thresholds",
)
def get_thresholds(
    _api_key: ApiKeyDep,
    service: InventoryServiceDep,
) -> list[ThresholdSettingResponse]:
    """Return all configured alert threshold settings (global and per-product overrides)."""
    return service.get_threshold_settings()


@router.put(
    "/settings/thresholds",
    response_model=ThresholdSettingResponse,
    tags=["Settings"],
    summary="Update global or per-product alert thresholds",
)
def update_thresholds(
    _api_key: ApiKeyDep,
    service: InventoryServiceDep,
    payload: ThresholdSettingUpdate,
) -> ThresholdSettingResponse:
    """Create or update global (product_id=null) or per-product alert thresholds."""
    return service.upsert_threshold_setting(payload)


@router.get(
    "/summary",
    response_model=DashboardSummaryResponse,
    tags=["Analytics"],
    summary="Get dashboard KPI summary and stock breakdowns",
)
def get_summary(
    _api_key: ApiKeyDep,
    service: InventoryServiceDep,
) -> DashboardSummaryResponse:
    """Return top-level inventory KPIs and breakdowns by warehouse and category."""
    return service.get_dashboard_summary()


@router.get(
    "/replenishment",
    response_model=ReplenishmentReport,
    tags=["Replenishment"],
    summary="Get replenishment recommendations grouped by supplier (JSON or CSV)",
)
def get_replenishment(
    _api_key: ApiKeyDep,
    session: DbSessionDep,
    format: Literal["json", "csv"] = Query(
        default="json", description="Response format: 'json' or 'csv'"
    ),
) -> ReplenishmentReport | Response:
    """Generate replenishment suggestions grouped by supplier, optionally exported as CSV."""
    engine = MonitoringEngine(session)
    if format == "csv":
        csv_text = engine.export_replenishment_to_csv()
        return Response(
            content=csv_text,
            media_type="text/csv",
            headers={
                "Content-Disposition": "attachment; filename=replenishment_suggestions.csv"
            },
        )
    return engine.generate_replenishment_report()


@router.post(
    "/import/csv",
    response_model=CSVImportReport,
    tags=["Import"],
    summary="Upload and validate a CSV file of products or stock levels",
)
async def import_csv_endpoint(
    _api_key: ApiKeyDep,
    session: DbSessionDep,
    file: UploadFile = File(..., description="CSV file to import"),
    import_type: Literal["products", "stock_levels"] = Form(
        default="products",
        description="Target entity type: 'products' or 'stock_levels'",
    ),
) -> CSVImportReport:
    """Validate and import a CSV file of products or warehouse stock levels."""
    raw_bytes = await file.read()
    if not raw_bytes:
        raise ValidationFailedError("Uploaded CSV file is empty.")

    if import_type == "products":
        report = import_products_csv(session, raw_bytes)
    else:
        report = import_stock_levels_csv(session, raw_bytes, user="api_csv_import")
        if report.imported_count > 0 or report.updated_count > 0:
            MonitoringEngine(session).run_check()

    return report

