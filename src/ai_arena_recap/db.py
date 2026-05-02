from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from ai_arena_recap.config import settings

engine = create_engine(
    settings.database_url,
    echo=False,
    connect_args={"check_same_thread": False, "timeout": 30},
)


@event.listens_for(Engine, "connect")
def _set_sqlite_pragma(dbapi_connection, _):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def init_db() -> None:
    # Importing models registers the tables on SQLModel.metadata.
    from ai_arena_recap import models  # noqa: F401

    SQLModel.metadata.create_all(engine)
    _apply_lightweight_migrations()


def _apply_lightweight_migrations() -> None:
    """Add columns that exist on the SQLModel models but not on the live tables.

    SQLModel.create_all only creates *missing tables*, not missing columns.
    Rather than pulling in Alembic for the MVP we just ALTER TABLE on demand.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    if "bot" not in inspector.get_table_names():
        return
    columns = {c["name"] for c in inspector.get_columns("bot")}
    if "bot_data_enabled" not in columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE bot ADD COLUMN bot_data_enabled BOOLEAN"))


@contextmanager
def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session
