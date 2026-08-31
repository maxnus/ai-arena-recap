"""Tests for the play-style similarity model and page.

The model's job is to measure *style*, not strength, so most of these tests are
built around bots with deliberately equal ELO: anything that scores them as
similar purely for being equally strong is a bug (see docs/similarity-model.md).
"""
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from ai_arena_recap.config import settings
from ai_arena_recap.models import (
    Bot,
    Competition,
    CompetitionParticipation,
    Map,
    Match,
    MatchParticipation,
    Round,
)
from ai_arena_recap.sync.common import upsert
from ai_arena_recap.web import similarity
from ai_arena_recap.web.deps import WEB_DIR
from ai_arena_recap.web.routes import similarity as similarity_route

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
COMP = settings.competition_id
OPPONENTS = list(range(100, 116))  # 16 filler opponents, comfortably over MIN_COMMON


@pytest.fixture(autouse=True)
def _clear_cache():
    similarity._CACHE.clear()
    yield
    similarity._CACHE.clear()


@pytest.fixture()
def client(engine):
    app = FastAPI()
    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")
    app.include_router(similarity_route.router)
    return TestClient(app)


def _seed_base(session):
    upsert(session, Competition, {"id": COMP, "name": "Test Cup", "last_synced": NOW})
    upsert(session, Map, {"id": 1, "name": "Acropolis", "last_synced": NOW})
    upsert(session, Round, {
        "id": 1, "number": 1, "competition_id": COMP, "complete": True, "last_synced": NOW,
    })


def _seed_bot(session, bot_id, name, race="T", *, elo=1600, user="alice", division=1, active=True):
    upsert(session, Bot, {
        "id": bot_id, "name": name, "plays_race": race, "user_name": user, "last_synced": NOW,
    })
    upsert(session, CompetitionParticipation, {
        "id": bot_id, "competition_id": COMP, "bot_id": bot_id, "elo": elo,
        "highest_elo": elo, "division_num": division, "active": active, "last_synced": NOW,
    })


_next_match = [1]


def _play(session, a, b, *, a_wins, games=5):
    """`games` matches between a and b, all with the same winner."""
    for _ in range(games):
        mid = _next_match[0]
        _next_match[0] += 1
        upsert(session, Match, {
            "id": mid, "round_id": 1, "map_id": 1, "started": NOW, "last_synced": NOW,
        })
        upsert(session, MatchParticipation, {
            "id": mid * 2 - 1, "match_id": mid, "bot_id": a, "participant_number": 1,
            "result": "win" if a_wins else "loss", "last_synced": NOW,
        })
        upsert(session, MatchParticipation, {
            "id": mid * 2, "match_id": mid, "bot_id": b, "participant_number": 2,
            "result": "loss" if a_wins else "win", "last_synced": NOW,
        })


def _seed_opponents(session):
    for i, oid in enumerate(OPPONENTS):
        _seed_bot(session, oid, f"Opp{i}", "T", elo=1600, user="filler")
    _seed_background(session)


def _seed_background(session, count=6):
    """A varied field, without which the model is right to see nothing.

    Double-centring removes the opponent main effect, and that effect is only
    identifiable if several bots have played each opponent. With just two bots in
    a column, "everyone over-performs against this opponent" and "these two
    over-perform against it" are the same statement, so a shared deviation is
    correctly cancelled. Real seasons have hundreds of bots per column; these
    stand in for them."""
    for k in range(count):
        bot_id = 200 + k
        _seed_bot(session, bot_id, f"Field{k}", "T", elo=1600, user=f"field{k}")
        for i, oid in enumerate(OPPONENTS):
            _play(session, bot_id, oid, a_wins=((i * 3 + k * 5) % 7 < 3))


def _seed_pattern(session, bot_id, name, beats, race="T", **kw):
    """A bot that beats exactly the opponents whose index is in `beats`."""
    _seed_bot(session, bot_id, name, race, **kw)
    for i, oid in enumerate(OPPONENTS):
        _play(session, bot_id, oid, a_wins=(i in beats))


def _text(response) -> str:
    """Page text with runs of whitespace collapsed.

    Copy assertions below pin what the page *claims*; without this they also pin
    where the template happens to wrap, and re-flowing a paragraph fails them for
    no reason."""
    return " ".join(response.text.split())


def _pair(data, a, b):
    for p in data["pairs"]:
        if {p["a"], p["b"]} == {a, b}:
            return p
    return None


def test_identical_style_scores_high(session):
    """Two bots that beat and lose to exactly the same opponents."""
    _seed_base(session)
    _seed_opponents(session)
    half = set(range(8))
    _seed_pattern(session, 1, "Twin A", half)
    _seed_pattern(session, 2, "Twin B", half)
    session.commit()

    pair = _pair(similarity.similarity_data(session), 1, 2)
    assert pair is not None
    assert pair["rho"] > 0.8, pair
    assert pair["conf"] > 0.9, pair
    assert pair["n"] == len(OPPONENTS)


def test_opposite_style_scores_negative(session):
    _seed_base(session)
    _seed_opponents(session)
    _seed_pattern(session, 1, "Alpha", set(range(8)))
    _seed_pattern(session, 2, "Mirror", set(range(8, 16)))
    session.commit()

    pair = _pair(similarity.similarity_data(session), 1, 2)
    assert pair is not None
    assert pair["rho"] < -0.5, pair
    assert pair["conf"] < 0.1, pair


def test_equal_strength_but_different_style_is_not_similar(session):
    """The confound this whole model exists to avoid.

    Both bots win exactly half their games and carry the same ELO, but they beat
    *different* halves of the field. A win-rate comparison would call them
    identical; a style comparison must not."""
    _seed_base(session)
    _seed_opponents(session)
    _seed_pattern(session, 1, "Even A", {0, 2, 4, 6, 8, 10, 12, 14})
    _seed_pattern(session, 2, "Odd B", {1, 3, 5, 7, 9, 11, 13, 15})
    session.commit()

    pair = _pair(similarity.similarity_data(session), 1, 2)
    assert pair is not None
    assert pair["rho"] < 0.0, pair
    assert pair["conf"] < 0.1, pair


def test_too_few_common_opponents_is_not_scored(session):
    _seed_base(session)
    _seed_opponents(session)
    _seed_pattern(session, 1, "Alpha", set(range(8)))
    # Bot 2 meets only the first four opponents — under MIN_COMMON.
    _seed_bot(session, 2, "Sparse")
    for i in range(4):
        _play(session, 2, OPPONENTS[i], a_wins=True)
    session.commit()

    assert _pair(similarity.similarity_data(session), 1, 2) is None


def test_cross_race_pairs_are_skipped_but_random_is_compared(session):
    """A Random bot dispatches per race, so it is comparable to everything."""
    _seed_base(session)
    _seed_opponents(session)
    half = set(range(8))
    _seed_pattern(session, 1, "Terran One", half, race="T")
    _seed_pattern(session, 2, "Zerg One", half, race="Z")
    _seed_pattern(session, 3, "Roller", half, race="R")
    session.commit()

    data = similarity.similarity_data(session)
    assert _pair(data, 1, 2) is None, "different races must not be compared"
    assert _pair(data, 1, 3) is not None, "Random must be comparable to Terran"
    assert _pair(data, 2, 3) is not None, "Random must be comparable to Zerg"


def test_bots_removed_from_the_ladder_are_still_scored(session):
    """Deliberate deviation from every other page's ladder scoping.

    aiarena resets a removed bot's division to 0, so the usual ladder filter
    would hide exactly the bots this page exists to surface."""
    _seed_base(session)
    _seed_opponents(session)
    half = set(range(8))
    _seed_pattern(session, 1, "Still Here", half)
    _seed_pattern(session, 2, "Removed", half, division=0, active=False)
    session.commit()

    data = similarity.similarity_data(session)
    assert _pair(data, 1, 2) is not None
    assert data["bots"][2]["on_ladder"] is False
    assert data["bots"][1]["on_ladder"] is True


def test_matrix_orders_by_elo_and_leaves_unscored_cells_blank(session):
    _seed_base(session)
    _seed_opponents(session)
    half = set(range(8))
    _seed_pattern(session, 1, "Weaker", half, elo=1500)
    _seed_pattern(session, 2, "Stronger", half, elo=1900)
    session.commit()

    m = similarity.matrix(session, "T")
    assert m["elos"] == sorted(m["elos"], reverse=True), "strongest first"
    assert m["labels"][0] == "Stronger" and m["labels"][-1] == "Weaker"

    i, j = m["labels"].index("Stronger"), m["labels"].index("Weaker")
    assert m["z"][i][i] is None, "a bot has no similarity to itself"
    assert m["z"][i][j] is not None and m["z"][i][j] == m["z"][j][i], "matrix is symmetric"


def test_cache_is_reused_until_the_data_changes(session):
    _seed_base(session)
    _seed_opponents(session)
    _seed_pattern(session, 1, "Alpha", set(range(8)))
    _seed_pattern(session, 2, "Beta", set(range(8)))
    session.commit()

    first = similarity.similarity_data(session)
    assert similarity.similarity_data(session) is first

    _seed_pattern(session, 3, "Gamma", set(range(4)))
    session.commit()
    assert similarity.similarity_data(session) is not first


def test_empty_database_does_not_blow_up(session):
    _seed_base(session)
    session.commit()
    data = similarity.similarity_data(session)
    assert data["pairs"] == []
    assert similarity.races(session) == []


def test_page_and_endpoints(client, session):
    _seed_base(session)
    _seed_opponents(session)
    half = set(range(8))
    _seed_pattern(session, 1, "Twin A", half)
    _seed_pattern(session, 2, "Twin B", half)
    session.commit()

    page = client.get("/similarity")
    assert page.status_code == 200
    assert "Play-style similarity" in page.text
    # The caveat is not decoration — it is what keeps the page from reading as
    # an accusation, so a template edit that drops it should fail the build.
    assert "not as an accusation" in _text(page)

    pairs = client.get("/api/similarity/pairs.json").json()["data"]
    assert any({p["a_name"], p["b_name"]} == {"Twin A", "Twin B"} for p in pairs)

    matrix = client.get("/api/similarity/matrix.json?race=T")
    assert matrix.status_code == 200
    assert "Twin A" in matrix.json()["labels"]

    assert client.get("/api/similarity/matrix.json?race=Z").status_code == 404


def test_race_badge_count_matches_matrix_size(session):
    """A Random bot appears in every single-race matrix, so the tab badge has to
    count the matrix rows, not the bots of that race."""
    _seed_base(session)
    _seed_opponents(session)
    half = set(range(8))
    _seed_pattern(session, 1, "Terran One", half, race="T")
    _seed_pattern(session, 2, "Roller", half, race="R")
    session.commit()

    for entry in similarity.races(session):
        assert entry["count"] == len(similarity.matrix(session, entry["code"])["labels"]), entry


def test_race_filters_are_ordered_terran_zerg_protoss_random(session):
    _seed_base(session)
    _seed_opponents(session)
    half = set(range(8))
    for i, race in enumerate("TZPR", start=1):
        _seed_pattern(session, i, f"Bot{race}", half, race=race)
    # A Random-only matrix needs two Random bots to be worth drawing at all.
    _seed_pattern(session, 5, "BotR2", half, race="R")
    session.commit()

    assert [r["code"] for r in similarity.races(session)] == ["T", "Z", "P", "R"]


def test_matrix_carries_authors_for_the_hover(session):
    _seed_base(session)
    _seed_opponents(session)
    half = set(range(8))
    _seed_pattern(session, 1, "Mine", half, user="alice")
    _seed_pattern(session, 2, "Theirs", half, user="bob")
    session.commit()

    m = similarity.matrix(session, "T")
    assert len(m["authors"]) == len(m["labels"])
    by_name = dict(zip(m["labels"], m["authors"]))
    assert by_name["Mine"] == "alice" and by_name["Theirs"] == "bob"


def test_matrix_fade_is_keyed_on_spread_not_on_p(client, session):
    """The veil must key on the posterior spread. Keyed on P(rho > 0.5) it erased
    every negative correlation, however well established, since P is ~0 for all
    of them. No legend explains the fade on the page any more, so this test is
    the only thing holding the encoding in place."""
    _seed_base(session)
    _seed_opponents(session)
    half = set(range(8))
    _seed_pattern(session, 1, "Twin A", half)
    _seed_pattern(session, 2, "Twin B", half)
    session.commit()

    page = client.get("/similarity")
    assert page.status_code == 200
    assert "SD_REF" in page.text, "fade must be keyed on posterior spread"
    assert "c.sd : SD_REF" in page.text
    assert "1 - (c ? c.conf" not in page.text, "keying the fade on P erases negatives"


def test_page_frames_the_same_author_scale_factually(client, session):
    """Splitting the scale risks the opposite problem: if same-author pairs are
    visibly de-emphasised, the remaining red cells start to read as an
    accusation. The wording that guards against that has to stay."""
    _seed_base(session)
    _seed_opponents(session)
    half = set(range(8))
    _seed_pattern(session, 1, "Mine A", half, user="alice")
    _seed_pattern(session, 2, "Mine B", half, user="alice")
    session.commit()

    page = client.get("/similarity")
    assert page.status_code == 200
    assert "Same author" in _text(page)
    assert "different authors" in _text(page)
    # The long note under the key was removed as redundant; the caveat box is
    # what now carries the framing, and it must not follow it out.
    assert "not as an accusation" in _text(page)
    assert "shared open-source template" in _text(page)


def test_pairs_table_columns_and_same_author_toggle(client, session):
    """Column order, headings and the same-author toggle are all things a
    template edit could quietly drop."""
    _seed_base(session)
    _seed_opponents(session)
    half = set(range(8))
    _seed_pattern(session, 1, "Mine A", half, user="alice")
    _seed_pattern(session, 2, "Mine B", half, user="alice")
    session.commit()

    page = client.get("/similarity")
    assert page.status_code == 200
    # Correlation is displayed before Confidence, and neither is abbreviated.
    assert 'headerName: "Correlation"' in page.text
    assert 'headerName: "Conf."' not in page.text
    # "Confidence" implies certainty about the estimate, which this is not.
    assert 'headerName: "Confidence"' not in page.text
    # P(rho>0.5) still orders the rows, but as a comparator on Correlation
    # rather than a column of its own — it is a squash of the two shown.
    assert 'headerName: "P(ρ > 0.5)"' not in page.text
    # The ranking statistic is not a column, so a real rank column carries the
    # default order — otherwise sorting by anything else is a one-way door.
    assert 'headerName: "#"' in page.text
    assert 'field: "rank"' in page.text
    assert "nodeA.data.conf" not in page.text, "Correlation must sort by correlation"
    assert 'headerName: "Spread"' in page.text
    # "At least" was dropped as redundant with Spread (corr -0.81); Spread reads
    # directly as a certainty, which is what the column is for.
    assert 'headerName: "At least"' not in page.text
    # Each ELO column names the bot it belongs to.
    assert 'headerName: "ELO A"' in page.text and 'headerName: "ELO B"' in page.text
    assert 'headerName: "Author(s)"' in page.text
    # The author is named even when both bots share one.
    assert "(same author)" in _text(page)
    assert 'id="sim-hide-same"' in page.text


def test_pairs_are_scored_even_when_the_two_bots_never_played_each_other(session):
    """A cell compares two bots against their *shared opponents*; the head-to-head
    is irrelevant and is explicitly excluded from the common set. On real data
    most scored pairs have never met, so this is the common case, not an edge."""
    _seed_base(session)
    _seed_opponents(session)
    half = set(range(8))
    _seed_pattern(session, 1, "Never Met A", half)
    _seed_pattern(session, 2, "Never Met B", half)
    session.commit()

    data = similarity.similarity_data(session)
    pair = _pair(data, 1, 2)
    assert pair is not None, "bots that never faced each other must still be scored"
    assert pair["n"] == len(OPPONENTS)


def test_page_quotes_the_threshold_the_model_actually_enforces(client, session):
    _seed_base(session)
    _seed_opponents(session)
    half = set(range(8))
    _seed_pattern(session, 1, "Twin A", half)
    _seed_pattern(session, 2, "Twin B", half)
    session.commit()

    page = client.get("/similarity")
    assert f"fewer than {similarity.MIN_COMMON} opponents in common" in _text(page)
    # The old wording implied the pair had to have played each other.
    assert "have not met often enough" not in _text(page)


# Every field the page reads. Kept as an explicit list because the failure mode
# is silent: a missing key renders as an empty cell or NaN rather than raising,
# so only an assertion catches it.
PAIR_FIELDS = {"rank", "a_id", "a_name", "a_race", "a_author", "a_elo", "a_on_ladder",
               "b_id", "b_name", "b_race", "b_author", "b_elo", "b_on_ladder",
               "rho", "conf", "sd", "n", "games", "same_author"}
CELL_FIELDS = {"conf", "sd", "n", "games"}


def test_api_payloads_carry_every_field_the_page_reads(client, session):
    _seed_base(session)
    _seed_opponents(session)
    half = set(range(8))
    _seed_pattern(session, 1, "Twin A", half)
    _seed_pattern(session, 2, "Twin B", half)
    session.commit()

    rows = client.get("/api/similarity/pairs.json").json()["data"]
    assert rows
    for row in rows:
        missing = PAIR_FIELDS - set(row)
        assert not missing, f"pairs.json row missing {missing}"
        assert all(row[k] is not None for k in ("rho", "conf", "sd"))

    m = client.get("/api/similarity/matrix.json?race=T").json()
    cells = [c for row in m["meta"] for c in row if c]
    assert cells
    for cell in cells:
        missing = CELL_FIELDS - set(cell)
        assert not missing, f"matrix cell missing {missing}"
        assert all(cell[k] is not None for k in ("conf", "sd"))


def test_negative_correlations_are_not_faded_away(session):
    """Half of all pairs are negative, and a well-established negative is a real
    finding (two bots countered by opposite things). Keying the fade on
    P(rho > 0.5) erased them, since that is ~0 for every negative value however
    solid; the fade must key on the posterior spread instead."""
    _seed_base(session)
    _seed_opponents(session)
    _seed_pattern(session, 1, "Alpha", set(range(8)))
    _seed_pattern(session, 2, "Mirror", set(range(8, 16)))
    session.commit()

    pair = _pair(similarity.similarity_data(session), 1, 2)
    assert pair["rho"] < -0.5, pair
    # A strongly negative pair backed by every opponent must be *precisely*
    # estimated, so a spread-keyed fade leaves it visible.
    assert pair["sd"] < 0.25, pair
    assert pair["conf"] < 0.05, "and P(rho>0.5) is ~0, which is why it must not drive the fade"


def test_matrix_scales_are_sequential_and_start_at_zero(client, session):
    """The page hunts bots that play alike, so the whole ramp is spent on
    positive correlation. Negative rho clamps to the base colour rather than
    taking half the scale, and the same-author ramp uses blue — which only works
    because blue is no longer carrying negative values."""
    _seed_base(session)
    _seed_opponents(session)
    half = set(range(8))
    _seed_pattern(session, 1, "Twin A", half)
    _seed_pattern(session, 2, "Twin B", half)
    session.commit()

    page = client.get("/similarity")
    assert page.status_code == 200
    assert "zmin: 0, zmax: 1" in page.text
    assert "zmid" not in page.text, "a sequential scale has no midpoint"
    # Both ramps share the base colour at rho 0 and diverge as rho climbs.
    assert '[0.00, "#3d4450"]' in page.text
    assert '[1.00, "#e05c4f"]' in page.text   # cross-author, red
    assert '[1.00, "#3987e5"]' in page.text   # same-author, blue
    # The sign is no longer coloured, but the hover still reports the true value.
    assert "style correlation" in page.text


def test_rank_column_reproduces_the_default_order(client, session):
    """Sorting by any other column has to be reversible."""
    _seed_base(session)
    _seed_opponents(session)
    half = set(range(8))
    _seed_pattern(session, 1, "Twin A", half)
    _seed_pattern(session, 2, "Twin B", half)
    _seed_pattern(session, 3, "Odd One", {1, 3, 5, 7})
    session.commit()

    rows = client.get("/api/similarity/pairs.json").json()["data"]
    assert [r["rank"] for r in rows] == list(range(1, len(rows) + 1))
    # Rank 1 is the pair the model ranks first, not merely the highest rho.
    assert rows[0]["rank"] == 1
    assert all(rows[i]["conf"] >= rows[i + 1]["conf"] for i in range(len(rows) - 1))


def test_single_game_cells_are_excluded(session):
    """A one-game record is not weak evidence, it is biased evidence.

    The Haldane-corrected log-odds of a 1-game cell is capped at ±1.10, but the
    ELO expectation against a much stronger opponent can be −5.65, so the
    residual lands hugely positive whichever way the game went. That bias is not
    absorbed by the `u` weighting, which only handles variance — it produced a
    spurious rho of +0.77 between bots 1154 ELO apart."""
    _seed_base(session)
    _seed_opponents(session)
    half = set(range(8))
    _seed_pattern(session, 1, "Solid", half)
    # Bot 2 meets every opponent, but only once each.
    _seed_bot(session, 2, "One Shot Each")
    for i, oid in enumerate(OPPONENTS):
        _play(session, 2, oid, a_wins=(i in half), games=1)
    session.commit()

    data = similarity.similarity_data(session)
    assert _pair(data, 1, 2) is None, "cells of a single game must not be scored"
    # The same bot with two games per opponent is scored normally.
    _seed_bot(session, 3, "Two Each")
    for i, oid in enumerate(OPPONENTS):
        _play(session, 3, oid, a_wins=(i in half), games=2)
    session.commit()
    similarity._CACHE.clear()
    assert _pair(similarity.similarity_data(session), 1, 3) is not None
