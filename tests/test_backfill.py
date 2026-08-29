"""Bulk import of a finished season's match history.

The thing worth pinning down: the API cannot narrow a bot's participation
history to one competition, so the importer pages whole careers and filters
locally. If that filter is wrong we either drop the season's own rows or try to
store rows for matches we don't have, so most of these tests are about which
rows survive the trip.
"""
import asyncio
from datetime import datetime, timezone

import pytest
from sqlmodel import select

from ai_arena_recap.models import Competition, Match, MatchParticipation, Round
from ai_arena_recap.sync import backfill as backfill_module
from ai_arena_recap.sync.backfill import backfill
from ai_arena_recap.sync.common import upsert

NOW = datetime(2026, 2, 28, tzinfo=timezone.utc)
COMP = 35          # the season being imported
OTHER_COMP = 33    # an earlier season the same bots played in


def _iso(day: int) -> str:
    return f"2026-01-{day:02d}T12:00:00Z"


class _FakeClient:
    """Covers every call the backfill makes, and counts the bulk pages."""

    def __init__(self, *, careers: dict[int, list[dict]], rounds: list[dict],
                 matches: dict[int, list[dict]], participations: list[dict]):
        self._careers = careers
        self._rounds = rounds
        self._matches = matches
        self._participations = participations
        self.bots_paged: list[int] = []
        self.per_match_calls: list[int] = []

    async def list_maps(self):
        for m in [{"id": 1, "name": "Ultralove", "enabled": True}]:
            yield m

    async def get_competition(self, competition_id: int):
        return {"id": competition_id, "name": f"Season {competition_id}", "status": "closed",
                "date_opened": _iso(4), "date_closed": _iso(28)}

    async def list_competition_participations(self, competition_id: int):
        for p in self._participations:
            if p["competition"] == competition_id:
                yield p

    async def list_rounds(self, competition_id: int):
        for r in self._rounds:
            if r["competition"] == competition_id:
                yield r

    async def list_matches_for_round(self, round_id: int):
        for m in self._matches.get(round_id, []):
            yield m

    async def list_match_participations(self, match_id: int):
        """Only the discovery pass should reach this — the bulk import must
        never fall back to one request per match."""
        self.per_match_calls.append(match_id)
        for career in self._careers.values():
            for p in career:
                if p["match"] == match_id:
                    yield p

    async def list_bot_match_participations(self, bot_id: int):
        self.bots_paged.append(bot_id)
        for p in self._careers.get(bot_id, []):
            yield p

    async def get_bot(self, bot_id: int):
        return {"id": bot_id, "name": f"Bot{bot_id}", "user": 1,
                "plays_race": {"label": "Z"}, "type": "python", "created": _iso(1)}

    async def get_user(self, user_id: int):
        return {"id": user_id, "username": "alice"}


def _client(**overrides) -> _FakeClient:
    """Two bots, two matches in the season being imported, plus history in
    another season that must not be stored."""
    participations = [
        {"id": 1, "competition": COMP, "bot": 10, "elo": 1700, "division_num": 1,
         "active": False, "match_count": 2, "win_count": 2, "loss_count": 0},
        {"id": 2, "competition": COMP, "bot": 20, "elo": 1500, "division_num": 1,
         "active": False, "match_count": 2, "win_count": 0, "loss_count": 2},
    ]
    rounds = [{"id": 900, "number": 1, "competition": COMP, "complete": True}]
    matches = {900: [
        {"id": 5001, "round": 900, "map": 1, "started": _iso(5),
         "result": {"type": "Player1Win", "winner": 10, "created": _iso(5), "game_steps": 100}},
        {"id": 5002, "round": 900, "map": 1, "started": _iso(6),
         "result": {"type": "Player1Win", "winner": 10, "created": _iso(6), "game_steps": 200}},
    ]}

    def row(pid, match, bot, elo):
        return {"id": pid, "match": match, "bot": bot, "participant_number": 1,
                "starting_elo": elo, "resultant_elo": elo + 4, "elo_change": 4,
                "avg_step_time": 0.01, "result": "win", "result_cause": "game_rules"}

    careers = {
        # Each career carries rows from an earlier season (matches 4001/4002)
        # that this import must not keep — we have no Match rows for them.
        10: [row(1, 4001, 10, 1600), row(2, 5001, 10, 1690), row(3, 5002, 10, 1694)],
        20: [row(4, 4002, 20, 1550), row(5, 5001, 20, 1500), row(6, 5002, 20, 1496)],
    }
    kwargs = {"careers": careers, "rounds": rounds, "matches": matches,
              "participations": participations}
    kwargs.update(overrides)
    return _FakeClient(**kwargs)


class TestBackfill:
    def test_imports_the_seasons_rows_and_drops_the_rest(self, session):
        client = _client()
        asyncio.run(backfill(session, client, [COMP]))

        rows = session.exec(select(MatchParticipation).order_by(MatchParticipation.id)).all()
        assert sorted(r.match_id for r in rows) == [5001, 5001, 5002, 5002]
        # The earlier season's matches were paged but not stored: no Match row
        # exists for them, and a participation row keyed to a missing match
        # would violate the foreign key.
        assert 4001 not in {r.match_id for r in rows}
        assert session.get(Match, 4001) is None

    def test_uses_the_bulk_path_not_one_request_per_match(self, session):
        client = _client()
        asyncio.run(backfill(session, client, [COMP]))

        assert sorted(client.bots_paged) == [10, 20]   # one page-through per bot
        assert client.per_match_calls == []            # never the per-match endpoint

    def test_stores_the_full_participation_detail(self, session):
        client = _client()
        asyncio.run(backfill(session, client, [COMP]))

        row = session.exec(
            select(MatchParticipation).where(
                MatchParticipation.match_id == 5001, MatchParticipation.bot_id == 10
            )
        ).one()
        assert (row.starting_elo, row.resultant_elo, row.elo_change) == (1690, 1694, 4)
        assert row.result == "win" and row.result_cause == "game_rules"
        assert row.avg_step_time == 0.01

    def test_imports_standings_and_match_rows_too(self, session):
        client = _client()
        asyncio.run(backfill(session, client, [COMP]))

        assert session.get(Competition, COMP).status == "closed"
        assert session.get(Round, 900).complete is True
        match = session.get(Match, 5001)
        assert match.result_type == "Player1Win" and match.map_id == 1

    def test_rerun_skips_bots_already_complete(self, session):
        client = _client()
        asyncio.run(backfill(session, client, [COMP]))
        assert sorted(client.bots_paged) == [10, 20]

        # Second run: every bot already has its standings' worth of rows, so
        # nothing is paged again. This is what makes an interrupted 20-minute
        # import resumable rather than restartable.
        again = _client()
        asyncio.run(backfill(session, again, [COMP]))
        assert again.bots_paged == []

    def test_force_repages_everything(self, session):
        asyncio.run(backfill(session, _client(), [COMP]))
        forced = _client()
        asyncio.run(backfill(session, forced, [COMP], force=True))
        assert sorted(forced.bots_paged) == [10, 20]

    def test_resumes_a_bot_whose_rows_are_incomplete(self, session):
        client = _client()
        asyncio.run(backfill(session, client, [COMP]))
        # Lose one of bot 10's rows, as an interrupted run would.
        session.exec(
            select(MatchParticipation).where(MatchParticipation.id == 3)
        ).one()
        session.delete(session.get(MatchParticipation, 3))
        session.commit()

        again = _client()
        asyncio.run(backfill(session, again, [COMP]))
        assert again.bots_paged == [10]
        assert session.get(MatchParticipation, 3) is not None

    def test_one_bots_failure_does_not_lose_the_others(self, session, caplog):
        client = _client()

        async def explode(bot_id):
            if bot_id == 20:
                raise RuntimeError("upstream 504")
            for p in client._careers[bot_id]:
                yield p
            client.bots_paged.append(bot_id)

        client.list_bot_match_participations = explode
        asyncio.run(backfill(session, client, [COMP]))

        stored = session.exec(select(MatchParticipation.bot_id)).all()
        assert set(stored) == {10}

    def test_counts_rows_per_bot(self, session):
        imported = asyncio.run(backfill(session, _client(), [COMP]))
        assert imported == {10: 2, 20: 2}


class TestScopeQueries:
    def test_in_scope_ids_cover_only_the_named_competitions(self, session):
        for comp in (COMP, OTHER_COMP):
            upsert(session, Competition, {"id": comp, "name": f"S{comp}", "last_synced": NOW})
            upsert(session, Round, {"id": 900 + comp, "number": 1, "competition_id": comp,
                                    "complete": True, "last_synced": NOW})
            upsert(session, Match, {"id": 7000 + comp, "round_id": 900 + comp,
                                    "result_created": NOW, "last_synced": NOW})
        session.commit()

        assert backfill_module._in_scope_match_ids(session, [COMP]) == {7000 + COMP}
        assert backfill_module._in_scope_match_ids(session, [COMP, OTHER_COMP]) == {
            7000 + COMP, 7000 + OTHER_COMP
        }

    def test_expected_rows_sum_match_counts_across_competitions(self, session):
        upsert(session, Competition, {"id": COMP, "name": "a", "last_synced": NOW})
        upsert(session, Competition, {"id": OTHER_COMP, "name": "b", "last_synced": NOW})
        from ai_arena_recap.sync.common import ensure_bot_stub
        ensure_bot_stub(session, 10)
        for pid, comp, count in [(1, COMP, 40), (2, OTHER_COMP, 60)]:
            upsert(session, __import__("ai_arena_recap.models", fromlist=["x"]).CompetitionParticipation, {
                "id": pid, "competition_id": comp, "bot_id": 10, "match_count": count,
                "last_synced": NOW,
            })
        session.commit()

        assert backfill_module._expected_rows(session, [COMP]) == {10: 40}
        assert backfill_module._expected_rows(session, [COMP, OTHER_COMP]) == {10: 100}


@pytest.mark.parametrize("competitions", [[COMP], [COMP, OTHER_COMP]])
def test_backfill_is_idempotent(session, competitions):
    """Re-importing must not duplicate rows — the whole thing is upserts."""
    asyncio.run(backfill(session, _client(), competitions))
    first = session.exec(select(MatchParticipation.id)).all()
    asyncio.run(backfill(session, _client(), competitions, force=True))
    assert session.exec(select(MatchParticipation.id)).all() == first


class TestRetryPass:
    """A bot whose pages time out and exhaust their retries comes back empty.
    Leaving it that way ships a season with quietly missing history, so a single
    invocation makes a second attempt at whatever is still short."""

    def test_a_bot_that_failed_is_repaged_in_the_same_run(self, session):
        client = _client()
        attempts: list[int] = []
        real = client.list_bot_match_participations

        def flaky(bot_id: int):
            attempts.append(bot_id)
            if bot_id == 20 and attempts.count(20) == 1:
                async def boom():
                    raise RuntimeError("read timeout")
                    yield  # pragma: no cover
                return boom()
            return real(bot_id)

        client.list_bot_match_participations = flaky
        asyncio.run(backfill(session, client, [COMP]))

        # Bot 20 was tried twice, and its rows landed on the retry.
        assert attempts.count(20) == 2
        stored = session.exec(select(MatchParticipation.bot_id)).all()
        assert sorted(set(stored)) == [10, 20]

    def test_no_retry_pass_when_the_first_one_covered_everything(self, session, caplog):
        client = _client()
        asyncio.run(backfill(session, client, [COMP]))
        assert client.bots_paged.count(10) == 1
        assert "re-paging" not in caplog.text


class TestErrorOnlyBots:
    """A bot whose every game failed to start has match_count 0 in the
    standings but thousands of participation rows in the API. Treating
    match_count as the target left it unswept, and every match it played showed
    one participation row instead of two — 10,563 of them in 2026 Pre-Season 1.
    """

    def _client_with_error_only_bot(self) -> _FakeClient:
        client = _client()
        # Bot 30 joined, never completed a game: match_count 0, but it has rows.
        client._participations.append(
            {"id": 3, "competition": COMP, "bot": 30, "elo": 1600, "division_num": 0,
             "active": False, "match_count": 0, "win_count": 0, "loss_count": 0}
        )
        client._matches[900].append(
            {"id": 5003, "round": 900, "map": 1, "started": _iso(7),
             "result": {"type": "InitializationError", "winner": None,
                        "created": _iso(7), "game_steps": 0}}
        )
        err = lambda pid, bot: {   # noqa: E731
            "id": pid, "match": 5003, "bot": bot, "participant_number": 1,
            "starting_elo": 1600, "resultant_elo": 1600, "elo_change": 0,
            "avg_step_time": None, "result": "none", "result_cause": "initialization_failure",
        }
        client._careers[30] = [err(7, 30)]
        client._careers[10].append(err(8, 10))
        return client

    def test_the_bot_is_swept_despite_a_zero_match_count(self, session):
        client = self._client_with_error_only_bot()
        asyncio.run(backfill(session, client, [COMP]))
        assert 30 in client.bots_paged

    def test_its_opponents_match_gets_both_rows(self, session):
        asyncio.run(backfill(session, self._client_with_error_only_bot(), [COMP]))
        rows = session.exec(
            select(MatchParticipation).where(MatchParticipation.match_id == 5003)
        ).all()
        assert sorted(r.bot_id for r in rows) == [10, 30]


class TestBotsMissingFromStandings:
    """A bot can play a season's matches and be absent from its standings
    entirely — bot 990 played 9,870 InitializationError games in 2026
    Pre-Season 1 with no competition-participation row. Nothing in the
    standings can name it, so the matches have to."""

    def _client_with_unlisted_bot(self) -> _FakeClient:
        client = _client()
        # Bot 99 plays match 5001 against bot 10 but never joined the standings.
        client._careers[99] = [{
            "id": 9, "match": 5001, "bot": 99, "participant_number": 2,
            "starting_elo": 1600, "resultant_elo": 1600, "elo_change": 0,
            "avg_step_time": None, "result": "none",
            "result_cause": "initialization_failure",
        }]
        # ...so bot 20's row for 5001 doesn't exist; only 10 and 99 played it.
        client._careers[20] = [p for p in client._careers[20] if p["match"] != 5001]
        return client

    def test_the_unlisted_bots_rows_are_found_and_imported(self, session):
        client = self._client_with_unlisted_bot()
        asyncio.run(backfill(session, client, [COMP]))

        rows = session.exec(
            select(MatchParticipation.bot_id).where(MatchParticipation.match_id == 5001)
        ).all()
        assert sorted(rows) == [10, 99]

    def test_discovery_asks_only_a_sample_of_matches(self, session):
        """Discovery uses the expensive per-match endpoint, so it must stay a
        probe to name the bot — the bulk sweep then does the actual import."""
        client = self._client_with_unlisted_bot()
        asyncio.run(backfill(session, client, [COMP]))

        assert 99 in client.bots_paged                      # bulk-paged
        assert len(client.per_match_calls) <= backfill_module._DISCOVERY_SAMPLE

    def test_a_complete_import_never_touches_the_per_match_endpoint(self, session):
        client = _client()
        asyncio.run(backfill(session, client, [COMP]))
        assert client.per_match_calls == []
