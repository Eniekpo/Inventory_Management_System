"""Seed script populating realistic sample inventory data (50 products, 3 warehouses, 90 days history)."""

from __future__ import annotations

import argparse
import random
import sys
from datetime import timedelta
from pathlib import Path

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.database import SessionLocal, init_db
from app.core.logging_config import get_logger
from app.models.entities import (
    Alert,
    AlertSetting,
    NotificationLog,
    Product,
    StockLevel,
    StockMovement,
    SystemMetadata,
    Warehouse,
    utc_now,
)
from app.models.enums import MovementReason

logger = get_logger("seed")

WAREHOUSES_DATA = [
    ("WH-EAST", "East Coast Distribution Center (Newark)"),
    ("WH-CENTRAL", "Central Hub Warehouse (Chicago)"),
    ("WH-WEST", "West Coast Fulfillment Center (Oakland)"),
]

CATALOG_BLUEPRINT: list[tuple[str, str, list[tuple[str, float, int, int]]]] = [
    (
        "Electronics",
        "Apex Components Ltd",
        [
            ("Industrial IoT Gateway v4", 185.00, 15, 45),
            ("USB-C Docking Station Pro", 89.50, 20, 60),
            ("24-Port Managed PoE Switch", 310.00, 10, 25),
            ("Wireless Barcode Scanner 2D", 112.00, 18, 50),
            ("RFID Handheld Reader Terminal", 425.00, 8, 20),
            ("Thermal Label Printer 300dpi", 215.00, 12, 30),
            ("Cat6A Shielded Patch Cable 5m", 8.75, 50, 150),
        ],
    ),
    (
        "Electronics",
        "GlobalTech Distributors",
        [
            ("Rackmount UPS Battery 1500VA", 360.00, 8, 20),
            ("Environmental Temp/Humidity Sensor", 48.00, 25, 75),
            ("Smart PDU 16-Outlet Metered", 240.00, 10, 25),
            ("Fiber Optic Transceiver SFP+ 10G", 34.50, 30, 90),
            ("Industrial Panel PC 15-inch", 640.00, 6, 15),
            ("DIN-Rail Power Supply 24V 10A", 62.00, 20, 60),
        ],
    ),
    (
        "Industrial Hardware",
        "PrimeLine Industrial",
        [
            ("Heavy-Duty Pallet Jack 5500lb", 385.00, 6, 12),
            ("Stainless Steel Fastener Kit M8", 29.00, 35, 100),
            ("Hydraulic Door Closer Commercial", 74.00, 15, 40),
            ("Precision Digital Torque Wrench", 168.00, 12, 30),
            ("Pneumatic Quick-Coupler Brass Set", 22.50, 40, 120),
            ("Industrial Safety Light Curtain", 295.00, 8, 20),
            ("Ball Bearing Assembly 6205-2RS", 14.20, 45, 150),
        ],
    ),
    (
        "Industrial Hardware",
        "Nordic Supply Co",
        [
            ("Modular Conveyor Roller 600mm", 54.00, 20, 60),
            ("High-Tensile Steel Wire Rope 50m", 96.00, 12, 35),
            ("Lockout Tagout Station Master Kit", 118.00, 10, 30),
            ("Heavy-Duty Swivel Caster 6-inch", 19.80, 40, 120),
            ("Digital Vernier Caliper 150mm IP67", 65.00, 15, 45),
            ("Viton O-Ring Assortment Box", 38.00, 25, 75),
        ],
    ),
    (
        "Office Supplies",
        "Metro Office Solutions",
        [
            ("Ergonomic Mesh Task Chair", 220.00, 10, 25),
            ("Standing Desk Dual-Motor Frame", 275.00, 8, 20),
            ("High-Yield Laser Toner Cartridge Black", 78.00, 25, 60),
            ("Recycled Copy Paper A4 Case (5000sh)", 42.00, 40, 120),
            ("Magnetic Glass Dry-Erase Board 4x6", 145.00, 8, 20),
            ("Acoustic Desk Privacy Divider", 88.00, 12, 30),
            ("Heavy-Duty Cross-Cut Paper Shredder", 190.00, 6, 15),
            ("Archival Lever Arch File Binder Pack", 24.00, 30, 90),
            ("Wireless Conference Speakerphone", 155.00, 10, 25),
            ("LED Architect Clamp Desk Lamp", 46.00, 18, 50),
            ("Executive Gel Rollerball Pens (Box 24)", 18.50, 35, 100),
            ("Fireproof Document Safe Box", 132.00, 8, 20),
        ],
    ),
    (
        "Packaging & Logistics",
        "PackRight Corp",
        [
            ("Corrugated Shipping Box 24x18x18 (25pk)", 36.00, 35, 100),
            ("Heavy-Duty Stretch Wrap Film 18in (4rl)", 52.00, 30, 90),
            ("Direct Thermal Shipping Labels 4x6 (4000)", 44.00, 35, 100),
            ("Biodegradable Packing Peanuts 14 cu ft", 28.50, 25, 75),
            ("Poly Bubble Mailers #4 9.5x14.5 (100pk)", 31.00, 30, 90),
            ("Reinforced Kraft Water-Activated Tape", 49.00, 25, 80),
            ("Polypropylene Strapping Roll 1/2in", 64.00, 15, 45),
            ("Anti-Static Shielding Bags 10x12 (100pk)", 39.00, 20, 60),
            ("Edge Corner Protectors Heavy Board (50pk)", 27.00, 30, 90),
            ("Handheld Tape Dispenser Industrial", 16.50, 25, 60),
            ("Reusable Collapsible Logistics Tote 60L", 29.50, 25, 75),
            ("Desiccant Silica Gel Packets 5g (500pk)", 23.00, 30, 90),
        ],
    ),
]


def seed_database(session: Session, reset: bool = True) -> dict[str, int]:
    """Seed realistic sample data: 50 products, 3 warehouses, 4 categories, 90 days of movements.

    Returns a dictionary of inserted entity counts.
    """
    rng = random.Random(42)

    if reset:
        session.execute(delete(NotificationLog))
        session.execute(delete(Alert))
        session.execute(delete(StockMovement))
        session.execute(delete(StockLevel))
        session.execute(delete(AlertSetting))
        session.execute(delete(Product))
        session.execute(delete(Warehouse))
        session.execute(delete(SystemMetadata))
        session.commit()

    existing_products = session.scalar(select(func.count(Product.id))) or 0
    if existing_products > 0 and not reset:
        logger.info("Database already contains %d products; skipping seed.", existing_products)
        return {"products": existing_products}

    now = utc_now()
    start_date = now - timedelta(days=90)

    # 1. Create 3 Warehouses
    warehouses: list[Warehouse] = []
    for code, name in WAREHOUSES_DATA:
        wh = Warehouse(code=code, name=name)
        session.add(wh)
        warehouses.append(wh)
    session.flush()

    # 2. Create 50 Products across 4 categories
    products: list[Product] = []
    cat_prefix = {
        "Electronics": "ELC",
        "Industrial Hardware": "IND",
        "Office Supplies": "OFF",
        "Packaging & Logistics": "PKG",
    }
    cat_counters = {k: 0 for k in cat_prefix}

    for category, supplier, items in CATALOG_BLUEPRINT:
        for name, unit_cost, reorder_point, reorder_qty in items:
            cat_counters[category] += 1
            sku = f"{cat_prefix[category]}-{cat_counters[category]:03d}"
            prod = Product(
                sku=sku,
                name=name,
                category=category,
                unit_cost=unit_cost,
                reorder_point=reorder_point,
                reorder_quantity=reorder_qty,
                supplier=supplier,
                created_at=start_date,
                updated_at=now,
            )
            session.add(prod)
            products.append(prod)
    session.flush()

    # 3. Create Global AlertSetting + 2 Per-Product overrides
    global_setting = AlertSetting(
        product_id=None,
        low_stock_threshold=None,
        overstock_threshold=500,
        slow_moving_days=30,
        forecast_lookback_days=30,
        forecast_warning_days=7,
        updated_at=now,
    )
    session.add(global_setting)

    if len(products) >= 2:
        session.add(
            AlertSetting(
                product_id=products[0].id,
                low_stock_threshold=20,
                overstock_threshold=400,
                slow_moving_days=25,
                forecast_lookback_days=30,
                forecast_warning_days=10,
                updated_at=now,
            )
        )

    # 4. Generate 90 days of stock movements & ending stock levels
    movements_to_insert: list[StockMovement] = []
    stock_levels_to_insert: list[StockLevel] = []
    operators = ["ops_lead", "warehouse_bot", "j.martinez", "s.chen", "erp_sync"]

    for p_idx, product in enumerate(products):
        for w_idx, warehouse in enumerate(warehouses):
            combo_idx = p_idx * len(warehouses) + w_idx

            # Decide target scenario for this (product, warehouse) pair so all conditions exist
            if combo_idx in {0, 17, 42, 88, 121}:
                scenario = "OUT_OF_STOCK"
            elif combo_idx in {1, 5, 14, 28, 53, 76, 99, 134}:
                scenario = "LOW_STOCK"
            elif combo_idx in {2, 65}:
                scenario = "NEGATIVE_ANOMALY"
            elif combo_idx in {3, 49, 110}:
                scenario = "OVERSTOCK"
            elif combo_idx in {4, 33, 91}:
                scenario = "SLOW_MOVING"
            elif combo_idx in {6, 22, 70}:
                scenario = "FORECAST_RISK"
            else:
                scenario = "HEALTHY"

            # Initial seed movement 90 days ago
            initial_qty = max(product.reorder_point * 3, product.reorder_quantity + 30)
            running_qty = initial_qty
            movements_to_insert.append(
                StockMovement(
                    product_id=product.id,
                    warehouse_id=warehouse.id,
                    change=initial_qty,
                    reason=MovementReason.INITIAL_SEED.value,
                    timestamp=start_date + timedelta(hours=rng.randint(1, 8)),
                    user="seed_script",
                )
            )

            # Simulate 90 days of activity
            max_day = 45 if scenario == "SLOW_MOVING" else 90
            step_days = rng.randint(4, 7)
            day = step_days
            while day < max_day:
                ts = start_date + timedelta(days=day, hours=rng.randint(8, 18))
                sale_qty = rng.randint(2, max(4, product.reorder_point // 2))
                running_qty -= sale_qty
                movements_to_insert.append(
                    StockMovement(
                        product_id=product.id,
                        warehouse_id=warehouse.id,
                        change=-sale_qty,
                        reason=MovementReason.SALE.value,
                        timestamp=ts,
                        user=rng.choice(operators),
                    )
                )

                # Restock when low during historical window
                if running_qty <= product.reorder_point and day < max_day - 5:
                    restock_ts = ts + timedelta(hours=4)
                    restock_qty = product.reorder_quantity + rng.randint(10, 30)
                    running_qty += restock_qty
                    movements_to_insert.append(
                        StockMovement(
                            product_id=product.id,
                            warehouse_id=warehouse.id,
                            change=restock_qty,
                            reason=MovementReason.RESTOCK.value,
                            timestamp=restock_ts,
                            user="erp_sync",
                        )
                    )
                day += rng.randint(4, 8)

            # Apply final adjustment to hit exact target scenario cleanly
            reserved = rng.randint(0, 4)
            if scenario == "OUT_OF_STOCK":
                target_on_hand = 0
                reserved = 0
            elif scenario == "LOW_STOCK":
                reserved = 2
                target_avail = max(1, product.reorder_point - rng.randint(2, 6))
                target_on_hand = target_avail + reserved
            elif scenario == "NEGATIVE_ANOMALY":
                if combo_idx == 2:
                    target_on_hand = -3
                    reserved = 0
                else:
                    target_on_hand = 10
                    reserved = 16  # reserved > on_hand anomaly
            elif scenario == "OVERSTOCK":
                target_on_hand = rng.randint(540, 680)
                reserved = 5
            elif scenario == "SLOW_MOVING":
                target_on_hand = product.reorder_point + 40
                reserved = 0
            elif scenario == "FORECAST_RISK":
                # Recent heavy sales in the last 15 days so avg daily consumption is high
                # while available stock is just above reorder_point (e.g. reorder_point + 3)
                burst_sale = product.reorder_point * 4
                movements_to_insert.append(
                    StockMovement(
                        product_id=product.id,
                        warehouse_id=warehouse.id,
                        change=-burst_sale,
                        reason=MovementReason.SALE.value,
                        timestamp=now - timedelta(days=3),
                        user="ops_lead",
                    )
                )
                running_qty -= burst_sale
                reserved = 1
                target_on_hand = product.reorder_point + 3 + reserved
            else:
                # HEALTHY
                target_on_hand = product.reorder_point + product.reorder_quantity + rng.randint(15, 65)

            final_delta = target_on_hand - running_qty
            if final_delta != 0 and scenario != "SLOW_MOVING":
                movements_to_insert.append(
                    StockMovement(
                        product_id=product.id,
                        warehouse_id=warehouse.id,
                        change=final_delta,
                        reason=(
                            MovementReason.RESTOCK.value
                            if final_delta > 0
                            else MovementReason.SALE.value
                        ),
                        timestamp=now - timedelta(hours=rng.randint(2, 36)),
                        user=rng.choice(operators),
                    )
                )
            elif final_delta != 0 and scenario == "SLOW_MOVING":
                movements_to_insert.append(
                    StockMovement(
                        product_id=product.id,
                        warehouse_id=warehouse.id,
                        change=final_delta,
                        reason=MovementReason.ADJUSTMENT.value,
                        timestamp=now - timedelta(days=44),
                        user="seed_script",
                    )
                )

            stock_levels_to_insert.append(
                StockLevel(
                    product_id=product.id,
                    warehouse_id=warehouse.id,
                    quantity_on_hand=target_on_hand,
                    quantity_reserved=reserved,
                    updated_at=now,
                )
            )

    session.add_all(movements_to_insert)
    session.add_all(stock_levels_to_insert)
    session.add(
        SystemMetadata(
            key="last_source_sync",
            value=now.isoformat(),
            updated_at=now,
        )
    )
    session.commit()

    summary = {
        "warehouses": len(warehouses),
        "products": len(products),
        "stock_levels": len(stock_levels_to_insert),
        "movements": len(movements_to_insert),
    }
    logger.info("Seeded database successfully: %s", summary)
    return summary


def main() -> None:
    """CLI entrypoint for seeding the database."""
    parser = argparse.ArgumentParser(description="Seed Inventory Monitoring database.")
    parser.add_argument(
        "--no-reset",
        action="store_true",
        help="Do not wipe existing data if products already exist.",
    )
    args = parser.parse_args()

    init_db()
    with SessionLocal() as session:
        counts = seed_database(session, reset=not args.no_reset)
        print(f"Database seeded successfully: {counts}")


if __name__ == "__main__":
    main()

