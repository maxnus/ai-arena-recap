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


@respx.mock
def test_bulk_bot_participations_are_ordered_and_paged_large():
    """Two request properties this endpoint cannot lose.

    `ordering=id` is a correctness requirement, not a preference: paged without
    it, one 18,588-row career came back with 2,163 rows duplicated and 2,163
    never returned at all. The large page size is what makes the sweep
    affordable — the endpoint offers only offset pagination, which gets slower
    the deeper it goes, so fewer-and-bigger pages beat more-and-smaller ones.
    """
    from ai_arena_recap.config import settings

    base = "https://example.test/api"
    route = respx.get(f"{base}/match-participations/").mock(
        return_value=httpx.Response(200, json={"count": 1, "results": [{"id": 1}]})
    )

    async def _run():
        async with AiArenaClient(base_url=base, token="test") as client:
            return [p async for p in client.list_bot_match_participations(42)]

    assert asyncio.run(_run()) == [{"id": 1}]
    params = route.calls[0].request.url.params
    assert params["ordering"] == "id"
    assert params["bot"] == "42"
    assert int(params["limit"]) == settings.backfill_page_size > settings.api_page_size


class TestRatePacing:
    """Pacing spreads a long import over hours instead of firing it in a burst.
    Off by default, so the live sync is unaffected."""

    def test_unpaced_by_default(self):
        async def _run():
            async with AiArenaClient(base_url="https://x.test", token="t") as c:
                return c._min_interval

        assert asyncio.run(_run()) == 0.0

    def test_rate_becomes_an_interval_between_request_starts(self):
        async def _run():
            async with AiArenaClient(base_url="https://x.test", token="t",
                                     rate_per_minute=30) as c:
                return c._min_interval

        assert asyncio.run(_run()) == 2.0

    def test_concurrent_callers_share_one_rate_rather_than_multiplying_it(self):
        """Eight workers at 60/min must produce 60 requests a minute between
        them, not 480 — the whole point of pacing."""
        starts: list[float] = []

        async def _run():
            async with AiArenaClient(base_url="https://x.test", token="t",
                                     rate_per_minute=60) as c:
                loop = asyncio.get_running_loop()
                slept = []

                async def fake_sleep(seconds):
                    slept.append(seconds)

                async def worker():
                    await c._await_pace()
                    starts.append(loop.time())

                import ai_arena_recap.api_client as mod
                real_sleep = mod.asyncio.sleep
                mod.asyncio.sleep = fake_sleep
                try:
                    await asyncio.gather(*[worker() for _ in range(8)])
                finally:
                    mod.asyncio.sleep = real_sleep
                return slept

        slept = asyncio.run(_run())
        # Eight slots at one second apart: the first goes immediately, and each
        # subsequent one waits a second longer than the last.
        assert len(slept) == 7
        assert [round(s) for s in sorted(slept)] == [1, 2, 3, 4, 5, 6, 7]

    def test_pacing_does_not_hold_the_lock_while_waiting(self):
        """Claiming a slot must be instant; only the waiting is staggered. If
        the lock covered the sleep, workers would serialise on each other and
        the effective rate would drift."""
        async def _run():
            async with AiArenaClient(base_url="https://x.test", token="t",
                                     rate_per_minute=60) as c:
                async def hog():
                    await c._await_pace()

                task = asyncio.create_task(hog())
                await asyncio.sleep(0)          # let it claim and start waiting
                locked_during_wait = c._pace_lock.locked()
                task.cancel()
                return locked_during_wait

        assert asyncio.run(_run()) is False


class TestBulkPageShrinks:
    """Server cost grows with offset, so a page size that works at the start of
    a long career starts failing deep into it. During the 2025 import, 5000-row
    pages past offset 25,000 returned 502/504 through all five retries and lost
    whole bots."""

    @respx.mock
    def test_page_size_drops_after_a_failure_and_the_offset_is_retried(self, fast_sleep):
        from ai_arena_recap.api_client import MIN_BULK_PAGE_SIZE
        from ai_arena_recap.config import settings

        base = "https://example.test/api"
        big = settings.backfill_page_size
        rows = [{"id": i} for i in range(big)]

        def responder(request):
            limit = int(request.url.params["limit"])
            offset = int(request.url.params["offset"])
            if offset == 0:
                return httpx.Response(200, json={"count": big + 1, "results": rows})
            # Deep page: only a smaller request is served.
            if limit == big:
                return httpx.Response(502)
            return httpx.Response(200, json={"count": big + 1, "results": [{"id": 9999}]})

        respx.get(f"{base}/match-participations/").mock(side_effect=responder)

        async def _run():
            async with AiArenaClient(base_url=base, token="t") as c:
                return [p async for p in c.list_bot_match_participations(7)]

        got = asyncio.run(_run())
        assert got[-1] == {"id": 9999}          # the deep page arrived after shrinking
        assert len(got) == big + 1              # nothing lost or duplicated
        limits = [int(c.request.url.params["limit"]) for c in respx.calls]
        assert limits[0] == big                 # started big
        assert limits[-1] < big                 # ended smaller
        assert limits[-1] >= MIN_BULK_PAGE_SIZE

    @respx.mock
    def test_it_gives_up_rather_than_shrinking_forever(self, fast_sleep):
        base = "https://example.test/api"
        respx.get(f"{base}/match-participations/").mock(return_value=httpx.Response(502))

        async def _run():
            async with AiArenaClient(base_url=base, token="t") as c:
                return [p async for p in c.list_bot_match_participations(7)]

        with pytest.raises(httpx.HTTPStatusError):
            asyncio.run(_run())


class TestAdaptiveBackoff:
    """A fixed rate is a guess about someone else's database. The 2025 import
    ran fine for an hour and three quarters, then degraded aiarena's
    participation endpoint with the failure depth sliding steadily lower. Every
    one of those failures was a signal to ease off that a fixed rate ignored."""

    def _client(self, **kw):
        return AiArenaClient(base_url="https://x.test", token="t", rate_per_minute=60, **kw)

    @respx.mock
    def test_a_server_error_widens_the_gap_between_requests(self, fast_sleep):
        respx.get("https://x.test/thing").mock(return_value=httpx.Response(503))

        async def _run():
            async with self._client() as c:
                try:
                    await c._get("https://x.test/thing", max_attempts=1)
                except httpx.HTTPStatusError:
                    pass
                return c._penalty

        assert asyncio.run(_run()) > 1.0

    @respx.mock
    def test_backoff_compounds_while_the_server_keeps_failing(self, fast_sleep):
        respx.get("https://x.test/thing").mock(return_value=httpx.Response(502))

        async def _run():
            async with self._client() as c:
                for _ in range(4):
                    try:
                        await c._get("https://x.test/thing", max_attempts=1)
                    except httpx.HTTPStatusError:
                        pass
                return c._penalty

        assert asyncio.run(_run()) == 16.0   # 2^4

    @respx.mock
    def test_backoff_is_capped(self, fast_sleep):
        from ai_arena_recap.api_client import _MAX_PACE_PENALTY
        respx.get("https://x.test/thing").mock(return_value=httpx.Response(502))

        async def _run():
            async with self._client() as c:
                for _ in range(40):
                    try:
                        await c._get("https://x.test/thing", max_attempts=1)
                    except httpx.HTTPStatusError:
                        pass
                return c._penalty

        assert asyncio.run(_run()) == _MAX_PACE_PENALTY

    @respx.mock
    def test_recovery_is_gradual_not_immediate(self, fast_sleep):
        """A recovering server must not be hammered the moment it answers once."""
        respx.get("https://x.test/thing").mock(return_value=httpx.Response(200, json={}))

        async def _run():
            async with self._client() as c:
                c._penalty = 8.0
                await c._get("https://x.test/thing")
                after_one = c._penalty
                for _ in range(200):
                    await c._get("https://x.test/thing")
                return after_one, c._penalty

        after_one, after_many = asyncio.run(_run())
        assert 7.0 < after_one < 8.0    # one good response barely moves it
        assert after_many == 1.0        # sustained success returns to the set rate

    @respx.mock
    def test_an_unpaced_client_is_unaffected(self, fast_sleep):
        """The live sync sets no rate, so backoff must cost it nothing."""
        respx.get("https://x.test/thing").mock(return_value=httpx.Response(502))

        async def _run():
            async with AiArenaClient(base_url="https://x.test", token="t") as c:
                try:
                    await c._get("https://x.test/thing", max_attempts=1)
                except httpx.HTTPStatusError:
                    pass
                import time as _t
                t0 = _t.monotonic()
                await c._await_pace()
                return _t.monotonic() - t0

        assert asyncio.run(_run()) < 0.05
