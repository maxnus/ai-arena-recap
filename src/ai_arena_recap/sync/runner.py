import asyncio
import logging
import time

from ai_arena_recap.api_client import AiArenaClient
from ai_arena_recap.config import settings
from ai_arena_recap.db import get_session
from ai_arena_recap.sync.bots import sync_bots
from ai_arena_recap.sync.competition import sync_competition, sync_participations
from ai_arena_recap.sync.maps import sync_maps
from ai_arena_recap.sync.rounds import sync_rounds_and_matches

log = logging.getLogger(__name__)

_lock = asyncio.Lock()


async def sync_all(*, max_rounds: int | None = None, force_bots: bool = False) -> None:
    """Run a complete incremental sync. Reentrancy-safe via asyncio.Lock."""
    if _lock.locked():
        log.info("Sync already in progress; skipping this tick")
        return
    async with _lock:
        t0 = time.monotonic()
        log.info("Starting sync (competition=%s, max_rounds=%s)", settings.competition_id, max_rounds)
        async with AiArenaClient() as client:
            with get_session() as session:
                await sync_maps(session, client)
                await sync_competition(session, client, settings.competition_id)
                referenced_bots = await sync_participations(session, client, settings.competition_id)
                match_bot_ids = await sync_rounds_and_matches(
                    session, client, settings.competition_id, max_rounds=max_rounds
                )
                await sync_bots(session, client, referenced_bots | match_bot_ids, force=force_bots)
        log.info("Sync complete in %.1fs", time.monotonic() - t0)
