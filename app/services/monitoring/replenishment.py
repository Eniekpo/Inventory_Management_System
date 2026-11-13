"""Replenishment calculation, supplier grouping, and CSV export logic."""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from app.models.entities import utc_now
from app.models.schemas import (
    ReplenishmentItem,
    ReplenishmentReport,
    SupplierReplenishmentGroup,
)
from app.services.monitoring.forecast import estimate_days_of_stock_remaining
from app.services.monitoring.rules import StockSnapshot, ThresholdConfig


def calculate_suggested_reorder_qty(
    available: int,
    reorder_point: int,
    reorder_quantity: int,
    avg_daily_consumption: float = 0.0,
    forecast_warning_days: int = 7,
    lead_time_days: int = 14,
) -> int:
    """Calculate the recommended order quantity for a product at a warehouse.

    A product requires replenishment when:
      1. available <= reorder_point, OR
      2. estimated days of stock remaining <= forecast_warning_days.

    When replenishment is needed, suggested quantity restores stock above
    reorder_point by at least reorder_quantity (or lead-time demand buffer if higher).
    """
    days_remaining = estimate_days_of_stock_remaining(available, avg_daily_consumption)
    needs_reorder = available <= reorder_point or (
        days_remaining is not None and days_remaining <= float(forecast_warning_days)
    )
    if not needs_reorder:
        return 0

    deficit = max(0, reorder_point - available)
    demand_buffer = int(round(max(0.0, avg_daily_consumption) * lead_time_days))
    base_replenish = max(reorder_quantity, demand_buffer)
    return max(reorder_quantity, deficit + base_replenish)


def build_replenishment_report(
    items_with_thresholds: Sequence[tuple[StockSnapshot, ThresholdConfig]],
    reference_time: datetime | None = None,
) -> ReplenishmentReport:
    """Generate a complete ReplenishmentReport grouped by supplier."""
    now = reference_time or utc_now()
    by_supplier: dict[str, list[ReplenishmentItem]] = defaultdict(list)

    for snapshot, thresholds in items_with_thresholds:
        effective_rop = thresholds.low_stock_threshold
        suggested_qty = calculate_suggested_reorder_qty(
            available=snapshot.available,
            reorder_point=effective_rop,
            reorder_quantity=snapshot.reorder_quantity,
            avg_daily_consumption=snapshot.avg_daily_consumption,
            forecast_warning_days=thresholds.forecast_warning_days,
        )
        if suggested_qty <= 0:
            continue

        days_remaining = estimate_days_of_stock_remaining(
            available=snapshot.available,
            avg_daily_consumption=snapshot.avg_daily_consumption,
        )

        if snapshot.available <= 0 or (days_remaining is not None and days_remaining <= 2.0):
            urgency = "CRITICAL"
        elif snapshot.available <= effective_rop:
            urgency = "HIGH"
        else:
            urgency = "MEDIUM"

        estimated_cost = round(suggested_qty * snapshot.unit_cost, 2)
        by_supplier[snapshot.supplier].append(
            ReplenishmentItem(
                product_id=snapshot.product_id,
                sku=snapshot.sku,
                product_name=snapshot.product_name,
                category=snapshot.category,
                supplier=snapshot.supplier,
                warehouse_id=snapshot.warehouse_id,
                warehouse_code=snapshot.warehouse_code,
                warehouse_name=snapshot.warehouse_name,
                available=snapshot.available,
                reorder_point=effective_rop,
                reorder_quantity=snapshot.reorder_quantity,
                avg_daily_consumption=round(snapshot.avg_daily_consumption, 2),
                days_of_stock_remaining=days_remaining,
                suggested_order_qty=suggested_qty,
                unit_cost=round(snapshot.unit_cost, 2),
                estimated_cost=estimated_cost,
                urgency=urgency,
            )
        )

    urgency_rank = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2}
    supplier_groups: list[SupplierReplenishmentGroup] = []
    total_line_items = 0
    total_units = 0
    total_cost = 0.0

    for supplier_name in sorted(by_supplier.keys()):
        group_items = sorted(
            by_supplier[supplier_name],
            key=lambda x: (urgency_rank.get(x.urgency, 9), x.available, x.sku),
        )
        group_units = sum(i.suggested_order_qty for i in group_items)
        group_cost = round(sum(i.estimated_cost for i in group_items), 2)
        total_line_items += len(group_items)
        total_units += group_units
        total_cost += group_cost

        supplier_groups.append(
            SupplierReplenishmentGroup(
                supplier=supplier_name,
                total_items=len(group_items),
                total_units_to_order=group_units,
                total_estimated_cost=group_cost,
                items=group_items,
            )
        )

    return ReplenishmentReport(
        generated_at=now,
        total_suppliers=len(supplier_groups),
        total_line_items=total_line_items,
        total_units_to_order=total_units,
        total_estimated_cost=round(total_cost, 2),
        supplier_groups=supplier_groups,
    )


def export_replenishment_csv(
    report: ReplenishmentReport,
    output_path: Path | None = None,
) -> str:
    """Serialize a ReplenishmentReport into CSV format and optionally write it to disk."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "supplier",
            "urgency",
            "sku",
            "product_name",
            "category",
            "warehouse_code",
            "warehouse_name",
            "available",
            "reorder_point",
            "reorder_quantity",
            "avg_daily_consumption",
            "days_of_stock_remaining",
            "suggested_order_qty",
            "unit_cost",
            "estimated_cost",
        ]
    )

    for group in report.supplier_groups:
        for item in group.items:
            writer.writerow(
                [
                    item.supplier,
                    item.urgency,
                    item.sku,
                    item.product_name,
                    item.category,
                    item.warehouse_code,
                    item.warehouse_name,
                    item.available,
                    item.reorder_point,
                    item.reorder_quantity,
                    f"{item.avg_daily_consumption:.2f}",
                    ""
                    if item.days_of_stock_remaining is None
                    else f"{item.days_of_stock_remaining:.2f}",
                    item.suggested_order_qty,
                    f"{item.unit_cost:.2f}",
                    f"{item.estimated_cost:.2f}",
                ]
            )

    csv_content = buffer.getvalue()
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(csv_content, encoding="utf-8")
    return csv_content

