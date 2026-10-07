"""Permanent, throttled replay archive for a finished season.

`replays.py` keeps a rolling window of the *live* season's replays and deletes
what falls out of it. This module does the opposite job: it walks a closed
season once and keeps every replay it can, because aiarena eventually deletes
them upstream. Sampling the API in September 2026 showed the horizon:

    2025 Pre-Season 1  (closed Mar 2025)   74% already cleaned
    2025 Season 1      (closed Jun 2025)   33% already cleaned
    2025 Pre-Season 2  (closed Oct 2025)    0% cleaned
    2026 Season 1      (closed Aug 2026)    0% cleaned

So a season is safe for roughly a year and then starts disappearing. Archiving
one is a race we win by a wide margin if we start early, and lose entirely if we
wait two years.

Three things make this different from the live cache, and all three are why it
is a separate module rather than a flag on `sync_replays`:

* **Bulk URL fetch.** The live cache calls `/matches/{id}/` once per replay.
  A season is ~240,000 matches, and that shape would mean 240,000 requests
  against a Django box that a previous backfill degraded at ~26 requests/min.
  `/matches/?round=N` embeds each match's `result.replay_file`, so the same
  URLs arrive a few hundred at a time — roughly 1,200 requests for a whole
  season instead of 240,000. (`?competition=` is accepted and silently
  ignored — it returns the unfiltered count — so the walk goes round by round.)

* **Two independent throttles.** Listing matches hits aiarena's own server,
  which is fragile and volunteer-run. Downloading hits their S3 bucket, which
  is Amazon's and won't fall over — but aiarena pays the egress, and a season
  is a couple of hundred gigabytes of it. Those are different resources with
  different limits, so they get separate limiters: requests/min on the API,
  bytes/sec on the downloads.

* **Nothing here may be cleaned up.** Archived replays live in a
  ``c<competition_id>/`` subdirectory. `_cleanup_old_replays` globs
  ``*.SC2Replay`` in the replay root, which does not recurse, so the archive is
  invisible to it — the separation is the directory layout, not a flag someone
  has to remember to check.

Safe to interrupt and re-run: files on disk are the only state, so a re-run
skips what is already there and picks up where it stopped.
"""
import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from sqlmodel import Session, select
from aiarena_api import AiArenaClient

from ai_arena_recap.models import Round
from ai_arena_recap.sync.replays import archive_dir

log = logging.getLogger(__name__)

# Signed S3 URLs carry X-Amz-Expires=3600. A page of URLs must therefore be
# fully downloaded within the hour, and pages are sized so that a page's worth
# of bytes takes about this long at the configured rate — a third of the
# budget, so a slow patch or a run of oversized replays still lands well inside
# it. Getting this wrong doesn't lose data (a re-run refetches), it just wastes
# the listing request and the partial transfers.
_PAGE_SECONDS = 1200.0

# Mean replay size measured over a 750-file stratified sample of 2026 Season 1
# (median 326 KB, p90 2.1 MB, max 30 MB). Only used to size pages against
# _PAGE_SECONDS, so being off by 2x costs nothing.
_MEAN_REPLAY_BYTES = 1_000_000

_MIN_PAGE = 25
_MAX_PAGE = 500

_CHUNK = 65536


class ByteRateLimiter:
    """Token bucket over bytes, shared by every concurrent download.

    Bandwidth is the thing worth capping here, not request count: replays range
    from 30 KB to 30 MB, so a files-per-minute limit would let a run of
    step-limit ties (mean 9 MB — they play to the end of the clock) pull thirty
    times the egress of a run of early crashes at the same nominal rate.

    Tokens are allowed to go negative and the caller sleeps off the deficit,
    which queues concurrent writers in arrival order without holding the lock
    across the sleep — the same shape as aiarena-api's ``Pacer.wait``.
    """

    def __init__(self, bytes_per_second: float, *, burst_seconds: float = 2.0) -> None:
        self.rate = bytes_per_second
        self._capacity = bytes_per_second * burst_seconds
        self._tokens = self._capacity
        self._updated: float | None = None
        self._lock = asyncio.Lock()

    async def acquire(self, amount: int) -> None:
        if self.rate <= 0:
            return
        loop = asyncio.get_running_loop()
        async with self._lock:
            now = loop.time()
            if self._updated is None:
                self._updated = now
            self._tokens = min(self._capacity, self._tokens + (now - self._updated) * self.rate)
            self._updated = now
            self._tokens -= amount
            deficit = -self._tokens
        if deficit > 0:
            await asyncio.sleep(deficit / self.rate)


@dataclass
class ArchiveStats:
    downloaded: int = 0
    bytes: int = 0
    already_had: int = 0
    no_replay: int = 0          # upstream cleaned it, or the match never produced one
    too_big: int = 0            # skipped by --max-file-mb, before transferring the body
    expired: int = 0            # signed URL died before we got to it; a re-run refetches
    failed: int = 0
    budget_reached: bool = False
    per_competition: dict[int, int] = field(default_factory=dict)

    # Set once at startup: the budget, and what the archive already weighed
    # before this run started.
    budget_bytes: int | None = None
    baseline_bytes: int = 0

    def over_budget(self) -> bool:
        """Checked before starting each file, not once per page.

        A page is up to 500 URLs and a replay averages a megabyte, so a
        per-page check could sail 500 MB past the limit — which makes a disk
        budget useless for the thing it exists to prevent.
        """
        if self.budget_bytes is None:
            return False
        return self.baseline_bytes + self.bytes >= self.budget_bytes


def _dir_bytes(path: Path) -> tuple[int, int]:
    """(file count, total bytes) of the replays already in ``path``."""
    count = 0
    total = 0
    with os.scandir(path) as entries:
        for entry in entries:
            if entry.is_file() and entry.name.endswith(".SC2Replay"):
                count += 1
                total += entry.stat().st_size
    return count, total


def _existing_match_ids(path: Path) -> set[int]:
    found: set[int] = set()
    with os.scandir(path) as entries:
        for entry in entries:
            name = entry.name
            if not name.endswith(".SC2Replay"):
                continue
            try:
                found.add(int(name[: -len(".SC2Replay")]))
            except ValueError:
                continue
    return found


def _clear_partial_downloads(path: Path) -> int:
    """Delete part-written `.tmp` files left by a previous run.

    Interrupting a run is expected — it is the documented way to stop one — and
    it leaves however many downloads were in flight as `.tmp` files. Nothing
    else would ever remove them: the live cache's cleanup sweeps `*.tmp` in the
    replay root only, and `glob` does not recurse into the archive. Without
    this, every interruption permanently leaks a few megabytes.

    Only safe because one archive run per directory is assumed; a second
    concurrent run would delete the first's in-flight downloads.
    """
    removed = 0
    for tmp in path.glob("*.SC2Replay.tmp"):
        tmp.unlink(missing_ok=True)
        removed += 1
    return removed


def _page_size(limiter: ByteRateLimiter) -> int:
    """How many URLs to hold at once, given how fast we can drain them.

    See _PAGE_SECONDS: signed URLs expire in an hour, so the page has to be
    small enough that the slowest plausible page still finishes inside it.
    """
    if limiter.rate <= 0:
        return _MAX_PAGE
    fits = int(limiter.rate * _PAGE_SECONDS / _MEAN_REPLAY_BYTES)
    return max(_MIN_PAGE, min(_MAX_PAGE, fits))


async def _download_one(
    http: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    limiter: ByteRateLimiter,
    match_id: int,
    url: str,
    dest_dir: Path,
    max_file_bytes: int | None,
    stats: ArchiveStats,
) -> None:
    tmp_path = dest_dir / f"{match_id}.SC2Replay.tmp"
    final_path = dest_dir / f"{match_id}.SC2Replay"
    async with sem:
        if stats.over_budget():
            stats.budget_reached = True
            return
        try:
            async with http.stream("GET", url, follow_redirects=True) as resp:
                if resp.status_code == 403:
                    # Signed URL outlived its hour. Not an error worth retrying
                    # here — the listing request that produced it is long gone,
                    # and a re-run gets a fresh one for free.
                    stats.expired += 1
                    return
                resp.raise_for_status()

                # Check the size before reading any of the body: closing the
                # stream here costs one request and a few packets instead of the
                # 30 MB the outliers actually weigh.
                declared = resp.headers.get("content-length")
                if max_file_bytes is not None and declared is not None and int(declared) > max_file_bytes:
                    stats.too_big += 1
                    return

                written = 0
                with open(tmp_path, "wb") as f:
                    async for chunk in resp.aiter_bytes(chunk_size=_CHUNK):
                        await limiter.acquire(len(chunk))
                        f.write(chunk)
                        written += len(chunk)
                        # A missing or lying content-length must not become an
                        # unbounded write.
                        if max_file_bytes is not None and written > max_file_bytes:
                            stats.too_big += 1
                            tmp_path.unlink(missing_ok=True)
                            return

            # Rename last: a killed run leaves a .tmp, never a truncated file
            # that a later run would mistake for a complete replay.
            tmp_path.rename(final_path)
            stats.downloaded += 1
            stats.bytes += written
        except Exception as exc:  # noqa: BLE001
            stats.failed += 1
            log.warning("Replay download failed for match %s: %s", match_id, exc)
            tmp_path.unlink(missing_ok=True)


async def _list_round_page(
    client: AiArenaClient, round_id: int, limit: int, offset: int
) -> tuple[list[tuple[int, str]], int, int, int]:
    """One page of a round's matches, reduced to (match_id, replay_url).

    Returns the downloadable pairs, the round's total match count, how many
    rows on this page had no replay URL, and how many rows came back at all —
    the caller advances the offset by that last number rather than by ``limit``,
    so a server that quietly caps the page size can't make us skip matches.

    ``ordering=id`` is what makes offset paging well-defined; without it pages
    overlap and skip, the same way they did on `/match-participations/`
    (see `AiArenaClient.list_bot_match_participations` in aiarena-api).
    """
    data = await client.get("/matches/", {"limit": limit, "offset": offset, "round": round_id, "ordering": "id"})
    results = data.get("results", [])
    pairs: list[tuple[int, str]] = []
    missing = 0
    for m in results:
        url = (m.get("result") or {}).get("replay_file")
        if url:
            pairs.append((m["id"], url))
        elif (m.get("result") or {}).get("created"):
            # Finished, but the file is gone (or was never kept). Unfinished
            # matches simply have no result and aren't counted as a miss.
            missing += 1
    return pairs, int(data.get("count") or 0), missing, len(results)


async def archive_replays(
    session: Session,
    client: AiArenaClient,
    competition_ids: list[int],
    *,
    download_bytes_per_second: float,
    api_rate_per_minute: float | None = None,
    max_file_bytes: int | None = None,
    budget_bytes: int | None = None,
    concurrency: int = 4,
) -> ArchiveStats:
    """Download and keep every available replay for ``competition_ids``.

    ``download_bytes_per_second`` caps egress from aiarena's S3 bucket;
    ``api_rate_per_minute`` caps listing requests against aiarena itself. Both
    default to something deliberate rather than "as fast as it will answer" —
    a previous import degraded the API for two hours at ~26 requests/min, and
    the download side spends someone else's bandwidth budget.

    ``budget_bytes`` stops the run once the archive reaches that size, and
    ``max_file_bytes`` skips individual outliers before their body transfers.

    Rounds are walked oldest first: upstream cleanup takes the oldest replays
    first, so that ordering saves the ones actually at risk if the run is cut
    short.
    """
    stats = ArchiveStats(budget_bytes=budget_bytes)
    limiter = ByteRateLimiter(download_bytes_per_second)
    page_size = _page_size(limiter)
    if api_rate_per_minute:
        client.set_rate_per_minute(api_rate_per_minute)

    sem = asyncio.Semaphore(concurrency)
    t0 = time.monotonic()

    log.info(
        "Archiving replays for competitions %s: <=%.1f MB/s download, %s listing req/min, "
        "%d URLs per page",
        competition_ids,
        download_bytes_per_second / 1e6,
        f"{api_rate_per_minute:.1f}" if api_rate_per_minute else "unpaced",
        page_size,
    )

    for competition_id in competition_ids:
        _, existing_bytes = _dir_bytes(archive_dir(competition_id))
        stats.baseline_bytes += existing_bytes
    if budget_bytes is not None:
        log.info(
            "Archive holds %.2f GB; budget %.2f GB",
            stats.baseline_bytes / 2**30, budget_bytes / 2**30,
        )

    async with httpx.AsyncClient(timeout=120.0) as http:
        for competition_id in competition_ids:
            if stats.budget_reached:
                break
            dest_dir = archive_dir(competition_id)
            partial = _clear_partial_downloads(dest_dir)
            if partial:
                log.info("Cleared %d part-written replay(s) from an interrupted run", partial)
            have = _existing_match_ids(dest_dir)
            rounds = list(session.exec(
                select(Round.id)
                .where(Round.competition_id == competition_id)
                .order_by(Round.number.asc())  # type: ignore[union-attr]
            ).all())
            if not rounds:
                log.warning(
                    "Competition %s has no rounds locally — sync or backfill it first", competition_id
                )
                continue
            log.info(
                "Competition %s: %d rounds, %d replays already archived",
                competition_id, len(rounds), len(have),
            )
            before = stats.downloaded

            for round_index, round_id in enumerate(rounds, start=1):
                if stats.budget_reached:
                    break
                offset = 0
                total: int | None = None
                while total is None or offset < total:
                    pairs, total, missing, rows = await _list_round_page(
                        client, round_id, page_size, offset
                    )
                    if not rows:
                        break
                    offset += rows
                    stats.no_replay += missing

                    todo = [(mid, url) for mid, url in pairs if mid not in have]
                    stats.already_had += len(pairs) - len(todo)
                    if not todo:
                        continue

                    await asyncio.gather(*[
                        _download_one(
                            http, sem, limiter, mid, url, dest_dir, max_file_bytes, stats
                        )
                        for mid, url in todo
                    ])
                    have.update(mid for mid, _ in todo)

                    if stats.budget_reached:
                        # The downloads stopped themselves; this only reports it.
                        log.warning(
                            "Disk budget of %.2f GB reached — stopping. Re-run with a larger "
                            "--max-gb to continue where this left off.",
                            (budget_bytes or 0) / 2**30,
                        )
                        break

                elapsed = time.monotonic() - t0
                log.info(
                    "Competition %s round %d/%d — %d downloaded (%.2f GB, %.2f MB/s), "
                    "%d already had, %d no replay, %d too big, %d expired, %d failed",
                    competition_id, round_index, len(rounds), stats.downloaded,
                    stats.bytes / 2**30, stats.bytes / 1e6 / elapsed if elapsed else 0,
                    stats.already_had, stats.no_replay, stats.too_big,
                    stats.expired, stats.failed,
                )

            stats.per_competition[competition_id] = stats.downloaded - before

    log.info(
        "Replay archive complete in %.0fs: %d downloaded (%.2f GB), %d already present, "
        "%d unavailable upstream, %d skipped as oversized, %d expired, %d failed",
        time.monotonic() - t0, stats.downloaded, stats.bytes / 2**30, stats.already_had,
        stats.no_replay, stats.too_big, stats.expired, stats.failed,
    )
    return stats
