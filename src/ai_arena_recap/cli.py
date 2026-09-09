import asyncio
import logging

import typer

app = typer.Typer(no_args_is_help=True, add_completion=False)


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)


@app.command("init-db")
def init_db_cmd(verbose: bool = typer.Option(False, "--verbose", "-v")):
    """Create database tables (idempotent)."""
    _setup_logging(verbose)
    from ai_arena_recap.db import init_db

    init_db()
    typer.echo("Database initialized.")


@app.command("sync")
def sync_cmd(
    max_rounds: int | None = typer.Option(
        None, "--max-rounds", help="Limit to the N most recent rounds (omit for all rounds)."
    ),
    force_bots: bool = typer.Option(False, "--force-bots", help="Refresh every referenced bot, even if recently synced."),
    competition: int | None = typer.Option(
        None,
        "--competition",
        help="Sync this competition instead of the tracked one — use it to import a past season "
             "into the archive (it stays browsable at /s/<slug>/).",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Run an incremental sync against aiarena.net."""
    _setup_logging(verbose)
    from ai_arena_recap.db import init_db
    from ai_arena_recap.sync.runner import sync_all

    init_db()
    asyncio.run(sync_all(max_rounds=max_rounds, force_bots=force_bots, competition_id=competition))


@app.command("backfill")
def backfill_cmd(
    competition: list[int] = typer.Option(
        ...,
        "--competition",
        "-c",
        help="Competition to import. Repeat for several — passing them together pages each "
             "bot's history once instead of once per season.",
    ),
    force: bool = typer.Option(
        False, "--force", help="Re-page bots whose rows are already all present."
    ),
    spread_hours: float | None = typer.Option(
        None,
        "--spread-hours",
        help="Pace the import to take roughly this long, instead of running flat out. "
             "Same total requests, spread thinner — kinder to aiarena.net.",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Import a finished season's full match history into the archive.

    Long-running (tens of minutes, or --spread-hours if you'd rather it trickle)
    and safe to interrupt — re-running resumes. Use
    `sync --competition N --max-rounds 0` instead for standings only.
    """
    _setup_logging(verbose)
    from ai_arena_recap.api_client import AiArenaClient
    from ai_arena_recap.db import get_session, init_db
    from ai_arena_recap.sync.backfill import backfill

    init_db()

    async def _run() -> None:
        from ai_arena_recap.config import settings

        async with AiArenaClient(timeout=settings.backfill_timeout_seconds) as client:
            with get_session() as session:
                await backfill(
                    session, client, list(competition), force=force,
                    spread_seconds=spread_hours * 3600 if spread_hours else None,
                )

    asyncio.run(_run())


@app.command("sync-replays")
def sync_replays_cmd(verbose: bool = typer.Option(False, "--verbose", "-v")):
    """Download replays for recent matches and clean up old ones."""
    _setup_logging(verbose)
    from ai_arena_recap.db import init_db
    from ai_arena_recap.sync.replays import sync_replays

    init_db()
    asyncio.run(sync_replays())


@app.command("archive-replays")
def archive_replays_cmd(
    competition: list[int] = typer.Option(
        ..., "--competition", "-c", help="Competition whose replays to archive. Repeat for several."
    ),
    mb_per_second: float = typer.Option(
        None, "--mb-per-second",
        help="Download bandwidth cap. aiarena pays the S3 egress; the default is deliberately low.",
    ),
    api_rate: float = typer.Option(
        None, "--api-rate", help="Match-listing requests per minute against aiarena.net.",
    ),
    max_file_mb: float | None = typer.Option(
        None, "--max-file-mb",
        help="Skip replays larger than this, before their body transfers. The size tail is long "
             "(p90 2 MB, max 30 MB) and dominated by step-limit ties, so a cap trades a few "
             "percent of replays for a large fraction of the disk. Defaults to "
             "REPLAY_ARCHIVE_MAX_FILE_MB; pass 0 for no cap.",
    ),
    max_gb: float | None = typer.Option(
        None, "--max-gb", help="Stop once the archive for these competitions reaches this size.",
    ),
    concurrency: int = typer.Option(None, "--concurrency", help="Simultaneous downloads."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Download and permanently keep a finished season's replays.

    Long-running (a season is ~200 GB and a day or two at the default rate) and
    safe to interrupt — files on disk are the state, so re-running resumes.
    Archived replays live in `<replay_dir>/c<competition_id>/` and are never
    touched by the live cache's cleanup.

    Requires the season's matches to be in the DB already (`sync --competition N`
    or `backfill -c N`).
    """
    _setup_logging(verbose)
    from ai_arena_recap.api_client import AiArenaClient
    from ai_arena_recap.config import settings
    from ai_arena_recap.db import get_session, init_db
    from ai_arena_recap.sync.replay_archive import archive_replays

    init_db()

    async def _run() -> None:
        rate = (mb_per_second * 1e6) if mb_per_second else settings.replay_archive_bytes_per_second
        # `is None` rather than `or`, so an explicit --max-file-mb 0 means
        # "no cap" instead of falling back to the configured default.
        cap_mb = settings.replay_archive_max_file_mb if max_file_mb is None else max_file_mb
        async with AiArenaClient(timeout=120.0) as client:
            with get_session() as session:
                await archive_replays(
                    session, client, list(competition),
                    download_bytes_per_second=rate,
                    api_rate_per_minute=api_rate or settings.replay_archive_api_rate_per_minute,
                    max_file_bytes=int(cap_mb * 2**20) if cap_mb else None,
                    budget_bytes=int(max_gb * 2**30) if max_gb else None,
                    concurrency=concurrency or settings.replay_archive_concurrency,
                )

    asyncio.run(_run())


@app.command("serve")
def serve_cmd(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port"),
    reload: bool = typer.Option(False, "--reload"),
):
    """Start the website (uvicorn). The background sync scheduler runs in-process."""
    import uvicorn

    log_config = uvicorn.config.LOGGING_CONFIG
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    log_config["formatters"]["default"]["fmt"] = fmt
    log_config["formatters"]["access"]["fmt"] = fmt

    uvicorn.run(
        "ai_arena_recap.web.app:app",
        host=host,
        port=port,
        reload=reload,
        log_config=log_config,
    )


@app.command("probe-replay")
def probe_replay_cmd(verbose: bool = typer.Option(False, "--verbose", "-v")):
    """Fetch a fresh signed replay URL for the most recent finished match and HEAD it."""
    _setup_logging(verbose)

    import httpx
    from sqlmodel import Session, select

    from ai_arena_recap.api_client import AiArenaClient
    from ai_arena_recap.db import engine
    from ai_arena_recap.models import Match

    async def _run() -> None:
        with Session(engine) as session:
            match = session.exec(
                select(Match).where(Match.result_created.is_not(None)).order_by(Match.result_created.desc())  # type: ignore[union-attr]
            ).first()
        if match is None:
            typer.echo("No finished matches in DB; run sync first.")
            raise typer.Exit(1)
        async with AiArenaClient() as client:
            data = await client.get_match(match.id)
        url = (data.get("result") or {}).get("replay_file")
        if not url:
            typer.echo("Match has no replay URL.")
            raise typer.Exit(1)
        typer.echo(f"Match {match.id} replay URL (truncated): {url[:120]}...")
        # Signed S3 URLs are method-locked; HEAD often 403s even when GET works. Use a tiny range GET.
        async with httpx.AsyncClient() as plain:
            r = await plain.get(url, headers={"Range": "bytes=0-15"})
        typer.echo(f"Unauthenticated GET (Range: bytes=0-15) status: {r.status_code}, bytes: {len(r.content)}")

    asyncio.run(_run())


if __name__ == "__main__":
    app()
