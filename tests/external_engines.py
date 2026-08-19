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
- BRouter is a THIRD independent engine, wired in as a hot-swap fallback
  (2026-08-19): it stands in as the primary comparison when OSRM is down,
  and as the arbiter when Valhalla is down (which has happened twice
  mid-batch). It is called only when needed, to stay polite. It exposes no
  snapped-waypoint location, so it can't carry OSRM's snap guard -- fine,
  because pairs are sampled from OUR OWN network nodes (BRouter snaps them
  trivially) and a bad snap surfaces as a human-triaged flag, not silent
  corruption; a crow-flight floor catches gross errors. Not a new coverage
  class: it shares the other engines' pedestrian-profile blind spots (a
  DUMBO spot-check had all three avoiding a stepped walkway we correctly
  used) -- its value is resilience and tie-breaking, not new signal.
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
BROUTER_URL = "https://brouter.de/brouter"
# A foot profile, not a bike one. hiking-mountain is BRouter's standard
# pedestrian profile; on flat NYC its elevation weighting is negligible, and
# a DUMBO->LES spot-check read 4,583m vs OSRM 4,576m / Valhalla 4,575m (within
# 0.2%), so it tracks the others closely. One constant to swap if a plainer
# walking profile proves better across a batch.
BROUTER_PROFILE = "hiking-mountain"
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


def brouter_foot_length_m(a, b):
    """Walking distance per BRouter, or (None, reason). a/b are (lat, lon).

    The fallback engine: used only when OSRM or Valhalla is unavailable (see
    the module docstring). BRouter reports the total metres as track-length
    and takes lon,lat order. It exposes no snapped-waypoint location, so
    instead of OSRM's snap guard we apply a crow-flight floor: a real walking
    route can never be shorter than the straight line between its endpoints,
    so a track-length below that means the request went wrong (a wild snap or
    a truncated route) and must not be compared."""
    params = {
        "lonlats": f"{a[1]},{a[0]}|{b[1]},{b[0]}",
        "profile": BROUTER_PROFILE,
        "alternativeidx": 0,
        "format": "geojson",
    }
    try:
        resp = requests.get(BROUTER_URL, params=params, timeout=30,
                            headers={"User-Agent": USER_AGENT})
        data = resp.json()
    except Exception as exc:  # noqa: BLE001 -- advisory oracle, log and move on
        return None, f"brouter error: {exc}"
    try:
        length = float(data["features"][0]["properties"]["track-length"])
    except (KeyError, IndexError, TypeError, ValueError):
        # BRouter reports routing failures as a plain-text body, not geojson.
        return None, f"brouter: no track ({str(data)[:80]})"
    crow = haversine_m(a[0], a[1], b[0], b[1])
    if length < crow:
        return None, f"brouter track {length:.0f}m < crow-flight {crow:.0f}m"
    return length, None


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


def primary_comparison(a, b, ours_m, osrm_fn=osrm_foot_length_m,
                       brouter_fn=brouter_foot_length_m, pause_fn=polite_pause):
    """The length to compare OURS against: OSRM, or BRouter if OSRM is DOWN.

    Returns (length, engine, err). A per-pair "osrm snap moved" is a real
    skip (BRouter can't be snap-guarded, so we don't paper over it); only a
    service/network error (or a bad code) triggers the BRouter stand-in, so
    the batch doesn't shrink whenever OSRM is flaky. Engine callables are
    injectable so this is unit-testable without live calls."""
    theirs, err = osrm_fn(a, b)
    pause_fn()
    if theirs is None and err is not None and not err.startswith("osrm snap"):
        brouter, brouter_err = brouter_fn(a, b)
        pause_fn()
        if brouter is not None:
            return brouter, "brouter", None
    if theirs is None:
        return None, "osrm", err
    return theirs, "osrm", None


def arbitrate(a, b, ours_m, primary_engine, valhalla_fn=valhalla_no_ferry_length_m,
              brouter_fn=brouter_foot_length_m, pause_fn=polite_pause):
    """Second opinion on a flagged pair: Valhalla-no-ferry, or BRouter if
    Valhalla is down (recorded twice mid-batch). BRouter never self-arbitrates
    -- if it was already the primary there is no independent second engine, so
    the pair stays an honest unarbitrated lead. Returns
    (arbiter_m, engine, err, agrees)."""
    arbiter, err = valhalla_fn(a, b)
    pause_fn()
    engine = "valhalla-no-ferry"
    if arbiter is None and primary_engine != "brouter":
        arbiter, err = brouter_fn(a, b)
        pause_fn()
        engine = "brouter"
    agrees = arbiter is not None and (
        1 / ARBITER_AGREE_RATIO <= arbiter / ours_m <= ARBITER_AGREE_RATIO
    )
    return arbiter, engine, err, agrees


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
