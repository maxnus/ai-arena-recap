# AI Arena Recap

Code behind [aiarenarecap.com](https://aiarenarecap.com), a website that aggregates and displays StarCraft 2 bot-vs-bot match data from [aiarena.net](https://aiarena.net). It syncs competitive match results to a local SQLite database and serves a dashboard with rankings, per-bot analytics, and match details.

## Features

- **Ladder rankings** with ELO ratings, division placement, and win/loss stats
- **Bot detail pages** with match history, head-to-head matchup records, and performance trends
- **Match pages** with game details and replay downloads
- **Season archive** -- finished seasons stay browsable at `/s/<slug>/` (e.g.
  `/s/2026-season-1/`), with their own ladder, rankings and bot pages
- **Background sync** periodically fetches updates from the aiarena.net API
- **Offline-capable** -- local caching means the site stays up even if the upstream API is unavailable

## Setup

Requires Python 3.11+.

```bash
pip install -e .
```

Create a `.env` file:

```
aiarena_api_token=YOUR_TOKEN
```

Optional settings:

| Variable | Default | Description |
|---|---|---|
| `competition_id` | `37` | Competition served at the un-prefixed URLs ("this season") |
| `archive_refresh_seconds` | `86400` | How often to re-check archived seasons |
| `sync_interval_seconds` | `600` | Background sync frequency |
| `db_path` | `ai_arena_recap.db` | SQLite database path |

## Usage

```bash
# Initialize the database
ai-arena-recap init-db

# Sync match data (use --max-rounds to limit initial import)
ai-arena-recap sync
ai-arena-recap sync --max-rounds 50

# Import a past season's final standings into the archive (seconds).
# Browsable at /s/<slug>/ afterwards: full ladder, no match history.
ai-arena-recap sync --competition 36 --max-rounds 0

# Import a past season in full, match history included (tens of minutes).
# Pass several at once — that pages each bot's history once instead of once
# per season. Safe to interrupt; re-running resumes.
ai-arena-recap backfill --competition 35
ai-arena-recap backfill -c 34 -c 33 -c 32

# Start the web server (http://127.0.0.1:8000)
ai-arena-recap serve
```

## Tech Stack

FastAPI, SQLite/SQLModel, Jinja2, APScheduler, httpx
