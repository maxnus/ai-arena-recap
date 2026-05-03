from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import aliased
from sqlmodel import Session, func, select

from ai_arena_recap.config import settings
from ai_arena_recap.models import Bot, CompetitionParticipation, Map, Match, MatchParticipation, Round
from ai_arena_recap.sync.common import utcnow
from ai_arena_recap.web.deps import get_session
from ai_arena_recap.web.queries import (
    MATCHUP_MIN_GAMES,
    MATCHUP_WINDOW_DAYS,
    bot_race_elo_history,
    bot_rank_history,
    recent_matchups,
)

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
            "author": bot.user_name,
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
        select(MatchParticipation, Match, Opp, OppBot, Map, Round)
        .join(Match, Match.id == MatchParticipation.match_id)
        .outerjoin(Opp, (Opp.match_id == Match.id) & (Opp.bot_id != bot_id))
        .outerjoin(OppBot, OppBot.id == Opp.bot_id)
        .outerjoin(Map, Map.id == Match.map_id)
        .outerjoin(Round, Round.id == Match.round_id)
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
    for mp, match, opp_mp, opp_bot, mp_map, round_row in rows:
        data.append({
            "match_id": match.id,
            "round_number": round_row.number if round_row else None,
            "started": match.started.isoformat() if match.started else None,
            "ended": match.result_created.isoformat() if match.result_created else None,
            "game_steps": match.result_game_steps,
            "map": mp_map.name if mp_map else None,
            "opponent_id": opp_bot.id if opp_bot else None,
            "opponent_name": opp_bot.name if opp_bot else None,
            "opponent_race": opp_bot.plays_race if opp_bot else None,
            "result": mp.result,
            "result_cause": mp.result_cause,
            "result_type": match.result_type,
            "starting_elo": mp.starting_elo,
            "resultant_elo": mp.resultant_elo,
            "elo_change": mp.elo_change,
            "avg_step_time": mp.avg_step_time,
        })

    return {"data": data, "last_page": last_page, "total": total}


@router.get("/matches/{match_id}/recent-vs.json")
def match_recent_vs_json(
    match_id: int,
    days: int = Query(30, ge=1, le=365),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """All matches between the same two bots in the last `days` days, newest first."""
    if session.get(Match, match_id) is None:
        raise HTTPException(status_code=404, detail="Match not found")

    bot_ids = list(session.exec(
        select(MatchParticipation.bot_id).where(MatchParticipation.match_id == match_id)
    ).all())
    if len(bot_ids) != 2:
        return {"data": [], "days": days}

    cutoff = utcnow() - timedelta(days=days)

    # Match IDs that have both of these bots as participants.
    head_to_head_match_ids = session.exec(
        select(MatchParticipation.match_id)
        .where(MatchParticipation.bot_id.in_(bot_ids))
        .group_by(MatchParticipation.match_id)
        .having(func.count(func.distinct(MatchParticipation.bot_id)) == 2)
    ).all()

    rows = session.exec(
        select(Match, Map)
        .outerjoin(Map, Map.id == Match.map_id)
        .where(Match.id.in_(head_to_head_match_ids))
        .where(Match.started >= cutoff)
        .order_by(Match.started.desc())
    ).all()

    bot_name_by_id = {
        b.id: b.name for b in session.exec(select(Bot).where(Bot.id.in_(bot_ids))).all()
    }

    data = []
    for m, mp in rows:
        winner_name = bot_name_by_id.get(m.result_winner_bot_id) if m.result_winner_bot_id else None
        data.append({
            "match_id": m.id,
            "started": m.started.isoformat() if m.started else None,
            "ended": m.result_created.isoformat() if m.result_created else None,
            "game_steps": m.result_game_steps,
            "map": mp.name if mp else None,
            "result_type": m.result_type,
            "winner_name": winner_name,
        })

    return {
        "data": data,
        "days": days,
        "bot_ids": bot_ids,
        "bot_names": [bot_name_by_id.get(b) for b in bot_ids],
    }


@router.get("/bots/{bot_id}/matchups.json")
def bot_matchups_json(bot_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    if session.get(Bot, bot_id) is None:
        raise HTTPException(status_code=404, detail="Bot not found")
    return {
        "data": recent_matchups(session, bot_id),
        "window_days": MATCHUP_WINDOW_DAYS,
        "min_games": MATCHUP_MIN_GAMES,
    }


@router.get("/bots/{bot_id}/rank-history.json")
def bot_rank_history_json(bot_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    """For each round in the current competition, return the bot's rank
    (1 = best) based on mean resultant_elo across that round's matches."""
    if session.get(Bot, bot_id) is None:
        raise HTTPException(status_code=404, detail="Bot not found")
    return {"data": bot_rank_history(session, bot_id)}


@router.get("/bots/{bot_id}/race-elo-history.json")
def bot_race_elo_history_json(bot_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    """Per-round per-opponent-race ELO, simulated forward with K=16 against
    the opponent's overall ELO."""
    if session.get(Bot, bot_id) is None:
        raise HTTPException(status_code=404, detail="Bot not found")
    return {"data": bot_race_elo_history(session, bot_id)}
