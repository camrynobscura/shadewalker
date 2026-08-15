"""Clients and flag rules for the external-engine validation harness.

Independent walking-route engines built on the same OSM data are the
project's proven bug-finder (325 ad-hoc comparisons to date; every
disagreement was a real bug, a filed lead, or a verified legitimate win).
This module makes the hard-won operational lessons permanent instead of
re-learned per ad-hoc script:

- OSRM foot must be routing.openstreetmap.de/routed-foot -- the popular
  router.project-osrm.org demo silently serves DRIVING routes for foot.
- Engines are ADVISORY, never verdicts, in both directions: OSRM misses
  real park entrances (Clark St) and passes phantom welds whose endpoints
  snap to one street. A flag is a lead for a human, not a failure.
- OSRM foot rides ferries; we deliberately don't. Valhalla with
  use_ferry=0 is the arbiter for flagged pairs (learned 2026-08-15: 4 of
  5 flags in a 100-route batch were OSRM taking the E 34th St ferry;
  Valhalla-no-ferry read all four at ratio 1.01-1.03). This OSRM instance
  rejects exclude=ferry, so the arbitration has to live in Valhalla.
- Both engines snap endpoints too; if any snap moved an endpoint more
  than SNAP_MAX_M, the comparison is no longer about the requested pair
  and must be discarded, not compared.
- These are free community servers: ~1 request/second, identify
  ourselves, and never call them from the default suite or CI.
"""

import math
import time

import requests

OSRM_FOOT_URL = "https://routing.openstreetmap.de/routed-foot/route/v1/foot"
VALHALLA_URL = "https://valhalla1.openstreetmap.de/route"
USER_AGENT = "shadewalker-validation-harness (personal project; tests/external_engines.py)"

SNAP_MAX_M = 40.0
POLITENESS_SLEEP_S = 1.0

# Field-tested flag thresholds (found the Queensboro disconnection with
# zero noise once the ferry arbitration is applied): both a ratio and an
# absolute floor, so short routes' natural variance doesn't flag.
LONG_RATIO = 1.25
LONG_ABS_M = 250.0
SHORT_RATIO = 0.8
SHORT_ABS_M = 200.0

# Arbitration: a flagged pair where Valhalla-no-ferry lands within this
# ratio of OUR length is explained (OSRM-specific: a ferry, or an OSRM
# gap), not a lead.
ARBITER_AGREE_RATIO = 1.10


def haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def polite_pause():
    time.sleep(POLITENESS_SLEEP_S)


def osrm_foot_length_m(a, b):
    """Walking distance per OSRM foot, or (None, reason). a/b are (lat, lon)."""
    url = f"{OSRM_FOOT_URL}/{a[1]},{a[0]};{b[1]},{b[0]}?overview=false"
    try:
        resp = requests.get(url, timeout=30, headers={"User-Agent": USER_AGENT})
        data = resp.json()
    except Exception as exc:  # noqa: BLE001 -- advisory oracle, log and move on
        return None, f"osrm error: {exc}"
    if data.get("code") != "Ok":
        return None, f"osrm code: {data.get('code')}"
    for wp, want in zip(data["waypoints"], (a, b)):
        moved = haversine_m(wp["location"][1], wp["location"][0], want[0], want[1])
        if moved > SNAP_MAX_M:
            return None, f"osrm snap moved {moved:.0f}m"
    return data["routes"][0]["distance"], None


def valhalla_no_ferry_length_m(a, b):
    """Walking distance per Valhalla pedestrian with ferries disabled, or
    (None, reason). No snap guard: this only arbitrates pairs whose
    endpoints both we and OSRM already snapped within SNAP_MAX_M, so a
    wildly different Valhalla snap would surface as a length disagreement
    anyway."""
    body = {
        "locations": [{"lat": a[0], "lon": a[1]}, {"lat": b[0], "lon": b[1]}],
        "costing": "pedestrian",
        "costing_options": {"pedestrian": {"use_ferry": 0}},
        "units": "kilometers",
    }
    try:
        resp = requests.post(VALHALLA_URL, json=body, timeout=30, headers={"User-Agent": USER_AGENT})
        data = resp.json()
    except Exception as exc:  # noqa: BLE001 -- advisory oracle, log and move on
        return None, f"valhalla error: {exc}"
    trip = data.get("trip")
    if not trip:
        return None, f"valhalla: {data.get('error', 'no trip in response')}"
    return trip["summary"]["length"] * 1000.0, None


def our_none_priority_length_m(store, a, b):
    """Our own NONE-priority (tree_weight=0) length, or (None, reason) --
    the plain shortest walking path, the same thing the external engines
    optimize, so lengths are directly comparable."""
    pair = store.snap_pair(a[0], a[1], b[0], b[1])
    if pair is None:
        return None, "no shared component"
    for sp, want in zip(pair, (a, b)):
        moved = haversine_m(sp.point[1], sp.point[0], want[0], want[1])
        if moved > SNAP_MAX_M:
            return None, f"our snap moved {moved:.0f}m"
    route = store.route(pair[0], pair[1], tree_weight=0.0, month=7)
    if route is None:
        return None, "no path"
    return route["length_m"], None


def classify_flag(ours_m, theirs_m):
    """The field-tested flag rule; None when the lengths roughly agree."""
    if ours_m > theirs_m * LONG_RATIO and ours_m - theirs_m > LONG_ABS_M:
        return "OURS_LONG (missing connection our side?)"
    if ours_m < theirs_m * SHORT_RATIO and theirs_m - ours_m > SHORT_ABS_M:
        return "OURS_SHORT (over-connection our side?)"
    return None


def google_walking_directions_url(a, b):
    """The field-check link format the manual spot-check workflow uses:
    the whole route, not a single point."""
    return (
        "https://www.google.com/maps/dir/?api=1"
        f"&origin={a[0]},{a[1]}&destination={b[0]},{b[1]}&travelmode=walking"
    )
