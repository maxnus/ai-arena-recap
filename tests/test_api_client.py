"""The recap's configuration of the aiarena-api client.

The client's own behaviour (retries, pacing, the bulk pager) is tested in the
aiarena-api package; what matters here is that the recap's settings reach it.
"""
import asyncio

import httpx
import respx

from ai_arena_recap.api_client import new_client
from ai_arena_recap.config import settings


def test_the_client_takes_its_configuration_from_settings():
    async def _run():
        async with new_client(timeout=99.0) as client:
            return client

    client = asyncio.run(_run())
    assert client.base_url == settings.api_base_url.rstrip("/")
    assert client.page_size == settings.api_page_size
    assert client._client.timeout.read == 99.0


@respx.mock
def test_the_bulk_pager_uses_the_large_backfill_page():
    """The bulk participation sweep pays for its offset on every page, so it
    reads in pages larger than the live sync's small list calls."""
    route = respx.get(url__startswith=f"{settings.api_base_url}/match-participations/").mock(
        return_value=httpx.Response(200, json={"count": 1, "results": [{"id": 1}]})
    )

    async def _run():
        async with new_client() as client:
            return [p async for p in client.list_bot_match_participations(42)]

    assert asyncio.run(_run()) == [{"id": 1}]
    limit = int(route.calls[0].request.url.params["limit"])
    assert limit == settings.backfill_page_size > settings.api_page_size


@respx.mock
def test_requests_carry_the_configured_token():
    route = respx.get(f"{settings.api_base_url}/bots/1/").mock(
        return_value=httpx.Response(200, json={"id": 1})
    )

    async def _run():
        async with new_client() as client:
            await client.get_bot(1)

    asyncio.run(_run())
    assert route.calls.last.request.headers["Authorization"] == f"Token {settings.aiarena_api_token}"
