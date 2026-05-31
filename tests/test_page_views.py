"""Tests for page-view tracking: the recording middleware and the
``most_viewed_bots`` ranking helper."""
from datetime import date, datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.testclient import TestClient
from sqlmodel import select

from ai_arena_recap.models import Bot, PageView
from ai_arena_recap.sync.common import upsert
from ai_arena_recap.web import app as app_module
from ai_arena_recap.web import rankings

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# record_page_view (the DB upsert)
# ---------------------------------------------------------------------------

def test_record_page_view_inserts_then_increments(engine, session):
    app_module.record_page_view("/bots/1")
    app_module.record_page_view("/bots/1")
    row = session.exec(select(PageView).where(PageView.path == "/bots/1")).one()
    assert row.count == 2
    assert isinstance(row.day, date)  # stored as a real date, today's


# ---------------------------------------------------------------------------
# The middleware (what it does / doesn't count)
# ---------------------------------------------------------------------------

@pytest.fixture()
def mw_client(engine):
    # A bare app with just the page-view middleware and a couple of stub routes,
    # so we can assert what gets counted without the full page-rendering stack.
    # The `engine` fixture monkeypatches app_module.engine, which record_page_view
    # writes through.
    app = FastAPI()
    app.middleware("http")(app_module.page_view_middleware)

    @app.get("/page")
    def _page():
        return HTMLResponse("<p>hi</p>")

    @app.get("/data.json")
    def _data():
        return JSONResponse({"ok": True})

    return TestClient(app)


def test_middleware_counts_html_get(mw_client, session):
    assert mw_client.get("/page").status_code == 200
    row = session.exec(select(PageView).where(PageView.path == "/page")).one()
    assert row.count == 1


def test_middleware_skips_json_response(mw_client, session):
    mw_client.get("/data.json")
    assert session.exec(select(PageView)).all() == []


def test_middleware_skips_crawlers(mw_client, session):
    mw_client.get("/page", headers={"user-agent": "Mozilla/5.0 (compatible; Googlebot/2.1)"})
    assert session.exec(select(PageView)).all() == []


def test_middleware_skips_404(mw_client, session):
    assert mw_client.get("/missing").status_code == 404
    assert session.exec(select(PageView)).all() == []


def test_full_app_stack_records_view(engine, session):
    """Regression: the view write must survive the *real* middleware stack
    (TrustedHost + page-view), not just a bare one-middleware app. A response
    BackgroundTask silently no-ops under Starlette's BaseHTTPMiddleware here,
    which is why record_page_view runs via run_in_threadpool instead."""
    from ai_arena_recap.config import settings
    from ai_arena_recap.models import Competition, CompetitionParticipation
    from ai_arena_recap.web.app import create_app

    upsert(session, Competition, {"id": settings.competition_id, "name": "T", "last_synced": NOW})
    _seed_bot(session, 1, "Alpha", "T")
    upsert(session, CompetitionParticipation, {
        "id": 1, "competition_id": settings.competition_id, "bot_id": 1,
        "elo": 1900, "highest_elo": 1950, "division_num": 1, "active": True,
        "match_count": 0, "win_count": 0, "loss_count": 0, "tie_count": 0,
        "crash_count": 0, "last_synced": NOW,
    })
    session.commit()

    # No context manager -> skip the lifespan (scheduler + network sync); the
    # `engine` fixture already created the tables. base_url must be an allowed
    # host or TrustedHostMiddleware returns 400.
    client = TestClient(create_app(), base_url="http://localhost")
    ua = {"user-agent": "Mozilla/5.0 (X11; Linux x86_64)"}
    for _ in range(3):
        assert client.get("/bots/1", headers=ua).status_code == 200

    row = session.exec(select(PageView).where(PageView.path == "/bots/1")).one()
    assert row.count == 3


# ---------------------------------------------------------------------------
# most_viewed_bots
# ---------------------------------------------------------------------------

def _seed_bot(session, bot_id, name, race="T"):
    upsert(session, Bot, {"id": bot_id, "name": name, "plays_race": race, "last_synced": NOW})


def test_most_viewed_bots_sums_across_days_and_ranks(session):
    _seed_bot(session, 1, "Alpha", "T")
    _seed_bot(session, 2, "Beta", "Z")
    # Alpha viewed on two days (3 + 4 = 7), Beta once (5).
    session.add(PageView(path="/bots/1", day=date(2026, 5, 30), count=3))
    session.add(PageView(path="/bots/1", day=date(2026, 5, 31), count=4))
    session.add(PageView(path="/bots/2", day=date(2026, 5, 31), count=5))
    session.commit()

    rows = rankings.most_viewed_bots(session)
    assert [r["name"] for r in rows] == ["Alpha", "Beta"]
    assert rows[0]["value"] == "7"
    assert rows[0]["href"] == "/bots/1"
    assert rows[0]["race"] == "T"


def test_most_viewed_bots_ignores_non_bot_paths_and_missing_bots(session):
    _seed_bot(session, 1, "Alpha", "T")
    session.add(PageView(path="/bots/1", day=date(2026, 5, 31), count=2))
    session.add(PageView(path="/rankings", day=date(2026, 5, 31), count=99))   # not a bot page
    session.add(PageView(path="/bots/999", day=date(2026, 5, 31), count=50))   # no Bot row
    session.commit()

    rows = rankings.most_viewed_bots(session)
    assert [r["name"] for r in rows] == ["Alpha"]
    assert rows[0]["value"] == "2"


def test_most_viewed_bots_empty_when_no_views(session):
    _seed_bot(session, 1, "Alpha", "T")
    session.commit()
    assert rankings.most_viewed_bots(session) == []
