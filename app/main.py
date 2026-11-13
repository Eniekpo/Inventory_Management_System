"""FastAPI application entrypoint, CORS setup, lifespan hooks, and global error handlers."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import __version__
from app.api.routes import router
from app.core.config import get_settings
from app.core.database import init_db
from app.core.exceptions import InventoryError
from app.core.logging_config import configure_logging, get_logger

logger = get_logger("api.main")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    """Initialize logging and database schema on application startup."""
    configure_logging()
    init_db()
    logger.info("FastAPI Inventory Monitoring API initialized (v%s).", __version__)
    yield


def create_app() -> FastAPI:
    """Create and configure the FastAPI application instance."""
    settings = get_settings()
    application = FastAPI(
        title=settings.app_name,
        description=(
            "Production REST API for automated inventory monitoring, anomaly detection, "
            "stock movements, replenishment planning, and multi-channel alerting."
        ),
        version=__version__,
        lifespan=lifespan,
    )

    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @application.exception_handler(InventoryError)
    async def handle_inventory_error(
        _request: Request,
        exc: InventoryError,
    ) -> JSONResponse:
        """Convert domain exceptions into structured HTTP JSON responses."""
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": exc.__class__.__name__,
                "detail": exc.message,
            },
        )

    @application.exception_handler(Exception)
    async def handle_unexpected_error(
        request: Request,
        exc: Exception,
    ) -> JSONResponse:
        """Catch unexpected server exceptions without leaking internal stack traces."""
        logger.error(
            "Unhandled exception on %s %s: %s",
            request.method,
            request.url.path,
            exc,
            exc_info=True,
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": "InternalServerError",
                "detail": "An unexpected internal server error occurred.",
            },
        )

    application.include_router(router)
    return application


app = create_app()

