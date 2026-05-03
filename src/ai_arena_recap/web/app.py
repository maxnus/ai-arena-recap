import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session, func, select

from ai_arena_recap.config import settings
from ai_arena_recap.db import engine, init_db
from ai_arena_recap.models import Bot, Competition, Match, Round
from ai_arena_recap.sync.replays import sync_replays
from ai_arena_recap.sync.runner import sync_all
from ai_arena_recap.web.routes import api, bot, ladder, match

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, force=True)
logging.getLogger("httpx").setLevel(logging.WARNING)

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent


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
    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

    app.include_router(ladder.router)
    app.include_router(bot.router)
    app.include_router(match.router)
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
