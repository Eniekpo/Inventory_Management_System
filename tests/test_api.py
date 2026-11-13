"""Integration tests for FastAPI REST endpoints, auth, rate limiting, and CSV upload."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.security import rate_limiter


class TestAuthenticationAndRateLimiting:
    """Tests for X-API-Key authentication and sliding-window rate limiting."""

    def test_health_endpoint_is_accessible(
        self,
        api_client: TestClient,
        sample_entities: dict[str, object],
    ) -> None:
        resp = api_client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "healthy"
        assert body["database_connected"] is True

    def test_missing_or_invalid_api_key_returns_401(
        self,
        api_client: TestClient,
        sample_entities: dict[str, object],
    ) -> None:
        assert api_client.get("/products").status_code == 401
        assert (
            api_client.get("/products", headers={"X-API-Key": "wrong-key"}).status_code
            == 401
        )

    def test_rate_limiter_enforces_429_when_exceeded(
        self,
        api_client: TestClient,
        api_headers: dict[str, str],
        sample_entities: dict[str, object],
    ) -> None:
        rate_limiter.reset()
        rate_limiter.max_requests = 3

        for _ in range(3):
            assert api_client.get("/products", headers=api_headers).status_code == 200

        blocked = api_client.get("/products", headers=api_headers)
        assert blocked.status_code == 429
        assert "Rate limit" in blocked.json()["detail"]


class TestProductsStockAndMovementsAPI:
    """Tests for /products, /stock, and /stock/movements endpoints."""

    def test_list_and_create_products(
        self,
        api_client: TestClient,
        api_headers: dict[str, str],
        sample_entities: dict[str, object],
    ) -> None:
        resp = api_client.get("/products?category=Electronics", headers=api_headers)
        assert resp.status_code == 200
        assert resp.json()["total"] == 1

        new_prod = {
            "sku": "SKU-NEW-10",
            "name": "Smart Torque Sensor",
            "category": "Electronics",
            "unit_cost": 140.0,
            "reorder_point": 8,
            "reorder_quantity": 25,
            "supplier": "Apex Components Ltd",
        }
        create_resp = api_client.post("/products", json=new_prod, headers=api_headers)
        assert create_resp.status_code == 201
        assert create_resp.json()["sku"] == "SKU-NEW-10"

        # Duplicate SKU returns 409
        dup_resp = api_client.post("/products", json=new_prod, headers=api_headers)
        assert dup_resp.status_code == 409

    def test_filter_stock_and_record_movement_with_alert_lifecycle(
        self,
        api_client: TestClient,
        api_headers: dict[str, str],
        sample_entities: dict[str, object],
    ) -> None:
        # SKU-002 starts at 15 available (reorder_point=20 -> LOW_STOCK)
        low_resp = api_client.get(
            "/stock?warehouse=WH-EAST&status=LOW_STOCK",
            headers=api_headers,
        )
        assert low_resp.status_code == 200
        assert low_resp.json()["total"] == 1
        assert low_resp.json()["items"][0]["sku"] == "SKU-002"

        # Record outbound movement of -15 on SKU-002 -> drops available to 0 (OUT_OF_STOCK)
        mov_resp = api_client.post(
            "/stock/movements",
            json={
                "sku": "SKU-002",
                "warehouse_code": "WH-EAST",
                "change": -15,
                "reason": "SALE",
                "user": "pytest_user",
            },
            headers=api_headers,
        )
        assert mov_resp.status_code == 201
        assert mov_resp.json()["new_available"] == 0

        # Verify OUT_OF_STOCK alert was automatically generated
        alerts_resp = api_client.get(
            "/alerts?type=OUT_OF_STOCK&active_only=true",
            headers=api_headers,
        )
        assert alerts_resp.status_code == 200
        assert alerts_resp.json()["total"] >= 1
        alert_id = alerts_resp.json()["items"][0]["id"]

        # Acknowledge the alert via POST /alerts/{id}/acknowledge
        ack_resp = api_client.post(
            f"/alerts/{alert_id}/acknowledge",
            json={"acknowledged_by": "qa_engineer"},
            headers=api_headers,
        )
        assert ack_resp.status_code == 200
        assert ack_resp.json()["is_active"] is False
        assert ack_resp.json()["acknowledged_at"] is not None


class TestSettingsSummaryReplenishmentAndImportAPI:
    """Tests for /settings/thresholds, /summary, /replenishment, and /import/csv."""

    def test_get_and_put_thresholds(
        self,
        api_client: TestClient,
        api_headers: dict[str, str],
        sample_entities: dict[str, object],
    ) -> None:
        get_resp = api_client.get("/settings/thresholds", headers=api_headers)
        assert get_resp.status_code == 200
        assert len(get_resp.json()) >= 1

        put_resp = api_client.put(
            "/settings/thresholds",
            json={
                "product_id": None,
                "low_stock_threshold": 18,
                "overstock_threshold": 600,
                "slow_moving_days": 45,
                "forecast_lookback_days": 30,
                "forecast_warning_days": 10,
            },
            headers=api_headers,
        )
        assert put_resp.status_code == 200
        updated = put_resp.json()
        assert updated["overstock_threshold"] == 600
        assert updated["slow_moving_days"] == 45

    def test_summary_and_replenishment_endpoints(
        self,
        api_client: TestClient,
        api_headers: dict[str, str],
        sample_entities: dict[str, object],
    ) -> None:
        sum_resp = api_client.get("/summary", headers=api_headers)
        assert sum_resp.status_code == 200
        kpi = sum_resp.json()
        assert kpi["total_products"] == 2
        assert kpi["total_stock_units"] == 50

        rep_json = api_client.get("/replenishment", headers=api_headers)
        assert rep_json.status_code == 200
        assert rep_json.json()["total_line_items"] >= 1

        rep_csv = api_client.get("/replenishment?format=csv", headers=api_headers)
        assert rep_csv.status_code == 200
        assert "supplier,urgency,sku" in rep_csv.text

    def test_import_csv_endpoint(
        self,
        api_client: TestClient,
        api_headers: dict[str, str],
        sample_entities: dict[str, object],
    ) -> None:
        csv_bytes = (
            b"sku,name,category,unit_cost,reorder_point,reorder_quantity,supplier\n"
            b"SKU-CSV-99,Uploaded Item,Office Supplies,14.50,10,40,Metro Office Solutions\n"
        )
        resp = api_client.post(
            "/import/csv",
            files={"file": ("products.csv", csv_bytes, "text/csv")},
            data={"import_type": "products"},
            headers=api_headers,
        )
        assert resp.status_code == 200
        report = resp.json()
        assert report["imported_count"] == 1
        assert report["failed_count"] == 0

