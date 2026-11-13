"""FastAPI dependencies for database sessions, services, and authentication."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import verify_api_key
from app.services.inventory_service import InventoryService

DbSessionDep = Annotated[Session, Depends(get_db)]
ApiKeyDep = Annotated[str, Depends(verify_api_key)]


def get_inventory_service(session: DbSessionDep) -> InventoryService:
    """Return an InventoryService bound to the current request's database session."""
    return InventoryService(session)


InventoryServiceDep = Annotated[InventoryService, Depends(get_inventory_service)]

