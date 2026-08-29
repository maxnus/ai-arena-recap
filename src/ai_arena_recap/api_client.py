import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ai_arena_recap.config import settings

log = logging.getLogger(__name__)

_RETRY_STATUSES = {429, 500, 502, 503, 504}


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
            self._next_slot = start + self._min_interval
        delay = start - now
        if delay > 0:
            await asyncio.sleep(delay)

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "AiArenaClient":
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.close()

    async def _get(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        await self._await_pace()
        async with self._sem:
            for attempt in range(5):
                try:
                    response = await self._client.get(url, params=params)
                except httpx.TransportError as exc:
                    if attempt == 4:
                        raise
                    delay = 2**attempt
                    log.warning("Transport error %s, retrying in %ss", exc, delay)
                    await asyncio.sleep(delay)
                    continue
                if response.status_code in _RETRY_STATUSES and attempt < 4:
                    delay = 2**attempt
                    log.warning("HTTP %s on %s, retrying in %ss", response.status_code, url, delay)
                    await asyncio.sleep(delay)
                    continue
                response.raise_for_status()
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
        career, which pages reliably to the end, and costs one request per 500
        rows instead of one per match. `ordering=id` is what makes offset paging
        well-defined; without it pages can overlap or skip.

        Callers get the bot's whole history and keep the rows they want — there
        is no way to ask the API for a narrower slice.

        Pages are large (``backfill_page_size``) because the only pagination on
        offer is offset-based, and an offset costs more the deeper it goes:
        walking a 55k-row career took 100s in 111 pages of 500, and 44s in 12
        pages of 5000. Dropping ``ordering`` would be faster still and is not an
        option — unordered offset paging returned 11.6% of one career twice and
        another 11.6% not at all.
        """
        async for item in self._paginate(
            "/match-participations/", {"bot": bot_id, "ordering": "id"},
            page_size=settings.backfill_page_size,
        ):
            yield item

    async def get_match(self, match_id: int) -> dict[str, Any]:
        return await self._get(f"{self.base_url}/matches/{match_id}/", {"format": "json"})

    async def list_maps(self) -> AsyncIterator[dict[str, Any]]:
        async for item in self._paginate("/maps/", {}):
            yield item
