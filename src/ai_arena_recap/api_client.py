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
    ) -> None:
        self.base_url = (base_url or settings.api_base_url).rstrip("/")
        self.token = token or settings.aiarena_api_token
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={"Authorization": f"Token {self.token}"},
        )
        self._sem = asyncio.Semaphore(concurrency or settings.request_concurrency)

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "AiArenaClient":
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.close()

    async def _get(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
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

    async def _paginate(self, path: str, params: dict[str, Any] | None = None) -> AsyncIterator[dict[str, Any]]:
        url: str | None = f"{self.base_url}{path}"
        merged = {"format": "json", "limit": 200, **(params or {})}
        while url:
            data = await self._get(url, params=merged)
            for item in data.get("results", []):
                yield item
            url = data.get("next")
            merged = None  # next URL already includes params

    # ----- typed helpers -----

    async def get_competition(self, competition_id: int) -> dict[str, Any]:
        return await self._get(f"{self.base_url}/competitions/{competition_id}/", {"format": "json"})

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

    async def get_match(self, match_id: int) -> dict[str, Any]:
        return await self._get(f"{self.base_url}/matches/{match_id}/", {"format": "json"})

    async def list_maps(self) -> AsyncIterator[dict[str, Any]]:
        async for item in self._paginate("/maps/", {}):
            yield item
