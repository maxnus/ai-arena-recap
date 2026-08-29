"""Tests for sync helpers: upsert idempotency, bot stubbing, and the
participation-repair pass that fixes the Match-finished-before-MatchParticipation
race against aiarena.net.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import select

from ai_arena_recap.config import settings
from ai_arena_recap.models import Bot, Competition, Map, Match, MatchParticipation, Round
from ai_arena_recap.sync import runner
from ai_arena_recap.sync.common import ensure_bot_stub, upsert, utcnow
from ai_arena_recap.sync.rounds import repair_incomplete_participations, sync_rounds_and_matches
from ai_arena_recap.sync.runner import archived_due


class _NullClient:
    """AiArenaClient stand-in for sync_all tests: makes no requests."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None


def _stub_sync_all(monkeypatch, **overrides) -> list[str]:
    """Replace everything sync_all calls out to, and return the call log.

    Lets the tests drive sync_all's control flow — which steps still run after
    a failure, what gets recorded — without touching the network."""
    calls: list[str] = []

    async def fake_tree(session, client, competition_id, *, max_rounds=None):
        calls.append(f"tree:{competition_id}")
        return {7}

    async def fake_sync_bots(session, client, bot_ids, *, force=False):
        calls.append(f"bots:{sorted(bot_ids)}")

    async def noop(*args, **kwargs):
        return set()

    stubs = {
        "AiArenaClient": _NullClient,
        "sync_maps": noop,
        "_sync_competition_tree": fake_tree,
        "archived_due": lambda session: [],
        "warn_if_season_rolled_over": noop,
        "repair_incomplete_participations": noop,
        "sync_bots": fake_sync_bots,
        **overrides,
    }
    for name, value in stubs.items():
        monkeypatch.setattr(runner, name, value)
    return calls


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

        asyncio.run(repair_incomplete_participations(session, client, 36))

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
        asyncio.run(repair_incomplete_participations(session, client, 36))
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
        asyncio.run(repair_incomplete_participations(session, client, 36))
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
        asyncio.run(repair_incomplete_participations(session, client, 36))
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


class TestArchivedDue:
    """The archive refresh schedule, and its blast radius when it goes wrong.

    Both cases are regressions from one bug: archived_due compared last_synced
    (naive, as SQLite always returns it) against an aware utcnow() in Python.
    Every tick raised TypeError between the rounds sync and sync_bots, so the
    ladder kept updating while bot names and races stayed frozen as "bot-<id>"
    placeholders for five days.
    """

    def _seed(self, session, *, archived_last_synced: datetime) -> None:
        upsert(session, Competition, {
            "id": settings.competition_id, "name": "Current", "status": "open",
            "last_synced": utcnow(),
        })
        upsert(session, Competition, {
            "id": settings.competition_id - 1, "name": "Archived", "status": "closed",
            "last_synced": archived_last_synced,
        })
        session.commit()

    def test_stale_archive_is_due(self, session):
        stale = utcnow() - timedelta(seconds=settings.archive_refresh_seconds + 60)
        self._seed(session, archived_last_synced=stale)
        assert archived_due(session) == [settings.competition_id - 1]

    def test_recently_synced_archive_is_not_due(self, session):
        self._seed(session, archived_last_synced=utcnow() - timedelta(seconds=60))
        assert archived_due(session) == []

    def test_a_broken_archive_pass_still_lets_bots_sync(self, session, monkeypatch):
        """The archive pass covers data that no longer changes. The live season's
        bot metadata does change, and is fetched after it — a failure upstairs
        must not cost us that."""
        def boom(session):
            raise TypeError("can't compare offset-naive and offset-aware datetimes")

        calls = _stub_sync_all(monkeypatch, archived_due=boom)
        asyncio.run(runner.sync_all())

        assert calls == [f"tree:{settings.competition_id}", "bots:[7]"]


class TestSyncOutcome:
    """/healthz reports whether the last tick finished, not just that one began."""

    @pytest.fixture(autouse=True)
    def _clear(self, monkeypatch):
        monkeypatch.setattr(runner, "_last_outcome", None)

    def test_none_until_a_sync_finishes(self):
        assert runner.last_sync_outcome() is None

    def test_records_success(self, session, monkeypatch):
        _stub_sync_all(monkeypatch)
        asyncio.run(runner.sync_all())

        outcome = runner.last_sync_outcome()
        assert outcome["ok"] is True
        assert outcome["error"] is None
        assert outcome["started"] <= outcome["finished"]
        assert outcome["age_seconds"] >= 0

    def test_records_the_exception_that_ended_the_tick(self, session, monkeypatch):
        """The failure mode this exists for: the tick dies partway, every
        freshness timestamp it already wrote still looks healthy."""
        async def boom(*args, **kwargs):
            raise TypeError("can't compare offset-naive and offset-aware datetimes")

        _stub_sync_all(monkeypatch, repair_incomplete_participations=boom)
        with pytest.raises(TypeError):
            asyncio.run(runner.sync_all())

        outcome = runner.last_sync_outcome()
        assert outcome["ok"] is False
        assert "offset-naive and offset-aware" in outcome["error"]


class TestRepairIsBounded:
    """The repair pass costs one request per match and runs inside the live sync
    tick, so its candidate set needs a ceiling — an interrupted backfill leaves
    tens of thousands of matches without participations, and an unbounded pass
    would put the live season behind hours of repair."""

    def _seed_incomplete(self, session, count: int) -> None:
        upsert(session, Competition, {"id": 36, "name": "T", "last_synced": _now()})
        upsert(session, Round, {"id": 1, "number": 1, "competition_id": 36,
                                "complete": True, "last_synced": _now()})
        for i in range(count):
            upsert(session, Match, {"id": 500 + i, "round_id": 1, "result_created": _now(),
                                    "result_type": "Player1Win", "last_synced": _now()})
        session.commit()

    def test_takes_the_oldest_slice_and_leaves_the_rest(self, session):
        self._seed_incomplete(session, 10)
        client = _FakeApiClient({})

        asyncio.run(repair_incomplete_participations(session, client, 36, limit=4))

        assert client.calls == [500, 501, 502, 503]

    def test_backlog_drains_across_calls(self, session):
        self._seed_incomplete(session, 6)
        first, second = _FakeApiClient({}), _FakeApiClient({})

        asyncio.run(repair_incomplete_participations(session, first, 36, limit=4))
        asyncio.run(repair_incomplete_participations(session, second, 36, limit=4))

        # Nothing was fixed (the fake returns no rows), so the second pass sees
        # the same candidates — the point is that each pass stays bounded.
        assert len(first.calls) == len(second.calls) == 4


class TestRepairIsScopedToOneCompetition:
    """The race this pass fixes — result lands before the participation rows —
    only happens in a competition still playing matches. Sweeping the archive
    too was waste, and once four seasons were backfilled it was ruinous: the
    candidate set hit 306,000 and the live tick spent 2,000 requests every five
    minutes on history, thirty times the rate the throttled backfill was using.
    """

    def _seed(self, session) -> None:
        for comp, round_id, match_id in [(36, 1, 500), (35, 2, 600)]:
            upsert(session, Competition, {"id": comp, "name": f"C{comp}", "last_synced": _now()})
            upsert(session, Round, {"id": round_id, "number": 1, "competition_id": comp,
                                    "complete": True, "last_synced": _now()})
            upsert(session, Match, {"id": match_id, "round_id": round_id,
                                    "result_created": _now(), "result_type": "Player1Win",
                                    "last_synced": _now()})
        session.commit()

    def test_only_the_named_competitions_matches_are_repaired(self, session):
        self._seed(session)
        client = _FakeApiClient({})

        asyncio.run(repair_incomplete_participations(session, client, 36))

        assert client.calls == [500]   # not 600, which belongs to the archive

    def test_an_archived_backlog_costs_the_live_tick_nothing(self, session):
        self._seed(session)
        # A whole archived season's worth of gaps, as a fresh backfill leaves.
        for i in range(50):
            upsert(session, Match, {"id": 700 + i, "round_id": 2, "result_created": _now(),
                                    "result_type": "Player1Win", "last_synced": _now()})
        session.commit()
        client = _FakeApiClient({})

        asyncio.run(repair_incomplete_participations(session, client, 36))

        assert client.calls == [500]
