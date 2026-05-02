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

# K-factor for the per-race ELO simulation.
RACE_ELO_K = 16

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


def round_position_for_timestamp(session: Session, ts) -> float | None:
    """Map a datetime to the bot detail chart's x-axis (round number).
    Linearly interpolates between consecutive rounds' `started` times so the
    marker lands at the right place even mid-round. Returns None if the
    timestamp can't be placed (no rounds with `started` set, or ts predates
    the very first round in the competition)."""
    from datetime import timezone

    if ts is None:
        return None
    rows = session.exec(
        select(Round.number, Round.started)
        .where(Round.competition_id == settings.competition_id)
        .where(Round.started.is_not(None))  # type: ignore[union-attr]
        .order_by(Round.started)
    ).all()
    if not rows:
        return None

    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)

    def aware(t):
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)

    starts = [(int(n), aware(s)) for n, s in rows]
    if ts < starts[0][1]:
        return None  # bot was last updated before the competition started

    for (n0, t0), (n1, t1) in zip(starts, starts[1:]):
        if t0 <= ts < t1:
            span = (t1 - t0).total_seconds()
            if span <= 0:
                return float(n0)
            frac = (ts - t0).total_seconds() / span
            return n0 + frac * (n1 - n0)

    # Past the last round's start — pin to the last round (incomplete or just done).
    return float(starts[-1][0])


def bot_race_elo_history(session: Session, bot_id: int) -> list[dict]:
    """Per-round per-opponent-race ELO, computed by walking the bot's matches
    chronologically and applying the standard ELO update with K=RACE_ELO_K
    against the opponent's overall (race-agnostic) rating at match time.

    Per-race ratings start at the bot's pre-competition ELO (`starting_elo`
    on its first match in the competition) so the four traces align with the
    overall ELO trace at round 1. A snapshot of all "activated" race ratings
    is emitted at the end of each round the bot played in."""
    rows = session.exec(text("""
        SELECT r.number AS round_number, mp.result, opp_bot.plays_race AS race,
               opp_mp.starting_elo AS opp_elo, mp.starting_elo AS bot_starting_elo
        FROM match_participation mp
        JOIN match m ON m.id = mp.match_id
        JOIN round r ON r.id = m.round_id
        JOIN match_participation opp_mp
          ON opp_mp.match_id = m.id AND opp_mp.bot_id != mp.bot_id
        JOIN bot opp_bot ON opp_bot.id = opp_mp.bot_id
        WHERE mp.bot_id = :bot_id
          AND r.competition_id = :competition_id
          AND mp.result IN ('win', 'loss', 'tie')
          AND opp_mp.starting_elo IS NOT NULL
          AND opp_bot.plays_race IS NOT NULL
        ORDER BY r.number, m.started, m.id
    """), params={"competition_id": settings.competition_id, "bot_id": bot_id}).all()

    if not rows:
        return []

    initial = next((r[4] for r in rows if r[4] is not None), 1600.0)
    ratings = {race: float(initial) for race in ("T", "Z", "P", "R")}
    seen_races: set[str] = set()
    out: list[dict] = []
    last_round: int | None = None

    def snapshot(round_number: int) -> None:
        for race in seen_races:
            out.append({
                "round_number": int(round_number),
                "race": race,
                "rating": round(ratings[race], 1),
            })

    for round_number, result, race, opp_elo, _bot_se in rows:
        if last_round is not None and round_number != last_round:
            snapshot(last_round)
        score = 1.0 if result == "win" else 0.5 if result == "tie" else 0.0
        expected = 1.0 / (1.0 + 10 ** ((float(opp_elo) - ratings[race]) / 400))
        ratings[race] += RACE_ELO_K * (score - expected)
        seen_races.add(race)
        last_round = int(round_number)

    if last_round is not None:
        snapshot(last_round)
    return out
