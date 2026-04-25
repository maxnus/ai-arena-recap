import asyncio
import logging

from sqlmodel import Session, select

from ai_arena_recap.api_client import AiArenaClient
from ai_arena_recap.models import Match, MatchParticipation, Round
from ai_arena_recap.sync.common import ensure_bot_stub, parse_dt, upsert, utcnow

log = logging.getLogger(__name__)


def _match_values(data: dict) -> dict:
    result = data.get("result") or {}
    map_id = data.get("map") if isinstance(data.get("map"), int) else None
    return {
        "id": data["id"],
        "round_id": data.get("round"),
        "map_id": map_id,
        "created": parse_dt(data.get("created")),
        "started": parse_dt(data.get("started")),
        "result_type": result.get("type"),
        "result_winner_bot_id": result.get("winner") if isinstance(result.get("winner"), int) else None,
        "result_created": parse_dt(result.get("created")),
        "result_game_steps": result.get("game_steps"),
        "bot1_name": result.get("bot1_name"),
        "bot2_name": result.get("bot2_name"),
        "last_synced": utcnow(),
    }


def _participation_values(data: dict) -> dict:
    return {
        "id": data["id"],
        "match_id": data["match"],
        "bot_id": data["bot"],
        "participant_number": data.get("participant_number") or 0,
        "starting_elo": data.get("starting_elo"),
        "resultant_elo": data.get("resultant_elo"),
        "elo_change": data.get("elo_change"),
        "avg_step_time": data.get("avg_step_time"),
        "result": data.get("result"),
        "result_cause": data.get("result_cause"),
        "last_synced": utcnow(),
    }


async def _fetch_participations(client: AiArenaClient, match_id: int) -> list[dict]:
    items: list[dict] = []
    async for p in client.list_match_participations(match_id):
        items.append(p)
    return items


async def sync_rounds_and_matches(
    session: Session,
    client: AiArenaClient,
    competition_id: int,
    *,
    max_rounds: int | None = None,
) -> set[int]:
    """Sync rounds for a competition.

    Skips rounds locally marked complete=True.
    Within an open round, skips matches that already have result_created set.
    Returns set of bot ids referenced.
    """
    bot_ids: set[int] = set()

    rounds_data: list[dict] = []
    async for r in client.list_rounds(competition_id):
        rounds_data.append(r)

    rounds_data.sort(key=lambda r: r.get("number") or 0, reverse=True)
    if max_rounds is not None:
        rounds_data = rounds_data[:max_rounds]

    for r in rounds_data:
        local = session.get(Round, r["id"])
        if local is not None and local.complete:
            continue
        upsert(session, Round, {
            "id": r["id"],
            "number": r.get("number") or 0,
            "competition_id": r.get("competition") or competition_id,
            "started": parse_dt(r.get("started")),
            "finished": parse_dt(r.get("finished")),
            "complete": bool(r.get("complete")),
            "last_synced": utcnow(),
        })
        session.commit()

        # Find matches in DB that are already finalized so we can skip them.
        already_final = set(
            session.exec(
                select(Match.id).where(Match.round_id == r["id"], Match.result_created.is_not(None))  # type: ignore[union-attr]
            ).all()
        )

        new_match_ids: list[int] = []
        async for m in client.list_matches_for_round(r["id"]):
            if m["id"] in already_final:
                continue
            upsert(session, Match, _match_values(m))
            new_match_ids.append(m["id"])
        session.commit()

        # Fetch participations concurrently, then write sequentially (single Session).
        if new_match_ids:
            log.info("Round %s: fetching participations for %d matches", r.get("number"), len(new_match_ids))
            results = await asyncio.gather(
                *[_fetch_participations(client, mid) for mid in new_match_ids]
            )
            for items in results:
                for p in items:
                    if isinstance(p.get("bot"), int):
                        ensure_bot_stub(session, p["bot"])
                        bot_ids.add(p["bot"])
                    upsert(session, MatchParticipation, _participation_values(p))
            session.commit()

    return bot_ids
