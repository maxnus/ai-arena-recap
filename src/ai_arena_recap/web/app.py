import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlmodel import Session, func, select
from starlette.concurrency import run_in_threadpool

from ai_arena_recap.config import settings
from ai_arena_recap.db import engine, init_db
from ai_arena_recap.models import Bot, Competition, Match, Round
from ai_arena_recap.sync.common import utcnow
from ai_arena_recap.sync.replays import sync_replays
from ai_arena_recap.sync.runner import sync_all
from ai_arena_recap.web.routes import api, bot, ladder, match, rankings

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, force=True)
logging.getLogger("httpx").setLevel(logging.WARNING)

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent

# Substrings that mark a user agent as a crawler/bot rather than a human reader.
# Lower-cased before matching. "bot" catches Googlebot/bingbot/AhrefsBot/etc.;
# no normal browser UA contains it.
_CRAWLER_UA_MARKERS = (
    "bot", "spider", "crawl", "slurp", "mediapartners",
    "facebookexternalhit", "embedly", "preview", "monitor", "uptime",
)


def _is_crawler(user_agent: str) -> bool:
    ua = user_agent.lower()
    return any(marker in ua for marker in _CRAWLER_UA_MARKERS)


def _should_count(request: Request, response) -> bool:
    """Only count human page views: successful GETs of HTML pages. This filters
    out static assets, JSON API/healthz responses (non-HTML content types), 404s
    for missing bots (non-200), and known crawlers."""
    if request.method != "GET" or response.status_code != 200:
        return False
    if not response.headers.get("content-type", "").startswith("text/html"):
        return False
    return not _is_crawler(request.headers.get("user-agent", ""))


def record_page_view(path: str) -> None:
    """Bump today's view counter for ``path`` (upsert on the (path, day) pair).

    Best-effort: never lets an analytics write break a page load. References the
    module-level ``engine`` by name so the test suite's monkeypatched engine is
    picked up. Blocking DB I/O — call it off the event loop (see the middleware)."""
    today = utcnow().date().isoformat()
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO page_view (path, day, count) VALUES (:path, :day, 1) "
                    "ON CONFLICT(path, day) DO UPDATE SET count = count + 1"
                ),
                {"path": path, "day": today},
            )
    except Exception:  # noqa: BLE001 - page-view tracking must never break a request
        log.exception("Failed to record page view for %s", path)


async def page_view_middleware(request: Request, call_next):
    """Record a view for each rendered HTML page. The counter write runs in a
    threadpool so the SQLite I/O doesn't block the event loop. (A response
    BackgroundTask would be lighter, but Starlette's BaseHTTPMiddleware doesn't
    reliably run a background set on the call_next response.)"""
    response = await call_next(request)
    if _should_count(request, response):
        await run_in_threadpool(record_page_view, request.url.path)
    return response


async def _scheduled_sync() -> None:
    """Wrapper around sync_all that logs the scheduler tick itself."""
    log.info("Scheduler tick — running sync (interval=%ss)", settings.sync_interval_seconds)
    await sync_all()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()

    log.info("Config: competition_id=%s, sync_interval=%ss, request_concurrency=%s",
             settings.competition_id, settings.sync_interval_seconds, settings.request_concurrency)
    log.info("Config: replay_cache_enabled=%s, replay_dir=%s, replay_max_age_days=%s, "
             "replay_sync_interval=%ss, replay_download_concurrency=%s",
             settings.replay_cache_enabled, settings.replay_dir, settings.replay_max_age_days,
             settings.replay_sync_interval_seconds, settings.replay_download_concurrency)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        _scheduled_sync,
        "interval",
        seconds=settings.sync_interval_seconds,
        id="sync_all",
        max_instances=1,
        coalesce=True,           # if many fires were missed (e.g. system sleep), run once
        misfire_grace_time=None, # ...no matter how late — default 1s would drop them
    )
    if settings.replay_cache_enabled:
        scheduler.add_job(
            sync_replays,
            "interval",
            seconds=settings.replay_sync_interval_seconds,
            id="sync_replays",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=None,
        )
    scheduler.start()

    # Kick off an initial sync in the background, but don't block startup.
    # Replay sync chains after match sync so there are matches to download for.
    async def _initial_sync():
        await sync_all()
        if settings.replay_cache_enabled:
            await sync_replays()

    app.state.initial_sync_task = asyncio.create_task(_initial_sync())

    try:
        yield
    finally:
        scheduler.shutdown(wait=False)
        task = app.state.initial_sync_task
        if not task.done():
            task.cancel()


def create_app() -> FastAPI:
    app = FastAPI(title="AI Arena Recap", lifespan=lifespan)
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=[
            "aiarenarecap.com",
            "www.aiarenarecap.com",
            "127.0.0.1",
            "localhost",
        ],
    )
    app.middleware("http")(page_view_middleware)
    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

    app.include_router(ladder.router)
    app.include_router(bot.router)
    app.include_router(match.router)
    app.include_router(rankings.router)
    app.include_router(api.router)

    @app.get("/healthz")
    def healthz():
        with Session(engine) as session:
            comp = session.exec(select(Competition).where(Competition.id == settings.competition_id)).first()
            counts = {
                "bots": session.exec(select(func.count()).select_from(Bot)).one(),
                "rounds": session.exec(select(func.count()).select_from(Round)).one(),
                "matches": session.exec(select(func.count()).select_from(Match)).one(),
            }
        replay_cache = {
            "enabled": settings.replay_cache_enabled,
            "cached_count": len(list(settings.replay_dir.glob("*.SC2Replay"))) if settings.replay_cache_enabled else 0,
        }
        return JSONResponse({
            "competition_id": settings.competition_id,
            "competition_name": comp.name if comp else None,
            "competition_last_synced": comp.last_synced.isoformat() if comp else None,
            "counts": counts,
            "replay_cache": replay_cache,
        })

    return app


app = create_app()
