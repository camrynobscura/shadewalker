"""Server-side geocoding proxy — Photon upstream, one hop, cached.

Until 2026-08-30 the browser called public Nominatim directly. That shape
can't grow: geocoder usage policies bind the APPLICATION in aggregate
(Nominatim caps the whole app at 1 req/s and forbids autocomplete
outright), so a thousand browsers are still one quota. One server-side
chokepoint fixes all of it at once:

- the LRU cache absorbs repeat queries — being polite to a fair-use
  upstream is the point of it, not saving milliseconds;
- the upstream is config (SHADEWALKER_PHOTON_URL): public komoot today,
  a self-hosted NYC index later (measured 305MB / ~0.5GB RSS,
  history/geocoding-photon.md), zero frontend change either way;
- visitors' IPs and search text stop flowing to a third party; requests
  leave from here under an app-identifying User-Agent instead.

Query text and coordinates are location data and are deliberately never
logged here. uvicorn's access log still sees request paths — that is the
deploy abuse-and-privacy decision's problem, noted in PLAN.md, not
solvable in this module.

Label building lived in web/src/api.ts against Nominatim's response
shape; it moved here with the proxy so the frontend only ever sees our
own lean shape ({lat, lon, label}) and a provider swap touches exactly
one file.
"""

from functools import lru_cache

import requests

from pipeline import config

# Photon's fair-use ask (github.com/komoot/photon): identify yourself and
# don't hammer. Identifies the app, carries no personal information.
USER_AGENT = "shady-stroll (NYC tree-shade walking-route planner; personal project)"

# Photon bbox format is lon_min,lat_min,lon_max,lat_max — pinned server
# side so the client cannot ask for anywhere else, mirroring the old
# viewbox+bounded=1 the frontend sent to Nominatim.
_CITY_BBOX = (
    f"{config.CITY_BBOX.lon_min},{config.CITY_BBOX.lat_min},"
    f"{config.CITY_BBOX.lon_max},{config.CITY_BBOX.lat_max}"
)

_session = requests.Session()
_session.headers["User-Agent"] = USER_AGENT


class UpstreamError(Exception):
    """Photon unreachable, timed out, or non-200 — app.py maps this to 502.

    Raised (not returned) so lru_cache never memoizes a failure: the next
    request retries the upstream instead of replaying an outage.
    """


def _get(path: str, params: dict) -> dict:
    try:
        resp = _session.get(
            f"{config.PHOTON_URL}{path}", params=params,
            timeout=config.PHOTON_TIMEOUT_S,
        )
    except requests.RequestException as exc:
        raise UpstreamError(str(exc)) from exc
    if resp.status_code != 200:
        raise UpstreamError(f"Photon answered {resp.status_code}")
    return resp.json()


@lru_cache(maxsize=config.GEOCODE_CACHE_MAX_ENTRIES)
def search(q: str, limit: int) -> tuple[dict, ...]:
    """Forward search, NYC-bounded, English labels. Callers pass q already
    whitespace-normalized (app.py does) so trivially-different keys don't
    double-cache. Returns a tuple — this exact object lives in the cache,
    so nobody gets a list they might mutate."""
    data = _get("/api", {"q": q, "limit": limit, "lang": "en", "bbox": _CITY_BBOX})
    results = []
    for feature in data.get("features", []):
        parsed = _parse_search_feature(feature)
        if parsed is not None:
            results.append(parsed)
    return tuple(results)


@lru_cache(maxsize=config.GEOCODE_CACHE_MAX_ENTRIES)
def reverse(lat: float, lon: float) -> str | None:
    """Point → short address label, or None when nothing address-shaped is
    nearby (callers fall back to showing coordinates). app.py rounds the
    coordinates to 5dp (~1m) first so re-clicks cache-hit."""
    data = _get("/reverse", {"lat": lat, "lon": lon, "lang": "en"})
    features = data.get("features", [])
    if not features:
        return None
    return _reverse_label(features[0].get("properties", {}))


def _parse_search_feature(feature: dict) -> dict | None:
    """One Photon GeoJSON feature → {lat, lon, label}, or None if it has
    nothing displayable. Label is "primary, context": the thing itself,
    then the neighborhood (Photon's `district`) or city that tells two
    same-named streets apart."""
    coords = feature.get("geometry", {}).get("coordinates")
    if not coords or len(coords) < 2:
        return None
    primary = _primary_name(feature.get("properties", {}))
    if primary is None:
        return None
    props = feature.get("properties", {})
    context = props.get("district") or props.get("city")
    label = f"{primary}, {context}" if context and context != primary else primary
    return {"lat": coords[1], "lon": coords[0], "label": label}


def _primary_name(props: dict) -> str | None:
    """A POI/street carries `name`; a plain address carries housenumber +
    street and no name. Forward search is allowed POI names — someone who
    typed "Lucali" wants Lucali back."""
    if props.get("name"):
        return props["name"]
    street = props.get("street")
    if props.get("housenumber") and street:
        return f"{props['housenumber']} {street}"
    return street or None


def _reverse_label(props: dict) -> str | None:
    """An address field needs an ADDRESS, never the nearest business's
    name — reverse is the one direction where `name` must lose.

    This rule shipped 2026-08-24 in web/src/api.ts against Nominatim and
    moved here verbatim in spirit: reverse-geocoding a point right
    outside Lucali (a Carroll Gardens restaurant) labeled the start field
    "Lucali" instead of "575 Henry Street". In Photon's shape the POI's
    own housenumber/street ride alongside its name, so preferring them
    IS the fix. A feature with only a name is accepted only when it is a
    way you physically stand on (osm_key "highway" — a park path, a
    bridge); a park polygon or shop with no address yields None and the
    caller's coordinate fallback.
    """
    street = props.get("street")
    if props.get("housenumber") and street:
        return f"{props['housenumber']} {street}"
    if street:
        return street
    if props.get("osm_key") == "highway" and props.get("name"):
        return props["name"]
    return None
