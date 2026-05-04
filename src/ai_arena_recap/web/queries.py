"""Reusable read queries shared by the page routes and the JSON API.

All queries here read from the local DB only — see AGENTS.md.
"""
from collections import defaultdict
from datetime import timedelta, timezone

from sqlalchemy import case, func, text
from sqlalchemy.orm import aliased
from sqlmodel import Session, select

from ai_arena_recap.config import settings
from ai_arena_recap.models import Bot, CompetitionParticipation, Match, MatchParticipation, Round
from ai_arena_recap.sync.common import utcnow

# Per-opponent matchup window.
MATCHUP_WINDOW_DAYS = 60
MATCHUP_MIN_GAMES = 10

# K-factor for the per-race ELO simulation.
RACE_ELO_K = 16

# StarCraft 2 game-loop rate: 22.4 ticks per real second on the standard speed
# the ladder runs at.
STEPS_PER_SECOND = 22.4

# Result values that count as a played game (excludes errors / crashes).
WLT_RESULTS = ("win", "loss", "tie")


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
        .where(MatchParticipation.result.in_(WLT_RESULTS))
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


def recent_matchups(
    session: Session,
    bot_id: int,
    window_days: int = MATCHUP_WINDOW_DAYS,
    min_games: int = MATCHUP_MIN_GAMES,
) -> list[dict]:
    """Per-opponent record over the last `window_days`, filtered to
    opponents we've played at least `min_games` times."""
    cutoff = utcnow() - timedelta(days=window_days)
    Opp = aliased(MatchParticipation)
    OppBot = aliased(Bot)

    half_cutoff = utcnow() - timedelta(days=window_days / 2)
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
        .where(MatchParticipation.result.in_(WLT_RESULTS))
        .group_by(OppBot.id, OppBot.name, OppBot.plays_race)
        .having(matches >= min_games)
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
            "avg_duration_s": (float(st) / STEPS_PER_SECOND) if st is not None else None,
            "std_duration_s": (float(vs) ** 0.5 / STEPS_PER_SECOND) if vs is not None and vs > 0 else None,
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
        .where(MatchParticipation.result.in_(WLT_RESULTS))
        .where(Opp2.bot_id.in_(opp_ids))
        .order_by(Match.started)
    ).all()

    abbrev = {"win": "w", "loss": "l", "tie": "t"}
    window_seconds = window_days * 86400
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


def bot_current_rank(session: Session, bot_id: int) -> int | None:
    """Bot's position on the current active ladder, ordered the same way as
    the ladder page (division asc, then ELO desc). Bots with division_num 0
    or NULL are awaiting placement and excluded from the ranking — matches
    the split done by /api/ladder.json. Returns None if the bot isn't an
    active, placed participant of the current competition."""
    rows = session.exec(
        select(CompetitionParticipation.bot_id)
        .where(CompetitionParticipation.competition_id == settings.competition_id)
        .where(CompetitionParticipation.active == True)  # noqa: E712
        .where(CompetitionParticipation.division_num > 0)
        .order_by(
            CompetitionParticipation.division_num.asc(),
            CompetitionParticipation.elo.desc().nullslast(),
        )
    ).all()
    for i, bid in enumerate(rows, start=1):
        if bid == bot_id:
            return i
    return None


def bot_rank_history(session: Session, bot_id: int) -> list[dict]:
    """Per-round end-of-round rank (1 = best) and end-of-round ELO across the
    current competition. End-of-round ELO is the resultant_elo of the bot's
    latest match in that round (by Match.started). Only completed rounds are
    returned — partial / in-progress rounds are excluded entirely."""
    rows = session.exec(text("""
        WITH ranked_matches AS (
            SELECT m.round_id, mp.bot_id, mp.resultant_elo,
                   ROW_NUMBER() OVER (
                       PARTITION BY m.round_id, mp.bot_id
                       ORDER BY m.started DESC, m.id DESC
                   ) AS rn
            FROM match_participation mp
            JOIN match m ON m.id = mp.match_id
            JOIN round r ON r.id = m.round_id
            WHERE mp.resultant_elo IS NOT NULL
              AND r.competition_id = :competition_id
              AND r.complete = 1
        ),
        per_round_bot AS (
            SELECT round_id, bot_id, resultant_elo AS end_elo
            FROM ranked_matches
            WHERE rn = 1
        ),
        ranked AS (
            SELECT round_id, bot_id, end_elo,
                   RANK() OVER (PARTITION BY round_id ORDER BY end_elo DESC) AS rk
            FROM per_round_bot
        )
        SELECT r.number AS round_number, ranked.rk AS rank, ranked.end_elo AS end_elo
        FROM ranked
        JOIN round r ON r.id = ranked.round_id
        WHERE ranked.bot_id = :bot_id
        ORDER BY r.number
    """), params={"competition_id": settings.competition_id, "bot_id": bot_id}).all()

    return [
        {"round_number": int(r[0]), "rank": int(r[1]), "end_elo": float(r[2])}
        for r in rows
    ]


def bot_avg_match_stats(session: Session, bot_id: int) -> dict:
    """Average game duration (seconds) and average per-step time (seconds)
    across the bot's matches in the current competition. Only counts matches
    with a recorded result (win/loss/tie) to exclude errors and crashes that
    skew the duration / step time."""
    row = session.exec(
        select(
            func.avg(Match.result_game_steps),
            func.avg(MatchParticipation.avg_step_time),
        )
        .join(Match, Match.id == MatchParticipation.match_id)
        .join(Round, Round.id == Match.round_id)
        .where(MatchParticipation.bot_id == bot_id)
        .where(Round.competition_id == settings.competition_id)
        .where(MatchParticipation.result.in_(WLT_RESULTS))
    ).one()
    avg_steps, avg_step_time = row
    return {
        "avg_duration_s": (float(avg_steps) / STEPS_PER_SECOND) if avg_steps is not None else None,
        "avg_step_time_s": float(avg_step_time) if avg_step_time is not None else None,
    }


def competition_round_ends(session: Session) -> dict[int, str]:
    """Map round_number -> ISO date string ("YYYY-MM-DD") of the round's
    end (Round.finished). Only completed rounds are returned. Used to label
    the bot detail chart's x-axis with the end date of each round."""
    rows = session.exec(
        select(Round.number, Round.finished)
        .where(Round.competition_id == settings.competition_id)
        .where(Round.complete == True)  # noqa: E712
        .where(Round.finished.is_not(None))  # type: ignore[union-attr]
        .order_by(Round.number)
    ).all()
    return {int(n): f.strftime("%Y-%m-%d") for n, f in rows}


def round_position_for_timestamp(session: Session, ts) -> float | None:
    """Map a datetime to the bot detail chart's x-axis (round number).
    Linearly interpolates between consecutive rounds' `finished` times so
    the marker lands at the right place across rounds. Returns None if the
    timestamp can't be placed (no completed rounds, or ts predates the end
    of the very first round in the competition)."""
    if ts is None:
        return None
    rows = session.exec(
        select(Round.number, Round.finished)
        .where(Round.competition_id == settings.competition_id)
        .where(Round.complete == True)  # noqa: E712
        .where(Round.finished.is_not(None))  # type: ignore[union-attr]
        .order_by(Round.finished)
    ).all()
    if not rows:
        return None

    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)

    def aware(t):
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)

    ends = [(int(n), aware(f)) for n, f in rows]
    if ts < ends[0][1]:
        return None  # bot was last updated before the first round finished

    for (n0, t0), (n1, t1) in zip(ends, ends[1:]):
        if t0 <= ts < t1:
            span = (t1 - t0).total_seconds()
            if span <= 0:
                return float(n0)
            frac = (ts - t0).total_seconds() / span
            return n0 + frac * (n1 - n0)

    # Past the last completed round's end — pin to that round.
    return float(ends[-1][0])


def bot_current_race_elo(session: Session, bot_id: int) -> dict[str, float]:
    """Latest per-race ELO from the simulation in `bot_race_elo_history`.
    Returns a dict keyed by race code; missing keys mean the bot hasn't faced
    that race yet."""
    history = bot_race_elo_history(session, bot_id)
    if not history:
        return {}
    last_round = max(e["round_number"] for e in history)
    return {e["race"]: e["rating"] for e in history if e["round_number"] == last_round}


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
          AND r.complete = 1
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
