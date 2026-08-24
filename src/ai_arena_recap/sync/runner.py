import asyncio
import logging
import time
from datetime import timedelta

from sqlmodel import Session, select

from ai_arena_recap.api_client import AiArenaClient
from ai_arena_recap.config import settings
from ai_arena_recap.db import get_session
from ai_arena_recap.models import Competition
from ai_arena_recap.sync.bots import sync_bots
from ai_arena_recap.sync.common import utcnow
from ai_arena_recap.sync.competition import sync_competition, sync_participations
from ai_arena_recap.sync.maps import sync_maps
from ai_arena_recap.sync.rounds import repair_incomplete_participations, sync_rounds_and_matches

log = logging.getLogger(__name__)

_lock = asyncio.Lock()


def archived_due(session: Session) -> list[int]:
    """Archived seasons that are due a refresh.

    Every competition in the DB other than the tracked one stays browsable at
    /s/<slug>/, so it has to keep existing — but a closed season's data never
    changes, so touching it once a day is enough (and covers the case where the
    final sync of a season was interrupted). Ordered oldest-synced first."""
    cutoff = utcnow() - timedelta(seconds=settings.archive_refresh_seconds)
    rows = session.exec(
        select(Competition.id, Competition.last_synced)
        .where(Competition.id != settings.competition_id)
        .order_by(Competition.last_synced.asc())
    ).all()
    return [cid for cid, last_synced in rows if last_synced is None or last_synced <= cutoff]


async def warn_if_season_rolled_over(session: Session, client: AiArenaClient) -> None:
    """Log loudly when the tracked competition has ended.

    aiarena starts the next season the same day it closes the old one, and the
    site keeps serving the finished one (as final standings) until
    ``competition_id`` is pointed at the successor. One extra API call, and only
    while the tracked competition is closed."""
    comp = session.get(Competition, settings.competition_id)
    if comp is None or (comp.status or "").lower() != "closed":
        return
    open_ids = [c["id"] async for c in client.list_competitions() if (c.get("status") or "").lower() == "open"]
    log.warning(
        "Tracked competition %s (%s) is CLOSED — the site is serving its final standings. "
        "Open competitions upstream: %s. Set COMPETITION_ID to the new season to switch over.",
        comp.id, comp.name, open_ids or "none",
    )


async def _sync_competition_tree(
    session: Session,
    client: AiArenaClient,
    competition_id: int,
    *,
    max_rounds: int | None = None,
) -> set[int]:
    """Competition row, standings and rounds/matches for one competition.
    Returns the bot ids it referenced."""
    await sync_competition(session, client, competition_id)
    bot_ids = await sync_participations(session, client, competition_id)
    bot_ids |= await sync_rounds_and_matches(session, client, competition_id, max_rounds=max_rounds)
    return bot_ids


async def sync_all(
    *,
    max_rounds: int | None = None,
    force_bots: bool = False,
    competition_id: int | None = None,
) -> None:
    """Run a complete incremental sync. Reentrancy-safe via asyncio.Lock.

    Syncs the tracked competition plus any archived season due a refresh. Pass
    ``competition_id`` to sync one competition instead — that's how a season the
    DB has never seen gets imported (``ai-arena-recap sync --competition 36``)."""
    if _lock.locked():
        log.info("Sync already in progress; skipping this tick")
        return
    async with _lock:
        t0 = time.monotonic()
        primary = competition_id or settings.competition_id
        log.info("Starting sync (competition=%s, max_rounds=%s)", primary, max_rounds)
        async with AiArenaClient() as client:
            with get_session() as session:
                await sync_maps(session, client)
                bot_ids = await _sync_competition_tree(session, client, primary, max_rounds=max_rounds)

                if competition_id is None:
                    await warn_if_season_rolled_over(session, client)
                    for archived_id in archived_due(session):
                        log.info("Refreshing archived season %s", archived_id)
                        bot_ids |= await _sync_competition_tree(session, client, archived_id)

                bot_ids |= await repair_incomplete_participations(session, client)
                await sync_bots(session, client, bot_ids, force=force_bots)
        log.info("Sync complete in %.1fs", time.monotonic() - t0)

        # A competition that just closed changes which rows count as its ladder,
        # so drop the memoised season before anything reads it again.
        from ai_arena_recap.web import season

        season.reset()

        # Pre-warm the /rankings cache off the event loop so the first visitor
        # after this sync never waits on the aggregate queries. A no-op when the
        # data fingerprint is unchanged; never raises. Imported lazily to keep
        # the web layer out of the sync module's import graph.
        from ai_arena_recap.web.rankings import warm_rankings
        await asyncio.to_thread(warm_rankings)
