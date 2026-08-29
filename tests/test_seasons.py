"""Season scoping: the current competition at "/", archived ones at /s/<slug>/.

Covers the three things that make an archived season work: URL resolution, the
ladder-membership switch (aiarena flips every participation to active=0 when a
competition closes, so closed seasons fall back to final division placement),
and the middleware that keeps route handlers unaware of any of it.
"""
import re
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from sqlmodel import select

from ai_arena_recap.config import settings
from ai_arena_recap.models import (
    Bot,
    Competition,
    CompetitionParticipation,
    Match,
    MatchParticipation,
    PageView,
    Round,
)
from ai_arena_recap.sync.common import upsert
from ai_arena_recap.web import rankings, season
from ai_arena_recap.web.app import SeasonMiddleware, page_view_middleware
from ai_arena_recap.web import deps
from ai_arena_recap.web.deps import WEB_DIR
from ai_arena_recap.web.routes import api as api_route
from ai_arena_recap.web.routes import bot as bot_route
from ai_arena_recap.web.routes import ladder as ladder_route
from ai_arena_recap.web.routes import rankings as rankings_route

NOW = datetime(2026, 8, 24, tzinfo=timezone.utc)
CURRENT = settings.competition_id          # open pre-season
ARCHIVED = settings.competition_id - 1     # the season that just closed
ARCHIVED_SLUG = "2026-season-1"


@pytest.fixture(autouse=True)
def _clear_rankings_cache():
    rankings._CACHE.clear()
    yield
    rankings._CACHE.clear()


@pytest.fixture()
def client(engine):
    app = FastAPI()
    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")
    app.include_router(ladder_route.router)
    app.include_router(bot_route.router)
    app.include_router(rankings_route.router)
    app.include_router(api_route.router)
    # Same order as create_app: page views innermost, season prefix stripped above it.
    app.middleware("http")(page_view_middleware)
    app.add_middleware(SeasonMiddleware)
    return TestClient(app)


def _seed(session):
    """One open season and one closed one, sharing a bot.

    The closed season keeps aiarena's post-close shape: every participation
    inactive, and a bot that dropped off the ladder mid-season reset to
    division 0."""
    upsert(session, Competition, {
        "id": CURRENT, "name": "Sc2 AI Arena 2026 Pre-Season 2", "status": "open",
        "date_opened": datetime(2026, 8, 23, tzinfo=timezone.utc), "last_synced": NOW,
    })
    upsert(session, Competition, {
        "id": ARCHIVED, "name": "Sc2 AI Arena 2026 Season 1", "status": "closed",
        "date_opened": datetime(2026, 3, 4, tzinfo=timezone.utc),
        "date_closed": datetime(2026, 8, 23, tzinfo=timezone.utc), "last_synced": NOW,
    })
    for bot_id, name, race in [(1, "Alpha", "T"), (2, "Beta", "Z"), (3, "Gamma", "P")]:
        upsert(session, Bot, {
            "id": bot_id, "name": name, "plays_race": race, "type": "python",
            "user_name": "alice", "created": NOW, "last_synced": NOW,
        })

    def cp(pid, comp, bot_id, elo, division, active):
        upsert(session, CompetitionParticipation, {
            "id": pid, "competition_id": comp, "bot_id": bot_id, "elo": elo,
            "highest_elo": elo + 50, "division_num": division, "active": active,
            "match_count": 40, "win_count": 30, "loss_count": 10, "tie_count": 0,
            "crash_count": 0, "win_perc": 75.0, "last_synced": NOW,
        })

    # Closed season: nothing is active any more; Gamma left the ladder (division 0).
    cp(1, ARCHIVED, 1, 2100, 1, False)
    cp(2, ARCHIVED, 2, 1900, 2, False)
    cp(3, ARCHIVED, 3, 1700, 0, False)
    # Open season: only Alpha has joined so far.
    cp(4, CURRENT, 1, 1650, 1, True)

    for comp, round_id in [(ARCHIVED, 1), (CURRENT, 2)]:
        upsert(session, Round, {
            "id": round_id, "number": 1, "competition_id": comp, "complete": True,
            "started": NOW, "finished": NOW, "last_synced": NOW,
        })
    upsert(session, Match, {
        "id": 1, "round_id": 1, "started": NOW, "result_created": NOW,
        "result_type": "Player1Win", "result_winner_bot_id": 1,
        "result_game_steps": 10000, "last_synced": NOW,
    })
    for pid, bot_id, result, elo in [(1, 1, "win", 2100), (2, 2, "loss", 1900)]:
        upsert(session, MatchParticipation, {
            "id": pid, "match_id": 1, "bot_id": bot_id, "participant_number": pid,
            "starting_elo": elo, "resultant_elo": elo, "elo_change": 0,
            "avg_step_time": 0.01, "result": result, "last_synced": NOW,
        })
    session.commit()


# --- slugs and resolution ---------------------------------------------------

@pytest.mark.parametrize(("name", "expected"), [
    ("Sc2 AI Arena 2026 Season 1", "2026-season-1"),
    ("Sc2 AI Arena 2026 Pre-Season 2", "2026-pre-season-2"),
    ("AI Arena - Season 2", "ai-arena-season-2"),
    ("", "competition-7"),
])
def test_slugify(name, expected):
    assert season.slugify(name, 7) == expected


def test_resolve_by_slug_id_and_full_name(engine, session):
    _seed(session)
    assert season.resolve(session, ARCHIVED_SLUG).id == ARCHIVED
    assert season.resolve(session, str(ARCHIVED)).id == ARCHIVED
    assert season.resolve(session, "sc2-ai-arena-2026-season-1").id == ARCHIVED
    assert season.resolve(session, "nope") is None


def test_current_season_is_unprefixed_archived_is_not(engine, session):
    _seed(session)
    seasons = season.all_seasons(session)
    assert [s.id for s in seasons] == [CURRENT, ARCHIVED]  # current first
    assert seasons[0].base == ""
    assert seasons[1].base == f"/s/{ARCHIVED_SLUG}"


# --- ladder membership ------------------------------------------------------

def test_closed_season_ladder_survives_the_active_flag_wipe(engine, session, client):
    _seed(session)
    rows = client.get(f"/s/{ARCHIVED_SLUG}/api/ladder.json").json()
    # Both placed bots are listed despite active=0 on every row...
    assert [r["name"] for r in rows["data"]] == ["Alpha", "Beta"]
    # ...while the bot that dropped off the ladder (division 0) is not.
    assert rows["awaiting"] == []


def test_current_season_still_filters_on_active(engine, session, client):
    _seed(session)
    rows = client.get("/api/ladder.json").json()
    assert [r["name"] for r in rows["data"]] == ["Alpha"]
    assert rows["data"][0]["elo"] == 1650  # the open season's rating, not the archive's


def test_ladder_page_renders_per_season(engine, session, client):
    _seed(session)
    current = client.get("/")
    archived = client.get(f"/s/{ARCHIVED_SLUG}/")
    assert current.status_code == archived.status_code == 200
    assert "Pre-Season 2" in current.text
    assert "Season 1" in archived.text
    # Links inside an archived season stay inside it.
    assert f'href="/s/{ARCHIVED_SLUG}/rankings"' in archived.text
    assert 'href="/rankings"' in current.text


def test_bot_page_shows_the_seasons_own_standing(engine, session, client):
    _seed(session)
    assert "1650" in client.get("/bots/1").text
    assert "2100" in client.get(f"/s/{ARCHIVED_SLUG}/bots/1").text


def test_rankings_page_is_scoped_to_the_season(engine, session, client):
    _seed(session)
    archived = client.get(f"/s/{ARCHIVED_SLUG}/rankings")
    assert archived.status_code == 200
    # Peak ELO comes from the archived standings (the raw-SQL and ORM paths both
    # have to honour the closed-season ladder filter).
    assert "2150" in archived.text
    current = client.get("/rankings")
    assert "1700" in current.text and "2150" not in current.text


def test_rankings_cache_keeps_a_slot_per_season(engine, session, client):
    _seed(session)
    client.get("/rankings")
    client.get(f"/s/{ARCHIVED_SLUG}/rankings")
    assert set(rankings._CACHE) == {CURRENT, ARCHIVED}


# --- asset / HTML pairing ---------------------------------------------------

def test_pages_carry_their_own_season_base(engine, session, client):
    """The season prefix JS builds URLs from ships with the HTML, not with
    app.js — a browser holding a stale cached app.js must not be able to pair
    it with fresh markup and break the page."""
    _seed(session)
    assert 'const SEASON_BASE = "";' in client.get("/").text
    assert f'const SEASON_BASE = "/s/{ARCHIVED_SLUG}";' in client.get(f"/s/{ARCHIVED_SLUG}/").text


def test_static_urls_are_content_hashed(engine, session, client):
    """Asset URLs change when the asset does, so a deploy retires cached copies."""
    page = client.get("/").text
    assert re.search(r'href="/static/styles\.css\?v=[0-9a-f]{10}"', page)
    assert re.search(r'src="/static/app\.js\?v=[0-9a-f]{10}"', page)
    assert deps.static_url("app.js") != deps.static_url("styles.css").replace("styles.css", "app.js")


# --- middleware -------------------------------------------------------------

def test_unknown_season_is_404(engine, session, client):
    _seed(session)
    response = client.get("/s/2019-season-9/")
    assert response.status_code == 404
    assert "Unknown season" in response.text


def test_season_prefix_does_not_leak_into_page_views(engine, session, client):
    _seed(session)
    client.get(f"/s/{ARCHIVED_SLUG}/bots/1")
    client.get("/bots/1")
    paths = session.exec(select(PageView.path, PageView.count)).all()
    # One counter for the bot, not one per season it was viewed in.
    assert [(p, c) for p, c in paths if p.startswith("/bots/")] == [("/bots/1", 2)]


def test_season_scope_does_not_outlive_the_request(engine, session, client):
    _seed(session)
    client.get(f"/s/{ARCHIVED_SLUG}/")
    assert season.current().id == CURRENT


def _seed_two_season_history(session):
    """The same bot playing in both seasons: one match in each."""
    _seed(session)
    upsert(session, Match, {
        "id": 2, "round_id": 2, "started": NOW, "result_created": NOW,
        "result_type": "Player1Win", "result_winner_bot_id": 1,
        "result_game_steps": 20000, "last_synced": NOW,
    })
    for pid, bot_id, result, elo in [(3, 1, "win", 1650), (4, 2, "loss", 1600)]:
        upsert(session, MatchParticipation, {
            "id": pid, "match_id": 2, "bot_id": bot_id, "participant_number": pid - 2,
            "starting_elo": elo, "resultant_elo": elo, "elo_change": 0,
            "avg_step_time": 0.02, "result": result, "last_synced": NOW,
        })
    session.commit()


class TestMatchHistoryStaysInItsSeason:
    """A bot's career spans seasons; a season's page must not.

    This leaked in production: an archived season's bot page listed matches
    played after it closed, and the live season's per-opponent record counted
    the previous season's games (709 for a bot with 292 that season).
    """

    def test_match_history_only_lists_this_seasons_matches(self, engine, session, client):
        _seed_two_season_history(session)

        current = client.get("/api/bots/1/matches.json").json()
        archived = client.get(f"/s/{ARCHIVED_SLUG}/api/bots/1/matches.json").json()

        assert [r["match_id"] for r in current["data"]] == [2]
        assert [r["match_id"] for r in archived["data"]] == [1]

    def test_the_total_is_scoped_too_so_paging_agrees_with_the_rows(self, engine, session, client):
        """A total counted across every season would page past the end."""
        _seed_two_season_history(session)

        for path in ("/api/bots/1/matches.json", f"/s/{ARCHIVED_SLUG}/api/bots/1/matches.json"):
            body = client.get(path).json()
            assert body["total"] == len(body["data"]) == 1

    def test_per_opponent_record_excludes_other_seasons(self, engine, session, client):
        _seed_two_season_history(session)

        body = client.get("/api/bots/1/matchups.json?window_days=365&min_games=1").json()
        # A 365-day window spans both seasons; only this one's game counts.
        assert [(r["opp_name"], r["matches"]) for r in body["data"]] == [("Beta", 1)]


class TestWindowsAnchorToTheSeason:
    """"The last 60 days" has to mean the last 60 days *of the season shown*.

    Measured from today, an archived season's window sits entirely after its
    final match, so every window-based panel renders empty — which is what the
    2026 Pre-Season 1 bot pages did once its history was imported."""

    def test_anchor_is_now_for_the_live_season(self, engine, session):
        _seed(session)
        with season.use(season.resolve(session, str(CURRENT))):
            assert (datetime.now(tz=timezone.utc) - season.window_anchor()).total_seconds() < 60

    def test_anchor_is_the_close_date_for_an_archived_season(self, engine, session):
        _seed(session)
        with season.use(season.resolve(session, ARCHIVED_SLUG)):
            assert season.window_anchor() == datetime(2026, 8, 23, tzinfo=timezone.utc)

    def test_the_anchor_is_tz_aware(self, engine, session):
        """SQLite hands dates back naive; these get compared to aware ones."""
        _seed(session)
        with season.use(season.resolve(session, ARCHIVED_SLUG)):
            assert season.window_anchor().tzinfo is not None

    def test_archived_matchups_are_populated_not_empty(self, engine, session, client):
        """The case the fix exists for: a season whose matches are long past.

        Dated so a window measured from today cannot reach them — that is
        exactly the state every archived season is in once it ages out."""
        _seed_two_season_history(session)
        long_ago = datetime(2026, 3, 1, tzinfo=timezone.utc)
        upsert(session, Competition, {
            "id": ARCHIVED, "name": "Sc2 AI Arena 2026 Season 1", "status": "closed",
            "date_opened": datetime(2026, 1, 4, tzinfo=timezone.utc),
            "date_closed": datetime(2026, 3, 5, tzinfo=timezone.utc), "last_synced": NOW,
        })
        upsert(session, Match, {
            "id": 1, "round_id": 1, "started": long_ago, "result_created": long_ago,
            "result_type": "Player1Win", "result_winner_bot_id": 1,
            "result_game_steps": 10000, "last_synced": NOW,
        })
        session.commit()
        season.reset()

        body = client.get(
            f"/s/{ARCHIVED_SLUG}/api/bots/1/matchups.json?window_days=30&min_games=1"
        ).json()
        assert [(r["opp_name"], r["matches"]) for r in body["data"]] == [("Beta", 1)]
