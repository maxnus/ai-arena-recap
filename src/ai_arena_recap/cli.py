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
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Run an incremental sync against aiarena.net."""
    _setup_logging(verbose)
    from ai_arena_recap.db import init_db
    from ai_arena_recap.sync.runner import sync_all

    init_db()
    asyncio.run(sync_all(max_rounds=max_rounds, force_bots=force_bots))


@app.command("sync-replays")
def sync_replays_cmd(verbose: bool = typer.Option(False, "--verbose", "-v")):
    """Download replays for recent matches and clean up old ones."""
    _setup_logging(verbose)
    from ai_arena_recap.db import init_db
    from ai_arena_recap.sync.replays import sync_replays

    init_db()
    asyncio.run(sync_replays())


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
