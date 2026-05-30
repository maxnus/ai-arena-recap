"""Top-5 "fun rankings" shown on the /rankings page.

Each ranking helper returns a list of `row` dicts (at most ``TOP_N``). The page
route calls :func:`all_rankings`, which assembles them into titled groups of
cards. Everything is scoped to active participants of the current competition
(the "ladder") and reads from the local DB only — see AGENTS.md.

The assembled page is cached and only recomputed when the underlying data
actually changes (a cheap content fingerprint — see :func:`_data_version` — not
``last_synced``, which bumps every sync). The sync job calls
:func:`warm_rankings` after each pass, so the cache is normally already warm
before any visitor arrives and nobody waits on the ~20 aggregate queries. The
heavy per-bot match aggregate and the per-opponent-race aggregate are each
computed once per build and shared across the cards that need them.
"""
import logging
import threading
from itertools import groupby

from sqlalchemy import case, func, text
from sqlalchemy.orm import aliased
from sqlmodel import Session, select

from ai_arena_recap.config import settings
from ai_arena_recap.models import (
    Bot,
    Competition,
    CompetitionParticipation,
    Match,
    MatchParticipation,
    Round,
)
from ai_arena_recap.web.queries import RACE_ELO_K, STEPS_PER_SECOND, WLT_RESULTS, _win_rate

log = logging.getLogger(__name__)

TOP_N = 10

# Minimum sample sizes so a bot with a couple of lucky games can't top a
# rate- or average-based board.
MIN_MATCHES = 20      # avg duration / step time / overall win rate
MIN_VS_RACE = 10      # games vs a specific race for the "Best vs <race>" boards
MIN_PER_RACE = 10     # per-race games required for the "most balanced" board
MIN_ROUNDS = 15       # completed rounds required for the ELO-volatility board
UPSET_MARGIN = 200    # ELO head-start that makes a win an "upset"
AUTHOR_MIN_BOTS = 2   # authors need >1 bot for a meaningful mean ELO


# ---------------------------------------------------------------------------
# Formatting + row helpers
# ---------------------------------------------------------------------------

def _fmt_duration(seconds: float) -> str:
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _fmt_pct(x: float) -> str:
    return f"{x:.0f}%"


def _row(name, value, *, href=None, race=None, sub=None) -> dict:
    return {"name": name, "value": value, "href": href, "race": race, "sub": sub}


def _active(query):
    """Restrict a query to active participants of the current competition.

    Assumes ``Bot`` is already in the FROM clause; joins the bot's
    CompetitionParticipation row (unique per competition, so no fan-out)."""
    return (
        query.join(
            CompetitionParticipation,
            (CompetitionParticipation.bot_id == Bot.id)
            & (CompetitionParticipation.competition_id == settings.competition_id),
        )
        .where(CompetitionParticipation.active == True)  # noqa: E712
    )


# ---------------------------------------------------------------------------
# Per-bot match aggregates: longest / shortest games, fastest step time
# ---------------------------------------------------------------------------

def _per_bot_match_stats(session: Session, *, min_matches: int = MIN_MATCHES) -> list:
    """(bot_id, name, race, n, avg_steps, avg_step_time) over the bot's W/L/T
    matches in the current competition, for active ladder bots with at least
    ``min_matches`` games. Computed once and shared by the duration / step-time
    cards."""
    n = func.count(Match.id)
    return session.exec(
        _active(
            select(
                Bot.id, Bot.name, Bot.plays_race,
                n.label("n"),
                func.avg(Match.result_game_steps).label("avg_steps"),
                func.avg(MatchParticipation.avg_step_time).label("avg_step_time"),
            )
            .join(MatchParticipation, MatchParticipation.bot_id == Bot.id)
            .join(Match, Match.id == MatchParticipation.match_id)
            .join(Round, Round.id == Match.round_id)
        )
        .where(Round.competition_id == settings.competition_id)
        .where(MatchParticipation.result.in_(WLT_RESULTS))
        .group_by(Bot.id, Bot.name, Bot.plays_race)
        .having(n >= min_matches)
    ).all()


def _stats_sorted(stats, attr, *, reverse):
    return sorted((r for r in stats if getattr(r, attr) is not None),
                  key=lambda r: getattr(r, attr), reverse=reverse)


def _games_rows(stats, *, longest, limit=TOP_N):
    rows = _stats_sorted(stats, "avg_steps", reverse=longest)
    return [_row(r.name, _fmt_duration(r.avg_steps / STEPS_PER_SECOND),
                 href=f"/bots/{r.id}", race=r.plays_race) for r in rows[:limit]]


def _step_rows(stats, *, limit=TOP_N):
    rows = _stats_sorted(stats, "avg_step_time", reverse=False)
    return [_row(r.name, f"{r.avg_step_time * 1000:.1f} ms",
                 href=f"/bots/{r.id}", race=r.plays_race) for r in rows[:limit]]


def longest_games(session, *, limit=TOP_N, min_matches=MIN_MATCHES) -> list[dict]:
    return _games_rows(_per_bot_match_stats(session, min_matches=min_matches),
                       longest=True, limit=limit)


def shortest_games(session, *, limit=TOP_N, min_matches=MIN_MATCHES) -> list[dict]:
    return _games_rows(_per_bot_match_stats(session, min_matches=min_matches),
                       longest=False, limit=limit)


def fastest_step_time(session, *, limit=TOP_N, min_matches=MIN_MATCHES) -> list[dict]:
    return _step_rows(_per_bot_match_stats(session, min_matches=min_matches), limit=limit)


# ---------------------------------------------------------------------------
# Race matchups (one shared query feeds best-vs-race x3 + most-balanced)
# ---------------------------------------------------------------------------

def _winrate_by_opprace(session: Session) -> list:
    """Per active subject bot, W/L/T vs each of Terran / Zerg / Protoss, in a
    single self-join aggregate. Rows: (id, name, plays_race, opp_race, n, wins,
    ties)."""
    Opp = aliased(MatchParticipation)
    OppBot = aliased(Bot)
    n = func.count(Match.id)
    wins = func.coalesce(func.sum(case((MatchParticipation.result == "win", 1), else_=0)), 0)
    ties = func.coalesce(func.sum(case((MatchParticipation.result == "tie", 1), else_=0)), 0)
    return session.exec(
        _active(
            select(
                Bot.id, Bot.name, Bot.plays_race, OppBot.plays_race.label("opp_race"),
                n.label("n"), wins.label("wins"), ties.label("ties"),
            )
            .join(MatchParticipation, MatchParticipation.bot_id == Bot.id)
            .join(Match, Match.id == MatchParticipation.match_id)
            .join(Round, Round.id == Match.round_id)
            .join(Opp, (Opp.match_id == Match.id) & (Opp.bot_id != Bot.id))
            .join(OppBot, OppBot.id == Opp.bot_id)
        )
        .where(Round.competition_id == settings.competition_id)
        .where(MatchParticipation.result.in_(WLT_RESULTS))
        .where(OppBot.plays_race.in_(("T", "Z", "P")))
        .group_by(Bot.id, Bot.name, Bot.plays_race, OppBot.plays_race)
    ).all()


def _race_elo_all(session: Session) -> dict[int, dict]:
    """Per active subject bot, current race-specific ELO vs each opponent race,
    using the same K-factor simulation as the bot detail page
    (queries.bot_race_elo_history) but computed for every bot in one pass.

    Returns {bot_id: {"name", "race", "elo": {race: rating}, "n": {race: games}}}
    covering only the races the bot has actually faced."""
    rows = session.exec(text("""
        SELECT mp.bot_id AS bot_id, subj.name AS subj_name, subj.plays_race AS subj_race,
               mp.result AS result, opp_bot.plays_race AS race,
               opp_mp.starting_elo AS opp_elo, mp.starting_elo AS bot_starting_elo
        FROM match_participation mp
        JOIN match m ON m.id = mp.match_id
        JOIN round r ON r.id = m.round_id
        JOIN competition_participation cp
          ON cp.bot_id = mp.bot_id AND cp.competition_id = :cid AND cp.active = 1
        JOIN bot subj ON subj.id = mp.bot_id
        JOIN match_participation opp_mp
          ON opp_mp.match_id = m.id AND opp_mp.bot_id != mp.bot_id
        JOIN bot opp_bot ON opp_bot.id = opp_mp.bot_id
        WHERE r.competition_id = :cid
          AND r.complete = 1
          AND mp.result IN ('win', 'loss', 'tie')
          AND opp_mp.starting_elo IS NOT NULL
          AND opp_bot.plays_race IS NOT NULL
        ORDER BY mp.bot_id, r.number, m.started, m.id
    """), params={"cid": settings.competition_id}).all()

    out: dict[int, dict] = {}
    for bot_id, group in groupby(rows, key=lambda x: x[0]):
        group = list(group)
        initial = next((g[6] for g in group if g[6] is not None), 1600.0)
        ratings = {r: float(initial) for r in ("T", "Z", "P", "R")}
        counts: dict[str, int] = {}
        for _bid, _name, _srace, result, race, opp_elo, _se in group:
            score = 1.0 if result == "win" else 0.5 if result == "tie" else 0.0
            expected = 1.0 / (1.0 + 10 ** ((float(opp_elo) - ratings[race]) / 400))
            ratings[race] += RACE_ELO_K * (score - expected)
            counts[race] = counts.get(race, 0) + 1
        out[bot_id] = {
            "name": group[0][1], "race": group[0][2],
            "elo": {race: round(ratings[race], 1) for race in counts},
            "n": dict(counts),
        }
    return out


def _best_vs_race_rows(race_elo, race, *, limit=TOP_N, min_vs=MIN_VS_RACE) -> list[dict]:
    cand = [
        (bid, d["name"], d["race"], d["elo"][race])
        for bid, d in race_elo.items()
        if race in d["elo"] and d["n"].get(race, 0) >= min_vs
    ]
    cand.sort(key=lambda x: x[3], reverse=True)
    return [_row(name, str(int(round(elo))), href=f"/bots/{bid}", race=br)
            for bid, name, br, elo in cand[:limit]]


def _most_balanced_rows(oppr, *, limit=TOP_N, min_per_race=MIN_PER_RACE) -> list[dict]:
    per_bot: dict[int, dict] = {}
    for r in oppr:
        d = per_bot.setdefault(r.id, {"name": r.name, "race": r.plays_race, "rates": {}})
        if r.n >= min_per_race:
            d["rates"][r.opp_race] = _win_rate(r.wins, r.ties, r.n)
    cand = [
        (bid, d["name"], d["race"], max(d["rates"].values()) - min(d["rates"].values()))
        for bid, d in per_bot.items() if len(d["rates"]) == 3
    ]
    cand.sort(key=lambda x: x[3])
    return [_row(name, f"{spread:.0f} pp", href=f"/bots/{bid}", race=race)
            for bid, name, race, spread in cand[:limit]]


def best_vs_race(session, race, *, limit=TOP_N, min_vs=MIN_VS_RACE) -> list[dict]:
    """Highest race-specific ELO vs opponents of `race`, current competition."""
    return _best_vs_race_rows(_race_elo_all(session), race, limit=limit, min_vs=min_vs)


def most_balanced(session, *, limit=TOP_N, min_per_race=MIN_PER_RACE) -> list[dict]:
    """Smallest spread between win rates vs Terran / Zerg / Protoss."""
    return _most_balanced_rows(_winrate_by_opprace(session), limit=limit, min_per_race=min_per_race)


def strongest_of_race(session, race, *, limit=TOP_N) -> list[dict]:
    """Highest current ELO among active bots that play `race`."""
    rows = session.exec(
        select(Bot.id, Bot.name, Bot.plays_race, CompetitionParticipation.elo)
        .join(CompetitionParticipation, CompetitionParticipation.bot_id == Bot.id)
        .where(CompetitionParticipation.competition_id == settings.competition_id)
        .where(CompetitionParticipation.active == True)  # noqa: E712
        .where(Bot.plays_race == race)
        .where(CompetitionParticipation.elo.is_not(None))
        .order_by(CompetitionParticipation.elo.desc())
        .limit(limit)
    ).all()
    return [_row(r.name, str(r.elo), href=f"/bots/{r.id}", race=r.plays_race) for r in rows]


# ---------------------------------------------------------------------------
# ELO / wins
# ---------------------------------------------------------------------------

def highest_peak_elo(session, *, limit=TOP_N) -> list[dict]:
    rows = session.exec(
        select(Bot.id, Bot.name, Bot.plays_race, CompetitionParticipation.highest_elo)
        .join(CompetitionParticipation, CompetitionParticipation.bot_id == Bot.id)
        .where(CompetitionParticipation.competition_id == settings.competition_id)
        .where(CompetitionParticipation.active == True)  # noqa: E712
        .where(CompetitionParticipation.highest_elo.is_not(None))
        .order_by(CompetitionParticipation.highest_elo.desc())
        .limit(limit)
    ).all()
    return [_row(r.name, str(r.highest_elo), href=f"/bots/{r.id}", race=r.plays_race) for r in rows]


def highest_win_rate(session, *, limit=TOP_N, min_matches=MIN_MATCHES) -> list[dict]:
    cp = CompetitionParticipation
    rows = session.exec(
        select(Bot.id, Bot.name, Bot.plays_race, cp.win_perc)
        .join(cp, cp.bot_id == Bot.id)
        .where(cp.competition_id == settings.competition_id)
        .where(cp.active == True)  # noqa: E712
        .where(cp.win_perc.is_not(None))
        .where(cp.match_count >= min_matches)
        .order_by(cp.win_perc.desc())
        .limit(limit)
    ).all()
    return [_row(r.name, _fmt_pct(r.win_perc), href=f"/bots/{r.id}", race=r.plays_race) for r in rows]


def tie_rate(session, *, limit=TOP_N, min_matches=MIN_MATCHES) -> list[dict]:
    """Highest share of games that ended in a tie (ties ÷ matches)."""
    cp = CompetitionParticipation
    rate = cp.tie_count * 1.0 / cp.match_count
    rows = session.exec(
        select(Bot.id, Bot.name, Bot.plays_race, rate.label("rate"))
        .join(cp, cp.bot_id == Bot.id)
        .where(cp.competition_id == settings.competition_id)
        .where(cp.active == True)  # noqa: E712
        .where(cp.match_count >= min_matches)
        .order_by(rate.desc())
        .limit(limit)
    ).all()
    return [_row(r.name, f"{r.rate * 100:.1f}%", href=f"/bots/{r.id}", race=r.plays_race) for r in rows]


def most_decisive(session, *, limit=TOP_N, min_matches=MIN_MATCHES) -> list[dict]:
    """Lowest tie rate — bots that almost always get a decisive result. Ties (in
    the ranking sense) broken by games played, since many bots never tie at all,
    so the busiest never-tie bot wins."""
    cp = CompetitionParticipation
    rate = cp.tie_count * 1.0 / cp.match_count
    rows = session.exec(
        select(Bot.id, Bot.name, Bot.plays_race, rate.label("rate"))
        .join(cp, cp.bot_id == Bot.id)
        .where(cp.competition_id == settings.competition_id)
        .where(cp.active == True)  # noqa: E712
        .where(cp.match_count >= min_matches)
        .order_by(rate.asc(), cp.match_count.desc())
        .limit(limit)
    ).all()
    return [_row(r.name, f"{r.rate * 100:.1f}%", href=f"/bots/{r.id}", race=r.plays_race) for r in rows]


def elo_volatility(session, *, limit=TOP_N, min_rounds=MIN_ROUNDS) -> list[dict]:
    """Bots whose ELO swung the most round to round — standard deviation of the
    change in end-of-round ELO between consecutive completed rounds.

    End-of-round ELO is the resultant_elo of the bot's last match in each round
    (the same definition the bot detail page's ELO history uses). Using the
    round-to-round *deltas* rather than the ELO level means a bot that climbs
    steadily reads as calm, while one that yo-yos up and down scores high — the
    actual "rollercoaster" we want, not just a measure of total range."""
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
              AND r.competition_id = :cid
              AND r.complete = 1
        )
        SELECT rm.bot_id, b.name, b.plays_race, r.number AS round_number,
               rm.resultant_elo AS end_elo
        FROM ranked_matches rm
        JOIN round r ON r.id = rm.round_id
        JOIN competition_participation cp
          ON cp.bot_id = rm.bot_id AND cp.competition_id = :cid AND cp.active = 1
        JOIN bot b ON b.id = rm.bot_id
        WHERE rm.rn = 1
        ORDER BY rm.bot_id, r.number
    """), params={"cid": settings.competition_id}).all()

    ranked = []
    for bot_id, group in groupby(rows, key=lambda x: x[0]):
        group = list(group)
        if len(group) < min_rounds:
            continue
        elos = [float(g[4]) for g in group]
        deltas = [elos[i] - elos[i - 1] for i in range(1, len(elos))]
        mean = sum(deltas) / len(deltas)
        var = sum((d - mean) ** 2 for d in deltas) / len(deltas)
        ranked.append((bot_id, group[0][1], group[0][2], var ** 0.5))

    ranked.sort(key=lambda x: x[3], reverse=True)
    return [_row(name, f"±{sd:.0f}", href=f"/bots/{bid}", race=race)
            for bid, name, race, sd in ranked[:limit]]


def longest_win_streak(session, *, limit=TOP_N) -> list[dict]:
    """Longest run of consecutive wins (a loss or tie breaks the streak),
    walking each bot's matches in chronological order."""
    rows = session.exec(
        _active(
            select(Bot.id, Bot.name, Bot.plays_race, MatchParticipation.result)
            .join(MatchParticipation, MatchParticipation.bot_id == Bot.id)
            .join(Match, Match.id == MatchParticipation.match_id)
            .join(Round, Round.id == Match.round_id)
        )
        .where(Round.competition_id == settings.competition_id)
        .where(MatchParticipation.result.in_(WLT_RESULTS))
        .order_by(Bot.id, Match.started, Match.id)
    ).all()

    best: dict[int, list] = {}  # bot_id -> [name, race, best_streak]
    cur_bot = None
    cur = 0
    for r in rows:
        if r.id != cur_bot:
            cur_bot, cur = r.id, 0
        cur = cur + 1 if r.result == "win" else 0
        slot = best.setdefault(r.id, [r.name, r.plays_race, 0])
        if cur > slot[2]:
            slot[2] = cur

    ranked = sorted(
        ((bid, v[0], v[1], v[2]) for bid, v in best.items() if v[2] > 0),
        key=lambda x: x[3], reverse=True,
    )[:limit]
    return [_row(name, str(streak), href=f"/bots/{bid}", race=race) for bid, name, race, streak in ranked]


def giant_killers(session, *, limit=TOP_N, margin=UPSET_MARGIN) -> list[dict]:
    """Most wins against an opponent who started the match at least `margin`
    ELO higher."""
    Opp = aliased(MatchParticipation)
    cnt = func.count(Match.id)
    rows = session.exec(
        _active(
            select(Bot.id, Bot.name, Bot.plays_race, cnt.label("c"))
            .join(MatchParticipation, MatchParticipation.bot_id == Bot.id)
            .join(Match, Match.id == MatchParticipation.match_id)
            .join(Round, Round.id == Match.round_id)
            .join(Opp, (Opp.match_id == Match.id) & (Opp.bot_id != Bot.id))
        )
        .where(Round.competition_id == settings.competition_id)
        .where(MatchParticipation.result == "win")
        .where(MatchParticipation.starting_elo.is_not(None))
        .where(Opp.starting_elo.is_not(None))
        .where(Opp.starting_elo - MatchParticipation.starting_elo >= margin)
        .group_by(Bot.id, Bot.name, Bot.plays_race)
        .order_by(cnt.desc())
        .limit(limit)
    ).all()
    return [_row(r.name, str(r.c), href=f"/bots/{r.id}", race=r.plays_race) for r in rows]


def biggest_upsets(session, *, limit=TOP_N) -> list[dict]:
    """Largest starting-ELO gap overcome by the winner — one row per winning
    bot (its single biggest upset), so the board shows `limit` distinct bots."""
    Winner = aliased(MatchParticipation)
    Loser = aliased(MatchParticipation)
    WinnerBot = aliased(Bot)
    LoserBot = aliased(Bot)
    gap = Loser.starting_elo - Winner.starting_elo
    # Rank each winning bot's upset wins by gap; keep only their biggest.
    rank = func.row_number().over(partition_by=Winner.bot_id, order_by=gap.desc())
    sub = (
        select(
            WinnerBot.id.label("bid"), WinnerBot.name.label("name"),
            WinnerBot.plays_race.label("race"), LoserBot.name.label("loser_name"),
            gap.label("gap"), rank.label("rn"),
        )
        .select_from(Winner)
        .join(Match, Match.id == Winner.match_id)
        .join(Round, Round.id == Match.round_id)
        .join(Loser, (Loser.match_id == Match.id) & (Loser.bot_id != Winner.bot_id))
        .join(WinnerBot, WinnerBot.id == Winner.bot_id)
        .join(LoserBot, LoserBot.id == Loser.bot_id)
        .where(Round.competition_id == settings.competition_id)
        .where(Winner.result == "win")
        .where(Winner.starting_elo.is_not(None))
        .where(Loser.starting_elo.is_not(None))
    ).subquery()

    rows = session.exec(
        select(sub.c.bid, sub.c.name, sub.c.race, sub.c.loser_name, sub.c.gap)
        .where(sub.c.rn == 1)
        .order_by(sub.c.gap.desc())
        .limit(limit)
    ).all()
    return [_row(r.name, f"+{int(r.gap)}", href=f"/bots/{r.bid}", race=r.race, sub=f"beat {r.loser_name}")
            for r in rows]


# ---------------------------------------------------------------------------
# Longevity / community
# ---------------------------------------------------------------------------

def _bots_by_date(session, column, *, descending: bool, limit=TOP_N) -> list[dict]:
    order = column.desc() if descending else column.asc()
    rows = session.exec(
        _active(select(Bot.id, Bot.name, Bot.plays_race, column.label("dt")))
        .where(column.is_not(None))
        .order_by(order)
        .limit(limit)
    ).all()
    return [_row(r.name, r.dt.strftime("%Y-%m-%d"), href=f"/bots/{r.id}", race=r.plays_race) for r in rows]


def oldest_bots(session, *, limit=TOP_N) -> list[dict]:
    return _bots_by_date(session, Bot.created, descending=False, limit=limit)


def newest_bots(session, *, limit=TOP_N) -> list[dict]:
    return _bots_by_date(session, Bot.created, descending=True, limit=limit)


def recently_updated(session, *, limit=TOP_N) -> list[dict]:
    return _bots_by_date(session, Bot.bot_zip_updated, descending=True, limit=limit)


def longest_since_update(session, *, limit=TOP_N) -> list[dict]:
    return _bots_by_date(session, Bot.bot_zip_updated, descending=False, limit=limit)


def top_authors_by_mean_elo(session, *, limit=TOP_N, min_bots=AUTHOR_MIN_BOTS) -> list[dict]:
    cp = CompetitionParticipation
    cnt = func.count(cp.id)
    mean_elo = func.avg(cp.elo)
    rows = session.exec(
        select(Bot.user_name, cnt.label("c"), mean_elo.label("mean_elo"))
        .join(cp, cp.bot_id == Bot.id)
        .where(cp.competition_id == settings.competition_id)
        .where(cp.active == True)  # noqa: E712
        .where(Bot.user_name.is_not(None))
        .where(cp.elo.is_not(None))
        .group_by(Bot.user_name)
        .having(cnt >= min_bots)
        .order_by(mean_elo.desc())
        .limit(limit)
    ).all()
    return [_row(r.user_name, str(int(round(r.mean_elo))), sub=f"{r.c} bots") for r in rows]


# ---------------------------------------------------------------------------
# Assembly (+ per-sync cache)
# ---------------------------------------------------------------------------

_CACHE: dict = {"key": None, "value": None}
_CACHE_LOCK = threading.Lock()


def _build_rankings(session: Session) -> list[dict]:
    """Compute all ranking cards. The per-bot match aggregate and the
    per-opponent-race aggregate are each run once here and reused across the
    cards that need them."""
    stats = _per_bot_match_stats(session)
    oppr = _winrate_by_opprace(session)
    race_elo = _race_elo_all(session)

    return [
        {
            "title": "ELO & Wins",
            "cards": [
                {"title": "Highest peak ELO", "value_label": "Peak ELO",
                 "note": None, "rows": highest_peak_elo(session)},
                {"title": "Best win rate", "value_label": "Win rate",
                 "note": f"Min {MIN_MATCHES} games", "rows": highest_win_rate(session)},
                {"title": "Strongest Terran", "value_label": "ELO",
                 "note": None, "rows": strongest_of_race(session, "T")},
                {"title": "Strongest Zerg", "value_label": "ELO",
                 "note": None, "rows": strongest_of_race(session, "Z")},
                {"title": "Strongest Protoss", "value_label": "ELO",
                 "note": None, "rows": strongest_of_race(session, "P")},
                {"title": "Strongest Random", "value_label": "ELO",
                 "note": None, "rows": strongest_of_race(session, "R")},
            ],
        },
        {
            "title": "Matchups",
            "cards": [
                {"title": "Best vs Terran", "value_label": "ELO",
                 "note": f"ELO vs Terran (min {MIN_VS_RACE} games)",
                 "rows": _best_vs_race_rows(race_elo, "T")},
                {"title": "Best vs Zerg", "value_label": "ELO",
                 "note": f"ELO vs Zerg (min {MIN_VS_RACE} games)",
                 "rows": _best_vs_race_rows(race_elo, "Z")},
                {"title": "Best vs Protoss", "value_label": "ELO",
                 "note": f"ELO vs Protoss (min {MIN_VS_RACE} games)",
                 "rows": _best_vs_race_rows(race_elo, "P")},
                {"title": "Best vs Random", "value_label": "ELO",
                 "note": f"ELO vs Random (min {MIN_VS_RACE} games)",
                 "rows": _best_vs_race_rows(race_elo, "R")},
                {"title": "Most balanced", "value_label": "Spread",
                 "note": f"Smallest win-rate spread across T/Z/P (min {MIN_PER_RACE} games each)",
                 "rows": _most_balanced_rows(oppr)},
            ],
        },
        {
            "title": "Quirks",
            "cards": [
                {"title": "Longest win streak", "value_label": "In a row",
                 "note": None, "rows": longest_win_streak(session)},
                {"title": "Giant killers", "value_label": "Upset wins",
                 "note": f"Wins vs an opponent ≥{UPSET_MARGIN} ELO higher",
                 "rows": giant_killers(session)},
                {"title": "Biggest upsets", "value_label": "ELO gap",
                 "note": "Largest rating gap overcome in a single match",
                 "rows": biggest_upsets(session)},
                {"title": "Rollercoaster", "value_label": "ELO σ",
                 "note": f"Most volatile ELO — std dev of round-to-round ELO change (min {MIN_ROUNDS} rounds)",
                 "rows": elo_volatility(session)},
                {"title": "Highest tie rate", "value_label": "Tie rate",
                 "note": f"Ties ÷ matches, min {MIN_MATCHES} games", "rows": tie_rate(session)},
                {"title": "Most decisive", "value_label": "Tie rate",
                 "note": f"Lowest tie rate (min {MIN_MATCHES} games)", "rows": most_decisive(session)},
            ],
        },
        {
            "title": "Games",
            "cards": [
                {"title": "Longest games", "value_label": "Avg length",
                 "note": f"Avg game length, min {MIN_MATCHES} games",
                 "rows": _games_rows(stats, longest=True)},
                {"title": "Shortest games", "value_label": "Avg length",
                 "note": f"Avg game length, min {MIN_MATCHES} games",
                 "rows": _games_rows(stats, longest=False)},
                {"title": "Fastest step time", "value_label": "Avg step",
                 "note": f"Avg per-step compute, min {MIN_MATCHES} games",
                 "rows": _step_rows(stats)},
            ],
        },
        {
            "title": "Longevity & community",
            "cards": [
                {"title": "Oldest bots", "value_label": "Created",
                 "note": None, "rows": oldest_bots(session)},
                {"title": "Newest bots", "value_label": "Created",
                 "note": None, "rows": newest_bots(session)},
                {"title": "Recently updated", "value_label": "Updated",
                 "note": None, "rows": recently_updated(session)},
                {"title": "Longest since an update", "value_label": "Updated",
                 "note": None, "rows": longest_since_update(session)},
                {"title": "Top authors by mean ELO", "value_label": "Mean ELO",
                 "note": f"Authors with ≥{AUTHOR_MIN_BOTS} bots",
                 "rows": top_authors_by_mean_elo(session)},
            ],
        },
    ]


def _data_version(session: Session):
    """A cheap fingerprint of everything the rankings depend on, scoped to the
    current competition. Changes whenever new matches arrive, ratings/standings
    update, or bots join / re-upload — but is stable across syncs that didn't
    actually change anything (unlike ``Competition.last_synced``, which bumps
    every cycle). A handful of indexed COUNT/SUM/MAX aggregates, far cheaper
    than a full rebuild, so it's fine to run on every page view and every sync."""
    cid = settings.competition_id
    matches = session.exec(text(
        "SELECT COUNT(*), COALESCE(MAX(m.id), 0) "
        "FROM match m JOIN round r ON r.id = m.round_id "
        "WHERE r.competition_id = :cid"
    ), params={"cid": cid}).first()
    standings = session.exec(text(
        "SELECT COUNT(*), COALESCE(SUM(elo), 0), COALESCE(SUM(highest_elo), 0), "
        "COALESCE(SUM(win_count), 0), COALESCE(SUM(tie_count), 0), "
        "COALESCE(SUM(match_count), 0) "
        "FROM competition_participation WHERE competition_id = :cid AND active = 1"
    ), params={"cid": cid}).first()
    bots = session.exec(text(
        "SELECT COUNT(*), COALESCE(MAX(b.created), ''), COALESCE(MAX(b.bot_zip_updated), '') "
        "FROM bot b JOIN competition_participation cp "
        "  ON cp.bot_id = b.id AND cp.competition_id = :cid AND cp.active = 1"
    ), params={"cid": cid}).first()
    return (cid, tuple(matches), tuple(standings), tuple(bots))


def all_rankings(session: Session) -> list[dict]:
    """All ranking cards, organised into titled groups for the page.

    Returns the cached result when the data fingerprint is unchanged; otherwise
    rebuilds (the ~20 aggregate queries). Double-checked locking means a cold
    cache hit by several concurrent requests only triggers one rebuild."""
    key = _data_version(session)
    if _CACHE["key"] == key and _CACHE["value"] is not None:
        return _CACHE["value"]
    with _CACHE_LOCK:
        if _CACHE["key"] == key and _CACHE["value"] is not None:
            return _CACHE["value"]
        value = _build_rankings(session)
        _CACHE["key"] = key
        _CACHE["value"] = value
        return value


def warm_rankings() -> None:
    """Rebuild the cache if the data changed. Called after each sync so the
    first visitor after new data never waits; a cheap no-op (just the
    fingerprint check) when nothing changed. Never raises — a warming failure
    must not break the sync pass."""
    # Imported here so the monkeypatched engine in tests is picked up, and to
    # keep the sync layer free of an import-time dependency on the web layer.
    from ai_arena_recap.db import engine
    try:
        with Session(engine) as session:
            all_rankings(session)
    except Exception:  # noqa: BLE001 - warming is best-effort
        log.exception("Failed to warm rankings cache")
