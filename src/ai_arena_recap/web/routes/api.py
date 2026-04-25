from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import aliased
from sqlmodel import Session, func, select

from ai_arena_recap.config import settings
from ai_arena_recap.models import Bot, CompetitionParticipation, Map, Match, MatchParticipation
from ai_arena_recap.web.deps import get_session

router = APIRouter(prefix="/api")


@router.get("/ladder.json")
def ladder_json(session: Session = Depends(get_session)) -> dict[str, Any]:
    rows = session.exec(
        select(CompetitionParticipation, Bot)
        .join(Bot, CompetitionParticipation.bot_id == Bot.id)
        .where(CompetitionParticipation.competition_id == settings.competition_id)
        .where(CompetitionParticipation.active == True)  # noqa: E712
        .order_by(
            CompetitionParticipation.division_num.asc().nullslast(),
            CompetitionParticipation.elo.desc().nullslast(),
        )
    ).all()
    data = []
    for i, (cp, bot) in enumerate(rows, start=1):
        data.append({
            "rank": i,
            "bot_id": bot.id,
            "name": bot.name,
            "race": bot.plays_race,
            "type": bot.type,
            "division": cp.division_num,
            "elo": cp.elo,
            "highest_elo": cp.highest_elo,
            "match_count": cp.match_count,
            "win_count": cp.win_count,
            "loss_count": cp.loss_count,
            "tie_count": cp.tie_count,
            "crash_count": cp.crash_count,
            "win_perc": round(cp.win_perc, 2) if cp.win_perc is not None else None,
        })
    return {"data": data}


@router.get("/bots/{bot_id}/matches.json")
def bot_matches_json(
    bot_id: int,
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=10000),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    if session.get(Bot, bot_id) is None:
        raise HTTPException(status_code=404, detail="Bot not found")

    Opp = aliased(MatchParticipation)
    OppBot = aliased(Bot)

    base = (
        select(MatchParticipation, Match, Opp, OppBot, Map)
        .join(Match, Match.id == MatchParticipation.match_id)
        .outerjoin(Opp, (Opp.match_id == Match.id) & (Opp.bot_id != bot_id))
        .outerjoin(OppBot, OppBot.id == Opp.bot_id)
        .outerjoin(Map, Map.id == Match.map_id)
        .where(MatchParticipation.bot_id == bot_id)
    )

    total = session.exec(
        select(func.count()).select_from(MatchParticipation).where(MatchParticipation.bot_id == bot_id)
    ).one()
    last_page = max(1, (total + size - 1) // size)

    rows = session.exec(
        base.order_by(Match.started.desc().nullslast(), Match.id.desc())
        .limit(size)
        .offset((page - 1) * size)
    ).all()

    data = []
    for mp, match, opp_mp, opp_bot, mp_map in rows:
        data.append({
            "match_id": match.id,
            "started": match.started.isoformat() if match.started else None,
            "ended": match.result_created.isoformat() if match.result_created else None,
            "game_steps": match.result_game_steps,
            "map": mp_map.name if mp_map else None,
            "opponent_id": opp_bot.id if opp_bot else None,
            "opponent_name": opp_bot.name if opp_bot else None,
            "opponent_race": opp_bot.plays_race if opp_bot else None,
            "result": mp.result,
            "result_cause": mp.result_cause,
            "starting_elo": mp.starting_elo,
            "resultant_elo": mp.resultant_elo,
            "elo_change": mp.elo_change,
            "avg_step_time": mp.avg_step_time,
        })

    return {"data": data, "last_page": last_page, "total": total}
