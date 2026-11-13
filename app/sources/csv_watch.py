"""CSV folder-watch InventorySource implementation."""

from __future__ import annotations

import shutil
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import PROJECT_ROOT, get_settings
from app.core.logging_config import get_logger
from app.models.entities import SystemMetadata, utc_now
from app.services.importer import import_products_csv, import_stock_levels_csv
from app.sources.base import InventorySource, SourceSyncResult

logger = get_logger("sources.csv_watch")


class CSVFolderWatchSource(InventorySource):
    """Polls a local directory for dropped CSV files and syncs products & stock levels.

    File naming convention in watch directory:
      - Files starting with `products` (e.g. `products_202501.csv`) are imported as Products.
      - Files starting with `stock` (e.g. `stock_levels_202501.csv`) are imported as StockLevels.
    Processed files are moved into `<watch_dir>/processed/` (or `<watch_dir>/failed/` if
    a header/format error prevents processing).
    """

    def __init__(self, watch_dir: str | Path | None = None) -> None:
        """Initialize folder watch source with target directory path."""
        settings = get_settings()
        raw_path = Path(watch_dir) if watch_dir is not None else Path(settings.watch_folder_path)
        if not raw_path.is_absolute():
            raw_path = PROJECT_ROOT / raw_path
        self.watch_dir = raw_path
        self.processed_dir = self.watch_dir / "processed"
        self.failed_dir = self.watch_dir / "failed"

    @property
    def source_name(self) -> str:
        """Return adapter identifier."""
        return "csv_folder_watch"

    def ensure_directories(self) -> None:
        """Create watch, processed, and failed directories if they do not exist."""
        self.watch_dir.mkdir(parents=True, exist_ok=True)
        self.processed_dir.mkdir(parents=True, exist_ok=True)
        self.failed_dir.mkdir(parents=True, exist_ok=True)

    def sync(self, session: Session) -> SourceSyncResult:
        """Scan watch folder, import products first then stock levels, and archive files."""
        self.ensure_directories()
        now = utc_now()
        result = SourceSyncResult(source_name=self.source_name, synced_at=now)

        csv_files = sorted(
            f for f in self.watch_dir.iterdir() if f.is_file() and f.suffix.lower() == ".csv"
        )
        # Process product CSVs before stock level CSVs so new SKUs exist first
        product_files = [f for f in csv_files if f.name.lower().startswith("product")]
        stock_files = [f for f in csv_files if f.name.lower().startswith("stock")]

        for file_path in product_files:
            report = import_products_csv(session, file_path)
            result.files_processed += 1
            result.products_created += report.imported_count
            result.products_updated += report.updated_count
            result.failed_rows += report.failed_count
            result.reports.append(report)
            self._archive_file(file_path, success=(report.total_rows > 0))

        for file_path in stock_files:
            report = import_stock_levels_csv(session, file_path, user="csv_folder_watch")
            result.files_processed += 1
            result.stock_levels_created += report.imported_count
            result.stock_levels_updated += report.updated_count
            result.failed_rows += report.failed_count
            result.reports.append(report)
            self._archive_file(file_path, success=(report.total_rows > 0))

        # Record sync timestamp in SystemMetadata
        meta = session.get(SystemMetadata, "last_source_sync")
        if meta is None:
            session.add(
                SystemMetadata(
                    key="last_source_sync",
                    value=now.isoformat(),
                    updated_at=now,
                )
            )
        else:
            meta.value = now.isoformat()
            meta.updated_at = now
        session.commit()

        if result.files_processed > 0:
            logger.info(
                "CSV folder watch synced %d file(s): products (+%d/~%d), stock (+%d/~%d), failed_rows=%d",
                result.files_processed,
                result.products_created,
                result.products_updated,
                result.stock_levels_created,
                result.stock_levels_updated,
                result.failed_rows,
            )
        return result

    def _archive_file(self, file_path: Path, success: bool) -> None:
        """Move a processed CSV file into processed/ or failed/."""
        target_dir = self.processed_dir if success else self.failed_dir
        timestamp_suffix = utc_now().strftime("%Y%m%d_%H%M%S")
        destination = target_dir / f"{file_path.stem}_{timestamp_suffix}{file_path.suffix}"
        shutil.move(str(file_path), str(destination))

