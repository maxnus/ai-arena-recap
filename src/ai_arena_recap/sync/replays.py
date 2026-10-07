import asyncio
import logging
import time
from datetime import timedelta
from pathlib import Path

import httpx
from sqlmodel import Session, select
from aiarena_api import AiArenaClient

from ai_arena_recap.api_client import new_client
from ai_arena_recap.config import settings
from ai_arena_recap.db import get_session
from ai_arena_recap.models import Match, Round
from ai_arena_recap.sync.common import utcnow

log = logging.getLogger(__name__)

_lock = asyncio.Lock()


# ----- on-disk layout -----
#
# The replay root holds the live rolling cache, one flat `<match_id>.SC2Replay`
# per file, and `_cleanup_old_replays` deletes from it on a 14-day window.
# Archived seasons live one directory down, in `c<competition_id>/`.
#
# That split is the whole safety mechanism: the cleanup below globs
# `*.SC2Replay` in the root, and `glob` does not recurse, so an archived season
# is structurally out of its reach rather than protected by a condition someone
# could later forget. It also keeps either directory to a size `scandir` can
# walk quickly — a quarter of a million files in one flat directory would be
# re-globbed by the cleanup every five minutes and by /healthz on every hit.


def archive_dir(competition_id: int, *, create: bool = True) -> Path:
    """Directory holding one archived season's replays."""
    path = settings.replay_path / f"c{competition_id}"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def find_local_replay(session: Session, match_id: int) -> Path | None:
    """The stored replay for a match, from the live cache or the archive.

    Checked cheapest-first: the live cache path needs no query at all, and only
    a miss there costs the round -> competition lookup.
    """
    live = settings.replay_dir / f"{match_id}.SC2Replay"
    if live.is_file():
        return live

    competition_id = session.exec(
        select(Round.competition_id)
        .join(Match, Match.round_id == Round.id)  # type: ignore[arg-type]
        .where(Match.id == match_id)
    ).first()
    if competition_id is None:
        return None
    archived = archive_dir(competition_id, create=False) / f"{match_id}.SC2Replay"
    return archived if archived.is_file() else None


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
    tmp_path = replay_dir / f"{match_id}.SC2Replay.tmp"
    final_path = replay_dir / f"{match_id}.SC2Replay"
    async with sem:
        try:
            data = await client.get_match(match_id)
            url = (data.get("result") or {}).get("replay_file")
            if not url:
                return False
            async with http.stream("GET", url, follow_redirects=True) as resp:
                resp.raise_for_status()
                with open(tmp_path, "wb") as f:
                    async for chunk in resp.aiter_bytes(chunk_size=65536):
                        f.write(chunk)
            tmp_path.rename(final_path)
            return True
        except Exception:
            log.warning("Failed to download replay for match %s", match_id, exc_info=True)
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

        async with new_client() as client:
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
