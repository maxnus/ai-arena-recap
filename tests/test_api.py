"""End-to-end tests for the JSON endpoints, using FastAPI TestClient
against a fixture DB."""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from ai_arena_recap.config import settings
from ai_arena_recap.models import Bot, Competition, CompetitionParticipation, Map, Match, MatchParticipation, Round
from ai_arena_recap.sync.common import upsert


def _now():
    return datetime(2026, 4, 25, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def client(engine):
    # create_app's lifespan starts a scheduler we don't want during tests; bypass it.
    from fastapi import FastAPI
    from ai_arena_recap.web.routes import api as api_router
    from ai_arena_recap.web.routes import bot as bot_router
    from ai_arena_recap.web.routes import ladder as ladder_router
    from ai_arena_recap.web.routes import match as match_router

    app = FastAPI()
    app.include_router(ladder_router.router)
    app.include_router(bot_router.router)
    app.include_router(match_router.router)
    app.include_router(api_router.router)
    return TestClient(app)


def _seed_two_bot_ladder(session):
    upsert(session, Competition, {
        "id": settings.competition_id, "name": "Test Comp", "last_synced": _now(),
    })
    for bot_id, name, race, elo, w, l in [(1, "Alpha", "T", 2000, 50, 10), (2, "Beta", "Z", 1800, 30, 20)]:
        upsert(session, Bot, {"id": bot_id, "name": name, "plays_race": race, "type": "python", "last_synced": _now()})
        upsert(session, CompetitionParticipation, {
            "id": bot_id, "competition_id": settings.competition_id, "bot_id": bot_id,
            "elo": elo, "highest_elo": elo + 50, "division_num": 1, "active": True,
            "match_count": w + l, "win_count": w, "loss_count": l, "tie_count": 0, "crash_count": 0,
            "win_perc": 100 * w / (w + l), "loss_perc": 100 * l / (w + l),
            "last_synced": _now(),
        })
    session.commit()


class TestLadderJson:
    def test_returns_active_bots_ranked_by_elo(self, client, session):
        _seed_two_bot_ladder(session)
        r = client.get("/api/ladder.json")
        assert r.status_code == 200
        data = r.json()["data"]
        assert [(row["rank"], row["name"], row["elo"]) for row in data] == [
            (1, "Alpha", 2000),
            (2, "Beta", 1800),
        ]

    def test_excludes_inactive_bots(self, client, session):
        _seed_two_bot_ladder(session)
        beta_cp = session.get(CompetitionParticipation, 2)
        beta_cp.active = False
        session.add(beta_cp)
        session.commit()

        data = client.get("/api/ladder.json").json()["data"]
        assert [row["name"] for row in data] == ["Alpha"]


class TestBotMatchesJson:
    def _seed_match(self, session, *, match_id: int, bot_id: int, opp_id: int, result: str, elo_change: int):
        upsert(session, Competition, {"id": settings.competition_id, "name": "T", "last_synced": _now()})
        upsert(session, Round, {"id": 1, "number": 1, "competition_id": settings.competition_id,
                                "complete": True, "last_synced": _now()})
        upsert(session, Map, {"id": 1, "name": "TestMap", "last_synced": _now()})
        upsert(session, Match, {
            "id": match_id, "round_id": 1, "map_id": 1,
            "started": _now(), "result_created": _now(), "result_game_steps": 11200,
            "last_synced": _now(),
        })
        upsert(session, MatchParticipation, {
            "id": match_id * 10, "match_id": match_id, "bot_id": bot_id, "participant_number": 1,
            "starting_elo": 1500, "resultant_elo": 1500 + elo_change, "elo_change": elo_change,
            "result": result, "last_synced": _now(),
        })
        upsert(session, MatchParticipation, {
            "id": match_id * 10 + 1, "match_id": match_id, "bot_id": opp_id, "participant_number": 2,
            "starting_elo": 1500, "resultant_elo": 1500 - elo_change, "elo_change": -elo_change,
            "result": "loss" if result == "win" else "win", "last_synced": _now(),
        })

    def test_returns_404_for_unknown_bot(self, client, engine):
        r = client.get("/api/bots/9999/matches.json")
        assert r.status_code == 404

    def test_includes_opponent_and_derived_game_steps(self, client, session):
        upsert(session, Bot, {"id": 1, "name": "Alpha", "plays_race": "T", "last_synced": _now()})
        upsert(session, Bot, {"id": 2, "name": "Beta", "plays_race": "Z", "last_synced": _now()})
        self._seed_match(session, match_id=100, bot_id=1, opp_id=2, result="win", elo_change=5)
        session.commit()

        body = client.get("/api/bots/1/matches.json").json()
        assert body["total"] == 1
        assert body["last_page"] == 1
        row = body["data"][0]
        assert row["opponent_name"] == "Beta"
        assert row["opponent_race"] == "Z"
        assert row["result"] == "win"
        assert row["elo_change"] == 5
        assert row["game_steps"] == 11200
        # Derived fields removed from the API on purpose:
        assert "duration_s" not in row
        assert "ingame_duration_s" not in row

    def test_pagination(self, client, session):
        upsert(session, Bot, {"id": 1, "name": "Alpha", "plays_race": "T", "last_synced": _now()})
        upsert(session, Bot, {"id": 2, "name": "Beta", "plays_race": "Z", "last_synced": _now()})
        for i in range(5):
            self._seed_match(session, match_id=200 + i, bot_id=1, opp_id=2, result="win", elo_change=i)
        session.commit()

        body = client.get("/api/bots/1/matches.json?page=1&size=2").json()
        assert body["total"] == 5
        assert body["last_page"] == 3
        assert len(body["data"]) == 2
