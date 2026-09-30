"""Tests for server/graph_store.py's build_steps -- raw route legs into
turn-by-turn steps.

The failure modes pinned here are the ones real routes produce without
these rules: crossings shredding a 3km walk into 72 lines; a geometric
turn filter collapsing the Brooklyn Bridge walk to one step; corner
scraps injecting "turn left onto Court Street" mid-Court; a bare length
threshold that a 25m scrap defeats. Folding is evidence-based --
crossings fold by OSM's own label, unnamed scraps fold only where
naming.py's fold_names claims them, side switches need a physical crossing
-- and every rule has a test that fails if a threshold sneaks in.

Geometry: legs are built due east/north at real NYC latitude so bearings
are exact. Lengths are real metres via the same flat scale naming.py uses.
"""

import math

from server.graph_store import build_steps

BASE_LON, BASE_LAT = -73.99, 40.70
_K = 111_320.0 * math.cos(math.radians(40.7))
_LAT_M = 110_540.0


def _walk(start, heading, length_m):
    """Straight [lon, lat] pair list from start, due `heading`, length_m."""
    lon, lat = start
    if heading == "east":
        end = (lon + length_m / _K, lat)
    elif heading == "west":
        end = (lon - length_m / _K, lat)
    elif heading == "north":
        end = (lon, lat + length_m / _LAT_M)
    else:
        end = (lon, lat - length_m / _LAT_M)
    return [list(start), list(end)], end


def leg(name, length_m, coords, kind="footway/sidewalk", side="",
        fold_names=()):
    return {"name": name, "side": side, "kind": kind,
            "fold_names": tuple(fold_names), "length_m": float(length_m),
            "coords": coords}


def chain(*specs):
    """Connected legs from (name, heading, length_m, extras) tuples."""
    legs = []
    at = (BASE_LON, BASE_LAT)
    for name, heading, length_m, extras in specs:
        coords, at = _walk(at, heading, length_m)
        legs.append(leg(name, length_m, coords, **extras))
    return legs


def test_a_crossing_between_two_runs_of_one_street_folds_away():
    """The case: [Court] [8m crossing] [Court] is one Court Street step.
    By OSM's own crossing label -- no length involved, so a 30m avenue
    crossing folds identically."""
    legs = chain(("Court Street", "east", 60.0, {}),
                 ("", "east", 8.0, {"kind": "footway/crossing"}),
                 ("Court Street", "east", 60.0, {}))
    steps = build_steps(legs)
    assert len(steps) == 1
    assert steps[0]["action"] == "depart"
    assert steps[0]["name"] == "Court Street"
    assert steps[0]["heading"] == "east"
    assert steps[0]["length_m"] == 128.0


def test_a_wide_crossing_folds_just_the_same():
    legs = chain(("Court Street", "east", 60.0, {}),
                 ("", "east", 31.0, {"kind": "footway/crossing"}),
                 ("Court Street", "east", 60.0, {}))
    assert len(build_steps(legs)) == 1


def test_a_corner_crossing_joins_the_street_being_turned_onto():
    """Turning off Court onto Sackett across a crossing: the crossing is
    transition ground and its metres belong to the arrival step."""
    legs = chain(("Court Street", "east", 60.0, {}),
                 ("", "east", 8.0, {"kind": "footway/crossing"}),
                 ("Sackett Street", "north", 60.0, {}))
    steps = build_steps(legs)
    assert [s["name"] for s in steps] == ["Court Street", "Sackett Street"]
    assert steps[1]["action"] == "left"   # east -> north is a left turn
    assert steps[1]["length_m"] == 68.0


def test_turn_words_match_the_geometry():
    legs = chain(("Court Street", "east", 60.0, {}),
                 ("Sackett Street", "south", 60.0, {}))
    assert build_steps(legs)[1]["action"] == "right"
    legs = chain(("Court Street", "east", 60.0, {}),
                 ("Union Street", "east", 60.0, {}))
    assert build_steps(legs)[1]["action"] == "continue"


def test_an_unnamed_scrap_folds_only_where_fold_names_claims_it():
    """The rule instead of a length threshold. Same 25m scrap, same
    geometry: with Court among its plausible parents it vanishes into
    Court; without, it stands as an honest unnamed path -- whatever its
    length."""
    claimed = chain(("Court Street", "east", 60.0, {}),
                    ("", "east", 25.0, {"fold_names": ("Court Street",)}),
                    ("Court Street", "east", 60.0, {}))
    assert len(build_steps(claimed)) == 1

    unclaimed = chain(("Court Street", "east", 60.0, {}),
                      ("", "east", 25.0, {}),
                      ("Court Street", "east", 60.0, {}))
    steps = build_steps(unclaimed)
    assert [s["name"] for s in steps] == [
        "Court Street", "unnamed path", "Court Street"]


def test_a_long_claimed_connector_still_folds_and_a_short_unclaimed_one_never_does():
    """No length threshold anywhere: 69m folds when claimed, 3m stands
    when nothing claims it (a nameless nub between two different streets
    with no candidates)."""
    long_claimed = chain(("Court Street", "east", 60.0, {}),
                         ("", "east", 69.0, {"fold_names": ("Court Street",)}),
                         ("Court Street", "east", 60.0, {}))
    assert len(build_steps(long_claimed)) == 1

    short_unclaimed = chain(("Court Street", "east", 60.0, {}),
                            ("", "north", 3.0, {}),
                            ("Sackett Street", "north", 60.0, {}))
    steps = build_steps(short_unclaimed)
    assert [s["name"] for s in steps] == [
        "Court Street", "unnamed path", "Sackett Street"]


def test_distinct_named_streets_never_collapse():
    """The Brooklyn Bridge case: a chain of genuinely different named
    ways must keep every boundary (a geometric filter tight enough to
    drop kerb jogs collapses this 3.8km walk to one step)."""
    legs = chain(("City Hall Park Greenway", "east", 100.0, {}),
                 ("Centre Street", "east", 16.0, {}),
                 ("Brooklyn Bridge Promenade", "east", 1800.0, {}))
    assert [s["name"] for s in build_steps(legs)] == [
        "City Hall Park Greenway", "Centre Street",
        "Brooklyn Bridge Promenade"]


def test_a_side_switch_across_a_crossing_says_cross_side():
    legs = chain(("Court Street", "east", 60.0, {"side": "N"}),
                 ("", "south", 8.0, {"kind": "footway/crossing"}),
                 ("Court Street", "east", 60.0, {"side": "S"}))
    steps = build_steps(legs)
    assert len(steps) == 2
    assert steps[0]["side"] == "north"
    assert steps[1]["action"] == "cross_side"
    assert steps[1]["side"] == "south"


def test_a_side_flip_without_a_crossing_is_ignored():
    """You cannot switch sides of a roadway without crossing it, so a
    lone mis-computed side value cannot inject a phantom step."""
    legs = chain(("Court Street", "east", 60.0, {"side": "N"}),
                 ("Court Street", "east", 60.0, {"side": "S"}),
                 ("Court Street", "east", 60.0, {"side": "N"}))
    steps = build_steps(legs)
    assert len(steps) == 1
    assert steps[0]["side"] == "north"


def test_a_crossing_over_a_side_street_does_not_read_as_a_side_switch():
    """The everyday corner: cross Sackett while staying on Court's north
    side. The crossing arms the detector but the side never changes, so
    no cross_side step appears."""
    legs = chain(("Court Street", "east", 60.0, {"side": "N"}),
                 ("", "east", 8.0, {"kind": "footway/crossing"}),
                 ("Court Street", "east", 60.0, {"side": "N"}))
    steps = build_steps(legs)
    assert len(steps) == 1
    assert steps[0]["side"] == "north"


def test_a_crossings_own_name_never_forms_a_run():
    """Under parallel naming a crossing over Court gets called after the
    side street it is parallel to -- a labeling artifact. Its kind says
    what it is, and its name must not break the run it sits in."""
    legs = chain(("Court Street", "east", 60.0, {"side": "N"}),
                 ("Sackett Street", "south", 9.0,
                  {"kind": "footway/crossing"}),
                 ("Court Street", "east", 60.0, {"side": "S"}))
    steps = build_steps(legs)
    assert [s["action"] for s in steps] == ["depart", "cross_side"]


def test_leading_and_trailing_connectors_join_their_street():
    """A route that starts on a kerb ramp still departs 'on Court
    Street'."""
    legs = chain(("", "east", 4.0, {"kind": "footway/crossing"}),
                 ("Court Street", "east", 60.0, {}),
                 ("", "east", 4.0, {"kind": "footway/crossing"}))
    steps = build_steps(legs)
    assert len(steps) == 1
    assert steps[0]["length_m"] == 68.0


def test_zero_length_legs_are_dropped():
    legs = chain(("Court Street", "east", 60.0, {}),
                 ("Court Street", "east", 0.04, {}))
    assert build_steps(legs)[0]["length_m"] == 60.0


def test_depart_heading_is_eight_way():
    legs = chain(("Court Street", "north", 60.0, {}))
    assert build_steps(legs)[0]["heading"] == "north"
    legs = chain(("Court Street", "west", 60.0, {}))
    assert build_steps(legs)[0]["heading"] == "west"


def test_no_legs_no_steps():
    assert build_steps([]) == []


def test_a_parallel_corner_crossing_cannot_arm_a_side_switch():
    """6.78% of block boundaries shift the compass word (bends,
    near-diagonal tilts; measured 2026-08-28), and every corner has a
    crossing. Only a crossing across the path -- your own street's -- may
    arm the switch, or those boundaries each fire a phantom step."""
    legs = chain(("Court Street", "east", 60.0, {"side": "N"}),
                 ("", "east", 8.0, {"kind": "footway/crossing"}),
                 ("Court Street", "east", 60.0, {"side": "E"}))
    steps = build_steps(legs)
    assert len(steps) == 1
    assert steps[0]["side"] == ""  # no majority between N and E halves


def test_a_pieces_side_word_needs_a_length_majority():
    """A run that genuinely bends between words shows none rather than
    the first half's; a rogue sliver cannot outvote the real side."""
    legs = chain(("Court Street", "east", 90.0, {"side": "N"}),
                 ("Court Street", "east", 10.0, {"side": "E"}))
    assert build_steps(legs)[0]["side"] == "north"

    legs = chain(("Court Street", "east", 60.0, {"side": "N"}),
                 ("Court Street", "east", 60.0, {"side": "E"}))
    assert build_steps(legs)[0]["side"] == ""
