import asyncio
import logging

from sqlalchemy import update
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


async def repair_incomplete_participations(session: Session, client: AiArenaClient) -> set[int]:
    """Find matches where the Match row says the game is over but the participation
    rows are missing or incomplete, and refetch them. Two failure modes are covered:

    1. Race condition: aiarena fills Match.result_created a moment before the
       participation rows get their elo_change / avg_step_time / result fields.
    2. Partial sync: a transient API error during the round's batch fetch caused
       us to drop the participations entirely (zero rows for the match).

    MatchCancelled games are excluded: they are terminal but never produce
    per-bot results, so they can never satisfy the "complete" check below.
    Including them made repair refetch every cancelled match ever, every tick,
    forever (the candidate set has no time bound).
    """
    from sqlalchemy import func

    finished = set(session.exec(
        select(Match.id)
        .where(Match.result_created.is_not(None))
        .where(Match.result_type.is_distinct_from("MatchCancelled"))
    ).all())
    if not finished:
        return set()

    # A 1v1 match is "complete" iff it has at least 2 participations with a populated result.
    complete = set(session.exec(
        select(MatchParticipation.match_id)
        .where(MatchParticipation.result.is_not(None))
        .group_by(MatchParticipation.match_id)
        .having(func.count(MatchParticipation.id) >= 2)
    ).all())

    incomplete_match_ids = sorted(finished - complete)
    if not incomplete_match_ids:
        return set()
    log.info("Repairing %d matches with incomplete or missing participations", len(incomplete_match_ids))

    bot_ids: set[int] = set()
    results = await asyncio.gather(
        *[_fetch_participations(client, mid) for mid in incomplete_match_ids],
        return_exceptions=True,
    )
    for mid, items in zip(incomplete_match_ids, results, strict=True):
        if isinstance(items, BaseException):
            log.warning("Failed to fetch participations for match %s: %s", mid, items)
            continue
        for p in items:
            if isinstance(p.get("bot"), int):
                ensure_bot_stub(session, p["bot"])
                bot_ids.add(p["bot"])
            upsert(session, MatchParticipation, _participation_values(p))
    session.commit()
    return bot_ids


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
        api_complete = bool(r.get("complete"))
        # Upsert with complete=False even if the API says complete, so a
        # partial failure during match sync doesn't lock the round in a bad
        # state. We re-mark complete only after match sync succeeds.
        upsert(session, Round, {
            "id": r["id"],
            "number": r.get("number") or 0,
            "competition_id": r.get("competition") or competition_id,
            "started": parse_dt(r.get("started")),
            "finished": parse_dt(r.get("finished")),
            "complete": False,
            "last_synced": utcnow(),
        })
        session.commit()

        # Find matches in DB that are already finalized so we can skip them.
        already_final = set(
            session.exec(
                select(Match.id).where(Match.round_id == r["id"], Match.result_created.is_not(None))  # type: ignore[union-attr]
            ).all()
        )

        # Only fetch participations for matches that have *finished*. The match
        # sweep above already embeds each match's result, so an unfinished match
        # has result_created=None and its participation rows carry no result data
        # yet. Polling those every tick (they stay unfinished for hours) was the
        # dominant source of tiny API requests. A finished match gets
        # result_created set here, so next tick it lands in `already_final` and is
        # never refetched; the repair pass covers the brief result-lands-before-
        # participations race.
        finished_match_ids: list[int] = []
        async for m in client.list_matches_for_round(r["id"]):
            if m["id"] in already_final:
                continue
            values = _match_values(m)
            upsert(session, Match, values)
            if values["result_created"] is not None:
                finished_match_ids.append(m["id"])
        session.commit()

        # Fetch participations concurrently, then write sequentially (single Session).
        # return_exceptions=True so a single API hiccup doesn't erase the whole batch.
        if finished_match_ids:
            log.info("Round %s: fetching participations for %d newly-finished matches", r.get("number"), len(finished_match_ids))
            results = await asyncio.gather(
                *[_fetch_participations(client, mid) for mid in finished_match_ids],
                return_exceptions=True,
            )
            for mid, items in zip(finished_match_ids, results, strict=True):
                if isinstance(items, BaseException):
                    log.warning("Failed to fetch participations for match %s: %s", mid, items)
                    continue
                for p in items:
                    if isinstance(p.get("bot"), int):
                        ensure_bot_stub(session, p["bot"])
                        bot_ids.add(p["bot"])
                    upsert(session, MatchParticipation, _participation_values(p))
            session.commit()

        # Match sync for this round succeeded; if the API says the round is
        # complete, mark it so future syncs can skip it.
        if api_complete:
            session.exec(update(Round).where(Round.id == r["id"]).values(complete=True))
            session.commit()

    return bot_ids
