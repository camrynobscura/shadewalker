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
"""

import base64
import gzip
import json
from datetime import datetime

import numpy as np
import pytest
from fastapi.testclient import TestClient

from pipeline import config
from server import app as server_app
from server.graph_store import SHADE_ANCHOR_DAY, GraphStore

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
                     m7h23=255, m7h0=255),                  # 23:59 -> hour 0 wrap (synthetic)
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


def _payload(with_rows=True, deciduous=0.0):
    edges = [_edge("A", "N", DETOUR_M, deciduous, _ROWS[("A", "N")] if with_rows else None),
             _edge("N", "B", DETOUR_M, deciduous, _ROWS[("N", "B")] if with_rows else None),
             _edge("A", "M", 100.0, deciduous),
             _edge("M", "B", 100.0, deciduous),
             _edge("A", "S", DETOUR_M, deciduous, _ROWS[("A", "S")] if with_rows else None),
             _edge("S", "B", DETOUR_M, deciduous, _ROWS[("S", "B")] if with_rows else None)]
    table = [[None] * 24 for _ in range(12)]
    table[6][9] = [90.0, 40.0]
    return {"meta": {"node_count": len(NODES), "edge_count": len(edges), "sun_table": table},
            "nodes": {k: list(v) for k, v in NODES.items()}, "edges": edges}


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
        trees = store._edge_density(month, layers="trees")
        for hour in range(24):
            for minute in (0, 30):
                both = store._edge_density(month, day, hour, minute)
                only_buildings = store._edge_density(month, day, hour, minute, layers="buildings")
                assert np.all(both >= trees - 1e-6)
                assert np.all(both >= only_buildings - 1e-6)


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
