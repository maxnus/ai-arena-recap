from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from ai_arena_recap.config import settings
from ai_arena_recap.db import engine
from ai_arena_recap.models import Competition

WEB_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))


def humanize_age(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


templates.env.filters["age"] = humanize_age


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session


def render(request: Request, template: str, **context):
    """Render a template with shared chrome (last-synced footer, etc.)."""
    with Session(engine) as session:
        comp = session.exec(select(Competition).where(Competition.id == settings.competition_id)).first()

    last_synced_dt = comp.last_synced if comp else None
    last_synced_age_s = None
    if last_synced_dt is not None:
        if last_synced_dt.tzinfo is None:
            last_synced_dt = last_synced_dt.replace(tzinfo=timezone.utc)
        last_synced_age_s = (datetime.now(tz=timezone.utc) - last_synced_dt).total_seconds()

    return templates.TemplateResponse(
        request,
        template,
        {
            "competition": comp,
            "last_synced_dt": last_synced_dt,
            "last_synced_age_s": last_synced_age_s,
            **context,
        },
    )
