"""Test fixtures.

Each test gets a fresh in-memory SQLite database. The global engine in
ai_arena_recap.db is monkey-patched so any code under test (sync helpers,
FastAPI routes, etc.) writes to the test DB rather than the real one.
"""
from collections.abc import Iterator

import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlalchemy.pool import StaticPool

from ai_arena_recap import db as db_module
from ai_arena_recap import models  # noqa: F401  (registers tables on metadata)


@pytest.fixture()
def engine(monkeypatch):
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,  # share a single connection so :memory: persists
    )
    SQLModel.metadata.create_all(eng)
    # Each module that did `from ai_arena_recap.db import engine` captured the
    # original reference at import time. Patch every site so test code reaches
    # the test engine.
    monkeypatch.setattr(db_module, "engine", eng)
    from ai_arena_recap.web import app as app_module
    from ai_arena_recap.web import deps as deps_module
    monkeypatch.setattr(app_module, "engine", eng)
    monkeypatch.setattr(deps_module, "engine", eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine) -> Iterator[Session]:
    with Session(engine) as s:
        yield s
