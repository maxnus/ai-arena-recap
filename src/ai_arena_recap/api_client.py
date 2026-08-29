import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ai_arena_recap.config import settings

log = logging.getLogger(__name__)

_RETRY_STATUSES = {429, 500, 502, 503, 504}

# Floor for the shrinking bulk page size. Below this the request count climbs
# faster than the per-request saving is worth.
MIN_BULK_PAGE_SIZE = 250

# Ceiling on the pacing backoff multiplier. At 32x a job paced to one request
# every 4s drops to one every two minutes, which is as good as stopped.
_MAX_PACE_PENALTY = 32.0


class AiArenaClient:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        token: str | None = None,
        concurrency: int | None = None,
        timeout: float = 30.0,
        rate_per_minute: float | None = None,
    ) -> None:
        self.base_url = (base_url or settings.api_base_url).rstrip("/")
        self.token = token or settings.aiarena_api_token
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={"Authorization": f"Token {self.token}"},
        )
        self._sem = asyncio.Semaphore(concurrency or settings.request_concurrency)
        self._min_interval = 0.0
        self._pace_lock = asyncio.Lock()
        self._next_slot = 0.0
        self._penalty = 1.0
        self.set_rate_per_minute(rate_per_minute)

    def set_rate_per_minute(self, rate: float | None) -> None:
        """Cap how often requests may *start*, across all concurrent callers.

        None or 0 means unpaced, which is the default and what the live sync
        uses. The backfill sets a rate to spread a long import over hours
        instead of minutes: aiarena is a volunteer-run service, and a job that
        does a day's worth of requests should not do it in one burst.

        This bounds the request rate, not the concurrency — the two are
        independent. At 13/min the semaphore is almost never contended.
        """
        self._min_interval = 60.0 / rate if rate else 0.0

    def _slow_down(self) -> None:
        """Back off after the server signals distress (5xx or a timeout).

        A fixed rate is a guess about someone else's database. This makes the
        guess self-correcting: the 2025 import ran fine for an hour and three
        quarters and then degraded aiarena's participation endpoint, with the
        depth at which requests failed sliding steadily lower. Every one of
        those failures was a signal to ease off that a fixed rate could not
        act on. Unpaced clients are unaffected — the penalty multiplies an
        interval that is zero for them.
        """
        self._penalty = min(self._penalty * 2.0, _MAX_PACE_PENALTY)

    def _speed_up(self) -> None:
        """Ease back toward the configured rate after a clean response.

        Decays slowly and geometrically: roughly 35 good responses to undo one
        backoff step, so a recovering server is approached gently rather than
        immediately hammered again."""
        if self._penalty > 1.0:
            self._penalty = max(1.0, self._penalty * 0.98)

    async def _await_pace(self) -> None:
        """Block until this request's turn in the paced schedule.

        Slots are handed out from a shared cursor so N concurrent workers share
        one rate rather than getting N times it. The lock covers claiming a
        slot, never the sleep, so workers queue instantly and then wait apart.
        """
        if not self._min_interval:
            return
        loop = asyncio.get_running_loop()
        async with self._pace_lock:
            now = loop.time()
            start = max(now, self._next_slot)
            self._next_slot = start + self._min_interval * self._penalty
        delay = start - now
        if delay > 0:
            await asyncio.sleep(delay)

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "AiArenaClient":
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.close()

    async def _get(
        self, url: str, params: dict[str, Any] | None = None, *, max_attempts: int = 5
    ) -> dict[str, Any]:
        """GET with retry/backoff, and pacing feedback.

        ``max_attempts`` is 5 for ordinary calls, where a 5xx is usually a blip
        worth riding out. Callers that can make the request *cheaper* instead
        should pass a lower count: retrying an over-expensive query unchanged
        costs the server the same failed work every time, so the bulk pager
        would rather learn quickly and ask for less.
        """
        await self._await_pace()
        async with self._sem:
            last = max_attempts - 1
            for attempt in range(max_attempts):
                try:
                    response = await self._client.get(url, params=params)
                except httpx.TransportError as exc:
                    self._slow_down()
                    if attempt == last:
                        raise
                    delay = 2**attempt
                    log.warning("Transport error %s, retrying in %ss", exc, delay)
                    await asyncio.sleep(delay)
                    continue
                if response.status_code in _RETRY_STATUSES:
                    self._slow_down()
                    if attempt < last:
                        delay = 2**attempt
                        log.warning("HTTP %s on %s, retrying in %ss", response.status_code, url, delay)
                        await asyncio.sleep(delay)
                        continue
                response.raise_for_status()
                self._speed_up()
                return response.json()
        raise RuntimeError("unreachable")

    async def _paginate(
        self, path: str, params: dict[str, Any] | None = None, *, page_size: int | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        url: str | None = f"{self.base_url}{path}"
        merged = {"format": "json", "limit": page_size or settings.api_page_size, **(params or {})}
        while url:
            data = await self._get(url, params=merged)
            for item in data.get("results", []):
                yield item
            url = data.get("next")
            merged = None  # next URL already includes params

    # ----- typed helpers -----

    async def get_competition(self, competition_id: int) -> dict[str, Any]:
        return await self._get(f"{self.base_url}/competitions/{competition_id}/", {"format": "json"})

    async def list_competitions(self) -> AsyncIterator[dict[str, Any]]:
        async for item in self._paginate("/competitions/", {}):
            yield item

    async def list_competition_participations(self, competition_id: int) -> AsyncIterator[dict[str, Any]]:
        async for item in self._paginate(
            "/competition-participations/", {"competition": competition_id}
        ):
            yield item

    async def get_bot(self, bot_id: int) -> dict[str, Any]:
        return await self._get(f"{self.base_url}/bots/{bot_id}/", {"format": "json"})

    async def get_user(self, user_id: int) -> dict[str, Any]:
        return await self._get(f"{self.base_url}/users/{user_id}/", {"format": "json"})

    async def list_rounds(self, competition_id: int) -> AsyncIterator[dict[str, Any]]:
        async for item in self._paginate("/rounds/", {"competition": competition_id}):
            yield item

    async def list_matches_for_round(self, round_id: int) -> AsyncIterator[dict[str, Any]]:
        async for item in self._paginate("/matches/", {"round": round_id}):
            yield item

    async def list_match_participations(self, match_id: int) -> AsyncIterator[dict[str, Any]]:
        async for item in self._paginate("/match-participations/", {"match": match_id}):
            yield item

    async def list_bot_match_participations(self, bot_id: int) -> AsyncIterator[dict[str, Any]]:
        """Every participation row for one bot, oldest first — the bulk path.

        The endpoint takes no competition filter and ignores id/match range
        filters, and paging it unfiltered dies at 504 past roughly a million
        rows of offset. Filtering by bot keeps offsets inside a single bot's
        career, which pages reliably to the end, and costs one request per page
        instead of one per match. `ordering=id` is what makes offset paging
        well-defined; without it pages overlap and skip — one 18,588-row career
        came back with 2,163 rows twice and 2,163 not at all.

        Callers get the bot's whole history and keep the rows they want — there
        is no way to ask the API for a narrower slice.

        Pages shrink as they get expensive. Server cost grows with offset, so a
        page size that is comfortable at the start of a long career starts
        returning 502/504 deep into it: at 5000 rows, offsets past 25,000 failed
        even after five retries, losing whole bots. On failure the page size
        drops and the same offset is retried — offsets are row counts, so
        changing the page size mid-career is safe — and it never grows back,
        since offsets only get deeper.
        """
        url = f"{self.base_url}/match-participations/"
        limit = settings.backfill_page_size
        offset = 0
        total: int | None = None
        while total is None or offset < total:
            params = {
                "format": "json", "limit": limit, "bot": bot_id,
                "ordering": "id", "offset": offset,
            }
            try:
                data = await self._get(url, params, max_attempts=2)
            except (httpx.HTTPStatusError, httpx.TransportError):
                if limit <= MIN_BULK_PAGE_SIZE:
                    raise
                limit = max(MIN_BULK_PAGE_SIZE, limit // 4)
                log.warning(
                    "Dropping to %d-row pages for bot %s at offset %d", limit, bot_id, offset
                )
                continue
            total = int(data.get("count") or 0)
            results = data.get("results", [])
            if not results:
                break
            for item in results:
                yield item
            offset += len(results)

    async def get_match(self, match_id: int) -> dict[str, Any]:
        return await self._get(f"{self.base_url}/matches/{match_id}/", {"format": "json"})

    async def list_maps(self) -> AsyncIterator[dict[str, Any]]:
        async for item in self._paginate("/maps/", {}):
            yield item
