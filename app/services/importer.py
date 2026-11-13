"""CSV import service for products and stock levels with row-level validation."""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging_config import get_logger
from app.models.entities import Product, StockLevel, StockMovement, Warehouse, utc_now
from app.models.enums import MovementReason
from app.models.schemas import CSVImportReport, CSVRowError, ProductCreate, StockCSVRow

logger = get_logger("importer")

REQUIRED_PRODUCT_COLUMNS = {
    "sku",
    "name",
    "category",
    "unit_cost",
    "reorder_point",
    "reorder_quantity",
    "supplier",
}

REQUIRED_STOCK_COLUMNS = {
    "sku",
    "warehouse_code",
    "quantity_on_hand",
}


def _read_csv_text(source: str | bytes | Path) -> str:
    """Resolve CSV input from a Path, raw bytes, or CSV text string."""
    if isinstance(source, Path):
        return source.read_text(encoding="utf-8-sig")
    if isinstance(source, bytes):
        return source.decode("utf-8-sig")
    # If string looks like an existing file path and has no newlines, read file
    if "\n" not in source and len(source) < 512:
        potential_path = Path(source)
        if potential_path.is_file():
            return potential_path.read_text(encoding="utf-8-sig")
    return source


def _extract_validation_error(exc: ValidationError) -> tuple[str, str]:
    """Extract the first field name and human-readable message from Pydantic ValidationError."""
    errors = exc.errors()
    if not errors:
        return ("unknown", str(exc))
    first = errors[0]
    loc = ".".join(str(part) for part in first.get("loc", ("row",)))
    msg = first.get("msg", "Invalid value")
    return (loc, msg)


def import_products_csv(
    session: Session,
    csv_source: str | bytes | Path,
) -> CSVImportReport:
    """Import products from CSV content or file path with row-by-row validation.

    Valid rows are upserted by SKU; invalid rows are rejected and detailed in the
    returned CSVImportReport.
    """
    text = _read_csv_text(csv_source)
    reader = csv.DictReader(io.StringIO(text.strip()))

    if not reader.fieldnames:
        return CSVImportReport(
            import_type="products",
            total_rows=0,
            imported_count=0,
            updated_count=0,
            failed_count=1,
            errors=[
                CSVRowError(
                    row_number=1,
                    raw_data={},
                    field="header",
                    error_message="CSV file is empty or missing a header row.",
                )
            ],
        )

    normalized_headers = {h.strip().lower() for h in reader.fieldnames if h}
    missing_cols = REQUIRED_PRODUCT_COLUMNS - normalized_headers
    if missing_cols:
        return CSVImportReport(
            import_type="products",
            total_rows=0,
            imported_count=0,
            updated_count=0,
            failed_count=1,
            errors=[
                CSVRowError(
                    row_number=1,
                    raw_data={"headers": list(reader.fieldnames)},
                    field="header",
                    error_message=f"Missing required columns: {sorted(missing_cols)}",
                )
            ],
        )

    total_rows = 0
    imported_count = 0
    updated_count = 0
    failed_count = 0
    errors: list[CSVRowError] = []
    seen_skus_in_batch: set[str] = set()

    for row_number, raw_row in enumerate(reader, start=2):
        # Skip completely empty trailing lines
        if not raw_row or all((v is None or str(v).strip() == "") for v in raw_row.values()):
            continue

        total_rows += 1
        clean_row: dict[str, Any] = {
            (k.strip().lower() if k else ""): (v.strip() if isinstance(v, str) else v)
            for k, v in raw_row.items()
            if k is not None
        }

        try:
            validated = ProductCreate.model_validate(clean_row)
        except ValidationError as exc:
            field_name, err_msg = _extract_validation_error(exc)
            failed_count += 1
            errors.append(
                CSVRowError(
                    row_number=row_number,
                    raw_data=clean_row,
                    field=field_name,
                    error_message=err_msg,
                )
            )
            continue

        if validated.sku in seen_skus_in_batch:
            failed_count += 1
            errors.append(
                CSVRowError(
                    row_number=row_number,
                    raw_data=clean_row,
                    field="sku",
                    error_message=f"Duplicate SKU '{validated.sku}' within the same CSV file.",
                )
            )
            continue
        seen_skus_in_batch.add(validated.sku)

        existing = session.scalar(select(Product).where(Product.sku == validated.sku))
        if existing is not None:
            existing.name = validated.name
            existing.category = validated.category
            existing.unit_cost = validated.unit_cost
            existing.reorder_point = validated.reorder_point
            existing.reorder_quantity = validated.reorder_quantity
            existing.supplier = validated.supplier
            existing.updated_at = utc_now()
            updated_count += 1
        else:
            new_product = Product(
                sku=validated.sku,
                name=validated.name,
                category=validated.category,
                unit_cost=validated.unit_cost,
                reorder_point=validated.reorder_point,
                reorder_quantity=validated.reorder_quantity,
                supplier=validated.supplier,
            )
            session.add(new_product)
            imported_count += 1

    session.commit()
    logger.info(
        "Products CSV import finished: total=%d, created=%d, updated=%d, failed=%d",
        total_rows,
        imported_count,
        updated_count,
        failed_count,
    )
    return CSVImportReport(
        import_type="products",
        total_rows=total_rows,
        imported_count=imported_count,
        updated_count=updated_count,
        failed_count=failed_count,
        errors=errors,
    )


def import_stock_levels_csv(
    session: Session,
    csv_source: str | bytes | Path,
    user: str = "csv_importer",
    allow_negative_or_anomalies: bool = False,
) -> CSVImportReport:
    """Import warehouse stock levels from CSV content or file path with row-level validation.

    Validates SKU and warehouse code existence, integer quantities, and records a
    StockMovement entry when quantity_on_hand changes.
    """
    text = _read_csv_text(csv_source)
    reader = csv.DictReader(io.StringIO(text.strip()))

    if not reader.fieldnames:
        return CSVImportReport(
            import_type="stock_levels",
            total_rows=0,
            imported_count=0,
            updated_count=0,
            failed_count=1,
            errors=[
                CSVRowError(
                    row_number=1,
                    raw_data={},
                    field="header",
                    error_message="CSV file is empty or missing a header row.",
                )
            ],
        )

    normalized_headers = {h.strip().lower() for h in reader.fieldnames if h}
    missing_cols = REQUIRED_STOCK_COLUMNS - normalized_headers
    if missing_cols:
        return CSVImportReport(
            import_type="stock_levels",
            total_rows=0,
            imported_count=0,
            updated_count=0,
            failed_count=1,
            errors=[
                CSVRowError(
                    row_number=1,
                    raw_data={"headers": list(reader.fieldnames)},
                    field="header",
                    error_message=f"Missing required columns: {sorted(missing_cols)}",
                )
            ],
        )

    # Pre-fetch product SKUs and warehouse codes for fast lookup
    products_by_sku = {
        p.sku: p for p in session.scalars(select(Product)).all()
    }
    warehouses_by_code = {
        w.code: w for w in session.scalars(select(Warehouse)).all()
    }

    total_rows = 0
    imported_count = 0
    updated_count = 0
    failed_count = 0
    errors: list[CSVRowError] = []

    for row_number, raw_row in enumerate(reader, start=2):
        if not raw_row or all((v is None or str(v).strip() == "") for v in raw_row.values()):
            continue

        total_rows += 1
        clean_row: dict[str, Any] = {
            (k.strip().lower() if k else ""): (v.strip() if isinstance(v, str) else v)
            for k, v in raw_row.items()
            if k is not None
        }
        if not clean_row.get("quantity_reserved"):
            clean_row["quantity_reserved"] = 0

        try:
            validated = StockCSVRow.model_validate(clean_row)
        except ValidationError as exc:
            field_name, err_msg = _extract_validation_error(exc)
            failed_count += 1
            errors.append(
                CSVRowError(
                    row_number=row_number,
                    raw_data=clean_row,
                    field=field_name,
                    error_message=err_msg,
                )
            )
            continue

        if not allow_negative_or_anomalies:
            if validated.quantity_on_hand < 0:
                failed_count += 1
                errors.append(
                    CSVRowError(
                        row_number=row_number,
                        raw_data=clean_row,
                        field="quantity_on_hand",
                        error_message="quantity_on_hand cannot be negative.",
                    )
                )
                continue
            if validated.quantity_reserved > validated.quantity_on_hand:
                failed_count += 1
                errors.append(
                    CSVRowError(
                        row_number=row_number,
                        raw_data=clean_row,
                        field="quantity_reserved",
                        error_message=(
                            f"quantity_reserved ({validated.quantity_reserved}) cannot exceed "
                            f"quantity_on_hand ({validated.quantity_on_hand})."
                        ),
                    )
                )
                continue

        product = products_by_sku.get(validated.sku)
        if product is None:
            failed_count += 1
            errors.append(
                CSVRowError(
                    row_number=row_number,
                    raw_data=clean_row,
                    field="sku",
                    error_message=f"Product SKU '{validated.sku}' does not exist.",
                )
            )
            continue

        warehouse = warehouses_by_code.get(validated.warehouse_code)
        if warehouse is None:
            failed_count += 1
            errors.append(
                CSVRowError(
                    row_number=row_number,
                    raw_data=clean_row,
                    field="warehouse_code",
                    error_message=f"Warehouse code '{validated.warehouse_code}' does not exist.",
                )
            )
            continue

        stock = session.scalar(
            select(StockLevel).where(
                StockLevel.product_id == product.id,
                StockLevel.warehouse_id == warehouse.id,
            )
        )
        now = utc_now()
        if stock is not None:
            delta = validated.quantity_on_hand - stock.quantity_on_hand
            stock.quantity_on_hand = validated.quantity_on_hand
            stock.quantity_reserved = validated.quantity_reserved
            stock.updated_at = now
            updated_count += 1
            if delta != 0:
                session.add(
                    StockMovement(
                        product_id=product.id,
                        warehouse_id=warehouse.id,
                        change=delta,
                        reason=MovementReason.CSV_IMPORT.value,
                        timestamp=now,
                        user=user,
                    )
                )
        else:
            stock = StockLevel(
                product_id=product.id,
                warehouse_id=warehouse.id,
                quantity_on_hand=validated.quantity_on_hand,
                quantity_reserved=validated.quantity_reserved,
                updated_at=now,
            )
            session.add(stock)
            imported_count += 1
            if validated.quantity_on_hand != 0:
                session.add(
                    StockMovement(
                        product_id=product.id,
                        warehouse_id=warehouse.id,
                        change=validated.quantity_on_hand,
                        reason=MovementReason.CSV_IMPORT.value,
                        timestamp=now,
                        user=user,
                    )
                )

    session.commit()
    logger.info(
        "Stock levels CSV import finished: total=%d, created=%d, updated=%d, failed=%d",
        total_rows,
        imported_count,
        updated_count,
        failed_count,
    )
    return CSVImportReport(
        import_type="stock_levels",
        total_rows=total_rows,
        imported_count=imported_count,
        updated_count=updated_count,
        failed_count=failed_count,
        errors=errors,
    )

