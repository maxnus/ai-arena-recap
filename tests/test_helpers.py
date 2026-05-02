from datetime import datetime, timezone

import pytest

from ai_arena_recap.sync.common import parse_dt
from ai_arena_recap.web.deps import humanize_age


class TestParseDt:
    def test_returns_none_for_none(self):
        assert parse_dt(None) is None

    def test_returns_none_for_empty(self):
        assert parse_dt("") is None

    def test_parses_z_suffix_as_utc(self):
        result = parse_dt("2026-04-25T10:00:00Z")
        assert result == datetime(2026, 4, 25, 10, 0, 0, tzinfo=timezone.utc)

    def test_parses_explicit_offset(self):
        result = parse_dt("2026-04-25T10:00:00+00:00")
        assert result == datetime(2026, 4, 25, 10, 0, 0, tzinfo=timezone.utc)

    def test_parses_microseconds(self):
        result = parse_dt("2026-04-25T10:00:00.123456Z")
        assert result.microsecond == 123456
        assert result.tzinfo == timezone.utc

    def test_invalid_string_raises(self):
        with pytest.raises(ValueError):
            parse_dt("not a date")


class TestHumanizeAge:
    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (0, "0s ago"),
            (30, "30s ago"),
            (59, "59s ago"),
            (60, "1m ago"),
            (90, "1m ago"),
            (3599, "59m ago"),
            (3600, "1h ago"),
            (3700, "1h ago"),
            (86399, "23h ago"),
            (86400, "1d ago"),
            (86400 * 3 + 50, "3d ago"),
        ],
    )
    def test_unit_boundaries(self, seconds, expected):
        assert humanize_age(seconds) == expected
