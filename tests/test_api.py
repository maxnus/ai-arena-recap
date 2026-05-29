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
def fixed_now(monkeypatch):
    """Pin `utcnow` so window-relative route tests don't drift with the wall
    clock (mirrors the fixture in test_queries.py). The recent-vs route binds
    `utcnow` in its own module namespace, so patch it there."""
    from ai_arena_recap.sync import common as common_module
    from ai_arena_recap.web.routes import api as api_module

    def _fake_utcnow():
        return _now()

    monkeypatch.setattr(common_module, "utcnow", _fake_utcnow)
    monkeypatch.setattr(api_module, "utcnow", _fake_utcnow)
    return _now()


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

    def test_division_zero_routed_to_awaiting(self, client, session):
        """Active bots with division 0 (or null) are awaiting placement,
        not yet ranked — they go into a separate list and don't get a rank
        in the main standings."""
        _seed_two_bot_ladder(session)
        upsert(session, Bot, {"id": 3, "name": "Gamma", "plays_race": "P", "type": "python", "last_synced": _now()})
        upsert(session, CompetitionParticipation, {
            "id": 3, "competition_id": settings.competition_id, "bot_id": 3,
            "elo": None, "highest_elo": None, "division_num": 0, "active": True,
            "match_count": 0, "win_count": 0, "loss_count": 0, "tie_count": 0, "crash_count": 0,
            "win_perc": None, "loss_perc": None, "last_synced": _now(),
        })
        session.commit()

        body = client.get("/api/ladder.json").json()
        assert [row["name"] for row in body["data"]] == ["Alpha", "Beta"]
        assert [row["rank"] for row in body["data"]] == [1, 2]
        assert [row["name"] for row in body["awaiting"]] == ["Gamma"]
        assert "rank" not in body["awaiting"][0]


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


class TestMatchRecentVsJson:
    def _seed_h2h_match(self, session, *, match_id: int, started, winner_id: int | None = 1):
        upsert(session, Match, {
            "id": match_id, "round_id": 1, "map_id": 1,
            "started": started, "result_created": started,
            "result_type": "Player1Win" if winner_id == 1 else "Player2Win",
            "result_winner_bot_id": winner_id,
            "last_synced": _now(),
        })
        upsert(session, MatchParticipation, {
            "id": match_id * 10, "match_id": match_id, "bot_id": 1, "participant_number": 1,
            "result": "win" if winner_id == 1 else "loss", "last_synced": _now(),
        })
        upsert(session, MatchParticipation, {
            "id": match_id * 10 + 1, "match_id": match_id, "bot_id": 2, "participant_number": 2,
            "result": "loss" if winner_id == 1 else "win", "last_synced": _now(),
        })

    def _seed_two_bots(self, session):
        upsert(session, Competition, {"id": settings.competition_id, "name": "T", "last_synced": _now()})
        upsert(session, Round, {"id": 1, "number": 1, "competition_id": settings.competition_id,
                                "complete": True, "last_synced": _now()})
        upsert(session, Map, {"id": 1, "name": "TestMap", "last_synced": _now()})
        upsert(session, Bot, {"id": 1, "name": "Alpha", "plays_race": "T", "last_synced": _now()})
        upsert(session, Bot, {"id": 2, "name": "Beta", "plays_race": "Z", "last_synced": _now()})

    def test_404_for_unknown_match(self, client, engine):
        r = client.get("/api/matches/9999/recent-vs.json")
        assert r.status_code == 404

    def test_returns_only_h2h_matches_within_window(self, client, session, fixed_now):
        self._seed_two_bots(session)
        # Two h2h matches inside the default 30d window:
        self._seed_h2h_match(session, match_id=100, started=_now(), winner_id=1)
        self._seed_h2h_match(session, match_id=101, started=_now(), winner_id=2)
        # And one that pre-dates the window — should be filtered out.
        from datetime import timedelta
        self._seed_h2h_match(session, match_id=102, started=_now() - timedelta(days=60), winner_id=1)
        # And a third bot in another match that is *not* h2h:
        upsert(session, Bot, {"id": 3, "name": "Gamma", "plays_race": "P", "last_synced": _now()})
        upsert(session, Match, {"id": 200, "round_id": 1, "map_id": 1,
                                "started": _now(), "result_created": _now(),
                                "last_synced": _now()})
        upsert(session, MatchParticipation, {"id": 2000, "match_id": 200, "bot_id": 1,
                                             "participant_number": 1, "result": "win",
                                             "last_synced": _now()})
        upsert(session, MatchParticipation, {"id": 2001, "match_id": 200, "bot_id": 3,
                                             "participant_number": 2, "result": "loss",
                                             "last_synced": _now()})
        session.commit()

        body = client.get("/api/matches/100/recent-vs.json").json()
        assert sorted(row["match_id"] for row in body["data"]) == [100, 101]
        assert sorted(body["bot_ids"]) == [1, 2]
        assert sorted(body["bot_names"]) == ["Alpha", "Beta"]
        # Winner names are resolved.
        names = {row["match_id"]: row["winner_name"] for row in body["data"]}
        assert names == {100: "Alpha", 101: "Beta"}


class TestBotMatchupsJson:
    def test_404_for_unknown_bot(self, client, engine):
        r = client.get("/api/bots/9999/matchups.json")
        assert r.status_code == 404

    def test_returns_window_and_min_games_metadata(self, client, session):
        upsert(session, Bot, {"id": 1, "name": "Alpha", "plays_race": "T", "last_synced": _now()})
        session.commit()

        body = client.get("/api/bots/1/matchups.json").json()
        assert body["data"] == []
        assert body["window_days"] == 60
        assert body["min_games"] == 10

    def test_echoes_custom_window_and_min_games(self, client, session):
        upsert(session, Bot, {"id": 1, "name": "Alpha", "plays_race": "T", "last_synced": _now()})
        session.commit()

        body = client.get("/api/bots/1/matchups.json?window_days=30&min_games=5").json()
        assert body["window_days"] == 30
        assert body["min_games"] == 5

    def test_rejects_out_of_range_params(self, client, session):
        upsert(session, Bot, {"id": 1, "name": "Alpha", "plays_race": "T", "last_synced": _now()})
        session.commit()

        assert client.get("/api/bots/1/matchups.json?window_days=0").status_code == 422
        assert client.get("/api/bots/1/matchups.json?window_days=400").status_code == 422
        assert client.get("/api/bots/1/matchups.json?min_games=0").status_code == 422


class TestBotSearchJson:
    def _seed_bot(self, session, *, bot_id, name, race="T", author=None, active=None, elo=None, highest_elo=None):
        upsert(session, Bot, {
            "id": bot_id, "name": name, "plays_race": race, "user_name": author,
            "type": "python", "last_synced": _now(),
        })
        if active is not None:
            upsert(session, Competition, {
                "id": settings.competition_id, "name": "T", "last_synced": _now(),
            })
            upsert(session, CompetitionParticipation, {
                "id": bot_id, "competition_id": settings.competition_id, "bot_id": bot_id,
                "elo": elo, "highest_elo": highest_elo, "active": active,
                "last_synced": _now(),
            })

    def test_empty_query_returns_no_data(self, client, session):
        self._seed_bot(session, bot_id=1, name="Alpha", active=True, elo=1500)
        session.commit()
        body = client.get("/api/bots/search.json?q=").json()
        assert body == {"data": []}

    def test_substring_match_case_insensitive(self, client, session):
        self._seed_bot(session, bot_id=1, name="MyCoolBot", active=True, elo=1500)
        self._seed_bot(session, bot_id=2, name="OtherBot", active=True, elo=1400)
        session.commit()
        names = [r["name"] for r in client.get("/api/bots/search.json?q=cool").json()["data"]]
        assert names == ["MyCoolBot"]
        names = [r["name"] for r in client.get("/api/bots/search.json?q=COOL").json()["data"]]
        assert names == ["MyCoolBot"]

    def test_includes_inactive_bots(self, client, session):
        self._seed_bot(session, bot_id=1, name="ActiveBot", active=True, elo=1600)
        self._seed_bot(session, bot_id=2, name="InactiveBot", active=False, elo=None, highest_elo=1900)
        self._seed_bot(session, bot_id=3, name="OffLadderBot")  # no participation row at all
        session.commit()
        body = client.get("/api/bots/search.json?q=bot").json()
        by_name = {r["name"]: r for r in body["data"]}
        assert by_name["ActiveBot"]["active"] is True
        assert by_name["InactiveBot"]["active"] is False
        assert by_name["InactiveBot"]["in_competition"] is True
        assert by_name["InactiveBot"]["highest_elo"] == 1900
        assert by_name["OffLadderBot"]["active"] is False
        assert by_name["OffLadderBot"]["in_competition"] is False

    def test_orders_exact_then_prefix_then_contains(self, client, session):
        self._seed_bot(session, bot_id=1, name="ZooBot", active=True, elo=1500)         # contains
        self._seed_bot(session, bot_id=2, name="bot", active=True, elo=1400)            # exact
        self._seed_bot(session, bot_id=3, name="BotMaster", active=True, elo=1300)       # prefix
        session.commit()
        names = [r["name"] for r in client.get("/api/bots/search.json?q=bot").json()["data"]]
        assert names == ["bot", "BotMaster", "ZooBot"]

    def test_active_before_inactive_within_same_tier(self, client, session):
        self._seed_bot(session, bot_id=1, name="OldBot", active=False, elo=None, highest_elo=2000)
        self._seed_bot(session, bot_id=2, name="NewBot", active=True, elo=1500)
        session.commit()
        names = [r["name"] for r in client.get("/api/bots/search.json?q=bot").json()["data"]]
        assert names == ["NewBot", "OldBot"]

    def test_like_wildcards_in_query_are_escaped(self, client, session):
        self._seed_bot(session, bot_id=1, name="Foo", active=True, elo=1500)
        self._seed_bot(session, bot_id=2, name="F_oo", active=True, elo=1400)
        session.commit()
        names = [r["name"] for r in client.get("/api/bots/search.json?q=_").json()["data"]]
        assert names == ["F_oo"]

    def test_limit_caps_results(self, client, session):
        for i in range(5):
            self._seed_bot(session, bot_id=i + 1, name=f"Bot{i}", active=True, elo=1500 + i)
        session.commit()
        body = client.get("/api/bots/search.json?q=bot&limit=2").json()
        assert len(body["data"]) == 2


class TestBotRankHistoryJson:
    def test_404_for_unknown_bot(self, client, engine):
        r = client.get("/api/bots/9999/rank-history.json")
        assert r.status_code == 404

    def test_empty_data_when_no_matches(self, client, session):
        upsert(session, Bot, {"id": 1, "name": "Alpha", "plays_race": "T", "last_synced": _now()})
        session.commit()

        body = client.get("/api/bots/1/rank-history.json").json()
        assert body == {"data": []}
