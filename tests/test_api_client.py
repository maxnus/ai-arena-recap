"""Tests for AiArenaClient pagination and retry behaviour.

The client is async, so each test uses asyncio.run on a small wrapper.
respx intercepts httpx without going to the network.
"""
import asyncio

import httpx
import pytest
import respx

from ai_arena_recap.api_client import AiArenaClient


@pytest.fixture()
def fast_sleep(monkeypatch):
    """Stub asyncio.sleep so retry-backoff tests don't actually wait."""
    async def _instant(_seconds):
        return None

    monkeypatch.setattr("ai_arena_recap.api_client.asyncio.sleep", _instant)


@respx.mock
def test_paginate_follows_next_url():
    base = "https://example.test/api"
    page1 = {
        "results": [{"id": 1}, {"id": 2}],
        "next": f"{base}/things/page2",
    }
    page2 = {"results": [{"id": 3}], "next": None}
    respx.get(f"{base}/things/page2").mock(return_value=httpx.Response(200, json=page2))
    respx.get(f"{base}/things/").mock(return_value=httpx.Response(200, json=page1))

    async def _run():
        async with AiArenaClient(base_url=base, token="test") as client:
            return [item async for item in client._paginate("/things/")]

    items = asyncio.run(_run())
    assert [i["id"] for i in items] == [1, 2, 3]


@respx.mock
def test_get_retries_on_5xx_then_succeeds(fast_sleep):
    base = "https://example.test/api"
    route = respx.get(f"{base}/competitions/1/").mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(503),
            httpx.Response(200, json={"id": 1, "name": "Test"}),
        ]
    )

    async def _run():
        async with AiArenaClient(base_url=base, token="test") as client:
            return await client.get_competition(1)

    data = asyncio.run(_run())
    assert data == {"id": 1, "name": "Test"}
    assert route.call_count == 3


@respx.mock
def test_get_raises_after_exhausting_retries(fast_sleep):
    base = "https://example.test/api"
    respx.get(f"{base}/competitions/1/").mock(return_value=httpx.Response(500))

    async def _run():
        async with AiArenaClient(base_url=base, token="test") as client:
            await client.get_competition(1)

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(_run())


@respx.mock
def test_4xx_does_not_retry():
    base = "https://example.test/api"
    route = respx.get(f"{base}/bots/1/").mock(return_value=httpx.Response(404))

    async def _run():
        async with AiArenaClient(base_url=base, token="test") as client:
            await client.get_bot(1)

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(_run())
    assert route.call_count == 1


@respx.mock
def test_authorization_header_is_set():
    base = "https://example.test/api"
    route = respx.get(f"{base}/competitions/1/").mock(
        return_value=httpx.Response(200, json={"id": 1})
    )

    async def _run():
        async with AiArenaClient(base_url=base, token="secret-xyz") as client:
            await client.get_competition(1)

    asyncio.run(_run())
    assert route.calls.last.request.headers["Authorization"] == "Token secret-xyz"
