"""Tests for the season replay archive.

The parts worth pinning down are the ones that cost real money or real disk if
they are wrong: the bandwidth limiter, the oversized-file skip (which must not
transfer the body), the layout split that keeps the live cleanup away from the
archive, and resumability.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from ai_arena_recap.api_client import AiArenaClient
from ai_arena_recap.models import Competition, Match, Round
from ai_arena_recap.sync.common import upsert
from ai_arena_recap.sync.replay_archive import (
    ByteRateLimiter,
    _page_size,
    archive_replays,
)
from ai_arena_recap.sync.replays import _cleanup_old_replays, archive_dir, find_local_replay

NOW = datetime(2026, 9, 9, 12, 0, 0, tzinfo=timezone.utc)
S3 = "https://bucket.example.com/replays"


@pytest.fixture()
def replay_root(tmp_path, monkeypatch):
    from ai_arena_recap.config import settings

    monkeypatch.setattr(settings, "replay_dir", tmp_path)
    return tmp_path


def _seed_season(session, *, competition_id=36, rounds=1, matches_per_round=3):
    upsert(session, Competition, {
        "id": competition_id, "name": f"Season {competition_id}",
        "status": "closed", "last_synced": NOW,
    })
    match_id = 1000
    for r in range(1, rounds + 1):
        round_id = 500 + r
        upsert(session, Round, {
            "id": round_id, "number": r, "competition_id": competition_id,
            "complete": True, "last_synced": NOW,
        })
        for _ in range(matches_per_round):
            match_id += 1
            upsert(session, Match, {
                "id": match_id, "round_id": round_id,
                "result_created": NOW - timedelta(days=100),
                "result_type": "Player1Win", "last_synced": NOW,
            })
    session.commit()


def _mock_round_page(round_id, match_ids, *, cleaned=()):
    results = []
    for mid in match_ids:
        result = {"id": mid, "created": "2026-05-01T00:00:00Z", "type": "Player1Win"}
        result["replay_file"] = None if mid in cleaned else f"{S3}/{mid}.SC2Replay"
        results.append({"id": mid, "round": round_id, "result": result})
    return {"count": len(match_ids), "results": results}


class TestByteRateLimiter:
    def test_unlimited_rate_never_sleeps(self):
        limiter = ByteRateLimiter(0)

        async def go():
            await limiter.acquire(10**9)

        asyncio.run(asyncio.wait_for(go(), timeout=1.0))

    def test_throttles_to_roughly_the_configured_rate(self):
        # 1000 B/s with a 2s burst: 4000 bytes is 2s of burst plus 2s of refill.
        limiter = ByteRateLimiter(1000, burst_seconds=2.0)

        async def go():
            loop = asyncio.get_running_loop()
            t0 = loop.time()
            for _ in range(4):
                await limiter.acquire(1000)
            return loop.time() - t0

        elapsed = asyncio.run(go())
        assert 1.5 <= elapsed <= 3.0

    def test_concurrent_callers_share_one_budget(self):
        limiter = ByteRateLimiter(1000, burst_seconds=1.0)

        async def go():
            loop = asyncio.get_running_loop()
            t0 = loop.time()
            await asyncio.gather(*[limiter.acquire(500) for _ in range(6)])
            return loop.time() - t0

        # 3000 bytes at 1000 B/s with 1000 of burst -> ~2s, regardless of the
        # split across callers.
        elapsed = asyncio.run(go())
        assert 1.5 <= elapsed <= 3.0


class TestPageSize:
    def test_scales_with_the_download_rate(self):
        assert _page_size(ByteRateLimiter(20_000)) < _page_size(ByteRateLimiter(2_000_000))

    def test_stays_within_bounds(self):
        assert _page_size(ByteRateLimiter(1)) == 25
        assert _page_size(ByteRateLimiter(10**9)) == 500
        assert _page_size(ByteRateLimiter(0)) == 500

    def test_a_page_fits_inside_the_signed_url_lifetime(self):
        # Signed URLs last an hour; a page's worth of bytes must drain well
        # inside that or the tail of every page 403s.
        from ai_arena_recap.sync.replay_archive import _MEAN_REPLAY_BYTES

        for rate in (50_000, 500_000, 2_000_000, 20_000_000):
            seconds = _page_size(ByteRateLimiter(rate)) * _MEAN_REPLAY_BYTES / rate
            assert seconds < 3600


def _run_archive(session, replay_root, **kwargs):
    """Run the archive against whatever routes the calling test registered.

    Deliberately not decorated with @respx.mock: the tests are, and nesting the
    decorator gives the inner scope a *copy* of the router, so `route.called`
    and `call_count` assertions in the test read zero even when the request
    happened.
    """
    async def go():
        async with AiArenaClient(token="t") as client:
            return await archive_replays(
                session, client, [36],
                download_bytes_per_second=0,
                api_rate_per_minute=None,
                **kwargs,
            )

    return asyncio.run(go())


class TestArchiveReplays:
    @respx.mock
    def test_downloads_every_available_replay(self, session, replay_root):
        _seed_season(session, matches_per_round=3)
        respx.get(url__startswith="https://aiarena.net/api/matches/").mock(
            return_value=httpx.Response(200, json=_mock_round_page(501, [1001, 1002, 1003]))
        )
        for mid in (1001, 1002, 1003):
            respx.get(f"{S3}/{mid}.SC2Replay").mock(
                return_value=httpx.Response(200, content=b"REPLAY" * 10)
            )

        stats = _run_archive(session, replay_root)

        assert stats.downloaded == 3
        assert stats.bytes == 3 * 60
        assert sorted(p.name for p in archive_dir(36).glob("*.SC2Replay")) == [
            "1001.SC2Replay", "1002.SC2Replay", "1003.SC2Replay",
        ]

    @respx.mock
    def test_counts_matches_whose_replay_is_gone_upstream(self, session, replay_root):
        _seed_season(session, matches_per_round=3)
        respx.get(url__startswith="https://aiarena.net/api/matches/").mock(
            return_value=httpx.Response(
                200, json=_mock_round_page(501, [1001, 1002, 1003], cleaned={1002, 1003})
            )
        )
        respx.get(f"{S3}/1001.SC2Replay").mock(return_value=httpx.Response(200, content=b"x"))

        stats = _run_archive(session, replay_root)

        assert stats.downloaded == 1
        assert stats.no_replay == 2

    @respx.mock
    def test_resumes_without_refetching_what_is_on_disk(self, session, replay_root):
        _seed_season(session, matches_per_round=3)
        (archive_dir(36) / "1001.SC2Replay").write_bytes(b"already here")
        respx.get(url__startswith="https://aiarena.net/api/matches/").mock(
            return_value=httpx.Response(200, json=_mock_round_page(501, [1001, 1002, 1003]))
        )
        hits = []
        for mid in (1002, 1003):
            respx.get(f"{S3}/{mid}.SC2Replay").mock(
                side_effect=lambda req, _h=hits: (_h.append(str(req.url)), httpx.Response(200, content=b"y"))[1]
            )

        stats = _run_archive(session, replay_root)

        assert stats.downloaded == 2
        assert stats.already_had == 1
        assert (archive_dir(36) / "1001.SC2Replay").read_bytes() == b"already here"

    @respx.mock
    def test_skips_oversized_files_without_reading_the_body(self, session, replay_root):
        _seed_season(session, matches_per_round=2)
        respx.get(url__startswith="https://aiarena.net/api/matches/").mock(
            return_value=httpx.Response(200, json=_mock_round_page(501, [1001, 1002]))
        )
        respx.get(f"{S3}/1001.SC2Replay").mock(
            return_value=httpx.Response(200, content=b"small")
        )
        # 50 MB declared; the body must never be transferred.
        big = respx.get(f"{S3}/1002.SC2Replay").mock(
            return_value=httpx.Response(
                200, headers={"content-length": str(50 * 2**20)}, content=b"z" * 100
            )
        )

        stats = _run_archive(session, replay_root, max_file_bytes=1024)

        assert stats.downloaded == 1
        assert stats.too_big == 1
        assert big.called
        assert not (archive_dir(36) / "1002.SC2Replay").exists()

    @respx.mock
    def test_enforces_the_cap_when_content_length_lies(self, session, replay_root):
        _seed_season(session, matches_per_round=1)
        respx.get(url__startswith="https://aiarena.net/api/matches/").mock(
            return_value=httpx.Response(200, json=_mock_round_page(501, [1001]))
        )
        # No content-length header at all -> the streaming guard has to catch it.
        respx.get(f"{S3}/1001.SC2Replay").mock(
            return_value=httpx.Response(200, content=b"z" * 5000)
        )

        stats = _run_archive(session, replay_root, max_file_bytes=100)

        assert stats.too_big == 1
        assert stats.downloaded == 0
        assert not (archive_dir(36) / "1001.SC2Replay").exists()
        assert not list(archive_dir(36).glob("*.tmp"))

    @respx.mock
    def test_expired_urls_are_counted_not_retried(self, session, replay_root):
        _seed_season(session, matches_per_round=1)
        respx.get(url__startswith="https://aiarena.net/api/matches/").mock(
            return_value=httpx.Response(200, json=_mock_round_page(501, [1001]))
        )
        route = respx.get(f"{S3}/1001.SC2Replay").mock(return_value=httpx.Response(403))

        stats = _run_archive(session, replay_root)

        assert stats.expired == 1
        assert stats.failed == 0
        assert route.call_count == 1

    @respx.mock
    def test_a_failed_download_leaves_no_partial_file(self, session, replay_root):
        _seed_season(session, matches_per_round=1)
        respx.get(url__startswith="https://aiarena.net/api/matches/").mock(
            return_value=httpx.Response(200, json=_mock_round_page(501, [1001]))
        )
        respx.get(f"{S3}/1001.SC2Replay").mock(side_effect=httpx.ConnectError("boom"))

        stats = _run_archive(session, replay_root)

        assert stats.failed == 1
        assert list(archive_dir(36).iterdir()) == []

    @respx.mock
    def test_stops_at_the_disk_budget(self, session, replay_root):
        _seed_season(session, rounds=3, matches_per_round=3)
        respx.get(url__startswith="https://aiarena.net/api/matches/").mock(
            side_effect=lambda req: httpx.Response(
                200,
                json=_mock_round_page(
                    int(req.url.params["round"]),
                    [int(req.url.params["round"]) * 10 + i for i in range(3)],
                ),
            )
        )
        respx.get(url__startswith=S3).mock(
            return_value=httpx.Response(200, content=b"x" * 1000)
        )

        stats = _run_archive(session, replay_root, budget_bytes=2500)

        assert stats.budget_reached
        # Stops at the end of the page that crossed the line, not mid-file.
        assert stats.downloaded == 3
        assert stats.bytes >= 2500

    @respx.mock
    def test_pages_through_a_round_larger_than_one_page(self, session, replay_root):
        """Rounds run to ~1,900 matches, well past the 500-row page ceiling."""
        _seed_season(session, rounds=1, matches_per_round=60)
        ids = list(range(1001, 1061))

        def page(request):
            limit = int(request.url.params["limit"])
            offset = int(request.url.params["offset"])
            window = ids[offset : offset + limit]
            body = _mock_round_page(501, window)
            body["count"] = len(ids)  # count is the round total, not the page
            return httpx.Response(200, json=body)

        listing = respx.get(url__startswith="https://aiarena.net/api/matches/").mock(side_effect=page)
        respx.get(url__startswith=S3).mock(return_value=httpx.Response(200, content=b"x" * 5))

        # 20 kB/s forces a 25-row page, so 60 matches need three of them.
        async def go():
            async with AiArenaClient(token="t") as client:
                return await archive_replays(
                    session, client, [36],
                    download_bytes_per_second=20_000, api_rate_per_minute=None,
                )

        stats = asyncio.run(go())

        assert stats.downloaded == 60
        assert listing.call_count == 3
        assert len(list(archive_dir(36).glob("*.SC2Replay"))) == 60

    @respx.mock
    def test_budget_stops_mid_page_not_at_the_end_of_it(self, session, replay_root):
        """A page is up to 500 URLs, so a per-page check could overshoot by ~500 MB."""
        _seed_season(session, rounds=1, matches_per_round=50)
        ids = list(range(1001, 1051))
        respx.get(url__startswith="https://aiarena.net/api/matches/").mock(
            return_value=httpx.Response(200, json=_mock_round_page(501, ids))
        )
        route = respx.get(url__startswith=S3).mock(
            return_value=httpx.Response(200, content=b"x" * 1000)
        )

        stats = _run_archive(session, replay_root, budget_bytes=5000, concurrency=1)

        assert stats.budget_reached
        # 5 files fills the budget; the remaining 45 URLs in the same page are
        # abandoned rather than downloaded.
        assert stats.downloaded == 5
        assert route.call_count == 5

    @respx.mock
    def test_warns_and_skips_a_competition_with_no_local_rounds(self, session, replay_root, caplog):
        stats = _run_archive(session, replay_root)

        assert stats.downloaded == 0
        assert "no rounds locally" in caplog.text


class TestLayoutKeepsTheArchiveSafe:
    def test_cleanup_never_touches_archived_replays(self, session, replay_root, monkeypatch):
        """The whole safety story: cleanup globs the root, which does not recurse."""
        from ai_arena_recap.sync import replays as replays_module

        monkeypatch.setattr(replays_module, "utcnow", lambda: NOW)
        _seed_season(session, matches_per_round=1)  # result_created 100 days ago

        archived = archive_dir(36) / "1001.SC2Replay"
        archived.write_bytes(b"keep me")
        stale_live = replay_root / "1001.SC2Replay"
        stale_live.write_bytes(b"drop me")

        deleted = _cleanup_old_replays(session, replay_root, max_age_days=14)

        assert deleted == 1
        assert not stale_live.exists()
        assert archived.exists()

    def test_find_local_replay_prefers_the_live_cache(self, session, replay_root):
        _seed_season(session, matches_per_round=1)
        live = replay_root / "1001.SC2Replay"
        live.write_bytes(b"live")
        (archive_dir(36) / "1001.SC2Replay").write_bytes(b"archived")

        assert find_local_replay(session, 1001) == live

    def test_find_local_replay_falls_back_to_the_archive(self, session, replay_root):
        _seed_season(session, matches_per_round=1)
        archived = archive_dir(36) / "1001.SC2Replay"
        archived.write_bytes(b"archived")

        assert find_local_replay(session, 1001) == archived

    def test_find_local_replay_returns_none_when_absent(self, session, replay_root):
        _seed_season(session, matches_per_round=1)
        assert find_local_replay(session, 1001) is None
        assert find_local_replay(session, 999999) is None
