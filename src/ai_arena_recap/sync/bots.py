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
        "bot_data_enabled": data.get("bot_data_enabled"),
        "bot_zip_updated": parse_dt(data.get("bot_zip_updated")),
        "last_synced": utcnow(),
    }


async def sync_bots(session: Session, client: AiArenaClient, bot_ids: set[int], *, force: bool = False) -> None:
    """Fetch each bot in `bot_ids`, skipping those synced within bot_refresh_seconds unless force=True.
    Also fetches the corresponding user records so we can populate Bot.user_name (the author)."""
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

    bot_data = await asyncio.gather(*[_one(b) for b in bot_ids])
    bot_data = [d for d in bot_data if d is not None]

    # Fetch user (author) info for each unique user_id referenced by these bots.
    user_ids = {d["user"] for d in bot_data if isinstance(d.get("user"), int)}

    async def _user(uid: int) -> tuple[int, str | None]:
        try:
            u = await client.get_user(uid)
            return uid, u.get("username")
        except Exception as exc:  # noqa: BLE001
            log.warning("Failed to fetch user %s: %s", uid, exc)
            return uid, None

    user_results = await asyncio.gather(*[_user(uid) for uid in user_ids])
    user_names: dict[int, str | None] = dict(user_results)

    for data in bot_data:
        values = _bot_values(data)
        uid = values.get("user_id")
        if uid is not None:
            values["user_name"] = user_names.get(uid)
        upsert(session, Bot, values)
    session.commit()
