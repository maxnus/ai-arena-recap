"""Bulk import of finished seasons the DB has never seen.

The live sync fetches match participations one request per match. That is the
right shape for a season in progress — a few hundred new matches per tick — and
the wrong shape entirely for a closed season: 2026 Pre-Season 1 alone is ~69,000
matches, which at the ~3.5 matches/sec the API sustains is over five hours of
requests, with 504s appearing under the load.

This module uses the other axis. `/match-participations/` takes no competition
filter and ignores id/match range filters, and paging it unfiltered dies at 504
past roughly a million rows of offset — but filtered by bot it pages reliably to
the end of that bot's career, 500 rows per request. A season has ~100 bots, so
its participation history costs thousands of requests instead of hundreds of
thousands. The trade is that the API cannot narrow a bot's history to one
season, so we page whole careers and keep the rows whose match is in scope
(~13x over-read for one season; ~3.5x when several are imported together, since
each career is paged once no matter how many seasons want rows from it).

Import several competitions in one call when you want several — that is where
the sharing happens.
"""
import asyncio
import logging
import time

from sqlmodel import Session, func, select

from ai_arena_recap.api_client import AiArenaClient
from ai_arena_recap.models import CompetitionParticipation, Match, MatchParticipation, Round
from ai_arena_recap.sync.bots import sync_bots
from ai_arena_recap.sync.common import ensure_bot_stub, upsert
from ai_arena_recap.sync.competition import sync_competition, sync_participations
from ai_arena_recap.sync.maps import sync_maps
from ai_arena_recap.sync.rounds import (
    _fetch_participations,
    _participation_values,
    sync_rounds_and_matches,
)

log = logging.getLogger(__name__)

# How many bots to page concurrently. The client's own semaphore caps requests
# in flight globally, so this only needs to be large enough to keep that
# saturated; past it, the only effect is holding more rows in memory. Careers
# vary wildly in length (a few hundred rows to 150,000), so bots are started as
# slots free up rather than in fixed batches — one long career must not leave
# the other slots idle while it finishes.
_BOTS_IN_FLIGHT = 16

# Passes over the bot list: sweep the standings, then keep sweeping whatever
# each pass reveals was still missing. Converges in two or three; the cap is
# only there so a season that never reconciles can't loop forever.
_MAX_PASSES = 6

# Short matches to ask about when hunting for bots outside the standings.
# Sampled at random, not taken in id order: consecutive short matches tend to
# belong to the same bot, so an ordered sample names one per pass and takes a
# pass per bot to converge. A scattered sample names them all at once.
_DISCOVERY_SAMPLE = 60


def _in_scope_match_ids(session: Session, competition_ids: list[int]) -> set[int]:
    """Every match id belonging to the competitions being imported.

    Participation rows are keyed to matches by a foreign key, so a row can only
    be stored once its match exists locally — and rows from the bot's other
    seasons have to be dropped on the floor rather than orphaned."""
    return set(session.exec(
        select(Match.id)
        .join(Round, Match.round_id == Round.id)  # type: ignore[arg-type]
        .where(Round.competition_id.in_(competition_ids))  # type: ignore[attr-defined]
    ).all())


def _expected_rows(session: Session, competition_ids: list[int]) -> dict[int, int]:
    """Rows each bot should end up with, as a floor rather than a target.

    ``match_count`` in the standings counts games that produced a result, and
    the API stores participation rows for games that never got that far — a bot
    whose every game hit InitializationError has match_count 0 and thousands of
    rows. Taken literally, that bot looks like it needs nothing and never gets
    paged, leaving its opponents' matches with one row instead of two (10,563
    such matches in 2026 Pre-Season 1). Hence the floor of 1: every bot in the
    standings is swept at least once, whatever its match count claims.
    """
    rows = session.exec(
        select(CompetitionParticipation.bot_id, func.sum(CompetitionParticipation.match_count))
        .where(CompetitionParticipation.competition_id.in_(competition_ids))  # type: ignore[attr-defined]
        .group_by(CompetitionParticipation.bot_id)  # type: ignore[arg-type]
    ).all()
    return {bot_id: max(int(total or 0), 1) for bot_id, total in rows}


def _stored_rows(session: Session, competition_ids: list[int]) -> dict[int, int]:
    """Participation rows each bot already has for these competitions."""
    rows = session.exec(
        select(MatchParticipation.bot_id, func.count(MatchParticipation.id))
        .join(Match, MatchParticipation.match_id == Match.id)  # type: ignore[arg-type]
        .join(Round, Match.round_id == Round.id)  # type: ignore[arg-type]
        .where(Round.competition_id.in_(competition_ids))  # type: ignore[attr-defined]
        .group_by(MatchParticipation.bot_id)  # type: ignore[arg-type]
    ).all()
    return {bot_id: int(count) for bot_id, count in rows}


async def _fetch_bot_rows(
    client: AiArenaClient, bot_id: int, in_scope: set[int]
) -> tuple[list[dict], int]:
    """One bot's whole career, filtered to the matches being imported.

    Returns the kept rows and how many were walked to find them. Filtering as we
    page is what keeps this affordable in memory: the fetch may walk 150,000
    rows and keep a couple of thousand.
    """
    kept: list[dict] = []
    walked = 0
    async for p in client.list_bot_match_participations(bot_id):
        walked += 1
        if p.get("match") in in_scope:
            kept.append(p)
    return kept, walked



def _short_match_ids(
    session: Session, competition_ids: list[int], limit: int | None = None, *, scatter: bool = False
) -> list[int]:
    """Finished matches holding fewer than the two rows a 1v1 owes."""
    stmt = (
        select(Match.id)
        .join(Round, Match.round_id == Round.id)  # type: ignore[arg-type]
        .where(Round.competition_id.in_(competition_ids))  # type: ignore[attr-defined]
        .where(Match.result_created.is_not(None))  # type: ignore[union-attr]
        .where(Match.result_type.is_distinct_from("MatchCancelled"))  # type: ignore[union-attr]
        .where(
            select(func.count())
            .select_from(MatchParticipation)
            .where(MatchParticipation.match_id == Match.id)
            .scalar_subquery() < 2
        )
    )
    if scatter:
        stmt = stmt.order_by(func.random())
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(session.exec(stmt).all())


async def _discover_unlisted_bots(
    session: Session, client: AiArenaClient, competition_ids: list[int], known: set[int]
) -> set[int]:
    """Bot ids that played these competitions but aren't in their standings.

    A bot can appear in a season's matches and not in its
    competition-participations list at all — bot 990 played 9,870
    InitializationError games in 2026 Pre-Season 1 and is absent from its
    standings, leaving every one of those matches a row short. Its rows are
    unreachable from the standings, so ask a sample of the short matches who
    owns them. It only takes a few matches to name the bot; one bulk sweep of
    that bot then fixes all of them, which beats repairing ten thousand matches
    one request at a time.
    """
    sample = _short_match_ids(session, competition_ids, limit=_DISCOVERY_SAMPLE, scatter=True)
    if not sample:
        return set()
    results = await asyncio.gather(
        *[_fetch_participations(client, mid) for mid in sample], return_exceptions=True
    )
    found: set[int] = set()
    for items in results:
        if isinstance(items, BaseException):
            continue
        for p in items:
            if isinstance(p.get("bot"), int):
                found.add(p["bot"])
    return found - known


async def _sweep_bots(
    session: Session, client: AiArenaClient, todo: list[int], in_scope: set[int]
) -> dict[int, int]:
    """Page each bot's career and store the rows belonging to this import."""
    imported: dict[int, int] = {}
    slots = asyncio.Semaphore(_BOTS_IN_FLIGHT)
    t_bots = time.monotonic()

    async def _one(bot_id: int) -> tuple[int, tuple[list[dict], int] | BaseException]:
        async with slots:
            try:
                return bot_id, await _fetch_bot_rows(client, bot_id, in_scope)
            except Exception as exc:  # noqa: BLE001
                return bot_id, exc

    tasks = [asyncio.create_task(_one(bot_id)) for bot_id in todo]
    done = 0
    walked_total = 0
    for finished in asyncio.as_completed(tasks):
        bot_id, result = await finished
        done += 1
        if isinstance(result, BaseException):
            # Losing one bot costs that bot's rows, not the run. The next pass
            # picks it up; the coverage check reports whatever never arrived.
            log.warning("Failed to page participations for bot %s: %s", bot_id, result)
            continue
        rows, walked = result
        walked_total += walked
        ensure_bot_stub(session, bot_id)
        for p in rows:
            upsert(session, MatchParticipation, _participation_values(p))
        # Commit per bot, not per N rows. The rows are already in memory, so
        # this holds SQLite's write lock for the length of one burst of inserts
        # rather than across the network waits between bots — a batching commit
        # would keep the transaction open for minutes and lock out the live
        # sync running in the other process.
        session.commit()
        imported[bot_id] = len(rows)
        elapsed = time.monotonic() - t_bots
        # Report career rows walked, not bots finished. Bots complete shortest
        # career first, so the completed count crawls while most of the work is
        # in flight — it read as 2/85 done when the run was a quarter through.
        log.info(
            "Bots %d/%d — %d rows kept, %d walked (%.0fs in, %.0f rows/s)",
            done, len(todo), sum(imported.values()), walked_total, elapsed,
            walked_total / elapsed if elapsed else 0,
        )
    return imported


async def backfill(
    session: Session,
    client: AiArenaClient,
    competition_ids: list[int],
    *,
    force: bool = False,
) -> dict[int, int]:
    """Import full match history for ``competition_ids``. Returns rows per bot.

    Safe to re-run: rounds already complete are skipped, matches already
    finalised are skipped, and a bot whose rows are all present is not paged
    again unless ``force``. An interrupted run resumes rather than restarting.
    """
    t0 = time.monotonic()
    log.info("Backfilling competitions %s", competition_ids)

    await sync_maps(session, client)

    for competition_id in competition_ids:
        await sync_competition(session, client, competition_id)
        await sync_participations(session, client, competition_id)
        log.info("Competition %s: importing match rows", competition_id)
        # Match rows only — their participations come from the bulk pass below.
        await sync_rounds_and_matches(
            session, client, competition_id, fetch_participations=False
        )

    in_scope = _in_scope_match_ids(session, competition_ids)
    expected = _expected_rows(session, competition_ids)
    log.info(
        "Match rows in scope: %d across %d bots (%d participation rows expected)",
        len(in_scope), len(expected), sum(expected.values()),
    )

    imported: dict[int, int] = {}
    swept: set[int] = set()   # successfully paged in this run; never paged twice
    for attempt in range(1, _MAX_PASSES + 1):
        stored = _stored_rows(session, competition_ids)
        # `force` re-pages everything, but only on the first pass — otherwise
        # later passes would re-page bots they just finished, forever.
        todo = {
            bot_id for bot_id, want in expected.items()
            if bot_id not in swept and ((force and attempt == 1) or stored.get(bot_id, 0) < want)
        }
        if attempt == 1:
            skipped = len(expected) - len(todo)
            if skipped:
                log.info("Skipping %d bots already fully imported", skipped)
        else:
            # Two reasons to come back round: a bot whose pages timed out and
            # exhausted their retries, and a bot that owns rows but never
            # appeared in the standings, which only the matches can name.
            unlisted = await _discover_unlisted_bots(
                session, client, competition_ids, known=set(expected) | swept
            )
            if unlisted:
                log.info("Found %d bots with rows here but no standings entry: %s",
                         len(unlisted), sorted(unlisted))
                expected.update({bot_id: 1 for bot_id in unlisted})
            todo |= unlisted
            if todo:
                log.info("Pass %d: paging %d bots", attempt, len(todo))
        if not todo:
            break
        swept_now = await _sweep_bots(session, client, sorted(todo), in_scope)
        imported.update(swept_now)
        swept.update(swept_now)

    await sync_bots(session, client, set(expected), force=False)

    _report_coverage(session, competition_ids, expected)
    log.info("Backfill complete in %.0fs", time.monotonic() - t0)
    return imported


def _report_coverage(session: Session, competition_ids: list[int], expected: dict[int, int]) -> None:
    """Report what the import actually produced.

    The headline number is matches missing a participation row, not rows
    imported. Row totals overshoot ``match_count`` for legitimate reasons
    (games that errored carry rows the standings don't count), so a percentage
    against it read as 109% while 10,563 matches were quietly one row short.
    A 1v1 match owes exactly two rows, and that is checkable.
    """
    stored = _stored_rows(session, competition_ids)
    got = sum(stored.get(bot_id, 0) for bot_id in expected)
    incomplete = session.exec(
        select(func.count())
        .select_from(Match)
        .join(Round, Match.round_id == Round.id)  # type: ignore[arg-type]
        .where(Round.competition_id.in_(competition_ids))  # type: ignore[attr-defined]
        .where(Match.result_created.is_not(None))  # type: ignore[union-attr]
        .where(Match.result_type.is_distinct_from("MatchCancelled"))  # type: ignore[union-attr]
        .where(
            select(func.count())
            .select_from(MatchParticipation)
            .where(MatchParticipation.match_id == Match.id)
            .scalar_subquery() < 2
        )
    ).one()
    log.info("Imported %d participation rows across %d bots", got, len(stored))
    if incomplete:
        log.warning(
            "%d finished matches still have fewer than 2 participation rows — "
            "re-run to page the bots they belong to", incomplete,
        )
    else:
        log.info("Every finished match has both participation rows")
