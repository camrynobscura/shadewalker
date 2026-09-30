"""Clients and flag rules for the external-engine validation harness.

Independent walking-route engines built on the same OSM data are the
project's bug-finder: every disagreement has been a real bug, a lead, or
a verified legitimate win. This module holds the operational rules:

- OSRM foot must be routing.openstreetmap.de/routed-foot -- the popular
  router.project-osrm.org demo silently serves driving routes for foot.
- Engines are advisory, never verdicts, in both directions: OSRM misses
  real park entrances (Clark St) and passes phantom welds whose endpoints
  snap to one street. A flag is a lead for a human, not a failure.
- OSRM foot rides ferries; we deliberately don't. Valhalla with
  use_ferry=0 is the arbiter for flagged pairs (in one 100-route batch, 4
  of 5 flags were OSRM taking the E 34th St ferry; Valhalla-no-ferry read
  all four at ratio 1.01-1.03). This OSRM instance rejects exclude=ferry,
  so the arbitration has to live in Valhalla.
- BRouter is a third independent engine, wired in as a hot-swap fallback:
  it stands in as the primary comparison when OSRM is down, and as the
  arbiter when Valhalla is down (which happens mid-batch). It is called
  only when needed, to stay polite. It exposes no snapped-waypoint
  location, so it can't carry OSRM's snap guard -- fine, because pairs
  are sampled from our own network nodes (BRouter snaps them trivially)
  and a bad snap surfaces as a human-triaged flag, not silent corruption;
  a crow-flight floor catches gross errors. It runs the `shortest`
  profile (a distance oracle, not a pedestrian router -- see
  BROUTER_PROFILE), so unlike OSRM/Valhalla it does not model pedestrian
  access finely; its value is resilience and tie-breaking on route
  length, not new pedestrian signal.
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
# A distance profile, deliberately -- BRouter's role here is a shortest-path
# length oracle, not a route planner. The pedestrian profile hiking-mountain
# optimizes ascent/energy, so in NYC's genuinely hilly areas it detours around
# grade and over-reports distance: measured 2026-08-19 over 18 pairs (Todt
# Hill, Sunset Park, Washington Heights, Riverdale + flat controls),
# hiking-mountain ran 1.021x OSRM foot in hilly boxes with a +16.6% tail on
# one steep pair -- enough to trip the 1.10 arbiter-agreement bar and
# manufacture false "ours too short" leads. `shortest` tracked OSRM foot at
# 0.993x hilly / 0.994x flat with no over-report tail. Swap this constant
# only against a fresh cross-terrain batch.
BROUTER_PROFILE = "shortest"
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


def osrm_route_uses_ferry(a, b, get_fn=None):
    """Does OSRM foot's own route between a and b ride a ferry? Returns
    (True/False, None) or (None, reason).

    OSRM foot rides ferries and we deliberately don't, so a ferry is the
    single most common reason an OSRM route is shorter than ours (11 of the
    1,000-route campaign's 13 flags). Rather than spend a Valhalla arbiter
    call to discover that, ask OSRM's own response: with steps=true every leg
    carries per-step `mode`, and a ferry leg is mode="ferry". Called only on
    an already-flagged OSRM pair (rare), so the extra round-trip is cheap and
    stays on the same engine -- no second engine needed for the ferry class.
    get_fn is injectable for unit tests."""
    get = get_fn if get_fn is not None else requests.get
    url = f"{OSRM_FOOT_URL}/{a[1]},{a[0]};{b[1]},{b[0]}?overview=false&steps=true"
    try:
        resp = get(url, timeout=30, headers={"User-Agent": USER_AGENT})
        data = resp.json()
    except Exception as exc:  # noqa: BLE001 -- advisory oracle, log and move on
        return None, f"osrm steps error: {exc}"
    if data.get("code") != "Ok":
        return None, f"osrm steps code: {data.get('code')}"
    for route in data.get("routes", []):
        for leg in route.get("legs", []):
            for step in leg.get("steps", []):
                if step.get("mode") == "ferry":
                    return True, None
    return False, None


def valhalla_no_ferry_length_m(a, b, post_fn=None, pause_fn=polite_pause,
                               retries=1):
    """Walking distance per Valhalla pedestrian with ferries disabled, or
    (None, reason). No snap guard: this only arbitrates pairs whose
    endpoints both we and OSRM already snapped within SNAP_MAX_M, so a
    wildly different Valhalla snap would surface as a length disagreement
    anyway.

    valhalla1.openstreetmap.de goes down mid-batch and leaves flags
    unarbitrated. A transient failure -- a network error or a 5xx -- is
    retried `retries` times (one extra attempt by default) with a polite
    pause between, before we give up and let arbitrate() fall back to
    BRouter. A clean 200 that simply carries no trip (a genuine "no route")
    is not retried -- it isn't transient. post_fn/pause_fn are injectable so
    the retry path is unit-testable without network or sleeps."""
    post = post_fn if post_fn is not None else requests.post
    body = {
        "locations": [{"lat": a[0], "lon": a[1]}, {"lat": b[0], "lon": b[1]}],
        "costing": "pedestrian",
        "costing_options": {"pedestrian": {"use_ferry": 0}},
        "units": "kilometers",
    }
    last_reason = None
    for attempt in range(retries + 1):
        if attempt > 0:
            pause_fn()  # polite gap before a retry
        try:
            resp = post(VALHALLA_URL, json=body, timeout=30,
                        headers={"User-Agent": USER_AGENT})
        except Exception as exc:  # noqa: BLE001 -- advisory oracle, log and move on
            last_reason = f"valhalla error: {exc}"
            continue  # transient -- retry
        if resp.status_code >= 500:
            last_reason = f"valhalla http {resp.status_code}"
            continue  # server-side transient -- retry
        data = resp.json()
        trip = data.get("trip")
        if not trip:
            # A well-formed non-transient failure (e.g. no route): don't retry.
            return None, f"valhalla: {data.get('error', 'no trip in response')}"
        return trip["summary"]["length"] * 1000.0, None
    return None, last_reason


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
    """The length to compare ours against: OSRM, or BRouter if OSRM is down.

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
    Valhalla is down. BRouter never self-arbitrates
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


# ── Gap-probe oracle rules ───────────────────────────────────────────────────
# Three rules a gap probe (asking an external engine whether two nearby
# points connect) needs, kept as small unit-tested helpers so a probe imports
# them instead of re-deriving them.

# Generous NYC envelope (all five boroughs + the harbor islands), for
# rejecting OSRM annotation/way ids that snapped to somewhere impossible.
NYC_LAT_MIN, NYC_LAT_MAX = 40.45, 40.95
NYC_LON_MIN, NYC_LON_MAX = -74.30, -73.68

# Rule (a): a gap probe asks OSRM to route between two points that are far
# apart in our graph but near in reality. OSRM prunes small components and
# silently snaps a probe endpoint across the very gap under test, then
# reports a short "connected" walk that is really about two other points.
# Rejecting only when the snap exceeds twice the gap lets hundreds of such
# false "connected" verdicts through. The snap must stay well inside the
# gap: if a snap displacement reaches half the gap, the probe has likely
# left the scrap and the verdict must read ABSENT_OR_PRUNED, never
# "connected".
GAP_PROBE_SNAP_FRACTION = 0.5


def gap_probe_snap_ok(snap_m, gap_m, max_fraction=GAP_PROBE_SNAP_FRACTION):
    """True if an OSRM probe's snap displacement is small enough (relative to
    the gap under test) to trust the verdict. When it returns False the probe
    result is ABSENT_OR_PRUNED, not a real "connected" -- see rule (a)."""
    if gap_m <= 0:
        return False
    return snap_m < max_fraction * gap_m


def in_nyc_bbox(lat, lon):
    """Rule (b): `annotations=nodes` emits garbage ids near snapped endpoints
    (a Swiss node id on a Brooklyn route). Any way/node looked up
    from an OSRM annotation id must be geometry-filtered to NYC before it is
    trusted or embedded in an Overpass query."""
    return (NYC_LAT_MIN <= lat <= NYC_LAT_MAX and
            NYC_LON_MIN <= lon <= NYC_LON_MAX)


def coerce_osrm_node_id(value):
    """Rule (c): OSRM emits some node ids as floats (1.234e9). `int()`-coerce
    before embedding in an Overpass query, or a whole chunk fails silently.
    Raises ValueError on a non-integral value rather than truncating one."""
    as_float = float(value)
    if as_float != int(as_float):
        raise ValueError(f"non-integral OSRM node id: {value!r}")
    return int(as_float)


def google_walking_directions_url(a, b):
    """The field-check link format the manual spot-check workflow uses:
    the whole route, not a single point."""
    return (
        "https://www.google.com/maps/dir/?api=1"
        f"&origin={a[0]},{a[1]}&destination={b[0]},{b[1]}&travelmode=walking"
    )
