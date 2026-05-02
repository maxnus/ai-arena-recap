"""Tests for replay cleanup + selection helpers.

The download path itself talks to S3 and is exercised manually via
`probe-replay`; the helpers below are the parts worth keeping under test
because they govern disk usage and what gets re-fetched on each tick.
"""
from datetime import datetime, timedelta, timezone

from ai_arena_recap.models import Match
from ai_arena_recap.sync.common import upsert
from ai_arena_recap.sync.replays import _cleanup_old_replays, _matches_needing_replays

NOW = datetime(2026, 4, 25, 12, 0, 0, tzinfo=timezone.utc)


def _touch(path):
    path.write_bytes(b"x")


def _seed_match(session, *, match_id: int, result_created):
    upsert(session, Match, {
        "id": match_id,
        "started": result_created,
        "result_created": result_created,
        "last_synced": NOW,
    })


class TestCleanupOldReplays:
    def test_deletes_tmp_files_unconditionally(self, session, tmp_path, monkeypatch):
        from ai_arena_recap.sync import replays as replays_module
        monkeypatch.setattr(replays_module, "utcnow", lambda: NOW)

        _touch(tmp_path / "1.SC2Replay.tmp")
        _touch(tmp_path / "2.SC2Replay.tmp")

        deleted = _cleanup_old_replays(session, tmp_path, max_age_days=14)
        assert deleted == 0  # tmp files don't count toward "deleted replays"
        assert list(tmp_path.glob("*.SC2Replay.tmp")) == []

    def test_keeps_recent_replays_deletes_old(self, session, tmp_path, monkeypatch):
        from ai_arena_recap.sync import replays as replays_module
        monkeypatch.setattr(replays_module, "utcnow", lambda: NOW)

        _seed_match(session, match_id=1, result_created=NOW - timedelta(days=2))
        _seed_match(session, match_id=2, result_created=NOW - timedelta(days=20))
        session.commit()

        recent = tmp_path / "1.SC2Replay"
        old = tmp_path / "2.SC2Replay"
        _touch(recent)
        _touch(old)

        deleted = _cleanup_old_replays(session, tmp_path, max_age_days=14)
        assert deleted == 1
        assert recent.exists()
        assert not old.exists()

    def test_deletes_replays_for_unknown_matches(self, session, tmp_path, monkeypatch):
        from ai_arena_recap.sync import replays as replays_module
        monkeypatch.setattr(replays_module, "utcnow", lambda: NOW)

        # No Match row in the DB, so we have no way to know if it's recent.
        # Current behavior: delete it.
        orphan = tmp_path / "999.SC2Replay"
        _touch(orphan)

        deleted = _cleanup_old_replays(session, tmp_path, max_age_days=14)
        assert deleted == 1
        assert not orphan.exists()

    def test_skips_files_with_non_numeric_stems(self, session, tmp_path, monkeypatch):
        from ai_arena_recap.sync import replays as replays_module
        monkeypatch.setattr(replays_module, "utcnow", lambda: NOW)

        garbage = tmp_path / "notanumber.SC2Replay"
        _touch(garbage)

        deleted = _cleanup_old_replays(session, tmp_path, max_age_days=14)
        assert deleted == 0
        assert garbage.exists()


class TestMatchesNeedingReplays:
    def test_returns_recent_matches_without_local_file(self, session, tmp_path, monkeypatch):
        from ai_arena_recap.sync import replays as replays_module
        monkeypatch.setattr(replays_module, "utcnow", lambda: NOW)

        _seed_match(session, match_id=1, result_created=NOW - timedelta(days=1))
        _seed_match(session, match_id=2, result_created=NOW - timedelta(days=2))
        _seed_match(session, match_id=3, result_created=NOW - timedelta(days=20))  # outside window
        session.commit()
        # Match 1 already has a local file.
        _touch(tmp_path / "1.SC2Replay")

        pending = _matches_needing_replays(session, tmp_path, max_age_days=14)
        assert pending == [2]

    def test_skips_in_progress_matches(self, session, tmp_path, monkeypatch):
        from ai_arena_recap.sync import replays as replays_module
        monkeypatch.setattr(replays_module, "utcnow", lambda: NOW)

        # No result_created — match still in progress.
        upsert(session, Match, {
            "id": 1, "started": NOW - timedelta(hours=1),
            "result_created": None, "last_synced": NOW,
        })
        session.commit()

        assert _matches_needing_replays(session, tmp_path, max_age_days=14) == []
