"""SQLAlchemy 2.x database engine, session factory, and initialization utilities."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import get_settings


class Base(DeclarativeBase):
    """Declarative base class for all SQLAlchemy 2.x ORM models."""


def build_engine(database_url: str | None = None) -> Engine:
    """Create a SQLAlchemy 2.x Engine configured for SQLite or PostgreSQL."""
    settings = get_settings()
    url = database_url or settings.database_url

    connect_args: dict[str, Any] = {}
    engine_kwargs: dict[str, Any] = {"future": True}

    if url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    else:
        engine_kwargs["pool_pre_ping"] = True
        engine_kwargs["pool_size"] = 10
        engine_kwargs["max_overflow"] = 20

    engine = create_engine(url, connect_args=connect_args, **engine_kwargs)

    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_connection: Any, _connection_record: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


engine: Engine = build_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def init_db(target_engine: Engine | None = None) -> None:
    """Create all database tables defined on Base.metadata."""
    # Import entities so models are registered with Base.metadata
    import app.models.entities  # noqa: F401

    active_engine = target_engine or engine
    Base.metadata.create_all(bind=active_engine)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope(custom_session_factory: Any = None) -> Generator[Session, None, None]:
    """Provide a transactional scope around a series of database operations."""
    factory = custom_session_factory or SessionLocal
    session: Session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

