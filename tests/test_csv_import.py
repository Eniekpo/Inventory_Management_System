"""Unit tests for CSV import validation and error reporting."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.services.importer import import_products_csv, import_stock_levels_csv


class TestProductsCSVImport:
    """Tests for product catalog CSV validation and upsert logic."""

    def test_valid_and_invalid_product_rows(
        self,
        db_session: Session,
        sample_entities: dict[str, object],
    ) -> None:
        csv_content = "\n".join(
            [
                "sku,name,category,unit_cost,reorder_point,reorder_quantity,supplier",
                "SKU-001,Updated Precision Sensor,Electronics,55.00,12,45,Apex Components Ltd",
                "SKU-003,New Barcode Scanner,Electronics,110.00,15,50,GlobalTech",
                ",Missing SKU Row,Electronics,20.00,5,10,GlobalTech",
                "SKU-004,Negative Cost Row,Electronics,-5.00,5,10,GlobalTech",
                "SKU-005,Zero Reorder Qty Row,Electronics,15.00,5,0,GlobalTech",
                "SKU-003,Duplicate SKU in Batch,Electronics,99.00,10,20,GlobalTech",
            ]
        )

        report = import_products_csv(db_session, csv_content)

        assert report.total_rows == 6
        assert report.imported_count == 1  # SKU-003
        assert report.updated_count == 1   # SKU-001
        assert report.failed_count == 4
        assert len(report.errors) == 4

        formatted = report.format_text_report()
        assert "Failed (Rejected)    : 4" in formatted
        assert "Row 4 [sku]" in formatted
        assert "Row 5 [unit_cost]" in formatted

    def test_missing_required_headers(self, db_session: Session) -> None:
        bad_header_csv = "sku,name,category\nSKU-100,Only Three Cols,Electronics"
        report = import_products_csv(db_session, bad_header_csv)
        assert report.failed_count == 1
        assert report.errors[0].field == "header"
        assert "Missing required columns" in report.errors[0].error_message


class TestStockLevelsCSVImport:
    """Tests for warehouse stock level CSV validation and movement tracking."""

    def test_stock_csv_validation_and_unknown_references(
        self,
        db_session: Session,
        sample_entities: dict[str, object],
    ) -> None:
        csv_content = "\n".join(
            [
                "sku,warehouse_code,quantity_on_hand,quantity_reserved",
                "SKU-001,WH-EAST,80,10",
                "SKU-001,WH-WEST,45,5",
                "UNKNOWN-SKU,WH-EAST,20,0",
                "SKU-001,UNKNOWN-WH,20,0",
                "SKU-002,WH-EAST,-5,0",
                "SKU-002,WH-EAST,10,25",
            ]
        )

        report = import_stock_levels_csv(db_session, csv_content)

        assert report.total_rows == 6
        assert report.imported_count == 1  # SKU-001 @ WH-WEST
        assert report.updated_count == 1   # SKU-001 @ WH-EAST
        assert report.failed_count == 4
        error_fields = [e.field for e in report.errors]
        assert "sku" in error_fields
        assert "warehouse_code" in error_fields
        assert "quantity_on_hand" in error_fields
        assert "quantity_reserved" in error_fields

