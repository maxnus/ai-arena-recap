"""Snapshot the SQLite DB next to itself, keeping the last few copies.

Run before a deploy (see deploy/update.sh). The DB is the only copy of every
finished season the site still serves at /s/<slug>/ — aiarena.net keeps the raw
data, but re-importing a closed season is hours of API calls.

Uses sqlite3's online backup API, so it is safe to run against the live DB while
the service is writing to it: no locking, no torn WAL.
"""
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

KEEP = 3
SUFFIX = ".backup-"


def backup(db_path: Path, keep: int = KEEP) -> Path | None:
    if not db_path.is_file():
        print(f"No database at {db_path}; nothing to back up.")
        return None

    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = db_path.with_name(f"{db_path.name}{SUFFIX}{stamp}")

    source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        dest = sqlite3.connect(target)
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()

    size_mb = target.stat().st_size / 1024 / 1024
    print(f"Backed up {db_path} -> {target.name} ({size_mb:.0f} MB)")

    stale = sorted(db_path.parent.glob(f"{db_path.name}{SUFFIX}*"))[:-keep]
    for old in stale:
        old.unlink()
        print(f"Removed old backup {old.name}")
    return target


def main() -> int:
    if len(sys.argv) > 1:
        db_path = Path(sys.argv[1])
    else:
        from ai_arena_recap.config import settings

        db_path = settings.db_path
    backup(db_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
