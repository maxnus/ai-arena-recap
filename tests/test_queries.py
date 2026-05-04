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
    bot_avg_match_stats,
    bot_current_race_elo,
    bot_race_elo_history,
    bot_rank_history,
    competition_round_ends,
    recent_matchups,
    round_position_for_timestamp,
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

    def test_custom_min_games_overrides_default(self, session, fixed_now):
        _seed_competition(session)
        _seed_bot(session, 1, "Alpha", "T")
        _seed_bot(session, 2, "Opp", "Z")

        # 5 games — below default MATCHUP_MIN_GAMES, but enough for min_games=3.
        for i in range(5):
            _seed_match(session, match_id=600 + i, bot_a=1, bot_b=2,
                        a_result="win", started=NOW - timedelta(days=i + 1))
        session.commit()

        assert recent_matchups(session, 1) == []
        m = recent_matchups(session, 1, min_games=3)
        assert len(m) == 1 and m[0]["matches"] == 5

    def test_custom_window_days_extends_lookback(self, session, fixed_now):
        _seed_competition(session)
        _seed_bot(session, 1, "Alpha", "T")
        _seed_bot(session, 2, "Old", "Z")

        # Outside default 60-day window but inside a 120-day window.
        for i in range(MATCHUP_MIN_GAMES):
            _seed_match(session, match_id=700 + i, bot_a=1, bot_b=2,
                        a_result="win", started=NOW - timedelta(days=70 + i))
        session.commit()

        assert recent_matchups(session, 1) == []
        m = recent_matchups(session, 1, window_days=120)
        assert len(m) == 1 and m[0]["matches"] == MATCHUP_MIN_GAMES

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
        assert alpha_history[0]["end_elo"] == 1600.0

        beta_history = bot_rank_history(session, 2)
        assert [(r["round_number"], r["rank"]) for r in beta_history] == [(1, 2), (2, 1)]


class TestBotAvgMatchStats:
    def test_returns_none_when_bot_has_no_matches(self, session):
        _seed_competition(session)
        _seed_bot(session, 1, "Alpha", "T")
        session.commit()

        stats = bot_avg_match_stats(session, 1)
        assert stats == {"avg_duration_s": None, "avg_step_time_s": None}

    def test_averages_steps_and_step_time_for_wlt_only(self, session):
        _seed_competition(session)
        _seed_bot(session, 1, "Alpha", "T")
        _seed_bot(session, 2, "Beta", "Z")

        # Two W/L/T matches with steps 11200 (=500s) and 22400 (=1000s) → avg 750s.
        _seed_match(session, match_id=10, bot_a=1, bot_b=2,
                    a_result="win", started=NOW - timedelta(days=1), game_steps=11200)
        _seed_match(session, match_id=11, bot_a=1, bot_b=2,
                    a_result="loss", started=NOW - timedelta(days=2), game_steps=22400)
        # Set distinct avg_step_time on each participation.
        for mp_id, t in [(100, 0.010), (110, 0.020)]:
            mp = session.get(MatchParticipation, mp_id)
            mp.avg_step_time = t
            session.add(mp)
        # An "error" match with no W/L/T result on the bot's side — should be excluded.
        upsert(session, Match, {
            "id": 12, "round_id": 1, "map_id": 1,
            "started": NOW, "result_created": NOW, "result_game_steps": 99999,
            "last_synced": NOW,
        })
        upsert(session, MatchParticipation, {
            "id": 120, "match_id": 12, "bot_id": 1, "participant_number": 1,
            "result": None, "avg_step_time": 9.999, "last_synced": NOW,
        })
        session.commit()

        stats = bot_avg_match_stats(session, 1)
        assert stats["avg_duration_s"] == pytest.approx((11200 + 22400) / 2 / 22.4)  # 750s
        assert stats["avg_step_time_s"] == pytest.approx(0.015)


class TestCompetitionRoundEnds:
    def test_returns_iso_dates_for_complete_rounds_only(self, session):
        upsert(session, Competition, {"id": settings.competition_id, "name": "T", "last_synced": NOW})
        upsert(session, Round, {
            "id": 1, "number": 1, "competition_id": settings.competition_id,
            "complete": True, "finished": datetime(2026, 1, 5, tzinfo=timezone.utc),
            "last_synced": NOW,
        })
        upsert(session, Round, {
            "id": 2, "number": 2, "competition_id": settings.competition_id,
            "complete": True, "finished": datetime(2026, 1, 12, tzinfo=timezone.utc),
            "last_synced": NOW,
        })
        # Incomplete round — must not appear.
        upsert(session, Round, {
            "id": 3, "number": 3, "competition_id": settings.competition_id,
            "complete": False, "finished": datetime(2026, 1, 19, tzinfo=timezone.utc),
            "last_synced": NOW,
        })
        # Complete but no finished timestamp — must not appear.
        upsert(session, Round, {
            "id": 4, "number": 4, "competition_id": settings.competition_id,
            "complete": True, "finished": None, "last_synced": NOW,
        })
        session.commit()

        ends = competition_round_ends(session)
        assert ends == {1: "2026-01-05", 2: "2026-01-12"}


class TestRoundPositionForTimestamp:
    def _seed_three_rounds(self, session):
        upsert(session, Competition, {"id": settings.competition_id, "name": "T", "last_synced": NOW})
        for n, day in [(1, 5), (2, 12), (3, 19)]:
            upsert(session, Round, {
                "id": n, "number": n, "competition_id": settings.competition_id,
                "complete": True,
                "finished": datetime(2026, 1, day, tzinfo=timezone.utc),
                "last_synced": NOW,
            })
        session.commit()

    def test_none_input_returns_none(self, session):
        self._seed_three_rounds(session)
        assert round_position_for_timestamp(session, None) is None

    def test_no_completed_rounds_returns_none(self, session):
        upsert(session, Competition, {"id": settings.competition_id, "name": "T", "last_synced": NOW})
        session.commit()
        assert round_position_for_timestamp(session, datetime(2026, 1, 10, tzinfo=timezone.utc)) is None

    def test_before_first_round_end_returns_none(self, session):
        self._seed_three_rounds(session)
        assert round_position_for_timestamp(session, datetime(2026, 1, 1, tzinfo=timezone.utc)) is None

    def test_at_round_end_returns_round_number(self, session):
        self._seed_three_rounds(session)
        assert round_position_for_timestamp(session, datetime(2026, 1, 5, tzinfo=timezone.utc)) == 1.0
        assert round_position_for_timestamp(session, datetime(2026, 1, 12, tzinfo=timezone.utc)) == 2.0

    def test_interpolates_between_rounds(self, session):
        self._seed_three_rounds(session)
        # Halfway between round 1 (Jan 5) and round 2 (Jan 12) → position 1.5.
        midway = datetime(2026, 1, 8, 12, 0, 0, tzinfo=timezone.utc)
        assert round_position_for_timestamp(session, midway) == pytest.approx(1.5)

    def test_after_last_round_pins_to_last(self, session):
        self._seed_three_rounds(session)
        future = datetime(2026, 2, 1, tzinfo=timezone.utc)
        assert round_position_for_timestamp(session, future) == 3.0

    def test_naive_input_treated_as_utc(self, session):
        self._seed_three_rounds(session)
        naive = datetime(2026, 1, 8, 12, 0, 0)  # no tzinfo
        assert round_position_for_timestamp(session, naive) == pytest.approx(1.5)


class TestBotRaceEloHistory:
    def test_empty_when_bot_has_no_matches(self, session):
        _seed_competition(session)
        _seed_bot(session, 1, "Alpha", "T")
        session.commit()
        assert bot_race_elo_history(session, 1) == []

    def test_single_win_against_equal_elo_opponent_increases_rating(self, session):
        _seed_competition(session)
        _seed_bot(session, 1, "Alpha", "T")
        _seed_bot(session, 2, "Beta", "Z")
        # Single win, opp_elo == bot starting_elo == 1500. Expected = 0.5,
        # delta = K*(1-0.5) = 8 → rating becomes 1508.
        _seed_match(session, match_id=10, bot_a=1, bot_b=2,
                    a_result="win", started=NOW - timedelta(days=1))
        session.commit()

        history = bot_race_elo_history(session, 1)
        # One snapshot, race Z, rating 1508.
        assert history == [{"round_number": 1, "race": "Z", "rating": 1508.0}]

    def test_emits_one_snapshot_per_round_for_all_seen_races(self, session):
        _seed_competition(session)
        upsert(session, Round, {
            "id": 2, "number": 2, "competition_id": settings.competition_id,
            "complete": True, "last_synced": NOW,
        })
        _seed_bot(session, 1, "Alpha", "T")
        _seed_bot(session, 2, "Z1", "Z")
        _seed_bot(session, 3, "P1", "P")

        # Round 1: bot 1 plays Z (win) and P (loss).
        _seed_match(session, match_id=10, bot_a=1, bot_b=2,
                    a_result="win", started=NOW - timedelta(days=10))
        upsert(session, Match, {
            "id": 11, "round_id": 1, "map_id": 1,
            "started": NOW - timedelta(days=9), "result_created": NOW - timedelta(days=9),
            "last_synced": NOW,
        })
        upsert(session, MatchParticipation, {
            "id": 110, "match_id": 11, "bot_id": 1, "participant_number": 1,
            "starting_elo": 1500, "result": "loss", "last_synced": NOW,
        })
        upsert(session, MatchParticipation, {
            "id": 111, "match_id": 11, "bot_id": 3, "participant_number": 2,
            "starting_elo": 1500, "result": "win", "last_synced": NOW,
        })
        # Round 2: bot 1 only plays Z.
        upsert(session, Match, {
            "id": 12, "round_id": 2, "map_id": 1,
            "started": NOW - timedelta(days=2), "result_created": NOW - timedelta(days=2),
            "last_synced": NOW,
        })
        upsert(session, MatchParticipation, {
            "id": 120, "match_id": 12, "bot_id": 1, "participant_number": 1,
            "starting_elo": 1500, "result": "win", "last_synced": NOW,
        })
        upsert(session, MatchParticipation, {
            "id": 121, "match_id": 12, "bot_id": 2, "participant_number": 2,
            "starting_elo": 1500, "result": "loss", "last_synced": NOW,
        })
        session.commit()

        history = bot_race_elo_history(session, 1)
        # Both rounds emit snapshots. After round 1 we expect Z and P; after
        # round 2 we still expect both (P unchanged since not played).
        rounds = sorted({h["round_number"] for h in history})
        assert rounds == [1, 2]
        races_round_1 = sorted(h["race"] for h in history if h["round_number"] == 1)
        races_round_2 = sorted(h["race"] for h in history if h["round_number"] == 2)
        assert races_round_1 == ["P", "Z"]
        assert races_round_2 == ["P", "Z"]
        # P rating in round 2 unchanged from round 1 (no further P games).
        p1 = next(h["rating"] for h in history if h["round_number"] == 1 and h["race"] == "P")
        p2 = next(h["rating"] for h in history if h["round_number"] == 2 and h["race"] == "P")
        assert p1 == p2

    def test_skips_matches_in_incomplete_rounds(self, session):
        _seed_competition(session)
        upsert(session, Round, {
            "id": 99, "number": 99, "competition_id": settings.competition_id,
            "complete": False, "last_synced": NOW,
        })
        _seed_bot(session, 1, "Alpha", "T")
        _seed_bot(session, 2, "Beta", "Z")
        upsert(session, Match, {
            "id": 50, "round_id": 99, "map_id": 1,
            "started": NOW, "result_created": NOW, "last_synced": NOW,
        })
        upsert(session, MatchParticipation, {
            "id": 500, "match_id": 50, "bot_id": 1, "participant_number": 1,
            "starting_elo": 1500, "result": "win", "last_synced": NOW,
        })
        upsert(session, MatchParticipation, {
            "id": 501, "match_id": 50, "bot_id": 2, "participant_number": 2,
            "starting_elo": 1500, "result": "loss", "last_synced": NOW,
        })
        session.commit()

        assert bot_race_elo_history(session, 1) == []


class TestBotCurrentRaceElo:
    def test_returns_latest_round_snapshot_only(self, session):
        _seed_competition(session)
        _seed_bot(session, 1, "Alpha", "T")
        _seed_bot(session, 2, "Beta", "Z")
        _seed_match(session, match_id=10, bot_a=1, bot_b=2,
                    a_result="win", started=NOW - timedelta(days=1))
        session.commit()

        current = bot_current_race_elo(session, 1)
        assert set(current.keys()) == {"Z"}
        assert current["Z"] == 1508.0

    def test_returns_empty_when_no_history(self, session):
        _seed_competition(session)
        _seed_bot(session, 1, "Alpha", "T")
        session.commit()
        assert bot_current_race_elo(session, 1) == {}
