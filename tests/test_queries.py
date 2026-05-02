"""Tests for the read queries shared by the page routes and the JSON API."""
from datetime import datetime, timedelta, timezone

import pytest

from ai_arena_recap.config import settings
from ai_arena_recap.models import (
    Bot,
    Competition,
    Map,
    Match,
    MatchParticipation,
    Round,
)
from ai_arena_recap.sync.common import upsert
from ai_arena_recap.web.queries import (
    MATCHUP_MIN_GAMES,
    bot_rank_history,
    recent_matchups,
    winrate_by_race,
)

NOW = datetime(2026, 4, 25, 12, 0, 0, tzinfo=timezone.utc)


def _seed_competition(session) -> None:
    upsert(session, Competition, {"id": settings.competition_id, "name": "Test", "last_synced": NOW})
    upsert(session, Round, {
        "id": 1, "number": 1, "competition_id": settings.competition_id,
        "complete": True, "last_synced": NOW,
    })
    upsert(session, Map, {"id": 1, "name": "TestMap", "last_synced": NOW})


def _seed_bot(session, bot_id: int, name: str, race: str) -> None:
    upsert(session, Bot, {"id": bot_id, "name": name, "plays_race": race, "last_synced": NOW})


def _seed_match(
    session,
    *,
    match_id: int,
    bot_a: int,
    bot_b: int,
    a_result: str,
    started: datetime,
    elo_change_a: int = 5,
    game_steps: int = 11200,
) -> None:
    """Seed a finished 1v1 match with both participations."""
    b_result = {"win": "loss", "loss": "win", "tie": "tie"}[a_result]
    winner_id = bot_a if a_result == "win" else (bot_b if a_result == "loss" else None)
    upsert(session, Match, {
        "id": match_id, "round_id": 1, "map_id": 1,
        "started": started, "result_created": started,
        "result_winner_bot_id": winner_id,
        "result_game_steps": game_steps,
        "last_synced": NOW,
    })
    upsert(session, MatchParticipation, {
        "id": match_id * 10, "match_id": match_id, "bot_id": bot_a, "participant_number": 1,
        "starting_elo": 1500, "resultant_elo": 1500 + elo_change_a, "elo_change": elo_change_a,
        "result": a_result, "last_synced": NOW,
    })
    upsert(session, MatchParticipation, {
        "id": match_id * 10 + 1, "match_id": match_id, "bot_id": bot_b, "participant_number": 2,
        "starting_elo": 1500, "resultant_elo": 1500 - elo_change_a, "elo_change": -elo_change_a,
        "result": b_result, "last_synced": NOW,
    })


@pytest.fixture()
def fixed_now(monkeypatch):
    """Pin `utcnow` so window-relative tests are deterministic."""
    from ai_arena_recap.sync import common as common_module
    from ai_arena_recap.web import queries as queries_module

    def _fake_utcnow():
        return NOW

    monkeypatch.setattr(common_module, "utcnow", _fake_utcnow)
    monkeypatch.setattr(queries_module, "utcnow", _fake_utcnow)
    return NOW


class TestWinrateByRace:
    def test_groups_results_by_opponent_race_with_ties_as_half_win(self, session):
        _seed_competition(session)
        _seed_bot(session, 1, "Alpha", "T")
        _seed_bot(session, 2, "ZergOpp", "Z")
        _seed_bot(session, 3, "ProtossOpp", "P")

        # vs Z: 2-1-1 (W L W T) -> 2.5/4 = 62.5%
        for i, result in enumerate(["win", "loss", "win", "tie"]):
            _seed_match(session, match_id=100 + i, bot_a=1, bot_b=2,
                        a_result=result, started=NOW - timedelta(days=i + 1))
        # vs P: 1-1 -> 50%
        for i, result in enumerate(["win", "loss"]):
            _seed_match(session, match_id=200 + i, bot_a=1, bot_b=3,
                        a_result=result, started=NOW - timedelta(days=i + 1))
        session.commit()

        wr = winrate_by_race(session, 1)
        assert wr["Z"] == {"matches": 4, "wins": 2, "losses": 1, "ties": 1, "win_rate": 62.5}
        assert wr["P"] == {"matches": 2, "wins": 1, "losses": 1, "ties": 0, "win_rate": 50.0}

    def test_excludes_other_competitions(self, session):
        _seed_competition(session)
        # A round in a *different* competition.
        upsert(session, Competition, {"id": 999, "name": "Other", "last_synced": NOW})
        upsert(session, Round, {"id": 2, "number": 1, "competition_id": 999,
                                "complete": True, "last_synced": NOW})
        _seed_bot(session, 1, "Alpha", "T")
        _seed_bot(session, 2, "Opp", "Z")

        # One win in the current competition...
        _seed_match(session, match_id=10, bot_a=1, bot_b=2, a_result="win", started=NOW - timedelta(days=1))
        # ...and a win in the *other* competition that should not count.
        upsert(session, Match, {
            "id": 20, "round_id": 2, "map_id": 1,
            "started": NOW - timedelta(days=1), "result_created": NOW - timedelta(days=1),
            "result_winner_bot_id": 1, "last_synced": NOW,
        })
        upsert(session, MatchParticipation, {
            "id": 200, "match_id": 20, "bot_id": 1, "participant_number": 1,
            "result": "win", "last_synced": NOW,
        })
        upsert(session, MatchParticipation, {
            "id": 201, "match_id": 20, "bot_id": 2, "participant_number": 2,
            "result": "loss", "last_synced": NOW,
        })
        session.commit()

        wr = winrate_by_race(session, 1)
        assert wr["Z"]["matches"] == 1


class TestRecentMatchups:
    def test_filters_to_min_games_and_returns_expected_shape(self, session, fixed_now):
        _seed_competition(session)
        _seed_bot(session, 1, "Alpha", "T")
        _seed_bot(session, 2, "Frequent", "Z")
        _seed_bot(session, 3, "Rare", "P")

        # MATCHUP_MIN_GAMES wins against bot 2 — should appear.
        for i in range(MATCHUP_MIN_GAMES):
            _seed_match(session, match_id=100 + i, bot_a=1, bot_b=2,
                        a_result="win", started=NOW - timedelta(days=i + 1))
        # Below threshold against bot 3 — should be filtered out.
        for i in range(MATCHUP_MIN_GAMES - 1):
            _seed_match(session, match_id=200 + i, bot_a=1, bot_b=3,
                        a_result="win", started=NOW - timedelta(days=i + 1))
        session.commit()

        matchups = recent_matchups(session, 1)
        assert [m["opp_id"] for m in matchups] == [2]
        m = matchups[0]
        assert m["matches"] == MATCHUP_MIN_GAMES
        assert m["wins"] == MATCHUP_MIN_GAMES
        assert m["win_rate"] == 100.0
        assert "history" in m
        assert len(m["history"]) == MATCHUP_MIN_GAMES

    def test_ignores_matches_outside_window(self, session, fixed_now):
        _seed_competition(session)
        _seed_bot(session, 1, "Alpha", "T")
        _seed_bot(session, 2, "Old", "Z")

        # All games are outside the 60-day window.
        for i in range(MATCHUP_MIN_GAMES):
            _seed_match(session, match_id=300 + i, bot_a=1, bot_b=2,
                        a_result="win", started=NOW - timedelta(days=70 + i))
        session.commit()

        assert recent_matchups(session, 1) == []

    def test_trend_pp_uses_first_vs_second_half(self, session, fixed_now):
        _seed_competition(session)
        _seed_bot(session, 1, "Alpha", "T")
        _seed_bot(session, 2, "Opp", "Z")

        # Earlier half (older than 30 days): 5 losses. Recent half: 5 wins.
        for i in range(5):
            _seed_match(session, match_id=400 + i, bot_a=1, bot_b=2,
                        a_result="loss", started=NOW - timedelta(days=45 + i))
        for i in range(5):
            _seed_match(session, match_id=500 + i, bot_a=1, bot_b=2,
                        a_result="win", started=NOW - timedelta(days=5 + i))
        session.commit()

        m = recent_matchups(session, 1)[0]
        assert m["wins"] == 5
        assert m["losses"] == 5
        assert m["trend_pp"] == pytest.approx(100.0)  # 100% recent − 0% earlier


class TestBotRankHistory:
    def test_ranks_bots_per_round_by_mean_resultant_elo(self, session):
        _seed_competition(session)
        # Add a second round to confirm we get one row per round.
        upsert(session, Round, {
            "id": 2, "number": 2, "competition_id": settings.competition_id,
            "complete": True, "last_synced": NOW,
        })
        _seed_bot(session, 1, "Alpha", "T")
        _seed_bot(session, 2, "Beta", "Z")

        # Round 1: Alpha mean elo 1600, Beta mean 1500 -> Alpha rank 1.
        upsert(session, Match, {"id": 1, "round_id": 1, "map_id": 1,
                                "started": NOW, "result_created": NOW,
                                "last_synced": NOW})
        upsert(session, MatchParticipation, {"id": 11, "match_id": 1, "bot_id": 1,
                                             "participant_number": 1, "resultant_elo": 1600,
                                             "result": "win", "last_synced": NOW})
        upsert(session, MatchParticipation, {"id": 12, "match_id": 1, "bot_id": 2,
                                             "participant_number": 2, "resultant_elo": 1500,
                                             "result": "loss", "last_synced": NOW})
        # Round 2: Alpha 1550, Beta 1620 -> Beta rank 1, Alpha rank 2.
        upsert(session, Match, {"id": 2, "round_id": 2, "map_id": 1,
                                "started": NOW, "result_created": NOW,
                                "last_synced": NOW})
        upsert(session, MatchParticipation, {"id": 21, "match_id": 2, "bot_id": 1,
                                             "participant_number": 1, "resultant_elo": 1550,
                                             "result": "loss", "last_synced": NOW})
        upsert(session, MatchParticipation, {"id": 22, "match_id": 2, "bot_id": 2,
                                             "participant_number": 2, "resultant_elo": 1620,
                                             "result": "win", "last_synced": NOW})
        session.commit()

        alpha_history = bot_rank_history(session, 1)
        assert [(r["round_number"], r["rank"]) for r in alpha_history] == [(1, 1), (2, 2)]
        assert alpha_history[0]["mean_elo"] == 1600.0

        beta_history = bot_rank_history(session, 2)
        assert [(r["round_number"], r["rank"]) for r in beta_history] == [(1, 2), (2, 1)]
