"""Tests for pipeline/graph/blockface.py -- which block face is this on?

Focused on `match_line_profile`, the rule scoring depends on. It replaced
`match_line` for scoring on 2026-08-24 because one answer per whole edge
cannot describe an edge that runs past several blocks -- and OSM draws a lot
of those. 28.3% of the city's sidewalk length is in pieces over 200m, and
the median-distance rule returned None for them, so the blocks they pass
kept their trees and lost their pavement.

Geometry here is synthetic but built at real NYC scale, in metres converted
through naming.py's own flat approximation -- the same one the index uses,
so a "2m offset" in a test is 2m to the code under test.
"""

import pytest

from pipeline import config
from pipeline.graph.blockface import BlockFaceIndex
from pipeline.graph.naming import _K, _LAT_M

# A patch of nothing in the harbour, so nothing here can be confused with a
# real place if these coordinates ever surface in a debug print.
ORIGIN_LON, ORIGIN_LAT = -74.0500, 40.6000


def at(east_m, north_m):
    """A [lon, lat] that many metres east/north of the origin."""
    return [ORIGIN_LON + east_m / _K, ORIGIN_LAT + north_m / _LAT_M]


def kerb(face_id, start_m, end_m, feat_code="2260", conflated="1"):
    """A straight kerb line running east along y=0, from start_m to end_m."""
    return {
        "blockf_id": face_id,
        "conflated": conflated,
        "feat_code": feat_code,
        "the_geom": {"type": "LineString",
                     "coordinates": [at(start_m, 0.0), at(end_m, 0.0)]},
    }


def cscl(face_id, segment="900", side_field="l_blockfaceid"):
    return {side_field: face_id, "physicalid": segment,
            "full_street_name": "COURT ST", "boroughcode": "3",
            "streetwidth": "32"}


def sidewalk(start_m, end_m, offset_m=2.0):
    """A line running east, parallel to the kerbs, offset_m north of them."""
    return [at(start_m, offset_m), at(end_m, offset_m)]


def index_of(pave_rows, cscl_rows):
    return BlockFaceIndex(pave_rows, cscl_rows)


# --- the ordinary case -------------------------------------------------

def test_a_line_beside_one_kerb_puts_all_its_length_on_that_face():
    index = index_of([kerb("F1", 0, 100)], [cscl("F1")])
    profile = index.match_line_profile(sidewalk(0, 100))
    assert set(profile) == {"F1"}
    assert profile["F1"] == pytest.approx(100.0, rel=0.02)


def test_a_line_far_from_any_kerb_matches_nothing():
    index = index_of([kerb("F1", 0, 100)], [cscl("F1")])
    assert index.match_line_profile(sidewalk(0, 100, offset_m=40.0)) == {}


def test_a_degenerate_line_matches_nothing():
    index = index_of([kerb("F1", 0, 100)], [cscl("F1")])
    point = at(10.0, 2.0)
    assert index.match_line_profile([point, list(point)]) == {}


# --- the long-edge case, which is why this function exists -------------

def test_a_line_spanning_two_faces_is_split_between_them():
    """THE BUG THIS FIXES. match_line returns None for this line; the
    profile gives each block the pavement actually beside it."""
    index = index_of([kerb("F1", 0, 100), kerb("F2", 100, 200)],
                     [cscl("F1"), cscl("F2", segment="901")])

    profile = index.match_line_profile(sidewalk(0, 200))

    assert set(profile) == {"F1", "F2"}
    assert profile["F1"] == pytest.approx(100.0, rel=0.05)
    assert profile["F2"] == pytest.approx(100.0, rel=0.05)


def test_the_old_median_rule_really_does_fail_on_that_same_line():
    """Pins the reason for the change rather than asserting it in prose.
    A 500m way beside a 100m block sits >5m from that kerb for most of its
    length, so the median exceeds BLOCK_FACE_MAX_M and nothing matches."""
    index = index_of([kerb("F1", 0, 100)], [cscl("F1")])
    long_line = sidewalk(0, 500)

    assert index.match_line(long_line) is None          # the old rule
    assert index.match_line_profile(long_line)          # the new one works


def test_an_unevenly_spanning_line_is_credited_in_proportion():
    index = index_of([kerb("F1", 0, 150), kerb("F2", 150, 200)],
                     [cscl("F1"), cscl("F2", segment="901")])
    profile = index.match_line_profile(sidewalk(0, 200))
    assert profile["F1"] > profile["F2"] * 2


# --- conservation ------------------------------------------------------

def test_the_profile_never_credits_more_than_the_lines_own_length():
    """Trees are divided by these metres, so over-crediting would deflate a
    block's density -- silently, and everywhere."""
    index = index_of([kerb("F1", 0, 100), kerb("F2", 100, 200)],
                     [cscl("F1"), cscl("F2", segment="901")])
    profile = index.match_line_profile(sidewalk(0, 200))
    assert sum(profile.values()) <= 200.0 * 1.001


def test_a_line_only_partly_beside_a_kerb_credits_only_that_part():
    """The other half is beside nothing and is credited to nothing, rather
    than inheriting the neighbouring block's shade."""
    index = index_of([kerb("F1", 0, 100)], [cscl("F1")])
    profile = index.match_line_profile(sidewalk(0, 200))
    assert profile["F1"] == pytest.approx(100.0, rel=0.05)


# --- what is excluded from the index -----------------------------------

def test_alleys_are_not_block_faces():
    """feat_code 2270. Alleys have no sidewalk, and leaving them in made
    ALLEY top the list of streets with no sidewalk -- never a real finding."""
    index = index_of([kerb("F1", 0, 100, feat_code="2270")], [cscl("F1")])
    assert index.match_line_profile(sidewalk(0, 100)) == {}


def test_airport_runways_are_not_block_faces():
    index = index_of([kerb("F1", 0, 100, feat_code="2230")], [cscl("F1")])
    assert index.match_line_profile(sidewalk(0, 100)) == {}


def test_a_kerb_nyc_could_not_conflate_is_not_used():
    """`conflated` is NYC's own flag for whether the kerb -> block-face link
    succeeded. Without it we would score links the source itself disowns."""
    index = index_of([kerb("F1", 0, 100, conflated="0")], [cscl("F1")])
    assert index.match_line_profile(sidewalk(0, 100)) == {}


def test_a_blockface_id_that_cscl_cannot_resolve_is_not_used():
    index = index_of([kerb("F1", 0, 100)], [cscl("F9")])
    assert index.match_line_profile(sidewalk(0, 100)) == {}


# --- the step size -----------------------------------------------------

def test_a_finer_step_does_not_change_the_answer_materially():
    """2m was chosen because it lands within 0.5% of a 1m reference. If that
    stopped being true the sampling would be resolution-dependent."""
    index = index_of([kerb("F1", 0, 100), kerb("F2", 100, 200)],
                     [cscl("F1"), cscl("F2", segment="901")])
    line = sidewalk(0, 200)

    coarse = index.match_line_profile(line, step_m=2.0)
    fine = index.match_line_profile(line, step_m=1.0)

    assert set(coarse) == set(fine)
    for face_id, metres in fine.items():
        assert coarse[face_id] == pytest.approx(metres, rel=0.02)


def test_the_default_step_is_the_configured_one():
    index = index_of([kerb("F1", 0, 100)], [cscl("F1")])
    line = sidewalk(0, 100)
    assert (index.match_line_profile(line)
            == index.match_line_profile(
                line, step_m=config.BLOCK_FACE_SAMPLE_STEP_M))


def test_an_edge_shorter_than_one_step_is_still_sampled_once():
    """Half of all sidewalk edges are under 5m. At a 2m step a 1.2m edge
    must still be measured, not rounded out of existence."""
    index = index_of([kerb("F1", 0, 100)], [cscl("F1")])
    profile = index.match_line_profile(sidewalk(10.0, 11.2))
    assert profile["F1"] == pytest.approx(1.2, rel=0.05)
