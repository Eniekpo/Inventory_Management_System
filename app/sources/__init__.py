"""Pluggable inventory source adapters (CSV Folder Watch, ERP/Google Sheets extensible)."""

from app.sources.base import InventorySource, SourceSyncResult
from app.sources.csv_watch import CSVFolderWatchSource

__all__ = [
    "CSVFolderWatchSource",
    "InventorySource",
    "SourceSyncResult",
]

