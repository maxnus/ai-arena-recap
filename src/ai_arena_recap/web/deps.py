import hashlib
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from ai_arena_recap.db import engine
from ai_arena_recap.models import Competition
from ai_arena_recap.web import season as season_mod

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


_STATIC_VERSIONS: dict[str, str] = {}


def static_url(name: str) -> str:
    """``/static/<name>?v=<content hash>`` — a URL that changes when the file does.

    The HTML is never cached but the assets are (no Cache-Control, so browsers
    cache heuristically), which means a returning visitor can pair fresh markup
    with a months-old app.js. That skew broke the ladder once already: the
    templates called ``seasonUrl()`` before the cached app.js defined it, and the
    grid silently never rendered. Hashing the URL retires the old copy instead."""
    if name not in _STATIC_VERSIONS:
        path = WEB_DIR / "static" / name
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()[:10]
        except OSError:
            digest = "0"
        _STATIC_VERSIONS[name] = digest
    return f"/static/{name}?v={_STATIC_VERSIONS[name]}"


templates.env.globals["static_url"] = static_url


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session


def render(request: Request, template: str, **context):
    """Render a template with shared chrome (season switcher, last-synced footer).

    ``season`` is whichever competition the URL asked for (see web/season.py);
    ``season_base`` is the prefix every internal link must carry so a visitor
    browsing an archived season stays inside it. ``subpath`` is the path with
    that prefix already stripped by the season middleware, which is what the
    switcher appends to another season's base to stay on the same page."""
    season = season_mod.current()
    with Session(engine) as session:
        comp = session.exec(select(Competition).where(Competition.id == season.id)).first()
        seasons = season_mod.all_seasons(session)

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
            "season": season,
            "season_base": season.base,
            "seasons": seasons,
            "subpath": request.url.path,
            "last_synced_dt": last_synced_dt,
            "last_synced_age_s": last_synced_age_s,
            **context,
        },
    )
