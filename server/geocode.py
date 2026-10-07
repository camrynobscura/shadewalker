"""Server-side geocoding proxy — Photon upstream, a backup behind it, cached.

The browser never calls a public geocoder directly. Geocoder usage
policies bind the application in aggregate (Nominatim, for one, caps the
whole app at 1 req/s and forbids autocomplete outright), so a thousand
browsers are still one quota. One server-side chokepoint fixes all of it
at once:

- the LRU cache absorbs repeat queries — being polite to a fair-use
  upstream is the point of it, not saving milliseconds;
- the upstream is config (SHADEWALKER_PHOTON_URL): public komoot today,
  a self-hosted NYC index later (measured at ~0.5 GB RSS), zero frontend
  change either way;
- visitors' IPs and search text stop flowing to a third party; requests
  leave from here under an app-identifying User-Agent instead.

Photon is a free public server with no guarantee, so a second geocoder
stands behind it (config.BACKUP_GEOCODER_URL). When a Photon call fails,
the backup answers that search and every search after it; Photon is asked
again in the background, and takes over once it answers. Only the search
that discovers an outage waits on Photon. "Photon found nothing" is an
answer, not a failure, and never goes to the backup. The other way round
is different: the backup knows every address and landmark but no
businesses, so a search it has nothing for is asked of Photon once more,
with a long wait, rather than answered with nothing.

Query text and coordinates are location data and are deliberately never
logged here. uvicorn's access log would still see request paths, which
is why production runs with --no-access-log (server/app.py).

Label building lives here rather than in the frontend so the frontend
only ever sees our own lean shape ({lat, lon, label}) and a provider swap
touches exactly one file.
"""

import logging
import string
import threading
import time
from functools import lru_cache

import requests

from pipeline import config

# Photon's fair-use ask (github.com/komoot/photon): identify yourself and
# don't hammer. Identifies the app, carries no personal information.
USER_AGENT = "shady-stroll (NYC tree-shade walking-route planner; personal project)"

# Photon bbox format is lon_min,lat_min,lon_max,lat_max — pinned server
# side so the client cannot ask for anywhere else.
_CITY_BBOX = (
    f"{config.CITY_BBOX.lon_min},{config.CITY_BBOX.lat_min},"
    f"{config.CITY_BBOX.lon_max},{config.CITY_BBOX.lat_max}"
)

# The backup lists a lot's named places ahead of its addresses at the same
# distance, so a reverse lookup reads a few candidates to reach an address.
_BACKUP_REVERSE_CANDIDATES = 10

_session = requests.Session()
_session.headers["User-Agent"] = USER_AGENT

logger = logging.getLogger(__name__)

# Whether Photon is failing, shared by every request thread. A failed call
# sets _photon_down; only a background check that gets an answer clears it.
_state_lock = threading.Lock()
_photon_down = False
_next_check_at = 0.0     # time.monotonic() before which no check starts
_check_running = False


class UpstreamError(Exception):
    """A geocoder unreachable, timed out, or answering anything but usable
    JSON. search() and reverse() raise it only when the backup failed too;
    app.py maps it to 502.

    Raised (not returned) so lru_cache never memoizes a failure: the next
    request retries the upstream instead of replaying an outage.
    """


def search(q: str, limit: int) -> tuple[dict, ...]:
    """Forward search, NYC-bounded, English labels. Callers pass q already
    whitespace-normalized (app.py does) so trivially-different keys don't
    double-cache. Returns a tuple — this exact object lives in the cache,
    so nobody gets a list they might mutate."""
    return _answer(lambda: _photon_search(q, limit), lambda: _backup_search(q, limit),
                   ask_photon_slowly=lambda: _photon_search_slowly(q, limit))


def reverse(lat: float, lon: float) -> str | None:
    """Point → short address label, or None when nothing address-shaped is
    nearby (callers fall back to showing coordinates). app.py rounds the
    coordinates to 5dp (~1m) first so re-clicks cache-hit."""
    return _answer(lambda: _photon_reverse(lat, lon), lambda: _backup_reverse(lat, lon))


def clear_caches() -> None:
    """Forget every cached answer and treat Photon as healthy. For tests."""
    global _photon_down, _next_check_at, _check_running
    for cached in (_photon_search, _photon_search_slowly, _photon_reverse, _backup_search,
                   _backup_reverse):
        cached.cache_clear()
    with _state_lock:
        _photon_down = False
        _next_check_at = 0.0
        _check_running = False


# --- Which geocoder answers ------------------------------------------------

def _answer(ask_photon, ask_backup, ask_photon_slowly=None):
    """Photon's answer, or the backup's while Photon is failing.

    The two are cached apart, so an answer the backup gave during an outage
    (no businesses) is never served once Photon is back.

    `ask_photon_slowly`, when given, is tried after an EMPTY backup answer:
    the backup has every address and landmark, so nothing from it means a
    business name (or a typo), which only Photon can answer. A slow answer
    does not mark Photon up again; the background check decides that. If
    the slow ask fails too, the empty answer stands: search is working, it
    just found nothing.
    """
    with _state_lock:
        photon_down = _photon_down
    if photon_down:
        _check_photon_in_background(ask_photon)
        return _backup_then_slow_photon(ask_backup, ask_photon_slowly)
    try:
        return ask_photon()
    except UpstreamError:
        _mark_photon_down()
        return _backup_then_slow_photon(ask_backup, ask_photon_slowly)


def _backup_then_slow_photon(ask_backup, ask_photon_slowly):
    answer = ask_backup()
    if answer or ask_photon_slowly is None:
        return answer
    try:
        return ask_photon_slowly()
    except UpstreamError:
        return answer


def _mark_photon_down() -> None:
    global _photon_down, _next_check_at
    with _state_lock:
        already_down = _photon_down
        _photon_down = True
        _next_check_at = _now() + config.PHOTON_RECHECK_AFTER_S
    if not already_down:
        logger.warning("[geocode] Photon failed; the backup geocoder answers until it recovers")


def _check_photon_in_background(ask_photon) -> None:
    """Ask Photon the search in hand, off the request's own thread, when a
    check is due and none is running. The visitor's answer never waits on
    it. A real search is used so that there is no traffic to Photon when
    nobody is searching."""
    global _check_running
    with _state_lock:
        if _check_running or _now() < _next_check_at:
            return
        _check_running = True
    _run_in_background(lambda: _check_photon(ask_photon))


def _check_photon(ask_photon) -> None:
    global _photon_down, _next_check_at, _check_running
    recovered = False
    try:
        ask_photon()
        recovered = True
    except Exception:
        # _get has already logged the kind of failure. Anything else is
        # swallowed too: an escaped exception would leave _check_running
        # set, and Photon would never be asked again.
        pass
    finally:
        with _state_lock:
            _check_running = False
            if recovered:
                _photon_down = False
            else:
                _next_check_at = _now() + config.PHOTON_RECHECK_AFTER_S
    if recovered:
        logger.info("[geocode] Photon is answering again")


def _run_in_background(task) -> None:
    threading.Thread(target=task, daemon=True).start()


def _now() -> float:
    return time.monotonic()


def _get(upstream: str, url: str, params: dict, timeout_s: float) -> dict:
    """One GET to a geocoder. `upstream` is its name in the log."""
    try:
        resp = _session.get(url, params=params, timeout=timeout_s)
    except requests.RequestException as exc:
        # The kind of failure only: the exception's own text holds the
        # request URL, and with it what someone searched for.
        logger.warning("[geocode] %s unreachable: %s", upstream, type(exc).__name__)
        raise UpstreamError(f"{upstream} unreachable: {type(exc).__name__}") from exc
    if resp.status_code != 200:
        # The one record of a fair-use upstream refusing us (a 429 is a
        # throttle). The status alone, never the searched text.
        logger.warning("[geocode] %s answered %s", upstream, resp.status_code)
        raise UpstreamError(f"{upstream} answered {resp.status_code}")
    try:
        return resp.json()
    except ValueError as exc:
        logger.warning("[geocode] %s answered unreadable JSON", upstream)
        raise UpstreamError(f"{upstream} answered unreadable JSON") from exc


# --- Photon ----------------------------------------------------------------

def _photon_get(path: str, params: dict, timeout_s: float = config.PHOTON_TIMEOUT_S) -> dict:
    return _get("Photon", f"{config.PHOTON_URL}{path}", params, timeout_s)


@lru_cache(maxsize=config.GEOCODE_CACHE_MAX_ENTRIES)
def _photon_search(q: str, limit: int) -> tuple[dict, ...]:
    return _parse_photon_search(_photon_get("/api", _photon_search_params(q, limit)))


@lru_cache(maxsize=config.GEOCODE_CACHE_MAX_ENTRIES)
def _photon_search_slowly(q: str, limit: int) -> tuple[dict, ...]:
    """The same search with the long wait, for a business name during an
    outage. Cached on its own: a hit here is a real Photon answer, but one
    that took seconds, and must not stand in for the fast path's."""
    return _parse_photon_search(
        _photon_get("/api", _photon_search_params(q, limit), config.PHOTON_SLOW_TIMEOUT_S))


def _photon_search_params(q: str, limit: int) -> dict:
    return {"q": q, "limit": limit, "lang": "en", "bbox": _CITY_BBOX}


def _parse_photon_search(data: dict) -> tuple[dict, ...]:
    results = []
    seen_labels = set()
    for feature in data.get("features", []):
        if not _in_nyc(feature.get("properties", {})):
            continue
        parsed = _parse_search_feature(feature)
        if parsed is None:
            continue
        # OSM often maps one shop twice (a point and its building), and
        # two rows a person cannot tell apart are one choice, not two.
        if parsed["label"] in seen_labels:
            continue
        seen_labels.add(parsed["label"])
        results.append(parsed)
    return tuple(results)


@lru_cache(maxsize=config.GEOCODE_CACHE_MAX_ENTRIES)
def _photon_reverse(lat: float, lon: float) -> str | None:
    data = _photon_get("/reverse", {"lat": lat, "lon": lon, "lang": "en"})
    features = data.get("features", [])
    if not features:
        return None
    return _reverse_label(features[0].get("properties", {}))


def _in_nyc(props: dict) -> bool:
    """Drop forward-search results outside the five boroughs. The bbox is a
    rectangle around boroughs that aren't one, so it admits Hoboken/Jersey
    City, southern Westchester, and western Nassau — all dead ends here,
    since /route rejects anything outside coverage anyway, and without
    this filter Hoboken addresses show up in autocomplete.

    Photon derives `city` from the OSM admin hierarchy, not addr:city, so
    every five-borough result carries city "New York" — verified across
    addresses, POIs, parks, and bridges in four boroughs (Queens postal
    cities like Astoria do not leak into the field), while Hoboken /
    Valley Stream / New Rochelle results carry their own city. The state
    check is belt and braces for any other US "New York" hamlet the bbox
    might graze.

    Search only: reverse() stays unfiltered, because a point already in
    hand (a geolocation fix just outside the city, say) deserves its honest
    nearest name over a silent coordinate fallback.
    """
    return props.get("city") == "New York" and props.get("state") == "New York"


def _parse_search_feature(feature: dict) -> dict | None:
    """One Photon GeoJSON feature → {lat, lon, label}, or None if it has
    nothing displayable. The label reads "the thing, where on the street,
    borough": a named place carries its street address, because a chain
    has many branches in one borough and the name alone cannot tell them
    apart. A named place with no address (a park, a plaza, a street)
    carries its neighborhood instead."""
    coords = feature.get("geometry", {}).get("coordinates")
    if not coords or len(coords) < 2:
        return None
    props = feature.get("properties", {})
    primary = _primary_name(props)
    if primary is None:
        return None
    parts = [primary]
    if props.get("name"):
        parts.append(_street_address(props) or props.get("locality"))
    parts.append(props.get("district") or props.get("city"))
    label_parts = []
    for part in parts:
        if part and part not in label_parts:
            label_parts.append(part)
    return {"lat": coords[1], "lon": coords[0], "label": ", ".join(label_parts)}


def _street_address(props: dict) -> str | None:
    """"250 7th Avenue", or just the street when there is no number."""
    street = props.get("street")
    if props.get("housenumber") and street:
        return f"{props['housenumber']} {street}"
    return street or None


def _primary_name(props: dict) -> str | None:
    """A POI/street carries `name`; a plain address carries housenumber +
    street and no name. Forward search is allowed POI names — someone who
    typed "Lucali" wants Lucali back."""
    if props.get("name"):
        return props["name"]
    return _street_address(props)


def _reverse_label(props: dict) -> str | None:
    """An address field needs an address, never the nearest business's
    name — reverse is the one direction where `name` must lose:
    reverse-geocoding a point right outside Lucali (a Carroll Gardens
    restaurant) would otherwise label the start field "Lucali" instead of
    "575 Henry Street". In Photon's shape the POI's own housenumber/street
    ride alongside its name, so preferring them is the fix. A feature
    with only a name is accepted only when it is a way you physically
    stand on (osm_key "highway" — a park path, a bridge); a park polygon
    or shop with no address yields None and the caller's coordinate
    fallback.
    """
    street = props.get("street")
    if props.get("housenumber") and street:
        return f"{props['housenumber']} {street}"
    if street:
        return street
    if props.get("osm_key") == "highway" and props.get("name"):
        return props["name"]
    return None


# --- The backup (Pelias over the city's address directory) -----------------

def _backup_get(path: str, params: dict) -> dict:
    return _get("backup geocoder", f"{config.BACKUP_GEOCODER_URL}{path}", params,
                config.BACKUP_GEOCODER_TIMEOUT_S)


@lru_cache(maxsize=config.GEOCODE_CACHE_MAX_ENTRIES)
def _backup_search(q: str, limit: int) -> tuple[dict, ...]:
    data = _backup_get("/autocomplete", {"text": q, "size": limit})
    results = []
    seen_labels = set()
    for feature in data.get("features", []):
        parsed = _parse_backup_feature(feature)
        if parsed is None or parsed["label"] in seen_labels:
            continue
        seen_labels.add(parsed["label"])
        results.append(parsed)
    return tuple(results)


@lru_cache(maxsize=config.GEOCODE_CACHE_MAX_ENTRIES)
def _backup_reverse(lat: float, lon: float) -> str | None:
    """The nearest real address, as an address field needs (the same rule
    as _reverse_label), and only when it is close by."""
    data = _backup_get("/reverse", {"point.lat": lat, "point.lon": lon,
                                    "size": _BACKUP_REVERSE_CANDIDATES})
    for feature in data.get("features", []):
        props = feature.get("properties", {})
        distance_km = props.get("distance")
        if distance_km is None or distance_km * 1000 > config.BACKUP_REVERSE_MAX_DISTANCE_M:
            continue
        address = _backup_address(props)
        if address:
            return address
    return None


def _parse_backup_feature(feature: dict) -> dict | None:
    """One backup feature → {lat, lon, label} in the labels Photon's
    results use: "350 Fifth Avenue, Manhattan" or "Prospect Park,
    Brooklyn". Only a result in one of the five boroughs carries `borough`;
    anything without it (a bare "United States") is dropped."""
    coords = feature.get("geometry", {}).get("coordinates")
    if not coords or len(coords) < 2:
        return None
    props = feature.get("properties", {})
    borough = props.get("borough")
    if not borough:
        return None
    # A plain address repeats itself as its name; a named place has no
    # house number and repeats its name as its street.
    primary = _backup_address(props) or _title(props.get("name"))
    if primary is None:
        return None
    return {"lat": coords[1], "lon": coords[0], "label": f"{primary}, {borough}"}


def _backup_address(props: dict) -> str | None:
    """"350 Fifth Avenue" when the feature is an address, else None."""
    if props.get("housenumber") and props.get("street"):
        return f"{props['housenumber']} {_title(props['street'])}"
    return None


def _title(text: str | None) -> str | None:
    """The directory is upper case. capwords, not str.title, which would
    write "5Th Avenue" and "Barclay'S Center"."""
    if not text:
        return None
    return string.capwords(text)
