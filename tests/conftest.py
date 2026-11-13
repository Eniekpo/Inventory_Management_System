"""Pytest fixtures providing isolated in-memory SQLite sessions and FastAPI TestClients."""

from __future__ import annotations

from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import get_settings
from app.core.database import Base, get_db
from app.core.security import rate_limiter
from app.main import create_app
from app.models.entities import AlertSetting, Product, StockLevel, Warehouse, utc_now


@pytest.fixture()
def db_session() -> Generator[Session, None, None]:
    """Yield an isolated in-memory SQLite session with all tables created."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _enable_fk(dbapi_connection, _record) -> None:  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
    )
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


@pytest.fixture()
def sample_entities(db_session: Session) -> dict[str, object]:
    """Create baseline warehouses, products, and global AlertSetting for unit tests."""
    now = utc_now()
    wh_east = Warehouse(code="WH-EAST", name="East Warehouse")
    wh_west = Warehouse(code="WH-WEST", name="West Warehouse")
    db_session.add_all([wh_east, wh_west])
    db_session.flush()

    prod_a = Product(
        sku="SKU-001",
        name="Precision Sensor",
        category="Electronics",
        unit_cost=50.0,
        reorder_point=10,
        reorder_quantity=40,
        supplier="Apex Components Ltd",
        created_at=now,
        updated_at=now,
    )
    prod_b = Product(
        sku="SKU-002",
        name="Heavy Bolt Pack",
        category="Industrial Hardware",
        unit_cost=12.5,
        reorder_point=20,
        reorder_quantity=80,
        supplier="PrimeLine Industrial",
        created_at=now,
        updated_at=now,
    )
    db_session.add_all([prod_a, prod_b])
    db_session.flush()

    global_setting = AlertSetting(
        product_id=None,
        low_stock_threshold=None,
        overstock_threshold=500,
        slow_moving_days=30,
        forecast_lookback_days=30,
        forecast_warning_days=7,
        updated_at=now,
    )
    db_session.add(global_setting)

    sl_a = StockLevel(
        product_id=prod_a.id,
        warehouse_id=wh_east.id,
        quantity_on_hand=35,
        quantity_reserved=5,
        updated_at=now,
    )
    sl_b = StockLevel(
        product_id=prod_b.id,
        warehouse_id=wh_east.id,
        quantity_on_hand=15,
        quantity_reserved=0,
        updated_at=now,
    )
    db_session.add_all([sl_a, sl_b])
    db_session.commit()

    return {
        "wh_east": wh_east,
        "wh_west": wh_west,
        "prod_a": prod_a,
        "prod_b": prod_b,
        "sl_a": sl_a,
        "sl_b": sl_b,
    }


@pytest.fixture()
def api_client(db_session: Session) -> Generator[TestClient, None, None]:
    """Provide a FastAPI TestClient wired to the isolated test db_session."""
    rate_limiter.reset()
    rate_limiter.max_requests = 120
    application = create_app()

    def _override_get_db() -> Generator[Session, None, None]:
        yield db_session

    application.dependency_overrides[get_db] = _override_get_db
    with TestClient(application) as client:
        yield client
    application.dependency_overrides.clear()
    rate_limiter.reset()


@pytest.fixture()
def api_headers() -> dict[str, str]:
    """Return valid X-API-Key authentication headers."""
    return {"X-API-Key": get_settings().api_key}
