from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import case, func
from sqlalchemy.orm import aliased
from sqlmodel import Session, select

from ai_arena_recap.config import settings
from ai_arena_recap.models import Bot, CompetitionParticipation, Match, MatchParticipation
from ai_arena_recap.sync.common import utcnow
from ai_arena_recap.web.deps import get_session, render

router = APIRouter()

MATCHUP_WINDOW_DAYS = 30
MATCHUP_MIN_GAMES = 10


def _recent_matchups(session: Session, bot_id: int) -> list[dict]:
    """Per-opponent record over the last MATCHUP_WINDOW_DAYS, filtered to
    opponents we've played at least MATCHUP_MIN_GAMES times."""
    cutoff = utcnow() - timedelta(days=MATCHUP_WINDOW_DAYS)
    Opp = aliased(MatchParticipation)
    OppBot = aliased(Bot)

    half_cutoff = utcnow() - timedelta(days=MATCHUP_WINDOW_DAYS / 2)
    is_recent = Match.started >= half_cutoff

    matches = func.count(Match.id)
    wins = func.coalesce(func.sum(case((MatchParticipation.result == "win", 1), else_=0)), 0)
    losses = func.coalesce(func.sum(case((MatchParticipation.result == "loss", 1), else_=0)), 0)
    ties = func.coalesce(func.sum(case((MatchParticipation.result == "tie", 1), else_=0)), 0)
    avg_change = func.avg(MatchParticipation.elo_change)
    avg_steps = func.avg(Match.result_game_steps)
    # Split window into halves for a recency trend.
    recent_matches = func.coalesce(func.sum(case((is_recent, 1), else_=0)), 0)
    recent_wins = func.coalesce(
        func.sum(case((is_recent & (MatchParticipation.result == "win"), 1), else_=0)), 0
    )

    rows = session.exec(
        select(
            OppBot.id, OppBot.name, OppBot.plays_race,
            matches.label("matches"),
            wins.label("wins"),
            losses.label("losses"),
            ties.label("ties"),
            avg_change.label("avg_change"),
            avg_steps.label("avg_steps"),
            recent_matches.label("recent_matches"),
            recent_wins.label("recent_wins"),
        )
        .join(Match, Match.id == MatchParticipation.match_id)
        .join(Opp, (Opp.match_id == Match.id) & (Opp.bot_id != bot_id))
        .join(OppBot, OppBot.id == Opp.bot_id)
        .where(MatchParticipation.bot_id == bot_id)
        .where(Match.started >= cutoff)
        .where(MatchParticipation.result.in_(("win", "loss", "tie")))
        .group_by(OppBot.id, OppBot.name, OppBot.plays_race)
        .having(matches >= MATCHUP_MIN_GAMES)
        .order_by(matches.desc())
    ).all()

    matchups = []
    for r in rows:
        opp_id, opp_name, opp_race, m, w, l, t, ec, st, rm, rw = r
        recent_n, earlier_n = rm, m - rm
        # Trend: pp difference between latter-half and earlier-half win rates.
        # Undefined if either half has zero games.
        if recent_n > 0 and earlier_n > 0:
            recent_wr = rw / recent_n * 100
            earlier_wr = (w - rw) / earlier_n * 100
            trend_pp = recent_wr - earlier_wr
        else:
            trend_pp = None
        matchups.append({
            "opp_id": opp_id,
            "opp_name": opp_name,
            "opp_race": opp_race,
            "matches": m,
            "wins": w,
            "losses": l,
            "ties": t,
            "win_rate": ((w + t / 2) / m * 100) if m else 0.0,
            "avg_change": float(ec) if ec is not None else None,
            "avg_duration_s": (float(st) / 22.4) if st is not None else None,
            "trend_pp": trend_pp,
        })
    matchups.sort(key=lambda m: m["win_rate"], reverse=True)
    return matchups


@router.get("/bots/{bot_id}")
def bot_page(bot_id: int, request: Request, session: Session = Depends(get_session)):
    bot = session.get(Bot, bot_id)
    if bot is None:
        raise HTTPException(status_code=404, detail="Bot not found")
    cp = session.exec(
        select(CompetitionParticipation)
        .where(
            CompetitionParticipation.bot_id == bot_id,
            CompetitionParticipation.competition_id == settings.competition_id,
        )
    ).first()
    return render(
        request,
        "bot.html",
        bot=bot,
        cp=cp,
        matchup_window_days=MATCHUP_WINDOW_DAYS,
        matchup_min_games=MATCHUP_MIN_GAMES,
    )
