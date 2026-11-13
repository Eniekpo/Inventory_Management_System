"""CLI script for importing products and warehouse stock levels from CSV files."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.database import SessionLocal, init_db
from app.services.importer import import_products_csv, import_stock_levels_csv


def main() -> None:
    """Parse CLI arguments and run the requested CSV import."""
    parser = argparse.ArgumentParser(
        description="Import products or stock levels from a CSV file with row-level validation."
    )
    parser.add_argument(
        "--type",
        choices=["products", "stock"],
        required=True,
        help="Type of CSV data to import ('products' or 'stock').",
    )
    parser.add_argument(
        "--file",
        type=Path,
        required=True,
        help="Path to the CSV file to import.",
    )
    args = parser.parse_args()

    if not args.file.is_file():
        print(f"Error: File not found: {args.file}", file=sys.stderr)
        sys.exit(1)

    init_db()
    with SessionLocal() as session:
        if args.type == "products":
            report = import_products_csv(session, args.file)
        else:
            report = import_stock_levels_csv(session, args.file)

        print(report.format_text_report())


if __name__ == "__main__":
    main()

