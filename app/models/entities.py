"""SQLAlchemy 2.x ORM entity models for the Inventory Monitoring System."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def utc_now() -> datetime:
    """Return current timezone-naive UTC datetime for consistent SQLite/Postgres storage."""
    return datetime.now(UTC).replace(tzinfo=None)


class Product(Base):
    """Catalog product tracked across warehouses."""

    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sku: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    unit_cost: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    reorder_point: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    reorder_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=50)
    supplier: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now, onupdate=utc_now, nullable=False
    )

    stock_levels: Mapped[list[StockLevel]] = relationship(
        "StockLevel", back_populates="product", cascade="all, delete-orphan"
    )
    movements: Mapped[list[StockMovement]] = relationship(
        "StockMovement", back_populates="product", cascade="all, delete-orphan"
    )
    alerts: Mapped[list[Alert]] = relationship(
        "Alert", back_populates="product", cascade="all, delete-orphan"
    )
    alert_setting: Mapped[AlertSetting | None] = relationship(
        "AlertSetting", back_populates="product", uselist=False, cascade="all, delete-orphan"
    )


class Warehouse(Base):
    """Physical or logical storage location holding product inventory."""

    __tablename__ = "warehouses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    stock_levels: Mapped[list[StockLevel]] = relationship(
        "StockLevel", back_populates="warehouse", cascade="all, delete-orphan"
    )
    movements: Mapped[list[StockMovement]] = relationship(
        "StockMovement", back_populates="warehouse", cascade="all, delete-orphan"
    )
    alerts: Mapped[list[Alert]] = relationship(
        "Alert", back_populates="warehouse", cascade="all, delete-orphan"
    )


class StockLevel(Base):
    """Current stock balances for a specific product at a specific warehouse."""

    __tablename__ = "stock_levels"
    __table_args__ = (
        UniqueConstraint("product_id", "warehouse_id", name="uq_stock_product_warehouse"),
        Index("ix_stock_product_warehouse", "product_id", "warehouse_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    warehouse_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("warehouses.id", ondelete="CASCADE"), nullable=False
    )
    quantity_on_hand: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    quantity_reserved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now, onupdate=utc_now, nullable=False
    )

    product: Mapped[Product] = relationship("Product", back_populates="stock_levels")
    warehouse: Mapped[Warehouse] = relationship("Warehouse", back_populates="stock_levels")

    @hybrid_property
    def available(self) -> int:
        """Return net available units (quantity_on_hand - quantity_reserved)."""
        return int(self.quantity_on_hand) - int(self.quantity_reserved)

    @available.inplace.expression
    @classmethod
    def _available_expr(cls):
        """Return SQL expression for available units."""
        return cls.quantity_on_hand - cls.quantity_reserved


class StockMovement(Base):
    """Immutable ledger entry recording an inbound or outbound stock change."""

    __tablename__ = "stock_movements"
    __table_args__ = (
        Index("ix_movements_product_warehouse_ts", "product_id", "warehouse_id", "timestamp"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    warehouse_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("warehouses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    change: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now, nullable=False, index=True
    )
    user: Mapped[str] = mapped_column(String(120), nullable=False, default="system")

    product: Mapped[Product] = relationship("Product", back_populates="movements")
    warehouse: Mapped[Warehouse] = relationship("Warehouse", back_populates="movements")


class Alert(Base):
    """Monitoring alert generated for a product and warehouse."""

    __tablename__ = "alerts"
    __table_args__ = (
        Index("ix_alerts_lookup", "product_id", "warehouse_id", "type", "is_active"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    warehouse_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("warehouses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now, nullable=False, index=True
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)

    product: Mapped[Product] = relationship("Product", back_populates="alerts")
    warehouse: Mapped[Warehouse] = relationship("Warehouse", back_populates="alerts")


class AlertSetting(Base):
    """Per-product or global (product_id=None) monitoring thresholds."""

    __tablename__ = "alert_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    product_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("products.id", ondelete="CASCADE"),
        nullable=True,
        unique=True,
        index=True,
    )
    low_stock_threshold: Mapped[int | None] = mapped_column(Integer, nullable=True)
    overstock_threshold: Mapped[int] = mapped_column(Integer, nullable=False, default=500)
    slow_moving_days: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    forecast_lookback_days: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    forecast_warning_days: Mapped[int] = mapped_column(Integer, nullable=False, default=7)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now, onupdate=utc_now, nullable=False
    )

    product: Mapped[Product | None] = relationship("Product", back_populates="alert_setting")


class NotificationLog(Base):
    """Audit trail of notification delivery attempts across channels."""

    __tablename__ = "notification_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    alert_key: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now, nullable=False, index=True
    )


class SystemMetadata(Base):
    """Key-value store for system operational state (last sync, scheduler status, etc.)."""

    __tablename__ = "system_metadata"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now, onupdate=utc_now, nullable=False
    )

