import asyncio
import logging
import time
from datetime import timedelta
from pathlib import Path

import httpx
from sqlmodel import Session, select

from ai_arena_recap.api_client import AiArenaClient
from ai_arena_recap.config import settings
from ai_arena_recap.db import get_session
from ai_arena_recap.models import Match
from ai_arena_recap.sync.common import utcnow

log = logging.getLogger(__name__)

_lock = asyncio.Lock()


def _cleanup_old_replays(session: Session, replay_dir: Path, max_age_days: int) -> int:
    # SQLite strips tzinfo on read, so compare against a naive UTC cutoff to
    # avoid TypeError between naive (DB) and aware (Python) datetimes.
    cutoff = (utcnow() - timedelta(days=max_age_days)).replace(tzinfo=None)
    deleted = 0

    for tmp in replay_dir.glob("*.SC2Replay.tmp"):
        tmp.unlink(missing_ok=True)

    for path in replay_dir.glob("*.SC2Replay"):
        try:
            match_id = int(path.stem)
        except ValueError:
            continue
        match = session.get(Match, match_id)
        result_created = match.result_created if match else None
        if result_created is not None and result_created.tzinfo is not None:
            result_created = result_created.replace(tzinfo=None)
        if result_created is not None and result_created >= cutoff:
            continue
        path.unlink(missing_ok=True)
        deleted += 1

    return deleted


def _matches_needing_replays(session: Session, replay_dir: Path, max_age_days: int) -> list[int]:
    cutoff = utcnow() - timedelta(days=max_age_days)
    match_ids = list(session.exec(
        select(Match.id)
        .where(Match.result_created.is_not(None))  # type: ignore[union-attr]
        .where(Match.result_created >= cutoff)  # type: ignore[operator]
        .order_by(Match.result_created.desc())  # type: ignore[union-attr]
    ).all())
    return [mid for mid in match_ids if not (replay_dir / f"{mid}.SC2Replay").exists()]


async def _download_one(
    http: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    client: AiArenaClient,
    match_id: int,
    replay_dir: Path,
) -> bool:
    async with sem:
        try:
            data = await client.get_match(match_id)
            url = (data.get("result") or {}).get("replay_file")
            if not url:
                return False
            tmp_path = replay_dir / f"{match_id}.SC2Replay.tmp"
            final_path = replay_dir / f"{match_id}.SC2Replay"
            async with http.stream("GET", url, follow_redirects=True) as resp:
                resp.raise_for_status()
                with open(tmp_path, "wb") as f:
                    async for chunk in resp.aiter_bytes(chunk_size=65536):
                        f.write(chunk)
            tmp_path.rename(final_path)
            return True
        except Exception:
            log.warning("Failed to download replay for match %s", match_id, exc_info=True)
            tmp_path = replay_dir / f"{match_id}.SC2Replay.tmp"
            tmp_path.unlink(missing_ok=True)
            return False


async def sync_replays() -> None:
    if not settings.replay_cache_enabled:
        return
    if _lock.locked():
        log.info("Replay sync already in progress; skipping")
        return
    async with _lock:
        t0 = time.monotonic()
        replay_dir = settings.replay_path
        log.info("Starting replay sync")

        with get_session() as session:
            deleted = _cleanup_old_replays(session, replay_dir, settings.replay_max_age_days)
            pending = _matches_needing_replays(session, replay_dir, settings.replay_max_age_days)

        if not pending:
            log.info("Replay sync: cleaned %d old, nothing to download (%.1fs)", deleted, time.monotonic() - t0)
            return

        sem = asyncio.Semaphore(settings.replay_download_concurrency)
        downloaded = 0
        failed = 0

        async with AiArenaClient() as client:
            async with httpx.AsyncClient(timeout=60.0) as http:
                batch_size = 50
                for i in range(0, len(pending), batch_size):
                    batch = pending[i : i + batch_size]
                    results = await asyncio.gather(
                        *[_download_one(http, sem, client, mid, replay_dir) for mid in batch],
                        return_exceptions=True,
                    )
                    for r in results:
                        if r is True:
                            downloaded += 1
                        else:
                            failed += 1

        log.info(
            "Replay sync complete: %d downloaded, %d failed, %d cleaned up (%.1fs)",
            downloaded, failed, deleted, time.monotonic() - t0,
        )
