"""Pydantic v2 request, response, and validation schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.enums import AlertSeverity, AlertType, MovementReason, StockStatus

T = TypeVar("T")


class PaginatedResponse(BaseModel, Generic[T]):
    """Generic paginated response envelope."""

    items: list[T]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1)
    total_pages: int = Field(ge=0)


# --- Product Schemas ---


class ProductBase(BaseModel):
    """Shared fields for Product validation."""

    sku: str = Field(..., min_length=1, max_length=64, description="Unique product SKU")
    name: str = Field(..., min_length=1, max_length=255, description="Product display name")
    category: str = Field(..., min_length=1, max_length=120, description="Product category")
    unit_cost: float = Field(..., ge=0.0, description="Cost per unit in USD")
    reorder_point: int = Field(..., ge=0, description="Minimum available stock before reorder")
    reorder_quantity: int = Field(..., gt=0, description="Standard order quantity when restocking")
    supplier: str = Field(..., min_length=1, max_length=255, description="Primary supplier name")

    @field_validator("sku", "name", "category", "supplier", mode="before")
    @classmethod
    def strip_non_empty(cls, value: Any) -> str:
        """Strip whitespace and reject blank strings."""
        if value is None:
            raise ValueError("Field cannot be null")
        cleaned = str(value).strip()
        if not cleaned:
            raise ValueError("Field cannot be empty or whitespace only")
        return cleaned


class ProductCreate(ProductBase):
    """Schema for creating or importing a product."""


class ProductResponse(ProductBase):
    """Schema for returning a product from the API."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    updated_at: datetime


# --- Warehouse Schemas ---


class WarehouseBase(BaseModel):
    """Shared fields for Warehouse validation."""

    code: str = Field(..., min_length=1, max_length=32)
    name: str = Field(..., min_length=1, max_length=255)

    @field_validator("code", "name", mode="before")
    @classmethod
    def strip_warehouse_str(cls, value: Any) -> str:
        """Strip and validate non-empty string."""
        cleaned = str(value or "").strip()
        if not cleaned:
            raise ValueError("Warehouse code and name cannot be blank")
        return cleaned


class WarehouseCreate(WarehouseBase):
    """Schema for creating a warehouse."""


class WarehouseResponse(WarehouseBase):
    """Schema for returning a warehouse."""

    model_config = ConfigDict(from_attributes=True)

    id: int


# --- Stock Level Schemas ---


class StockCSVRow(BaseModel):
    """Validation schema for a single row in a stock-level CSV import."""

    sku: str = Field(..., min_length=1, max_length=64)
    warehouse_code: str = Field(..., min_length=1, max_length=32)
    quantity_on_hand: int = Field(...)
    quantity_reserved: int = Field(default=0, ge=0)

    @field_validator("sku", "warehouse_code", mode="before")
    @classmethod
    def clean_codes(cls, value: Any) -> str:
        """Strip codes and reject empty values."""
        cleaned = str(value or "").strip()
        if not cleaned:
            raise ValueError("Code cannot be empty")
        return cleaned


class StockLevelResponse(BaseModel):
    """Enriched stock level response including product, warehouse, and computed status."""

    model_config = ConfigDict(from_attributes=True)

    id: int
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
    available: int
    stock_value: float
    status: StockStatus
    days_of_stock_remaining: float | None = None
    updated_at: datetime


# --- Stock Movement Schemas ---


class StockMovementCreate(BaseModel):
    """Request schema for recording a stock movement."""

    product_id: int | None = Field(default=None, ge=1)
    sku: str | None = Field(default=None)
    warehouse_id: int | None = Field(default=None, ge=1)
    warehouse_code: str | None = Field(default=None)
    change: int = Field(..., description="Non-zero integer quantity delta (+inbound, -outbound)")
    reason: str = Field(default=MovementReason.ADJUSTMENT.value, min_length=1, max_length=64)
    user: str = Field(default="api_user", min_length=1, max_length=120)

    @field_validator("change")
    @classmethod
    def validate_non_zero_change(cls, value: int) -> int:
        """Reject zero-quantity stock movements."""
        if value == 0:
            raise ValueError("Movement change quantity cannot be 0")
        return value


class StockMovementResponse(BaseModel):
    """Response schema for a recorded stock movement."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    product_id: int
    sku: str
    product_name: str
    warehouse_id: int
    warehouse_code: str
    warehouse_name: str
    change: int
    reason: str
    timestamp: datetime
    user: str
    new_quantity_on_hand: int | None = None
    new_available: int | None = None


# --- Alert Schemas ---


class AlertResponse(BaseModel):
    """Response schema for an inventory alert."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    product_id: int
    sku: str
    product_name: str
    category: str
    warehouse_id: int
    warehouse_code: str
    warehouse_name: str
    type: AlertType | str
    severity: AlertSeverity | str
    message: str
    created_at: datetime
    acknowledged_at: datetime | None = None
    resolved_at: datetime | None = None
    is_active: bool


class AlertAcknowledgeRequest(BaseModel):
    """Optional note/user when acknowledging an alert."""

    acknowledged_by: str = Field(default="operator", min_length=1, max_length=120)


# --- Threshold Settings Schemas ---


class ThresholdSettingUpdate(BaseModel):
    """Request payload for updating global or per-product alert thresholds."""

    product_id: int | None = Field(
        default=None, description="Product ID for override, or null for global defaults"
    )
    low_stock_threshold: int | None = Field(default=None, ge=0)
    overstock_threshold: int = Field(..., gt=0)
    slow_moving_days: int = Field(..., gt=0)
    forecast_lookback_days: int = Field(default=30, gt=0)
    forecast_warning_days: int = Field(default=7, gt=0)


class ThresholdSettingResponse(BaseModel):
    """Response schema for global or per-product alert threshold settings."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    product_id: int | None = None
    sku: str | None = None
    product_name: str | None = None
    low_stock_threshold: int | None = None
    overstock_threshold: int
    slow_moving_days: int
    forecast_lookback_days: int
    forecast_warning_days: int
    updated_at: datetime


# --- CSV Import Report Schemas ---


class CSVRowError(BaseModel):
    """Detailed validation error for a single rejected CSV row."""

    row_number: int
    raw_data: dict[str, Any]
    field: str
    error_message: str


class CSVImportReport(BaseModel):
    """Structured report summarizing a CSV import operation."""

    import_type: str
    total_rows: int
    imported_count: int
    updated_count: int
    failed_count: int
    errors: list[CSVRowError] = Field(default_factory=list)

    def format_text_report(self) -> str:
        """Render a clean human-readable summary and row error breakdown."""
        lines = [
            f"=== CSV Import Report ({self.import_type}) ===",
            f"Total Rows Processed : {self.total_rows}",
            f"Created              : {self.imported_count}",
            f"Updated              : {self.updated_count}",
            f"Failed (Rejected)    : {self.failed_count}",
        ]
        if self.errors:
            lines.append("\nRow-Level Validation Errors:")
            for err in self.errors:
                lines.append(
                    f"  - Row {err.row_number} [{err.field}]: {err.error_message} "
                    f"| Data={err.raw_data}"
                )
        else:
            lines.append("All rows passed validation with 0 errors.")
        return "\n".join(lines)


# --- Replenishment Schemas ---


class ReplenishmentItem(BaseModel):
    """Single product replenishment recommendation at a specific warehouse."""

    product_id: int
    sku: str
    product_name: str
    category: str
    supplier: str
    warehouse_id: int
    warehouse_code: str
    warehouse_name: str
    available: int
    reorder_point: int
    reorder_quantity: int
    avg_daily_consumption: float
    days_of_stock_remaining: float | None = None
    suggested_order_qty: int
    unit_cost: float
    estimated_cost: float
    urgency: str


class SupplierReplenishmentGroup(BaseModel):
    """Replenishment recommendations aggregated by supplier."""

    supplier: str
    total_items: int
    total_units_to_order: int
    total_estimated_cost: float
    items: list[ReplenishmentItem]


class ReplenishmentReport(BaseModel):
    """Complete replenishment report across all suppliers."""

    generated_at: datetime
    total_suppliers: int
    total_line_items: int
    total_units_to_order: int
    total_estimated_cost: float
    supplier_groups: list[SupplierReplenishmentGroup]


# --- Dashboard Summary & Health Schemas ---


class WarehouseStockBreakdown(BaseModel):
    """Aggregated stock metrics for a single warehouse."""

    warehouse_id: int
    warehouse_code: str
    warehouse_name: str
    total_on_hand: int
    total_reserved: int
    total_available: int
    total_value: float


class CategoryStockBreakdown(BaseModel):
    """Aggregated stock metrics for a single product category."""

    category: str
    product_count: int
    total_on_hand: int
    total_available: int
    total_value: float


class DashboardSummaryResponse(BaseModel):
    """Top-level KPIs and chart breakdowns for the dashboard."""

    total_products: int
    total_warehouses: int
    total_stock_units: int
    total_available_units: int
    low_stock_items: int
    out_of_stock_items: int
    overstock_items: int
    anomaly_items: int
    active_alerts_count: int
    critical_alerts_count: int
    warning_alerts_count: int
    total_stock_value: float
    by_warehouse: list[WarehouseStockBreakdown]
    by_category: list[CategoryStockBreakdown]


class HealthResponse(BaseModel):
    """System health check response."""

    status: str
    database_connected: bool
    last_monitoring_run: str | None = None
    last_source_sync: str | None = None
    scheduler_status: str
    timestamp: datetime
    version: str

