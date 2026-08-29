"""Season (competition) scoping for the web layer.

Every read query under ``web/`` is scoped to a single competition — a "season".
Which one depends on the URL: un-prefixed paths (``/``, ``/bots/123``) serve the
*current* season (``settings.competition_id``), while ``/s/<slug>/...`` serves an
archived one. The season middleware in ``web/app.py`` strips that prefix,
resolves it to a :class:`Season` and stores it in a ContextVar, so the ~40 query
helpers can keep reading it parameter-free the way they used to read
``settings.competition_id``.

Two things vary per season:

* **Which competition's rows to read** — :func:`cid`.
* **What counts as being "on the ladder"** — :func:`ladder_filter`. While a
  competition is open that means ``active = 1``. aiarena flips *every*
  participation to ``active = 0`` the moment a competition closes (verified on
  competition 36 at the 2026 Season 1 rollover), so for a closed season the
  final standings are the bots that ended in a division instead —
  ``division_num > 0``. Bots that dropped off the ladder mid-season have their
  division reset to 0, so this reproduces the ladder as it stood at close.
"""
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlmodel import Session, select

from ai_arena_recap.config import settings
from ai_arena_recap.models import Competition, CompetitionParticipation

# URL prefix for archived seasons: /s/<slug>/...
SEASON_URL_PREFIX = "/s"

# aiarena names every ladder competition "Sc2 AI Arena <year> <phase>"; the
# common prefix carries no information, so slugs drop it ("2026-season-1").
_NAME_PREFIX_RE = re.compile(r"^sc2\s+ai\s+arena\s+", re.IGNORECASE)
_NON_SLUG_RE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class Season:
    id: int
    name: str
    slug: str
    closed: bool
    is_current: bool
    date_opened: datetime | None = None
    date_closed: datetime | None = None

    @property
    def base(self) -> str:
        """URL prefix for links belonging to this season ("" for the current one)."""
        return "" if self.is_current else f"{SEASON_URL_PREFIX}/{self.slug}"


def slugify(name: str, competition_id: int) -> str:
    """URL slug for a competition name: "Sc2 AI Arena 2026 Season 1" -> "2026-season-1"."""
    stripped = _NAME_PREFIX_RE.sub("", (name or "").strip()).lower()
    slug = _NON_SLUG_RE.sub("-", stripped).strip("-")
    return slug or f"competition-{competition_id}"


def _from_row(comp: Competition) -> Season:
    return Season(
        id=comp.id,
        name=comp.name,
        slug=slugify(comp.name, comp.id),
        closed=(comp.status or "").lower() == "closed",
        is_current=comp.id == settings.competition_id,
        date_opened=comp.date_opened,
        date_closed=comp.date_closed,
    )


def _fallback(competition_id: int) -> Season:
    """Season for a competition we have no row for yet (fresh DB, tests)."""
    return Season(
        id=competition_id,
        name=f"Competition {competition_id}",
        slug=f"competition-{competition_id}",
        closed=False,
        is_current=competition_id == settings.competition_id,
    )


def load(session: Session, competition_id: int) -> Season:
    comp = session.get(Competition, competition_id)
    return _from_row(comp) if comp is not None else _fallback(competition_id)


def all_seasons(session: Session) -> list[Season]:
    """Every competition in the DB, current first then newest-opened first."""
    rows = session.exec(select(Competition)).all()
    seasons = [_from_row(c) for c in rows]
    if not any(s.is_current for s in seasons):
        seasons.append(_fallback(settings.competition_id))
    seasons.sort(key=lambda s: (not s.is_current, -(s.date_opened.timestamp() if s.date_opened else 0)))
    return seasons


def resolve(session: Session, token: str) -> Season | None:
    """Look up a season by URL slug, by its full slugified name, or by id."""
    token = (token or "").strip().lower()
    if not token:
        return None
    for season in all_seasons(session):
        if token in {season.slug, str(season.id), _NON_SLUG_RE.sub("-", season.name.lower()).strip("-")}:
            return season
    return None


# ---------------------------------------------------------------------------
# Ambient season (set per request by the season middleware)
# ---------------------------------------------------------------------------

_current: ContextVar[Season | None] = ContextVar("current_season", default=None)
_default: Season | None = None


def current() -> Season:
    """The season the caller is scoped to.

    Inside a request that's whatever the middleware resolved from the URL.
    Outside one (the rankings cache warmer, the CLI, tests) it's the configured
    current competition, read from the DB once and memoised — call :func:`reset`
    after a sync so a competition that just closed is picked up."""
    season = _current.get()
    if season is not None:
        return season
    global _default
    if _default is None or _default.id != settings.competition_id:
        # Imported here so the engine monkeypatched in tests is picked up.
        from ai_arena_recap.db import engine

        with Session(engine) as session:
            _default = load(session, settings.competition_id)
    return _default


def cid() -> int:
    """Competition id of the current season — the scope of every read query."""
    return current().id


def window_anchor() -> datetime:
    """The instant a "last N days" window counts back from.

    For the live season that is now. For a closed one it is when the season
    ended: now is months past its final match, so a window measured from today
    covers a stretch in which the season did not exist, and every such panel
    renders empty. Anchoring to the close date makes "the last 60 days" mean
    the last 60 days *of that season*.

    Always tz-aware. ``date_closed`` comes back from SQLite naive, and these
    values get compared against aware datetimes in Python.
    """
    season = current()
    if season.closed and season.date_closed is not None:
        anchor = season.date_closed
        return anchor if anchor.tzinfo else anchor.replace(tzinfo=timezone.utc)
    return datetime.now(tz=timezone.utc)


def reset() -> None:
    """Drop the memoised default season (after a sync, or between tests)."""
    global _default
    _default = None


@contextmanager
def use(season: Season):
    token = _current.set(season)
    try:
        yield season
    finally:
        _current.reset(token)


# ---------------------------------------------------------------------------
# Ladder membership
# ---------------------------------------------------------------------------

def ladder_filter():
    """SQLAlchemy predicate for "this bot is on the season's ladder".

    See the module docstring: `active` is meaningless once a competition closes,
    so a closed season falls back to final division placement."""
    if current().closed:
        return CompetitionParticipation.division_num > 0
    return CompetitionParticipation.active == True  # noqa: E712


def ladder_sql(alias: str = "cp") -> str:
    """Same predicate as :func:`ladder_filter`, for the raw-SQL aggregates."""
    return f"{alias}.division_num > 0" if current().closed else f"{alias}.active = 1"
