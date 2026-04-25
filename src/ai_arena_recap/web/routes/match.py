import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlmodel import Session, select

from ai_arena_recap.api_client import AiArenaClient
from ai_arena_recap.models import Bot, Map, Match, MatchParticipation
from ai_arena_recap.web.deps import get_session, render

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/matches/{match_id}")
def match_page(match_id: int, request: Request, session: Session = Depends(get_session)):
    match = session.get(Match, match_id)
    if match is None:
        raise HTTPException(status_code=404, detail="Match not found")

    parts = session.exec(
        select(MatchParticipation, Bot)
        .join(Bot, Bot.id == MatchParticipation.bot_id)
        .where(MatchParticipation.match_id == match_id)
        .order_by(MatchParticipation.participant_number.asc())
    ).all()

    map_obj = session.get(Map, match.map_id) if match.map_id else None

    return render(
        request,
        "match.html",
        match=match,
        participations=parts,
        map=map_obj,
    )


@router.get("/matches/{match_id}/replay")
async def match_replay(match_id: int, session: Session = Depends(get_session)):
    """Fetch a fresh signed replay URL from aiarena (URLs expire in 1h) and redirect."""
    match = session.get(Match, match_id)
    if match is None:
        raise HTTPException(status_code=404, detail="Match not found")
    try:
        async with AiArenaClient() as client:
            data = await client.get_match(match_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to fetch fresh replay URL for match %s: %s", match_id, exc)
        raise HTTPException(
            status_code=503,
            detail="aiarena.net is unreachable; cannot generate a replay download link right now.",
        ) from exc
    result = data.get("result") or {}
    url = result.get("replay_file")
    if not url:
        raise HTTPException(status_code=404, detail="No replay available for this match")
    return RedirectResponse(url, status_code=302)
