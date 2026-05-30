"""Tests for the Top-5 rankings page and its query helpers."""
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from ai_arena_recap.config import settings
from ai_arena_recap.web.deps import WEB_DIR
from ai_arena_recap.models import (
    Bot,
    Competition,
    CompetitionParticipation,
    Map,
    Match,
    MatchParticipation,
    Round,
)
from ai_arena_recap.sync.common import upsert
from ai_arena_recap.web import rankings
from ai_arena_recap.web.routes import rankings as rankings_route

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
COMP = settings.competition_id


@pytest.fixture(autouse=True)
def _clear_rankings_cache():
    # The rankings cache is a module-global keyed on a data fingerprint. Two
    # tests with coincidentally-equal fingerprints (e.g. both an empty DB) could
    # otherwise share a stale entry, so reset it around every test.
    rankings._CACHE["key"] = None
    rankings._CACHE["value"] = None
    yield
    rankings._CACHE["key"] = None
    rankings._CACHE["value"] = None


@pytest.fixture()
def client(engine):
    # Mirror test_api.py: a bare app with just the route under test, so the real
    # app's lifespan (background sync) and TrustedHost middleware don't interfere.
    # The `engine` fixture monkeypatches the DB so the route sees the test data.
    app = FastAPI()
    # base.html references url_for('static', ...), so the route must exist.
    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")
    app.include_router(rankings_route.router)
    return TestClient(app)


def _seed_base(session):
    upsert(session, Competition, {"id": COMP, "name": "Test Cup", "last_synced": NOW})
    upsert(session, Map, {"id": 1, "name": "Acropolis", "last_synced": NOW})
    upsert(session, Round, {
        "id": 1, "number": 1, "competition_id": COMP, "complete": True, "last_synced": NOW,
    })


def _seed_bot(session, bot_id, name, race="T", *, user="alice", created=None, updated=None):
    upsert(session, Bot, {
        "id": bot_id, "name": name, "plays_race": race, "user_name": user,
        "created": created, "bot_zip_updated": updated, "last_synced": NOW,
    })


def _seed_cp(session, bot_id, *, elo=1600, highest=None, matches=0, wins=0, ties=0, active=True):
    upsert(session, CompetitionParticipation, {
        "id": bot_id, "competition_id": COMP, "bot_id": bot_id, "elo": elo,
        "highest_elo": highest if highest is not None else elo,
        "division_num": 1, "active": active,
        "match_count": matches, "win_count": wins, "tie_count": ties,
        "win_perc": (100.0 * wins / matches) if matches else None,
        "last_synced": NOW,
    })


def _seed_round(session, round_id, number):
    upsert(session, Round, {
        "id": round_id, "number": number, "competition_id": COMP,
        "complete": True, "last_synced": NOW,
    })


def _seed_match(session, mid, *, a, a_res, a_elo, b, b_res, b_elo, steps, started,
                a_res_elo=None, b_res_elo=None, round_id=1, a_step=None, b_step=None):
    upsert(session, Match, {
        "id": mid, "round_id": round_id, "map_id": 1, "started": started,
        "result_game_steps": steps, "last_synced": NOW,
    })
    upsert(session, MatchParticipation, {
        "id": mid * 2 - 1, "match_id": mid, "bot_id": a, "participant_number": 1,
        "starting_elo": a_elo, "resultant_elo": a_res_elo if a_res_elo is not None else a_elo,
        "result": a_res, "avg_step_time": a_step, "last_synced": NOW,
    })
    upsert(session, MatchParticipation, {
        "id": mid * 2, "match_id": mid, "bot_id": b, "participant_number": 2,
        "starting_elo": b_elo, "resultant_elo": b_res_elo if b_res_elo is not None else b_elo,
        "result": b_res, "avg_step_time": b_step, "last_synced": NOW,
    })


def _minutes(i):
    return NOW.replace(minute=i)


def test_longest_win_streak(session):
    _seed_base(session)
    _seed_bot(session, 1, "Alpha")
    _seed_bot(session, 2, "Beta", "Z")
    _seed_cp(session, 1)
    _seed_cp(session, 2)
    # Alpha results in chronological order: W W W L W -> best streak 3.
    for i, res in enumerate(["win", "win", "win", "loss", "win"]):
        opp = "loss" if res == "win" else "win"
        _seed_match(session, i + 1, a=1, a_res=res, a_elo=1600,
                    b=2, b_res=opp, b_elo=1600, steps=5000, started=_minutes(i))
    session.commit()

    rows = rankings.longest_win_streak(session)
    top = next(row for row in rows if row["name"] == "Alpha")
    assert top["value"] == "3"


def test_best_vs_race(session):
    _seed_base(session)
    _seed_bot(session, 1, "Alpha", "T")
    _seed_bot(session, 2, "Zergling", "Z")
    _seed_cp(session, 1)
    _seed_cp(session, 2)
    for i in range(2):  # Alpha beats a Zerg twice
        _seed_match(session, i + 1, a=1, a_res="win", a_elo=1600,
                    b=2, b_res="loss", b_elo=1600, steps=5000, started=_minutes(i))
    session.commit()

    rows = rankings.best_vs_race(session, "Z", min_vs=2)
    assert rows and rows[0]["name"] == "Alpha"
    # Card now ranks by race-specific ELO; two wins push Alpha's vs-Zerg ELO
    # above its 1600 start.
    assert int(rows[0]["value"]) > 1600


def test_biggest_upsets(session):
    _seed_base(session)
    _seed_bot(session, 1, "David", "T")
    _seed_bot(session, 2, "Goliath", "Z")
    _seed_cp(session, 1)
    _seed_cp(session, 2)
    _seed_match(session, 1, a=1, a_res="win", a_elo=1500,
                b=2, b_res="loss", b_elo=1700, steps=5000, started=NOW)
    session.commit()

    rows = rankings.biggest_upsets(session)
    assert rows[0]["name"] == "David"
    assert rows[0]["value"] == "+200"
    assert rows[0]["sub"] == "beat Goliath"


def test_biggest_upsets_one_row_per_bot(session):
    _seed_base(session)
    _seed_bot(session, 1, "David", "T")
    _seed_bot(session, 2, "Goliath", "Z")
    _seed_bot(session, 3, "Titan", "P")
    for bid in (1, 2, 3):
        _seed_cp(session, bid)
    # David pulls off two upsets (+200 and +150); only the bigger should show, once.
    _seed_match(session, 1, a=1, a_res="win", a_elo=1500,
                b=2, b_res="loss", b_elo=1700, steps=5000, started=_minutes(0))
    _seed_match(session, 2, a=1, a_res="win", a_elo=1500,
                b=3, b_res="loss", b_elo=1650, steps=5000, started=_minutes(1))
    session.commit()

    rows = rankings.biggest_upsets(session)
    names = [r["name"] for r in rows]
    assert names.count("David") == 1
    david = next(r for r in rows if r["name"] == "David")
    assert david["value"] == "+200"
    assert david["sub"] == "beat Goliath"


def test_most_balanced(session):
    _seed_base(session)
    _seed_bot(session, 1, "Allrounder", "T")
    _seed_bot(session, 2, "T2", "T")
    _seed_bot(session, 3, "Z2", "Z")
    _seed_bot(session, 4, "P2", "P")
    for bid in (1, 2, 3, 4):
        _seed_cp(session, bid)
    # Allrounder wins once vs each race -> all win rates 100%, spread 0.
    for mid, (opp, _race) in enumerate(((2, "T"), (3, "Z"), (4, "P")), start=1):
        _seed_match(session, mid, a=1, a_res="win", a_elo=1600,
                    b=opp, b_res="loss", b_elo=1600, steps=5000, started=_minutes(mid))
    session.commit()

    rows = rankings.most_balanced(session, min_per_race=1)
    top = next(row for row in rows if row["name"] == "Allrounder")
    assert top["value"] == "0 pp"


def test_most_efficient(session):
    _seed_base(session)
    _seed_bot(session, 1, "FastStrong", "T")
    _seed_bot(session, 2, "SlowStrong", "Z")
    _seed_bot(session, 3, "Punchbag", "P")
    _seed_cp(session, 1, elo=2000)
    _seed_cp(session, 2, elo=2000)
    _seed_cp(session, 3, elo=1600)
    # avg_step_time is in seconds. Efficiency = (ELO - 1600) / (step_time * 1000):
    # FastStrong (2000-1600)/(0.01*1000) = 40; SlowStrong .../(0.04*1000) = 10.
    _seed_match(session, 1, a=1, a_res="win", a_elo=1600, b=3, b_res="loss", b_elo=1600,
                steps=5000, started=_minutes(0), a_step=0.01, b_step=0.01)
    _seed_match(session, 2, a=2, a_res="win", a_elo=1600, b=3, b_res="loss", b_elo=1600,
                steps=5000, started=_minutes(1), a_step=0.04, b_step=0.01)
    session.commit()

    rows = rankings.most_efficient(session, min_matches=1)
    names = [r["name"] for r in rows]
    assert names[0] == "FastStrong"
    assert names.index("FastStrong") < names.index("SlowStrong")
    assert rows[0]["value"] == "40.0"


def test_top_authors_by_mean_elo(session):
    _seed_base(session)
    _seed_bot(session, 1, "A1", user="alice")
    _seed_bot(session, 2, "A2", user="alice")
    _seed_bot(session, 3, "B1", user="bob")
    _seed_cp(session, 1, elo=1800)
    _seed_cp(session, 2, elo=1600)  # alice mean 1700
    _seed_cp(session, 3, elo=2000)  # bob has only 1 bot -> excluded by min_bots
    session.commit()

    rows = rankings.top_authors_by_mean_elo(session)
    assert [row["name"] for row in rows] == ["alice"]
    assert rows[0]["value"] == "1700"
    assert rows[0]["sub"] == "2 bots"


def test_oldest_and_newest(session):
    _seed_base(session)
    _seed_bot(session, 1, "Old", created=datetime(2019, 5, 1, tzinfo=timezone.utc))
    _seed_bot(session, 2, "New", created=datetime(2025, 5, 1, tzinfo=timezone.utc))
    _seed_cp(session, 1)
    _seed_cp(session, 2)
    session.commit()

    assert rankings.oldest_bots(session)[0]["name"] == "Old"
    assert rankings.newest_bots(session)[0]["name"] == "New"


def test_tie_rate(session):
    _seed_base(session)
    _seed_bot(session, 1, "Drawish", "T")
    _seed_bot(session, 2, "Decisive", "Z")
    _seed_bot(session, 3, "TinySample", "P")
    _seed_cp(session, 1, matches=40, ties=10)   # 25%
    _seed_cp(session, 2, matches=40, ties=2)    # 5%
    _seed_cp(session, 3, matches=5, ties=5)     # 100% but below the min-games floor
    session.commit()

    rows = rankings.tie_rate(session, min_matches=20)
    names = [r["name"] for r in rows]
    assert "TinySample" not in names
    assert rows[0]["name"] == "Drawish"
    assert rows[0]["value"] == "25.0%"


def test_most_decisive(session):
    _seed_base(session)
    _seed_bot(session, 1, "NeverTiesBusy", "T")
    _seed_bot(session, 2, "NeverTiesQuiet", "Z")
    _seed_bot(session, 3, "Drawish", "P")
    _seed_bot(session, 4, "TinySample", "R")
    _seed_cp(session, 1, matches=80, ties=0)    # 0%, most games
    _seed_cp(session, 2, matches=30, ties=0)    # 0%, fewer games
    _seed_cp(session, 3, matches=40, ties=10)   # 25%
    _seed_cp(session, 4, matches=5, ties=0)     # 0% but below the min-games floor
    session.commit()

    rows = rankings.most_decisive(session, min_matches=20)
    names = [r["name"] for r in rows]
    assert "TinySample" not in names
    # 0%-tie bots come first, busiest among them on top; Drawish last.
    assert names[0] == "NeverTiesBusy"
    assert names[1] == "NeverTiesQuiet"
    assert names[-1] == "Drawish"
    assert rows[0]["value"] == "0.0%"


def test_elo_volatility(session):
    _seed_base(session)  # round 1
    for rnum in range(2, 6):
        _seed_round(session, rnum, rnum)
    _seed_bot(session, 1, "Swingy", "T")
    _seed_bot(session, 2, "Steady", "Z")
    _seed_cp(session, 1)
    _seed_cp(session, 2)
    # One match per round, so each match's resultant_elo is that round's
    # end-of-round ELO. Swingy yo-yos round to round; Steady barely moves.
    swingy = [1400, 2000, 1300, 1900, 1350]
    steady = [1600, 1605, 1598, 1602, 1600]
    for i, (se, st) in enumerate(zip(swingy, steady)):
        _seed_match(session, i + 1, a=1, a_res="win", a_elo=1600,
                    b=2, b_res="loss", b_elo=1600, steps=5000, started=_minutes(i),
                    a_res_elo=se, b_res_elo=st, round_id=i + 1)
    session.commit()

    rows = rankings.elo_volatility(session, min_rounds=5)
    assert rows[0]["name"] == "Swingy"
    assert rows[0]["value"].startswith("±")
    swingy_sd = next(r for r in rows if r["name"] == "Swingy")["value"]
    steady_sd = next(r for r in rows if r["name"] == "Steady")["value"]
    assert int(swingy_sd.lstrip("±")) > int(steady_sd.lstrip("±"))


def test_elo_volatility_uses_end_of_round_not_intra_round(session):
    # A bot whose ELO bounces wildly *within* a round but ends every round at
    # the same value should read as NOT volatile.
    _seed_base(session)  # round 1
    for rnum in range(2, 6):
        _seed_round(session, rnum, rnum)
    _seed_bot(session, 1, "Churny", "T")
    _seed_bot(session, 2, "Opp", "Z")
    _seed_cp(session, 1)
    _seed_cp(session, 2)
    mid = 1
    for rnum in range(1, 6):
        # Two matches in the round: a wild mid-round value, then settle to 1600.
        _seed_match(session, mid, a=1, a_res="win", a_elo=1600, b=2, b_res="loss",
                    b_elo=1600, steps=5000, started=_minutes(0), a_res_elo=1200, round_id=rnum)
        mid += 1
        _seed_match(session, mid, a=1, a_res="loss", a_elo=1600, b=2, b_res="win",
                    b_elo=1600, steps=5000, started=_minutes(1), a_res_elo=1600, round_id=rnum)
        mid += 1
    session.commit()

    rows = rankings.elo_volatility(session, min_rounds=5)
    churny = next(r for r in rows if r["name"] == "Churny")
    assert churny["value"] == "±0"  # every round ends at 1600


def test_all_rankings_caches_until_data_changes(session):
    _seed_base(session)
    _seed_bot(session, 1, "Alpha", "T")
    _seed_cp(session, 1, elo=1900, highest=1950)
    session.commit()

    first = rankings.all_rankings(session)
    # Same data -> same cached object (no rebuild).
    assert rankings.all_rankings(session) is first

    # A change to the underlying data invalidates the fingerprint -> rebuild.
    _seed_bot(session, 2, "Beta", "Z")
    _seed_cp(session, 2, elo=2000, highest=2050)
    session.commit()
    second = rankings.all_rankings(session)
    assert second is not first
    assert "Beta" in {r["name"] for g in second for c in g["cards"] for r in c["rows"]}


def test_warm_rankings_populates_cache(engine, session):
    _seed_base(session)
    _seed_bot(session, 1, "Alpha", "T")
    _seed_cp(session, 1, elo=1900, highest=1950)
    session.commit()

    assert rankings._CACHE["value"] is None
    rankings.warm_rankings()  # opens its own session via the (patched) engine
    assert rankings._CACHE["value"] is not None
    # A subsequent page build reuses the warmed cache (no rebuild).
    assert rankings.all_rankings(session) is rankings._CACHE["value"]


def test_warm_rankings_never_raises(monkeypatch):
    # Warming is best-effort; a failure mid-build must not propagate (it would
    # otherwise break the sync pass that calls it).
    def boom(_session):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(rankings, "_build_rankings", boom)
    rankings.warm_rankings()  # must not raise


def test_rankings_page_renders(client, session):
    _seed_base(session)
    _seed_bot(session, 1, "Alpha", "T", created=datetime(2020, 1, 1, tzinfo=timezone.utc))
    _seed_cp(session, 1, elo=1900, highest=1950)
    session.commit()

    resp = client.get("/rankings")
    assert resp.status_code == 200
    assert "Top 10 Rankings" in resp.text
    assert "ELO &amp; Wins" in resp.text
    assert "Alpha" in resp.text
