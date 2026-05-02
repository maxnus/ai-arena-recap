"""Tests for sync helpers: upsert idempotency, bot stubbing, and the
participation-repair pass that fixes the Match-finished-before-MatchParticipation
race against aiarena.net.
"""
import asyncio
from datetime import datetime, timezone

from sqlmodel import select

from ai_arena_recap.models import Bot, Competition, Match, MatchParticipation, Round
from ai_arena_recap.sync.common import ensure_bot_stub, upsert
from ai_arena_recap.sync.rounds import repair_incomplete_participations


def _now() -> datetime:
    return datetime(2026, 4, 25, 12, 0, 0, tzinfo=timezone.utc)


class TestUpsert:
    def test_insert_then_update_keeps_one_row(self, session):
        upsert(session, Bot, {"id": 1, "name": "Foo", "last_synced": _now()})
        upsert(session, Bot, {"id": 1, "name": "Foo v2", "last_synced": _now()})
        session.commit()

        rows = session.exec(select(Bot)).all()
        assert len(rows) == 1
        assert rows[0].name == "Foo v2"

    def test_upsert_only_changes_named_fields(self, session):
        upsert(session, Bot, {"id": 1, "name": "Foo", "plays_race": "T", "last_synced": _now()})
        upsert(session, Bot, {"id": 1, "name": "Foo v2", "last_synced": _now()})
        session.commit()

        bot = session.get(Bot, 1)
        assert bot.name == "Foo v2"
        assert bot.plays_race == "T"  # not in second upsert payload, preserved


class TestEnsureBotStub:
    def test_creates_placeholder_with_epoch_synced(self, session):
        ensure_bot_stub(session, 42)
        session.commit()
        bot = session.get(Bot, 42)
        assert bot is not None
        assert bot.name == "bot-42"
        assert bot.last_synced.year == 1970  # marker for "not yet really synced"

    def test_does_not_overwrite_real_bot_data(self, session):
        upsert(session, Bot, {"id": 42, "name": "RealName", "plays_race": "Z", "last_synced": _now()})
        session.commit()

        ensure_bot_stub(session, 42)
        session.commit()

        bot = session.get(Bot, 42)
        assert bot.name == "RealName"
        assert bot.plays_race == "Z"
        assert bot.last_synced.year == 2026


class _FakeApiClient:
    """Stand-in for AiArenaClient that yields canned participation rows."""

    def __init__(self, by_match: dict[int, list[dict]]):
        self._by_match = by_match
        self.calls: list[int] = []

    async def list_match_participations(self, match_id: int):
        self.calls.append(match_id)
        for p in self._by_match.get(match_id, []):
            yield p


def _seed_finished_match_with_empty_participations(session, *, match_id: int, bot1: int, bot2: int) -> None:
    upsert(session, Competition, {"id": 36, "name": "T", "last_synced": _now()})
    upsert(session, Round, {"id": 1, "number": 1, "competition_id": 36, "complete": False, "last_synced": _now()})
    upsert(session, Match, {
        "id": match_id,
        "round_id": 1,
        "started": _now(),
        "result_type": "Player1Win",
        "result_created": _now(),
        "result_winner_bot_id": bot1,
        "last_synced": _now(),
    })
    ensure_bot_stub(session, bot1)
    ensure_bot_stub(session, bot2)
    # The participation rows exist (they were inserted at match dispatch) but
    # the result-related fields are still None — the race we care about.
    upsert(session, MatchParticipation, {
        "id": 100, "match_id": match_id, "bot_id": bot1, "participant_number": 1,
        "starting_elo": 1500, "result": None, "elo_change": None, "avg_step_time": None,
        "last_synced": _now(),
    })
    upsert(session, MatchParticipation, {
        "id": 101, "match_id": match_id, "bot_id": bot2, "participant_number": 2,
        "starting_elo": 1500, "result": None, "elo_change": None, "avg_step_time": None,
        "last_synced": _now(),
    })
    session.commit()


class TestRepairIncompleteParticipations:
    def test_refetches_and_fills_in_missing_results(self, session):
        _seed_finished_match_with_empty_participations(session, match_id=999, bot1=10, bot2=20)

        client = _FakeApiClient({
            999: [
                {"id": 100, "match": 999, "participant_number": 1, "bot": 10,
                 "starting_elo": 1500, "resultant_elo": 1505, "elo_change": 5,
                 "result": "win", "result_cause": "game_rules", "avg_step_time": 0.01},
                {"id": 101, "match": 999, "participant_number": 2, "bot": 20,
                 "starting_elo": 1500, "resultant_elo": 1495, "elo_change": -5,
                 "result": "loss", "result_cause": "game_rules", "avg_step_time": 0.02},
            ],
        })

        asyncio.run(repair_incomplete_participations(session, client))

        assert client.calls == [999]
        parts = session.exec(
            select(MatchParticipation).where(MatchParticipation.match_id == 999)
            .order_by(MatchParticipation.participant_number)
        ).all()
        assert [p.result for p in parts] == ["win", "loss"]
        assert [p.elo_change for p in parts] == [5, -5]

    def test_skips_when_nothing_to_repair(self, session):
        # Healthy match: participations already have results.
        upsert(session, Competition, {"id": 36, "name": "T", "last_synced": _now()})
        upsert(session, Round, {"id": 1, "number": 1, "competition_id": 36, "complete": True, "last_synced": _now()})
        upsert(session, Match, {
            "id": 1, "round_id": 1, "started": _now(), "result_created": _now(),
            "result_type": "Player1Win", "last_synced": _now(),
        })
        ensure_bot_stub(session, 10)
        ensure_bot_stub(session, 20)
        upsert(session, MatchParticipation, {
            "id": 100, "match_id": 1, "bot_id": 10, "participant_number": 1,
            "result": "win", "elo_change": 5, "last_synced": _now(),
        })
        upsert(session, MatchParticipation, {
            "id": 101, "match_id": 1, "bot_id": 20, "participant_number": 2,
            "result": "loss", "elo_change": -5, "last_synced": _now(),
        })
        session.commit()

        client = _FakeApiClient({})
        asyncio.run(repair_incomplete_participations(session, client))
        assert client.calls == []  # no API hits

    def test_does_not_touch_in_progress_matches(self, session):
        # Match without result_created — still in progress, should not be repaired.
        upsert(session, Competition, {"id": 36, "name": "T", "last_synced": _now()})
        upsert(session, Round, {"id": 1, "number": 1, "competition_id": 36, "complete": False, "last_synced": _now()})
        upsert(session, Match, {
            "id": 1, "round_id": 1, "started": _now(),
            "result_created": None, "last_synced": _now(),
        })
        ensure_bot_stub(session, 10)
        upsert(session, MatchParticipation, {
            "id": 100, "match_id": 1, "bot_id": 10, "participant_number": 1,
            "result": None, "elo_change": None, "last_synced": _now(),
        })
        session.commit()

        client = _FakeApiClient({})
        asyncio.run(repair_incomplete_participations(session, client))
        assert client.calls == []
