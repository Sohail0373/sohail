from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings

_connect_args: dict = {}
_db_url = settings.DATABASE_URL
if _db_url.startswith("sqlite"):
    # SQLite requires this for multi-threaded FastAPI usage
    _connect_args["check_same_thread"] = False
elif _db_url.startswith("postgres"):
    # Railway PostgreSQL requires SSL
    _connect_args["sslmode"] = "require"

engine = create_engine(
    _db_url,
    connect_args=_connect_args,
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    """FastAPI dependency — yields a DB session and closes it after the request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create all tables. Called once at startup."""
    from . import models  # noqa: F401 — registers ORM models with Base
    Base.metadata.create_all(bind=engine)
