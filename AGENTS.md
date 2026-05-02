# AI Arena Recap — agent guide

Website that aggregates StarCraft 2 bot-vs-bot match data from
[aiarena.net](https://aiarena.net) into a local SQLite DB and serves a
dashboard. The site stays usable when the upstream API is down (except for
direct replay downloads from S3).

## Pages

- **Ladder** (`/`) — current competition ranking with ELO, division, W/L/T/crash,
  and per-bot win-rate columns. AG Grid table.
- **Bot detail** (`/bots/{id}`) — author, race, type, status, ELO history chart
  (mean per round + rank trace), per-opponent-race win rates, recent matchups
  table, paginated match history.
- **Match detail** (`/matches/{id}`) — bots, duration, replay download button,
  recent head-to-head between the same two bots.

## Layout

- `src/ai_arena_recap/`
  - `models.py` — SQLModel tables (Bot, Competition, CompetitionParticipation,
    Round, Map, Match, MatchParticipation).
  - `config.py` — pydantic-settings, reads `.env`. `aiarena_api_token` is
    required; everything else has defaults.
  - `db.py` — SQLite engine + `init_db()`.
  - `api_client.py` — async httpx wrapper around the aiarena.net API with
    retry/backoff and a concurrency semaphore.
  - `sync/` — incremental sync from the API into the DB. `runner.sync_all`
    is the entry point; called both by the scheduler and the `sync` CLI.
    `replays.py` runs separately and caches recent replay files locally.
  - `web/`
    - `app.py` — FastAPI factory with lifespan-managed APScheduler.
    - `routes/{ladder,bot,match,api}.py` — page + JSON endpoints.
    - `templates/` — Jinja2 (base, _macros, ladder, bot, match).
    - `static/` — `styles.css`, race SVGs, JS helpers.
  - `cli.py` — typer commands (`init-db`, `sync`, `sync-replays`, `serve`,
    `probe-replay`).
- `tests/` — pytest with an in-memory SQLite fixture (`tests/conftest.py`
  monkey-patches the global engine so route handlers hit the test DB).
- `.github/workflows/ci.yml` — lint + test on push to main and on every PR.

## Running locally

Project uses `uv` (see `uv.lock`). `pip install -e .` also works but the lock
file is the source of truth.

```powershell
uv sync --all-groups            # install deps incl. dev
uv run ai-arena-recap init-db   # create SQLite tables
uv run ai-arena-recap sync      # incremental sync from aiarena.net
uv run ai-arena-recap serve     # http://127.0.0.1:8000
```

A long-running dev server may already be on :8000 — check with
`curl http://127.0.0.1:8000/healthz` before starting another one. UI changes
should be loaded in a browser before claiming a task is done; if a real
browser is unavailable, fetch the rendered HTML with curl and verify the
relevant fragment.

## Testing & lint

```powershell
uv run pytest          # unit + integration tests against an in-memory DB
uv run ruff check src tests
```

Both commands run in CI. Keep them green.

Style notes the linter (ruff) is configured for:

- `line-length = 120`, `target-version = py311`.
- `E741` is disabled — `l` is used as the W/L/T loss-count alias throughout
  this codebase. Don't rename to "fix" it.

## CI

GitHub Actions runs lint and tests on every push to `main` and every PR
(see `.github/workflows/ci.yml`). It uses `uv sync --frozen`, so the lock
file must stay in sync with `pyproject.toml`.

**Agents must verify CI status after every push.** Don't tell the user a
push is done until CI has gone green. Workflow:

```powershell
# Right after git push:
& "C:\Program Files\GitHub CLI\gh.exe" run list --limit 1 --json databaseId,status,conclusion,headSha
# Then watch (blocks until completion):
& "C:\Program Files\GitHub CLI\gh.exe" run watch <id> --exit-status
# If failed, read the failing step's log:
& "C:\Program Files\GitHub CLI\gh.exe" run view <id> --log-failed
```

`gh` is installed via winget but not on PATH for non-interactive shells;
invoke the full path `C:\Program Files\GitHub CLI\gh.exe`. If CI fails,
fix the underlying issue (don't just rerun) and push again.

## Conventions

- The app reads from the local DB only — no live API calls in route handlers.
  All network I/O lives in `sync/`.
- New API/JSON endpoints go under `/api/` and return `{"data": [...]}`-shaped
  responses for the AG Grid tables on the page.
- Tests must not hit the network. `respx` is used to mock httpx where needed.
- `Settings()` is constructed at import time — anything that imports
  `ai_arena_recap.config` requires `AIARENA_API_TOKEN` to be set (CI sets a
  dummy value).
- Prefer extending existing tables/routes over adding new abstractions.
- When changing `pyproject.toml` deps, refresh `uv.lock` (`uv lock`).
