"""InventorySource adapter interface for syncing inventory from external ERPs or files."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from sqlalchemy.orm import Session

from app.models.entities import utc_now
from app.models.schemas import CSVImportReport


@dataclass
class SourceSyncResult:
    """Result returned by an InventorySource synchronization run."""

    source_name: str
    synced_at: datetime = field(default_factory=utc_now)
    files_processed: int = 0
    products_created: int = 0
    products_updated: int = 0
    stock_levels_created: int = 0
    stock_levels_updated: int = 0
    failed_rows: int = 0
    reports: list[CSVImportReport] = field(default_factory=list)


class InventorySource(ABC):
    """Pluggable adapter interface for syncing catalog and stock data from external systems.

    Future implementations (e.g. OdooInventorySource, GoogleSheetsInventorySource,
    NetSuiteInventorySource) only need to subclass InventorySource and implement `sync()`.
    """

    @property
    @abstractmethod
    def source_name(self) -> str:
        """Human-readable identifier for this inventory data source."""

    @abstractmethod
    def sync(self, session: Session) -> SourceSyncResult:
        """Pull updates from the external source and apply them to the local database."""

