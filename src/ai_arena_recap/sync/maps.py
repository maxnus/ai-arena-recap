from sqlmodel import Session

from ai_arena_recap.api_client import AiArenaClient
from ai_arena_recap.models import Map
from ai_arena_recap.sync.common import upsert, utcnow


async def sync_maps(session: Session, client: AiArenaClient) -> None:
    now = utcnow()
    async for m in client.list_maps():
        upsert(session, Map, {
            "id": m["id"],
            "name": m.get("name") or f"map-{m['id']}",
            "enabled": bool(m.get("enabled", True)),
            "last_synced": now,
        })
    session.commit()
