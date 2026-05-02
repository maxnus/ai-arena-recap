import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, func, select

from ai_arena_recap.config import settings
from ai_arena_recap.db import engine, init_db
from ai_arena_recap.models import Bot, Competition, Match, Round
from ai_arena_recap.sync.runner import sync_all
from ai_arena_recap.web.routes import api, bot, ladder, match

# Uvicorn doesn't add a handler to the root logger, so our package's INFO
# messages (e.g. "Starting sync") would otherwise be silently dropped. Attach
# a handler directly to the package logger so its records are emitted
# regardless of how the app was started.
_pkg_log = logging.getLogger("ai_arena_recap")
if not _pkg_log.handlers:
    _pkg_log.setLevel(logging.INFO)
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    _pkg_log.addHandler(_h)
    _pkg_log.propagate = False

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))


def _humanize_age(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


templates.env.filters["age"] = _humanize_age


async def _scheduled_sync() -> None:
    """Wrapper around sync_all that logs the scheduler tick itself."""
    log.info("Scheduler tick — running sync (interval=%ss)", settings.sync_interval_seconds)
    await sync_all()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        _scheduled_sync,
        "interval",
        seconds=settings.sync_interval_seconds,
        id="sync_all",
        max_instances=1,
        coalesce=True,           # if many fires were missed (e.g. system sleep), run once
        misfire_grace_time=None, # ...no matter how late — default 1s would drop them
        next_run_time=None,
    )
    scheduler.start()

    # Kick off an initial sync in the background, but don't block startup.
    app.state.initial_sync_task = asyncio.create_task(sync_all())

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
        return JSONResponse({
            "competition_id": settings.competition_id,
            "competition_name": comp.name if comp else None,
            "competition_last_synced": comp.last_synced.isoformat() if comp else None,
            "counts": counts,
        })

    return app


app = create_app()
