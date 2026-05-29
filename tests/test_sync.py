"""Tests for sync helpers: upsert idempotency, bot stubbing, and the
participation-repair pass that fixes the Match-finished-before-MatchParticipation
race against aiarena.net.
"""
import asyncio
from datetime import datetime, timezone

from sqlmodel import select

from ai_arena_recap.models import Bot, Competition, Map, Match, MatchParticipation, Round
from ai_arena_recap.sync.common import ensure_bot_stub, upsert
from ai_arena_recap.sync.rounds import repair_incomplete_participations, sync_rounds_and_matches


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

    def test_skips_cancelled_matches(self, session):
        # MatchCancelled games have participation rows but never get per-bot
        # results, so they can never become "complete". Repair must treat them
        # as settled and not refetch them every tick forever.
        upsert(session, Competition, {"id": 36, "name": "T", "last_synced": _now()})
        upsert(session, Round, {"id": 1, "number": 1, "competition_id": 36, "complete": False, "last_synced": _now()})
        upsert(session, Match, {
            "id": 777, "round_id": 1, "started": _now(),
            "result_type": "MatchCancelled", "result_created": _now(),
            "last_synced": _now(),
        })
        ensure_bot_stub(session, 10)
        ensure_bot_stub(session, 20)
        upsert(session, MatchParticipation, {
            "id": 100, "match_id": 777, "bot_id": 10, "participant_number": 1,
            "result": None, "elo_change": None, "last_synced": _now(),
        })
        upsert(session, MatchParticipation, {
            "id": 101, "match_id": 777, "bot_id": 20, "participant_number": 2,
            "result": None, "elo_change": None, "last_synced": _now(),
        })
        session.commit()

        client = _FakeApiClient({})
        asyncio.run(repair_incomplete_participations(session, client))
        assert client.calls == []  # cancelled match is settled, not refetched

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


class _FakeRoundClient:
    """Stand-in for AiArenaClient covering the calls sync_rounds_and_matches makes."""

    def __init__(self, rounds: list[dict], matches_by_round: dict[int, list[dict]],
                 parts_by_match: dict[int, list[dict]]):
        self._rounds = rounds
        self._matches_by_round = matches_by_round
        self._parts_by_match = parts_by_match
        self.participation_calls: list[int] = []

    async def list_rounds(self, competition_id: int):
        for r in self._rounds:
            yield r

    async def list_matches_for_round(self, round_id: int):
        for m in self._matches_by_round.get(round_id, []):
            yield m

    async def list_match_participations(self, match_id: int):
        self.participation_calls.append(match_id)
        for p in self._parts_by_match.get(match_id, []):
            yield p


class TestSyncRoundsAndMatches:
    def test_only_fetches_participations_for_finished_matches(self, session):
        # One open round with a finished match (10) and an in-progress match (11).
        upsert(session, Competition, {"id": 36, "name": "T", "last_synced": _now()})
        upsert(session, Map, {"id": 5, "name": "M", "last_synced": _now()})
        session.commit()
        rounds = [{"id": 1, "number": 1, "competition": 36, "complete": False}]
        matches = {1: [
            {"id": 10, "round": 1, "map": 5, "started": "2026-04-25T11:00:00Z",
             "result": {"type": "Player1Win", "winner": 100, "created": "2026-04-25T12:00:00Z",
                        "game_steps": 100, "bot1_name": "A", "bot2_name": "B"}},
            {"id": 11, "round": 1, "map": 5, "started": "2026-04-25T11:00:00Z", "result": None},
        ]}
        parts = {10: [
            {"id": 200, "match": 10, "participant_number": 1, "bot": 100,
             "result": "win", "elo_change": 5, "resultant_elo": 1505, "avg_step_time": 0.01},
            {"id": 201, "match": 10, "participant_number": 2, "bot": 101,
             "result": "loss", "elo_change": -5, "resultant_elo": 1495, "avg_step_time": 0.02},
        ]}
        client = _FakeRoundClient(rounds, matches, parts)

        asyncio.run(sync_rounds_and_matches(session, client, 36))

        # Only the finished match's participations were fetched — the in-progress
        # match (11) was not polled, even though it has no local result yet.
        assert client.participation_calls == [10]
        # Both matches are tracked; only the finished one has result_created.
        assert session.get(Match, 10).result_created is not None
        assert session.get(Match, 11).result_created is None
        # Finished match got its participation rows with results.
        parts_10 = session.exec(
            select(MatchParticipation).where(MatchParticipation.match_id == 10)
            .order_by(MatchParticipation.participant_number)
        ).all()
        assert [p.result for p in parts_10] == ["win", "loss"]

    def test_skips_match_already_finalized_locally(self, session):
        # A match already finalized (result_created set) must not be refetched.
        upsert(session, Competition, {"id": 36, "name": "T", "last_synced": _now()})
        upsert(session, Round, {"id": 1, "number": 1, "competition_id": 36,
                                "complete": False, "last_synced": _now()})
        upsert(session, Match, {"id": 10, "round_id": 1, "result_created": _now(),
                                "result_type": "Player1Win", "last_synced": _now()})
        session.commit()

        rounds = [{"id": 1, "number": 1, "competition": 36, "complete": False}]
        matches = {1: [
            {"id": 10, "round": 1, "result": {"type": "Player1Win", "created": "2026-04-25T12:00:00Z"}},
        ]}
        client = _FakeRoundClient(rounds, matches, {})

        asyncio.run(sync_rounds_and_matches(session, client, 36))
        assert client.participation_calls == []
