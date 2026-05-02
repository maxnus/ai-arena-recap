"""Reusable read queries shared by the page routes and the JSON API.

All queries here read from the local DB only — see AGENTS.md.
"""
from collections import defaultdict
from datetime import timedelta, timezone

from sqlalchemy import case, func, text
from sqlalchemy.orm import aliased
from sqlmodel import Session, select

from ai_arena_recap.config import settings
from ai_arena_recap.models import Bot, Match, MatchParticipation, Round
from ai_arena_recap.sync.common import utcnow

# Per-opponent matchup window.
MATCHUP_WINDOW_DAYS = 60
MATCHUP_MIN_GAMES = 10

_RESULTS = ("win", "loss", "tie")


def _wlt_aggregates():
    """Returns (matches, wins, losses, ties) SQL aggregate expressions
    over MatchParticipation, with COALESCE so empty groups stay zero."""
    matches = func.count(Match.id)
    wins = func.coalesce(func.sum(case((MatchParticipation.result == "win", 1), else_=0)), 0)
    losses = func.coalesce(func.sum(case((MatchParticipation.result == "loss", 1), else_=0)), 0)
    ties = func.coalesce(func.sum(case((MatchParticipation.result == "tie", 1), else_=0)), 0)
    return matches, wins, losses, ties


def _win_rate(wins: int, ties: int, matches: int) -> float | None:
    """Ties count as half a win."""
    if not matches:
        return None
    return (wins + ties / 2) / matches * 100


def winrate_by_race(session: Session, bot_id: int) -> dict[str, dict]:
    """Per-opponent-race W/L/T totals across the current competition."""
    Opp = aliased(MatchParticipation)
    OppBot = aliased(Bot)
    matches, wins, losses, ties = _wlt_aggregates()

    rows = session.exec(
        select(
            OppBot.plays_race,
            matches.label("matches"),
            wins.label("wins"),
            losses.label("losses"),
            ties.label("ties"),
        )
        .join(Match, Match.id == MatchParticipation.match_id)
        .join(Round, Round.id == Match.round_id)
        .join(Opp, (Opp.match_id == Match.id) & (Opp.bot_id != bot_id))
        .join(OppBot, OppBot.id == Opp.bot_id)
        .where(MatchParticipation.bot_id == bot_id)
        .where(Round.competition_id == settings.competition_id)
        .where(MatchParticipation.result.in_(_RESULTS))
        .group_by(OppBot.plays_race)
    ).all()

    return {
        race: {
            "matches": m,
            "wins": w,
            "losses": l,
            "ties": t,
            "win_rate": _win_rate(w, t, m),
        }
        for race, m, w, l, t in rows
    }


def recent_matchups(session: Session, bot_id: int) -> list[dict]:
    """Per-opponent record over the last MATCHUP_WINDOW_DAYS, filtered to
    opponents we've played at least MATCHUP_MIN_GAMES times."""
    cutoff = utcnow() - timedelta(days=MATCHUP_WINDOW_DAYS)
    Opp = aliased(MatchParticipation)
    OppBot = aliased(Bot)

    half_cutoff = utcnow() - timedelta(days=MATCHUP_WINDOW_DAYS / 2)
    is_recent = Match.started >= half_cutoff

    matches, wins, losses, ties = _wlt_aggregates()
    avg_change = func.avg(MatchParticipation.elo_change)
    avg_steps = func.avg(Match.result_game_steps)
    var_steps = (
        func.avg(Match.result_game_steps * Match.result_game_steps)
        - func.avg(Match.result_game_steps) * func.avg(Match.result_game_steps)
    )
    # Split the window into halves for a simple recency trend.
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
            var_steps.label("var_steps"),
            recent_matches.label("recent_matches"),
            recent_wins.label("recent_wins"),
        )
        .join(Match, Match.id == MatchParticipation.match_id)
        .join(Opp, (Opp.match_id == Match.id) & (Opp.bot_id != bot_id))
        .join(OppBot, OppBot.id == Opp.bot_id)
        .where(MatchParticipation.bot_id == bot_id)
        .where(Match.started >= cutoff)
        .where(MatchParticipation.result.in_(_RESULTS))
        .group_by(OppBot.id, OppBot.name, OppBot.plays_race)
        .having(matches >= MATCHUP_MIN_GAMES)
        .order_by(matches.desc())
    ).all()

    matchups = []
    for r in rows:
        opp_id, opp_name, opp_race, m, w, l, t, ec, st, vs, rm, rw = r
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
            "win_rate": _win_rate(w, t, m) or 0.0,
            "avg_change": float(ec) if ec is not None else None,
            "avg_duration_s": (float(st) / 22.4) if st is not None else None,
            "std_duration_s": (float(vs) ** 0.5 / 22.4) if vs is not None and vs > 0 else None,
            "trend_pp": trend_pp,
        })
    if not matchups:
        return matchups

    opp_ids = [m["opp_id"] for m in matchups]
    Opp2 = aliased(MatchParticipation)
    timeline_rows = session.exec(
        select(Opp2.bot_id, MatchParticipation.result, Match.started, MatchParticipation.elo_change)
        .join(Match, Match.id == MatchParticipation.match_id)
        .join(Opp2, (Opp2.match_id == Match.id) & (Opp2.bot_id != bot_id))
        .where(MatchParticipation.bot_id == bot_id)
        .where(Match.started >= cutoff)
        .where(MatchParticipation.result.in_(_RESULTS))
        .where(Opp2.bot_id.in_(opp_ids))
        .order_by(Match.started)
    ).all()

    abbrev = {"win": "w", "loss": "l", "tie": "t"}
    window_seconds = MATCHUP_WINDOW_DAYS * 86400
    per_opp: dict[int, list] = defaultdict(list)
    for opp_id, result, started, elo_chg in timeline_rows:
        started_aware = started if started.tzinfo else started.replace(tzinfo=timezone.utc)
        age = (utcnow() - started_aware).total_seconds()
        t = round(1 - age / window_seconds, 4)
        per_opp[opp_id].append([t, abbrev.get(result, result), elo_chg])

    for m in matchups:
        m["history"] = per_opp.get(m["opp_id"], [])

    matchups.sort(key=lambda m: m["win_rate"], reverse=True)
    return matchups


def bot_rank_history(session: Session, bot_id: int) -> list[dict]:
    """Per-round rank (1 = best) for the bot across the current competition,
    computed from mean resultant_elo within each round."""
    rows = session.exec(text("""
        WITH per_round_bot AS (
            SELECT m.round_id, mp.bot_id, AVG(mp.resultant_elo) AS mean_elo
            FROM match_participation mp
            JOIN match m ON m.id = mp.match_id
            JOIN round r ON r.id = m.round_id
            WHERE mp.resultant_elo IS NOT NULL
              AND r.competition_id = :competition_id
            GROUP BY m.round_id, mp.bot_id
        ),
        ranked AS (
            SELECT round_id, bot_id, mean_elo,
                   RANK() OVER (PARTITION BY round_id ORDER BY mean_elo DESC) AS rk
            FROM per_round_bot
        )
        SELECT r.number AS round_number, ranked.rk AS rank, ranked.mean_elo AS mean_elo
        FROM ranked
        JOIN round r ON r.id = ranked.round_id
        WHERE ranked.bot_id = :bot_id
        ORDER BY r.number
    """), params={"competition_id": settings.competition_id, "bot_id": bot_id}).all()

    return [
        {"round_number": int(r[0]), "rank": int(r[1]), "mean_elo": float(r[2])}
        for r in rows
    ]
