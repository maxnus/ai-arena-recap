import asyncio
import logging
from datetime import timedelta

from sqlmodel import Session, select

from ai_arena_recap.api_client import AiArenaClient
from ai_arena_recap.config import settings
from ai_arena_recap.models import Bot
from ai_arena_recap.sync.common import parse_dt, upsert, utcnow

log = logging.getLogger(__name__)


def _bot_values(data: dict) -> dict:
    plays_race = data.get("plays_race")
    race_label: str | None = None
    if isinstance(plays_race, dict):
        race_label = plays_race.get("label")
    elif isinstance(plays_race, str):
        race_label = plays_race
    return {
        "id": data["id"],
        "name": data.get("name") or "",
        "user_id": data.get("user") if isinstance(data.get("user"), int) else None,
        "user_name": None,
        "plays_race": race_label,
        "type": data.get("type"),
        "created": parse_dt(data.get("created")),
        "game_display_id": data.get("game_display_id"),
        "wiki_article_content": data.get("wiki_article_content"),
        "last_synced": utcnow(),
    }


async def sync_bots(session: Session, client: AiArenaClient, bot_ids: set[int], *, force: bool = False) -> None:
    """Fetch each bot in `bot_ids`, skipping those synced within bot_refresh_seconds unless force=True."""
    if not bot_ids:
        return
    cutoff = utcnow() - timedelta(seconds=settings.bot_refresh_seconds)
    if not force:
        existing = session.exec(
            select(Bot.id).where(Bot.id.in_(bot_ids), Bot.last_synced > cutoff)  # type: ignore[attr-defined]
        ).all()
        fresh = set(existing)
        bot_ids = bot_ids - fresh
    if not bot_ids:
        return

    log.info("Syncing %d bots", len(bot_ids))

    async def _one(bid: int) -> dict | None:
        try:
            return await client.get_bot(bid)
        except Exception as exc:  # noqa: BLE001
            log.warning("Failed to fetch bot %s: %s", bid, exc)
            return None

    results = await asyncio.gather(*[_one(b) for b in bot_ids])
    for data in results:
        if data is None:
            continue
        upsert(session, Bot, _bot_values(data))
    session.commit()
