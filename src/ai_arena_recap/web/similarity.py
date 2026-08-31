"""Play-style similarity between bots, from win rates alone.

For every pair of bots in a season this produces two numbers:

* ``rho`` — the **style correlation**, in (-1, 1). Do the two bots over- and
  under-perform against the *same* opponents, once each bot's strength and each
  opponent's difficulty are accounted for?
* ``conf`` — ``P(rho > 0.5)``, the posterior mass above a threshold. This is
  what the page ranks on. Note it is *not* a precision measure: it fuses "is the
  correlation large" with "do we know it", so it should never be labelled
  "confidence" in the plain sense of certainty about the estimate.
* ``sd`` — the posterior standard deviation: how tightly ``rho`` is pinned
  down. This is the certainty measure; ``conf`` is not one.

The full derivation and the validation against known cases live in
``docs/similarity-model.md``. The short version of why it is not
simply "correlate the win-rate vectors": win rate is dominated by *strength*, so
two bots of equal ELO look alike however they play. Steps 2 and 3 below strip
the bot effect and the opponent effect, and what is left — the interaction — is
what "style" means here.

Neither number is the probability that one bot copied the other: they carry no
base rate for copying, and shared public templates or identical standard build
orders produce high ``rho`` innocently. Present this as similarity, never as an
accusation.

Scoping notes:

* Everything is per competition — ELO, opponents and the map pool all change
  between seasons.
* Unlike every other page, this one deliberately does **not** apply
  ``season.ladder_filter()``. A bot removed from the ladder mid-season has its
  division reset to 0 and would vanish, and removed bots are precisely the
  interesting ones here. Membership is instead "has a participation row for this
  competition and enough opponents to profile".
* Pairs are only scored within a race, or where either bot plays Random. A
  Random bot dispatches to a different bot per race, so a single-race bot can
  legitimately resemble one; cross-race pairs are coincidence.

The result is cached and rebuilt only when a content fingerprint changes, the
same pattern as ``web/rankings.py``; the sync job calls :func:`warm_similarity`
after each pass so nobody waits on a rebuild.
"""
import logging
import math
import threading

import numpy as np
from sqlalchemy import text
from sqlmodel import Session

from ai_arena_recap.web import season as season_mod

log = logging.getLogger(__name__)

# Log-odds per ELO point: an ELO gap of d implies log-odds C * d.
ELO_C = math.log(10) / 400.0

# A single game against an opponent tells us almost nothing about style, and at
# a large ELO gap it is actively misleading: the Haldane-corrected log-odds of a
# 1-game record is capped at +-1.10, while the ELO expectation can be -5.65, so
# the residual comes out hugely positive whatever the result. That is bias, not
# noise, so the `u` weighting does not absorb it — it has to be excluded.
MIN_CELL_GAMES = 2
MIN_OPPONENTS = 10      # opponents needed before a bot can be profiled at all
MIN_COMMON = 12         # common opponents needed before a pair is scored
CENTRE_PASSES = 8       # alternating row/column removals (see docs §3)
SIGMA2_FLOOR = 0.05     # guards a degenerate covariance; never binds in practice
# Threshold for the ranking statistic P(rho > CONF_THRESHOLD). The value is a
# choice, not a fact: 0.5 sits well below the 0.8+ where genuinely related bots
# land. Raising it does not make the page more conservative in any useful sense
# — past ~0.8 almost every pair has negligible mass above the line, so the
# statistic flattens to zero and stops ranking anything.
CONF_THRESHOLD = 0.5
TOP_PAIRS = 300         # pairs kept for the page's ranked table

# Posterior grid for rho. Uniform prior, so the spacing cancels in the weights.
RHO_GRID = np.linspace(-0.98, 0.98, 79)

_CACHE: dict[int, dict] = {}
_CACHE_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def _load(session: Session) -> tuple[list[dict], np.ndarray, np.ndarray]:
    """Bots in this season plus their decided win/loss counts against each other.

    Returns ``(bots, wins, losses)`` where the two arrays are ``N x N`` and
    indexed by position in ``bots``: ``wins[i, j]`` is how often bot *i* beat
    bot *j*. Ties are excluded — see docs §1. Cells below ``MIN_CELL_GAMES``
    are dropped later, in :func:`_profiles`.
    """
    cid = season_mod.cid()
    rows = session.exec(text(
        "SELECT cp.bot_id, b.name, b.plays_race, b.user_name, b.type, cp.elo, cp.division_num, cp.active "
        "FROM competition_participation cp JOIN bot b ON b.id = cp.bot_id "
        "WHERE cp.competition_id = :cid AND cp.elo IS NOT NULL"
    ), params={"cid": cid}).all()
    bots = [
        {
            "bot_id": r[0], "name": r[1], "race": r[2], "author": r[3],
            "type": r[4], "elo": r[5],
            # A bot that left the ladder mid-season keeps its rows but loses its
            # division; surfaced so the page can mark it rather than hide it.
            "on_ladder": bool(r[6] and r[6] > 0) or bool(r[7]),
        }
        for r in rows
    ]
    index = {b["bot_id"]: i for i, b in enumerate(bots)}
    n = len(bots)
    wins = np.zeros((n, n))
    losses = np.zeros((n, n))
    if n == 0:
        return bots, wins, losses

    counts = session.exec(text(
        "SELECT a.bot_id, b.bot_id, "
        "       SUM(CASE WHEN a.result = 'win' THEN 1 ELSE 0 END), "
        "       SUM(CASE WHEN a.result = 'loss' THEN 1 ELSE 0 END) "
        "FROM match_participation a "
        "JOIN match_participation b ON b.match_id = a.match_id AND b.bot_id <> a.bot_id "
        "JOIN match m ON m.id = a.match_id "
        "JOIN round r ON r.id = m.round_id "
        "WHERE r.competition_id = :cid AND a.result IN ('win', 'loss') "
        "GROUP BY a.bot_id, b.bot_id"
    ), params={"cid": cid}).all()
    for bot_id, opp_id, w, l in counts:
        i, j = index.get(bot_id), index.get(opp_id)
        if i is not None and j is not None:
            wins[i, j] = w or 0
            losses[i, j] = l or 0
    return bots, wins, losses


# ---------------------------------------------------------------------------
# The model (docs/similarity-model.md §1-§4)
# ---------------------------------------------------------------------------

def _profiles(bots: list[dict], wins: np.ndarray, losses: np.ndarray):
    """Double-centred style residuals and their sampling variances.

    Returns ``(x, u, played, sigma2)``: the residual for each (bot, opponent)
    cell, its sampling variance, a boolean mask of which cells have data, and the
    pooled style variance for the season.
    """
    # §1 — log-odds with the Haldane-Anscombe correction, and its variance. The
    # +1/2 keeps a 0-win or 0-loss cell finite; `u` is what lets a thin cell
    # contribute in proportion to what it actually knows instead of being cut.
    played = (wins + losses) >= MIN_CELL_GAMES
    np.fill_diagonal(played, False)
    w = wins + 0.5
    l = losses + 0.5
    x = np.log(w / l)
    u = 1.0 / w + 1.0 / l

    # §2 — subtract what ELO alone predicts. ELO is a globally fitted, schedule-
    # aware strength estimate, which matters because matchmaking gives every bot
    # a different opponent mix.
    elo = np.array([b["elo"] for b in bots], dtype=float)
    x -= ELO_C * (elo[:, None] - elo[None, :])
    x = np.where(played, x, 0.0)

    # §3 — double-centring. ELO is one parameter per bot and cannot say "this
    # opponent is harder than its rating suggests", so a bias every bot shares
    # against a given opponent survives step 2. Left in, it is common-mode and
    # inflates every pair's correlation. Removing row and column means leaves the
    # interaction, which is the style. The matrix is incomplete, so removing one
    # margin re-introduces the other and this has to iterate.
    col_n = played.sum(axis=0)
    row_n = played.sum(axis=1)
    col_safe = np.where(col_n > 0, col_n, 1)
    row_safe = np.where(row_n > 0, row_n, 1)
    for _ in range(CENTRE_PASSES):
        x -= played * (x.sum(axis=0) / col_safe)[None, :]
        x -= played * (x.sum(axis=1) / row_safe)[:, None]

    # §4 — one pooled style variance for the whole ladder. Observed spread is
    # true spread plus sampling noise, so the noise is subtracted off.
    #
    # Pooled rather than per-bot because a single bot's estimate is noisy: it
    # still comes out negative for about 12% of bots. Partial pooling — shrinking
    # each bot's own estimate toward this one — was implemented and measured, and
    # reverted: it moved AUC by +0.007 and +0.001 across the two seasons, with
    # the labelled cases split, which does not earn a tuned constant.
    #
    # Known limitation: a Random wrapper plays a different bot per race, so its
    # deviations average out and its true style variance is near zero (median
    # +0.04 against +1.02 for single-race bots on competition 36). Pooling hands
    # it the ladder value regardless, overstating how much style it has to
    # correlate with. Fixing that properly needs a hierarchical estimate, not a
    # hand-set shrinkage weight.
    if played.sum() == 0:
        return x, u, played, SIGMA2_FLOOR
    sigma2 = float((x[played] ** 2).mean() - u[played].mean())
    return x, u, played, max(sigma2, SIGMA2_FLOOR)


def _score(xa, xb, ua, ub, sigma2: float) -> tuple[float, float, float]:
    """Posterior summary for one pair (docs §5-§7).

    Returns ``(rho, sd, conf)``:

    * ``rho`` — posterior mean, the estimate.
    * ``sd`` — posterior standard deviation: how tightly ``rho`` is pinned down.
      The certainty measure, and what fades the matrix cells.
    * ``conf`` — ``P(rho > 0.5)``, the ranking key. Beware the name: it fuses
      effect size with evidence and is mostly a transform of ``rho``, so it does
      not answer "how certain are we of this number" — ``sd`` does.

    ``sd`` is a spread, not half an interval: the posterior is markedly
    left-skewed near the top of the range (mass piles against the grid ceiling),
    so ``rho ± sd`` is not a valid credible interval — on real data it misplaces
    an endpoint by a median of 0.12 and can imply a correlation above 1.

    The two bots' latent styles are bivariate normal with correlation rho;
    measurement noise is independent, so it lands on the diagonal only and rho
    stays the correlation of the *true* styles rather than of the noisy
    measurements. Vectorised over the rho grid.
    """
    p = sigma2 + ua                       # (n,)
    q = sigma2 + ub                       # (n,)
    c = RHO_GRID * sigma2                 # (79,)
    det = (p * q)[None, :] - (c ** 2)[:, None]
    quad = ((q * xa * xa)[None, :]
            - 2.0 * c[:, None] * (xa * xb)[None, :]
            + (p * xb * xb)[None, :])
    # A non-positive determinant means the proposed correlation is impossible
    # given these variances; drive its likelihood to zero rather than take a
    # log of a negative number.
    bad = det <= 1e-12
    det = np.where(bad, 1.0, det)
    ll = (-math.log(2 * math.pi) - 0.5 * np.log(det) - 0.5 * quad / det)
    ll = np.where(bad, -np.inf, ll).sum(axis=1)

    weights = np.exp(ll - ll.max())
    total = weights.sum()
    if not np.isfinite(total) or total <= 0:
        return 0.0, 0.0, 0.0
    weights = weights / total
    rho = float((RHO_GRID * weights).sum())
    sd = float(np.sqrt(((RHO_GRID - rho) ** 2 * weights).sum()))
    conf = float(weights[RHO_GRID > CONF_THRESHOLD].sum())
    return rho, sd, conf


def _comparable(a: dict, b: dict) -> bool:
    """Same race, or either bot plays Random.

    A Random bot runs a different bot per race, so a single-race bot can
    genuinely resemble one — an early version of this filter compared declared
    races only and silently discarded exactly that case.
    """
    ra, rb = a["race"], b["race"]
    if not ra or not rb:
        return False
    return ra == rb or "R" in (ra, rb)


def _build(session: Session) -> dict:
    bots, wins, losses = _load(session)
    x, u, played, sigma2 = _profiles(bots, wins, losses)
    games = wins + losses

    eligible = [i for i in range(len(bots)) if played[i].sum() >= MIN_OPPONENTS]
    pairs = []
    for pos, i in enumerate(eligible):
        for j in eligible[pos + 1:]:
            if not _comparable(bots[i], bots[j]):
                continue
            common = played[i] & played[j]
            common[i] = common[j] = False
            n = int(common.sum())
            if n < MIN_COMMON:
                continue
            rho, sd, conf = _score(
                x[i][common], x[j][common], u[i][common], u[j][common], sigma2)
            pairs.append({
                "a": bots[i]["bot_id"], "b": bots[j]["bot_id"],
                "rho": round(rho, 4), "sd": round(sd, 4), "conf": round(conf, 4),
                "n": n, "games": int(games[i][common].sum() + games[j][common].sum()),
            })
    # Ranked on P(rho > threshold). A one-sided lower bound on rho was tried as
    # the sort key and rejected: it is roughly rho - 1.645*sd, and sd is driven
    # almost entirely by how many shared opponents a pair has (corr -0.83), so it
    # ends up ranking by data volume — its own correlation with opponent count
    # was +0.68 on competition 36, where the same-author AUC fell from 0.734 to
    # 0.532 and two of three known-positive pairs moved down.
    pairs.sort(key=lambda p: (-p["conf"], -p["rho"]))
    log.info("similarity: %d bots, %d pairs scored, sigma^2=%.3f",
             len(eligible), len(pairs), sigma2)
    return {
        "bots": {b["bot_id"]: b for b in bots},
        "pairs": pairs,
        "sigma2": round(sigma2, 4),
        "scored_bots": [bots[i]["bot_id"] for i in eligible],
    }


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def _data_version(session: Session):
    """Cheap fingerprint of everything the similarity model reads.

    Match volume drives the residuals and ELO drives the expectation, so those
    two aggregates are enough. Unlike ``Competition.last_synced`` this is stable
    across syncs that changed nothing, so the cache survives them."""
    cid = season_mod.cid()
    matches = session.exec(text(
        "SELECT COUNT(*), COALESCE(MAX(m.id), 0) FROM match m "
        "JOIN round r ON r.id = m.round_id WHERE r.competition_id = :cid"
    ), params={"cid": cid}).first()
    standings = session.exec(text(
        "SELECT COUNT(*), COALESCE(SUM(elo), 0), COALESCE(SUM(match_count), 0) "
        "FROM competition_participation WHERE competition_id = :cid"
    ), params={"cid": cid}).first()
    return (cid, tuple(matches or ()), tuple(standings or ()))


def similarity_data(session: Session) -> dict:
    """Scored pairs for the current season, cached on a data fingerprint.

    Double-checked locking, so a cold cache hit by several concurrent requests
    only triggers one rebuild."""
    key = _data_version(session)
    slot = _CACHE.setdefault(season_mod.cid(), {"key": None, "value": None})
    if slot["key"] == key and slot["value"] is not None:
        return slot["value"]
    with _CACHE_LOCK:
        if slot["key"] == key and slot["value"] is not None:
            return slot["value"]
        value = _build(session)
        slot["key"] = key
        slot["value"] = value
        return value


def warm_similarity() -> None:
    """Rebuild the cache if stale. Called by the sync job after each pass."""
    from ai_arena_recap.db import engine

    try:
        with Session(engine) as session:
            similarity_data(session)
    except Exception:
        log.exception("similarity cache warm failed")


# ---------------------------------------------------------------------------
# Views for the page
# ---------------------------------------------------------------------------

def _members(data: dict, race: str) -> list[int]:
    """Bots that belong in one race's matrix, strongest first.

    Random bots appear in every single-race matrix, since a Random bot plays all
    three and is comparable to any of them."""
    bots = data["bots"]
    members = [b for b in data["scored_bots"]
               if bots[b]["race"] == race or (race != "R" and bots[b]["race"] == "R")]
    members.sort(key=lambda b: -(bots[b]["elo"] or 0))
    return members


def races(session: Session) -> list[dict]:
    """Races with a matrix worth drawing, and how many bots each one shows.

    The count is the matrix size, not the number of bots of that race — a Zerg
    matrix also contains every Random bot, and a badge that disagreed with the
    row count would just look like a bug."""
    data = similarity_data(session)
    out = []
    for code, label in (("T", "Terran"), ("Z", "Zerg"), ("P", "Protoss"), ("R", "Random")):
        if not any(data["bots"][b]["race"] == code for b in data["scored_bots"]):
            continue
        members = _members(data, code)
        if len(members) >= 2:
            out.append({"code": code, "label": label, "count": len(members)})
    return out


def matrix(session: Session, race: str) -> dict:
    """Symmetric rho matrix for one race, ordered by ELO descending.

    Unscored cells come back as ``None`` so the client can leave them blank
    rather than render them as zero — "no data" and "no similarity" must not
    look the same.
    """
    data = similarity_data(session)
    bots = data["bots"]
    members = _members(data, race)
    pos = {b: i for i, b in enumerate(members)}

    size = len(members)
    grid = [[None] * size for _ in range(size)]
    meta = [[None] * size for _ in range(size)]
    for p in data["pairs"]:
        i, j = pos.get(p["a"]), pos.get(p["b"])
        if i is None or j is None:
            continue
        grid[i][j] = grid[j][i] = p["rho"]
        cell = {"conf": p["conf"], "sd": p["sd"], "n": p["n"], "games": p["games"]}
        meta[i][j] = meta[j][i] = cell
    return {
        "labels": [bots[b]["name"] for b in members],
        "bot_ids": members,
        "elos": [bots[b]["elo"] for b in members],
        "races": [bots[b]["race"] for b in members],
        "authors": [bots[b]["author"] for b in members],
        "z": grid,
        "meta": meta,
    }


def top_pairs(session: Session, limit: int = TOP_PAIRS) -> list[dict]:
    """Most similar pairs across the whole season, ranked by confidence."""
    data = similarity_data(session)
    bots = data["bots"]
    out = []
    for position, p in enumerate(data["pairs"][:limit], start=1):
        a, b = bots[p["a"]], bots[p["b"]]
        out.append({
            # The ranking made concrete, so the table has a real column to sort
            # on. Rows arrive ordered by P(rho > 0.5), which is deliberately not
            # a column — without this, restoring the default order after sorting
            # by anything else would be impossible.
            "rank": position,
            "a_id": a["bot_id"], "a_name": a["name"], "a_race": a["race"],
            "a_author": a["author"], "a_elo": a["elo"], "a_on_ladder": a["on_ladder"],
            "b_id": b["bot_id"], "b_name": b["name"], "b_race": b["race"],
            "b_author": b["author"], "b_elo": b["elo"], "b_on_ladder": b["on_ladder"],
            "rho": p["rho"], "conf": p["conf"], "sd": p["sd"],
            "n": p["n"], "games": p["games"],
            "same_author": bool(a["author"] and a["author"] == b["author"]),
        })
    return out
