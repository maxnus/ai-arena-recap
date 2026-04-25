from datetime import datetime, timezone
from typing import Any

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Session, SQLModel


def utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def upsert(session: Session, model: type[SQLModel], values: dict[str, Any]) -> None:
    """SQLite UPSERT keyed on the model's primary key."""
    table = model.__table__  # type: ignore[attr-defined]
    pk_cols = [c.name for c in table.primary_key.columns]
    update_cols = {k: v for k, v in values.items() if k not in pk_cols}
    stmt = sqlite_insert(table).values(**values)
    if update_cols:
        stmt = stmt.on_conflict_do_update(index_elements=pk_cols, set_=update_cols)
    else:
        stmt = stmt.on_conflict_do_nothing(index_elements=pk_cols)
    session.exec(stmt)  # type: ignore[arg-type]


_STUB_SYNCED_AT = datetime(1970, 1, 1, tzinfo=timezone.utc)


def ensure_bot_stub(session: Session, bot_id: int) -> None:
    """Insert a placeholder Bot row if one doesn't exist, to satisfy FK constraints
    before sync_bots runs and fills in real data. last_synced=epoch marks it as not-yet-fetched."""
    from ai_arena_recap.models import Bot

    stmt = sqlite_insert(Bot.__table__).values(  # type: ignore[attr-defined]
        id=bot_id,
        name=f"bot-{bot_id}",
        last_synced=_STUB_SYNCED_AT,
    ).on_conflict_do_nothing(index_elements=["id"])
    session.exec(stmt)  # type: ignore[arg-type]
