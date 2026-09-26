"""Tests for building shade on the server: the loader, the minute/day
blend, the union with trees, the layers switch, and /route's time
parameters (PLAN `building-shadows`, PR 2).

A synthetic export with three parallel ways between the same two nodes
-- a "north" way, a "middle" way and a "south" way, each two edges long
-- and NO trees, so every shade number here is building shade alone.
Rows are hand-written bytes, so each expected value is arithmetic on the
numbers in this file, not a re-run of the engine:

  north:   255 at July 09:00, 0 at July 16:00  (morning shade)
  south:   0 at July 09:00, 255 at July 16:00  (afternoon shade)
  middle:  no row at all (the export omits all-zero rows)
  plus the blend fixtures on the north way's first edge (see _ROWS).

What is pinned, and why:
  - minute 0 / day 15 reproduce a row EXACTLY; minute 30 is the plain
    average of two hours; day 30 of a 31-day month leans on the next
    month by 15/31; values move monotonically between anchors; 23:59
    blends into hour 0; Dec 31 blends into January. These are the
    "no hour-boundary jumps / no month-boundary jumps" design decisions.
  - trees and buildings combine by union, not by sum.
  - the hour flips the MED route: morning via north, afternoon via south.
  - shade priority NONE never changes with the clock (the shade-off
    invariant, extended to time).
  - no hour = trees only = the pre-shadow behaviour every existing test
    pins with `month=7` alone; an export without the field loads as
    all-zero; `layers` selects.
  - /route: NYC-time defaults, "month given -> day 15", validation of day
    against the month, hour, minute and layers, and the echo.
  - night (PLAN `night-shade`): a dark slot counts as FULL shade, so dusk
    climbs to 1.0 instead of falling to the stored 0; is_night needs
    every blended slot dark; /route at night hands every weight the one
    fastest route; no sun table, no night; the pinned test clock.
  - trees by the day (PLAN `tree-seasonal-blend`): CANOPY_BY_MONTH is
    each month's value on the 15th, blended between 15ths by the same
    day rule as the rows, so tree shade no longer jumps on the 1st;
    December blends into January; the day reaches route()'s shade. On a
    synthetic curve, so the real numbers stay a data choice in config.

The synthetic sun table is daylight in EVERY slot unless a test darkens
some (`dark=`), so the blend tests above read the stored rows untouched.
"""

import base64
import calendar
import gzip
import json
from datetime import datetime

import numpy as np
import pytest
from fastapi.testclient import TestClient

from pipeline import config
from server import app as server_app
from server.graph_store import SHADE_ANCHOR_DAY, GraphStore, _dark_slots

RATE = config.DENSITY_AT_FULL_COVERAGE

# Nodes: A (west) and B (east) 200 m apart on the equator-ish grid; three
# ways between them via a midpoint each, at different latitudes.
NODES = {
    "A": (-74.0000, 40.6800),
    "B": (-73.9976, 40.6800),      # ~200 m east
    "N": (-73.9988, 40.6809),      # north midpoint (~100 m north of the line)
    "M": (-73.9988, 40.6800),      # straight midpoint
    "S": (-73.9988, 40.6791),      # south midpoint
}
DETOUR_M = 141.0   # A->N and N->B legs; the middle legs are 100 m


def _slot(month, hour):
    return (month - 1) * 24 + hour


def _row(**slots) -> bytes:
    """_row(m7h9=255, m8h9=100) -> 288 bytes."""
    row = bytearray(288)
    for key, value in slots.items():
        month, hour = key[1:].split("h")
        row[_slot(int(month), int(hour))] = value
    return bytes(row)


# The north way's first edge carries every blend fixture; the second edge
# carries the plain morning row so the ROUTE choice is clean.
_ROWS = {
    ("A", "N"): _row(m7h9=255, m7h10=255, m7h16=0,        # morning
                     m7h13=200, m7h14=100,                  # minute blend: 13:30 -> 150
                     m8h13=40,                              # day blend: Jul 30 -> 200 + 15/31*(40-200)
                     m12h11=120, m1h11=60,                  # Dec 31 -> Jan
                     m7h23=255, m7h0=255,                   # 23:59 -> hour 0 wrap (synthetic)
                     m7h20=200),                            # dusk: lit, and 21:00 dark in the night tests
    ("N", "B"): _row(m7h9=255, m7h10=255),
    ("A", "S"): _row(m7h16=255, m7h17=255),
    ("S", "B"): _row(m7h16=255, m7h17=255),
}


def _edge(u, v, length_m, deciduous=0.0, shade: bytes | None = None) -> dict:
    record = {"u": u, "v": v, "key": 0, "side": "", "kind": "footway/sidewalk",
              "length_m": length_m, "name": f"{u}-{v}", "tree_deciduous": deciduous,
              "tree_evergreen": 0.0, "tree_count": 0.0, "tree_park_canopy": 0.0,
              "coords": [list(NODES[u]), list(NODES[v])]}
    if shade and any(shade):
        record["building_shade"] = base64.b64encode(shade).decode("ascii")
    return record


def _payload(with_rows=True, deciduous=0.0, north_deciduous=0.0, dark=(), with_sun_table=True):
    north = deciduous + north_deciduous
    edges = [_edge("A", "N", DETOUR_M, north, _ROWS[("A", "N")] if with_rows else None),
             _edge("N", "B", DETOUR_M, north, _ROWS[("N", "B")] if with_rows else None),
             _edge("A", "M", 100.0, deciduous),
             _edge("M", "B", 100.0, deciduous),
             _edge("A", "S", DETOUR_M, deciduous, _ROWS[("A", "S")] if with_rows else None),
             _edge("S", "B", DETOUR_M, deciduous, _ROWS[("S", "B")] if with_rows else None)]
    table = [[[180.0, 30.0] for _ in range(24)] for _ in range(12)]    # daylight everywhere
    table[6][9] = [90.0, 40.0]
    for month, hour in dark:
        table[month - 1][hour] = None
    meta = {"node_count": len(NODES), "edge_count": len(edges)}
    if with_sun_table:
        meta["sun_table"] = table
    return {"meta": meta, "nodes": {k: list(v) for k, v in NODES.items()}, "edges": edges}


def _store(tmp_path, monkeypatch, **kw) -> GraphStore:
    monkeypatch.setattr(config, "EXPORT_DIR", tmp_path)
    with gzip.open(tmp_path / "tile.json.gz", "wt") as fh:
        json.dump(_payload(**kw), fh)
    store = GraphStore()
    store.load()
    return store


def _idx(store, u, v) -> int:
    return store._names.index(f"{u}-{v}")


def _shade(store, u, v, **time) -> float:
    """Combined shaded fraction (0-1) of one edge at a time."""
    density = store._edge_density(**time)
    return float(density[_idx(store, u, v)] / RATE)


# ── loader ───────────────────────────────────────────────────────────────────

def test_loader_reads_rows_and_the_sun_table(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    assert store._building_shade.shape == (288, 6)
    assert store._building_shade[_slot(7, 9), _idx(store, "A", "N")] == 255
    assert store._building_shade[:, _idx(store, "A", "M")].max() == 0    # omitted row = zeros
    assert store._sun_table[6][9] == [90.0, 40.0]


def test_an_export_without_the_field_loads_as_all_zero(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch, with_rows=False)
    assert store._building_shade.shape == (288, 6)
    assert store._building_shade.max() == 0
    assert _shade(store, "A", "N", month=7, hour=9) == 0.0


# ── the blend ────────────────────────────────────────────────────────────────

def test_anchor_minute_and_day_reproduce_the_row_exactly(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    assert _shade(store, "A", "N", month=7, day=15, hour=13, minute=0) == pytest.approx(200 / 255)
    assert _shade(store, "A", "N", month=7, day=15, hour=9) == pytest.approx(1.0)


def test_half_past_is_the_plain_average_of_two_hours(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    assert _shade(store, "A", "N", month=7, hour=13, minute=30) == pytest.approx(150 / 255)
    assert _shade(store, "A", "N", month=7, hour=13, minute=45) == pytest.approx(125 / 255)


def test_day_30_of_july_leans_on_august_by_15_31(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    expected = 200 + 15 / 31 * (40 - 200)
    assert _shade(store, "A", "N", month=7, day=30, hour=13) == pytest.approx(expected / 255)


def test_values_move_monotonically_between_anchors(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    by_day = [_shade(store, "A", "N", month=7, day=d, hour=13) for d in range(15, 32)]
    by_day += [_shade(store, "A", "N", month=8, day=d, hour=13) for d in range(1, 16)]
    assert by_day[0] == pytest.approx(200 / 255) and by_day[-1] == pytest.approx(40 / 255)
    assert all(a >= b for a, b in zip(by_day, by_day[1:]))
    by_minute = [_shade(store, "A", "N", month=7, hour=13, minute=m) for m in range(60)]
    assert all(a >= b for a, b in zip(by_minute, by_minute[1:]))


def test_2359_blends_into_hour_0(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    # both synthetic rows are 255, so a wrap into hour 0 keeps 1.0; a wrap
    # into a nonexistent hour 24 would index the next month's row (0)
    assert _shade(store, "A", "N", month=7, hour=23, minute=59) == pytest.approx(1.0)


def test_dec_31_blends_into_january(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    expected = 120 + 16 / 31 * (60 - 120)          # Dec 31 is 16 days past the 15th
    assert _shade(store, "A", "N", month=12, day=31, hour=11) == pytest.approx(expected / 255)
    expected = 120 + 30 / 31 * (60 - 120)          # Jan 14 is 30 days past Dec 15
    assert _shade(store, "A", "N", month=1, day=14, hour=11) == pytest.approx(expected / 255)


# ── union with trees, the layers switch ──────────────────────────────────────

def test_trees_and_buildings_combine_by_union(tmp_path, monkeypatch):
    # a deciduous score giving exactly 0.5 tree cover in July (canopy 1.0)
    half = 0.5 * RATE * DETOUR_M
    store = _store(tmp_path, monkeypatch, deciduous=half)
    assert _shade(store, "A", "N", month=7, hour=16) == pytest.approx(0.5)        # trees only lit
    assert _shade(store, "A", "N", month=7, hour=9) == pytest.approx(1.0)         # 1-(1-.5)(1-1)
    assert _shade(store, "A", "N", month=7, hour=13) == pytest.approx(1 - 0.5 * (1 - 200 / 255))


def test_layers_switch_selects_trees_buildings_or_both(tmp_path, monkeypatch):
    half = 0.5 * RATE * DETOUR_M
    store = _store(tmp_path, monkeypatch, deciduous=half)
    assert _shade(store, "A", "N", month=7, hour=13, layers="trees") == pytest.approx(0.5)
    assert _shade(store, "A", "N", month=7, hour=13, layers="buildings") == pytest.approx(200 / 255)
    assert _shade(store, "A", "N", month=7, hour=13, layers="both") == pytest.approx(1 - 0.5 * (1 - 200 / 255))
    with pytest.raises(ValueError):
        store._edge_density(7, hour=13, layers="sun")


def test_no_hour_means_trees_only(tmp_path, monkeypatch):
    half = 0.5 * RATE * DETOUR_M
    store = _store(tmp_path, monkeypatch, deciduous=half)
    assert _shade(store, "A", "N", month=7) == pytest.approx(0.5)
    assert np.allclose(store.edge_costs(15.0, 7), store.edge_costs(15.0, 7, layers="trees", hour=13))


def test_adding_building_shade_never_lowers_any_edge(tmp_path, monkeypatch):
    """The union rule's one-way promise: for every edge and every moment,
    trees + buildings >= trees alone, and >= buildings alone. Both layers
    can only add. (A CHOSEN route at a higher priority may still trade a
    little tree shade for more building shade -- that is the router's
    business, not this rule's.)"""
    half = 0.5 * RATE * DETOUR_M
    store = _store(tmp_path, monkeypatch, deciduous=half)
    for month, day in ((7, 15), (7, 30), (1, 3), (12, 31)):
        trees = store._edge_density(month, day, layers="trees")
        for hour in range(24):
            for minute in (0, 30):
                both = store._edge_density(month, day, hour, minute)
                only_buildings = store._edge_density(month, day, hour, minute, layers="buildings")
                assert np.all(both >= trees - 1e-6)
                assert np.all(both >= only_buildings - 1e-6)


# ── trees by the day (PLAN `tree-seasonal-blend`) ────────────────────────────

# A synthetic curve, so these pin the blend, not config's real numbers.
# April -> May is the big step on purpose.
CURVE = [0.2, 0.2, 0.2, 0.4, 1.0, 1.0, 1.0, 1.0, 1.0, 0.8, 0.5, 0.3]


def _tree_store(tmp_path, monkeypatch) -> GraphStore:
    """Every edge's deciduous score gives the north way exactly 0.5 x the
    canopy factor: never capped, so each value below is plain arithmetic."""
    monkeypatch.setattr(config, "CANOPY_BY_MONTH", CURVE)
    return _store(tmp_path, monkeypatch, deciduous=0.5 * RATE * DETOUR_M)


def test_trees_on_the_15th_are_exactly_the_months_value(tmp_path, monkeypatch):
    store = _tree_store(tmp_path, monkeypatch)
    for month in range(1, 13):
        assert _shade(store, "A", "N", month=month, day=15) == pytest.approx(0.5 * CURVE[month - 1])


def test_trees_between_two_15ths_are_a_straight_line_mix(tmp_path, monkeypatch):
    store = _tree_store(tmp_path, monkeypatch)
    # April has 30 days: Apr 30 is 15/30 of the way from April 15 to May 15, May 1 is 16/30
    assert _shade(store, "A", "N", month=4, day=30) == pytest.approx(0.5 * (0.4 + 15 / 30 * (1.0 - 0.4)))
    assert _shade(store, "A", "N", month=5, day=1) == pytest.approx(0.5 * (0.4 + 16 / 30 * (1.0 - 0.4)))


def test_trees_no_longer_jump_on_the_1st(tmp_path, monkeypatch):
    """The step's point. A per-month lookup moved this edge 0.3 overnight
    on May 1 (0.5 x (1.0 - 0.4)); blended, no day-to-day change can exceed
    the biggest month-to-month step spread over the shortest month --
    including Dec 31 -> Jan 1."""
    store = _tree_store(tmp_path, monkeypatch)
    days = [(m, d) for m in range(1, 13) for d in range(1, calendar.monthrange(2026, m)[1] + 1)]
    shade = [_shade(store, "A", "N", month=m, day=d) for m, d in days]
    biggest_step = max(abs(b - a) for a, b in zip(CURVE, CURVE[1:] + CURVE[:1]))
    changes = [abs(b - a) for a, b in zip(shade, shade[1:] + shade[:1])]
    assert max(changes) <= 0.5 * biggest_step / 28 + 1e-6


def test_trees_blend_december_into_january(tmp_path, monkeypatch):
    store = _tree_store(tmp_path, monkeypatch)
    # Dec 31 is 16 days past Dec 15; Jan 14 is 30. December has 31 days.
    assert _shade(store, "A", "N", month=12, day=31) == pytest.approx(0.5 * (0.3 + 16 / 31 * (0.2 - 0.3)))
    assert _shade(store, "A", "N", month=1, day=14) == pytest.approx(0.5 * (0.3 + 30 / 31 * (0.2 - 0.3)))


def test_the_day_reaches_the_routes_shade(tmp_path, monkeypatch):
    """Through route(), the stat a person sees: the same walk on May 1
    reads the blended canopy, not May's."""
    store = _tree_store(tmp_path, monkeypatch)
    start, end = store.snap_pair(NODES["A"][1], NODES["A"][0], NODES["B"][1], NODES["B"][0])
    straight = 0.5 * DETOUR_M / 100.0       # the middle way's cover per unit of canopy
    for day, canopy in ((15, 1.0), (1, 0.4 + 16 / 30 * (1.0 - 0.4))):
        result = store.route(start, end, tree_weight=0.0, month=5, day=day)
        assert result["length_m"] == pytest.approx(200.0)
        assert result["shade_fraction"] == pytest.approx(straight * canopy, abs=1e-3)


# ── routing ──────────────────────────────────────────────────────────────────

def _route_names(store, weight, **time):
    start, end = store.snap_pair(NODES["A"][1], NODES["A"][0], NODES["B"][1], NODES["B"][0])
    result = store.route(start, end, tree_weight=weight, month=7, **time)
    return [leg["name"] for leg in result["segments"]] if "segments" in result else result, result


def test_the_hour_flips_the_shaded_route(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    _, morning = _route_names(store, 15.0, hour=9)
    _, afternoon = _route_names(store, 15.0, hour=16)
    _, noon = _route_names(store, 15.0, hour=12)
    assert morning["length_m"] == pytest.approx(2 * DETOUR_M, abs=0.2)
    assert afternoon["length_m"] == pytest.approx(2 * DETOUR_M, abs=0.2)
    assert noon["length_m"] == pytest.approx(200.0, abs=0.2)          # nothing shaded: shortest
    assert morning["shade_fraction"] == afternoon["shade_fraction"] == 1.0
    assert noon["shade_fraction"] == 0.0
    # and they are different ways, not the same detour twice
    assert morning["coords"] != afternoon["coords"]


def test_shade_priority_none_never_changes_with_the_clock(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    start, end = store.snap_pair(NODES["A"][1], NODES["A"][0], NODES["B"][1], NODES["B"][0])
    baseline = store.route(start, end, tree_weight=0.0, month=7)
    for time in ({"hour": 9}, {"hour": 16, "minute": 30}, {"month": 1, "day": 3, "hour": 12},
                 {"hour": 13, "layers": "buildings"}):
        kw = {"month": 7, **time}
        other = store.route(start, end, tree_weight=0.0, **kw)
        assert other["coords"] == baseline["coords"]
        assert other["length_m"] == baseline["length_m"]


# ── /route ───────────────────────────────────────────────────────────────────

@pytest.fixture
def client(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    monkeypatch.setattr(server_app, "store", store)
    return TestClient(server_app.app)


def _get(client, **params):
    base = {"from_lat": NODES["A"][1], "from_lon": NODES["A"][0],
            "to_lat": NODES["B"][1], "to_lon": NODES["B"][0]}
    return client.get("/route", params={**base, **params})


def test_route_echoes_the_time_and_layers(client):
    body = _get(client, month=7, day=20, hour=9, minute=15, layers="buildings").json()
    assert (body["month"], body["day"], body["hour"], body["minute"], body["layers"]) == (7, 20, 9, 15, "buildings")


def test_route_defaults_to_new_york_now(client):
    body = _get(client).json()
    now = datetime.now(server_app.NYC_TZ)
    assert (body["month"], body["day"]) == (now.month, now.day)
    assert 0 <= body["hour"] <= 23 and 0 <= body["minute"] <= 59
    assert body["layers"] == "both"


def test_route_month_without_day_gets_the_anchor_day(client):
    body = _get(client, month=7).json()
    assert body["day"] == SHADE_ANCHOR_DAY
    body = _get(client, month=7, hour=9).json()
    assert (body["day"], body["hour"], body["minute"]) == (SHADE_ANCHOR_DAY, 9, 0)


@pytest.mark.parametrize("params", [
    {"month": 13}, {"month": 2, "day": 30}, {"month": 4, "day": 31}, {"day": 0},
    {"hour": 24}, {"hour": -1}, {"minute": 60}, {"layers": "sun"},
])
def test_route_rejects_bad_time_parameters(client, params):
    assert _get(client, **params).status_code == 400


def test_route_uses_the_hour_it_is_given(client):
    morning = _get(client, month=7, hour=9, tree_weights=[15.0]).json()["routes"][0]["properties"]
    noon = _get(client, month=7, hour=12, tree_weights=[15.0]).json()["routes"][0]["properties"]
    assert morning["shade_fraction"] == 1.0
    assert noon["shade_fraction"] == 0.0


# ── night (PLAN `night-shade`) ───────────────────────────────────────────────

# July's real dark slots (pipeline/sun.py, on the 15th: 21:00-05:00), plus
# August's 20:00-23:00 (synthetic) so a late-July evening blends a lit
# slot with a dark one across the month line.
JULY_NIGHT = tuple([(7, h) for h in (21, 22, 23, 0, 1, 2, 3, 4, 5)]
                   + [(8, h) for h in (20, 21, 22, 23)])


def test_a_dark_slot_counts_as_full_shade(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch, dark=JULY_NIGHT)
    # the middle way has no row at all, so its shade at 02:00 is the night's
    assert _shade(store, "A", "M", month=7, hour=2) == 1.0
    # ...and it is the sun's doing: the trees-alone view has no night
    assert _shade(store, "A", "M", month=7, hour=2, layers="trees") == 0.0


def test_dusk_climbs_toward_full_shade(tmp_path, monkeypatch):
    """The bug this step found: the stored 0 of a dark slot pulled the
    last hour before dark toward NO shade (20:30 read 100, not 227.5)."""
    store = _store(tmp_path, monkeypatch, dark=JULY_NIGHT)
    assert _shade(store, "A", "N", month=7, hour=20) == pytest.approx(200 / 255)
    assert _shade(store, "A", "N", month=7, hour=20, minute=30) == pytest.approx((200 + 255) / 2 / 255)
    by_minute = [_shade(store, "A", "N", month=7, hour=20, minute=m) for m in range(60)]
    assert by_minute == sorted(by_minute)
    assert _shade(store, "A", "N", month=7, hour=21) == 1.0
    # across the month line: July 31 at 20:00 is 16/31 of August's dark 20:00
    assert _shade(store, "A", "M", month=7, day=31, hour=20) == pytest.approx(16 / 31)


def test_dawn_falls_from_full_shade(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch, dark=JULY_NIGHT)
    assert _shade(store, "A", "M", month=7, hour=5, minute=15) == pytest.approx(0.75)
    assert _shade(store, "A", "M", month=7, hour=6) == 0.0


@pytest.mark.parametrize("moment, night", [
    ((7, 15, 20, 59), False),    # 1/60 of lit 20:00 still counts
    ((7, 15, 21, 0), True),
    ((7, 15, 2, 30), True),
    ((7, 15, 5, 0), True),
    ((7, 15, 5, 1), False),      # 06:00 is lit
    ((7, 31, 20, 0), False),     # lit July 20:00 blended with dark August 20:00
    ((8, 15, 20, 0), True),      # August's own 20:00, alone
    ((7, 15, 12, 0), False),
])
def test_is_night_only_when_every_blended_slot_is_dark(tmp_path, monkeypatch, moment, night):
    store = _store(tmp_path, monkeypatch, dark=JULY_NIGHT)
    assert store.is_night(*moment) is night


def test_an_export_without_a_sun_table_has_no_night(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch, with_sun_table=False)
    assert store.is_night(7, 15, 2, 0) is False
    assert _shade(store, "A", "M", month=7, hour=2) == 0.0


def test_a_sun_table_of_the_wrong_shape_is_refused():
    with pytest.raises(ValueError):
        _dark_slots([[None] * 24] * 11)
    with pytest.raises(ValueError):
        _dark_slots([[None] * 23] * 12)


def test_route_at_night_is_the_fastest_route_for_every_weight(tmp_path, monkeypatch):
    """The live bug (2026-09-25, Fifth Ave at 02:00): MED/MAX still took a
    canopy detour in the dark. Trees fully shade the north way here, so by
    day MED and MAX take it; at 02:00 every weight gets the straight way."""
    store = _store(tmp_path, monkeypatch, dark=JULY_NIGHT, north_deciduous=RATE * DETOUR_M)
    monkeypatch.setattr(server_app, "store", store)
    client = TestClient(server_app.app)

    day = _get(client, month=7, hour=12).json()
    assert day["night"] is False
    assert day["routes"][3]["properties"]["length_m"] == pytest.approx(2 * DETOUR_M, abs=0.2)

    night = _get(client, month=7, hour=2).json()
    assert night["night"] is True
    props = [route["properties"] for route in night["routes"]]
    assert [p["tree_weight"] for p in props] == [0.0, 5.0, 15.0, 40.0]
    assert all(route["geometry"] == night["routes"][0]["geometry"] for route in night["routes"])
    assert props[0]["length_m"] == pytest.approx(200.0, abs=0.2)
    assert all(p["shade_fraction"] == 1.0 for p in props)

    # the trees-alone view keeps the trees' own night: MAX still detours
    trees = _get(client, month=7, hour=2, layers="trees").json()
    assert trees["night"] is False
    assert trees["routes"][3]["properties"]["length_m"] == pytest.approx(2 * DETOUR_M, abs=0.2)


def test_a_pinned_clock_stands_in_for_now(client, monkeypatch):
    monkeypatch.setattr(server_app, "PINNED_NOW", server_app.parse_pinned_now("2026-07-15T12:00"))
    body = _get(client).json()
    assert (body["month"], body["day"], body["hour"], body["minute"]) == (7, 15, 12, 0)
    assert _get(client, hour=2).json()["hour"] == 2        # a request's own time still wins


def test_the_pinned_clock_is_new_york_time():
    assert server_app.parse_pinned_now(None) is None
    assert server_app.parse_pinned_now("") is None
    noon = server_app.parse_pinned_now("2026-07-15T12:00")
    assert (noon.hour, noon.utcoffset().total_seconds()) == (12, -4 * 3600)
    # an offset is converted, not dropped: 16:00 UTC is noon in July's New York
    assert server_app.parse_pinned_now("2026-07-15T16:00+00:00").hour == 12
    with pytest.raises(ValueError):
        server_app.parse_pinned_now("noon")
