from sqlmodel import Session

from ai_arena_recap.api_client import AiArenaClient
from ai_arena_recap.models import Competition, CompetitionParticipation
from ai_arena_recap.sync.common import ensure_bot_stub, parse_dt, upsert, utcnow


async def sync_competition(session: Session, client: AiArenaClient, competition_id: int) -> None:
    data = await client.get_competition(competition_id)
    upsert(session, Competition, {
        "id": data["id"],
        "name": data.get("name") or "",
        "status": data.get("status"),
        "date_opened": parse_dt(data.get("date_opened")),
        "date_closed": parse_dt(data.get("date_closed")),
        "last_synced": utcnow(),
    })
    session.commit()


async def sync_participations(session: Session, client: AiArenaClient, competition_id: int) -> set[int]:
    """Returns the set of bot ids referenced by participations."""
    bot_ids: set[int] = set()
    now = utcnow()
    async for p in client.list_competition_participations(competition_id):
        ensure_bot_stub(session, p["bot"])
        upsert(session, CompetitionParticipation, {
            "id": p["id"],
            "competition_id": p["competition"],
            "bot_id": p["bot"],
            "elo": p.get("elo"),
            "highest_elo": p.get("highest_elo"),
            "division_num": p.get("division_num"),
            "in_placements": bool(p.get("in_placements")),
            "active": bool(p.get("active")),
            "match_count": p.get("match_count") or 0,
            "win_count": p.get("win_count") or 0,
            "loss_count": p.get("loss_count") or 0,
            "tie_count": p.get("tie_count") or 0,
            "crash_count": p.get("crash_count") or 0,
            "win_perc": p.get("win_perc"),
            "loss_perc": p.get("loss_perc"),
            "tie_perc": p.get("tie_perc"),
            "crash_perc": p.get("crash_perc"),
            "slug": p.get("slug"),
            "last_synced": now,
        })
        bot_ids.add(p["bot"])
    session.commit()
    return bot_ids
