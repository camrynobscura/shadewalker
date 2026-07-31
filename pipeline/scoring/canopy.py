"""Sample the 2021 NYC land-cover raster for tree-canopy coverage.

One shared sampler: Phase 2's street audit and Phase 3's park PoC (see
PLAN.md's Park-canopy section) both call this rather than each rolling
their own raster access, so a fix to one (e.g. a nodata-handling bug)
can't silently diverge between them.

The raster is never resampled or reprojected -- only vectors move, by
reprojecting a geometry INTO the raster's own CRS (CANOPY_RASTER_CRS)
before sampling it. Resampling a categorical raster (bilinear/cubic
averaging class *codes*) would invent meaningless in-between classes, so
sampling always reads native pixels via rasterio.mask's windowed read.
"""

import functools
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
import shapely
from pyproj import Transformer
from rasterio.mask import mask
from shapely import prepared
from shapely.geometry import mapping
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shapely_transform

from pipeline import config
from pipeline.fetch import parks as park_fetch
from pipeline.graph import boundary
from pipeline.graph.centerline import METRIC_CRS

_TO_RASTER_CRS = Transformer.from_crs(METRIC_CRS, config.CANOPY_RASTER_CRS, always_xy=True).transform
_TO_METRIC_CRS = Transformer.from_crs("EPSG:4326", METRIC_CRS, always_xy=True).transform


@functools.lru_cache(maxsize=1)
def citywide_park_shape_m() -> BaseGeometry:
    """The union of every real NYC park (boundary.park_polygon() --
    roadside slivers and cemeteries already excluded), reprojected into
    METRIC_CRS. Cached for the process's lifetime: a borough run calls
    apply_park_canopy() once per tile, and every tile needs this exact
    same shape -- recomputing the ~1,500-polygon union and reprojection
    per tile would be pure waste.

    Reprojection itself can reintroduce a self-intersection even from an
    already-valid EPSG:4326 input -- confirmed live: park_polygon()'s
    result was valid, but transforming it into METRIC_CRS produced an
    invalid geometry at the exact same coordinate a repaired-but-still-
    numerically-tight ring sat at, later crashing apply_park_canopy()'s
    .intersection() call. make_valid() runs again here, after the
    transform, not just inside park_polygon() before it.
    """
    geojson = park_fetch.fetch_park_properties()
    park_shape_4326 = boundary.park_polygon(geojson)
    return shapely.make_valid(shapely_transform(_TO_METRIC_CRS, park_shape_4326))


@functools.lru_cache(maxsize=1)
def citywide_park_reach_m() -> prepared.PreparedGeometry:
    """citywide_park_shape_m() buffered out to PARK_REACH_BUFFER_M and
    prepared for fast repeated point-in-shape tests.

    Lives here rather than in pipeline/fetch/ because it shares
    citywide_park_shape_m()'s whole fetch → union → reproject → cache
    chain, and pipeline/graph/boundary.py is deliberately pure functions
    over raw GeoJSON with no fetching of its own. Its consumer is the
    fetch stage, though (pipeline/fetch/streets.py's park-reach sidewalk
    rule), injected via run_tile.py so fetch/ takes no dependency on
    scoring/.

    The buffer is what makes this usable at all: the Parks Properties
    polygons sit a real distance off the paths they ought to contain --
    measured directly, a 211m stretch of Central Park's own Outer Loop
    where it hugs the park's southern edge falls ~9m OUTSIDE Central
    Park's polygon. A strict inside-the-polygon test therefore rejects
    exactly the park-entrance paths this is meant to find, which is the
    same lesson PARK_REACH_BUFFER_M was introduced for during the
    park-canopy work (see PLAN.md).

    prepared.prep() because the caller runs one containment test per
    candidate sidewalk segment -- thousands per tile -- against a
    ~1,500-polygon citywide union; unprepared, each test re-walks the
    whole geometry.
    """
    return prepared.prep(shapely.make_valid(citywide_park_shape_m().buffer(config.PARK_REACH_BUFFER_M)))


def raster_available(path: Path = config.CANOPY_RASTER_PATH) -> bool:
    """Whether the (large, gitignored, manually-downloaded) canopy raster is
    present. Callers that must survive its absence -- the pilot tile
    pipeline, CI -- check this before opening it."""
    return path.exists()


def canopy_fraction(raster: rasterio.DatasetReader, geometry_m) -> tuple[float, int]:
    """(canopy fraction, valid pixel count) for one geometry.

    geometry_m is in METRIC_CRS (meters); reprojected here into the
    raster's own CRS to sample it. valid_pixel_count excludes nodata
    pixels from both the numerator and denominator, and is reported
    separately so a geometry that falls outside the raster's coverage (0
    valid pixels) can be told apart from one that's genuinely all
    non-canopy (fraction 0.0, valid pixels > 0) -- collapsing those two
    into a single 0.0 would hide a coverage gap as an ordinary result.
    """
    geometry_native = shapely_transform(_TO_RASTER_CRS, geometry_m)
    try:
        out_image, _ = mask(
            raster, [mapping(geometry_native)], crop=True, nodata=raster.nodata, filled=True
        )
    except ValueError:
        return 0.0, 0  # geometry doesn't overlap the raster's extent at all

    band = out_image[0]
    valid = band != raster.nodata
    valid_count = int(valid.sum())
    if valid_count == 0:
        return 0.0, 0
    canopy_count = int((band[valid] == config.CANOPY_RASTER_TREE_CLASS).sum())
    return canopy_count / valid_count, valid_count


def sample_corridors(
    geometries_m, buffer_m: float, raster_path: Path = config.CANOPY_RASTER_PATH
) -> list[tuple[float, int]]:
    """canopy_fraction() for each geometry in geometries_m, buffered by
    buffer_m first (meters -- same corridor convention tree scoring's
    TREE_BUFFER_M uses). Opens the raster once and reuses it across every
    geometry; the COG's windowed reads make this cheap per-geometry even
    at citywide edge counts.
    """
    with rasterio.open(raster_path) as raster:
        return [canopy_fraction(raster, geometry.buffer(buffer_m)) for geometry in geometries_m]


def apply_park_canopy(
    edges: gpd.GeoDataFrame,
    park_shape_m: BaseGeometry,
    raster_path: Path = config.CANOPY_RASTER_PATH,
    buffer_m: float = config.PARK_REACH_BUFFER_M,
) -> gpd.GeoDataFrame:
    """The park-edge reach rule (see PLAN.md): add each edge's park-canopy
    credit onto tree_deciduous.

    For every edge, buffer its centerline by buffer_m -- PARK_REACH_BUFFER_M
    by default, NOT TREE_BUFFER_M; see that constant's comment for why a
    park-edge street's corridor needs a much bigger reach to touch the
    park polygon at all than it does to catch a real curbside tree -- and
    intersect that corridor with park_shape_m, the "reach" into the park.
    Sampling just that sliver (not the park's whole interior) means a
    street bordering a bare lawn gets less credit than one bordering
    dense canopy, and a street on the far side of the park from this edge
    gets none. Forestry keeps crediting whatever real street trees it
    already found in the same corridor (typically on the non-park side)
    -- the two sources are spatially disjoint within one edge's corridor,
    so this never double counts a tree Forestry already scored.

    The credit is added to tree_deciduous, not tree_evergreen, so it
    decays through the existing seasonal CANOPY_BY_MONTH curve exactly
    like a real deciduous street tree would -- matching the locked
    seasonality decision (the raster is a leaf-on peak snapshot).

    Edges whose corridor never touches park_shape_m are returned
    unchanged. If the raster isn't present locally (pilot tile, CI),
    edges are returned unchanged rather than raising -- callers that need
    to know whether this ran should check raster_available() themselves.
    """
    if not raster_available(raster_path):
        return edges

    # prepared: O(1)-ish repeated .intersects() checks against a citywide
    # union of ~1,500 park polygons -- a plain (unprepared) .intersects()
    # call re-walks every vertex of the whole union on every edge, which is
    # fine for a two-park PoC but far too slow once this runs over every
    # edge in a citywide pipeline run. Only the boolean pre-check benefits;
    # the real .intersection() geometry op still needs park_shape_m itself.
    prepared_park = prepared.prep(park_shape_m)

    added = np.zeros(len(edges))
    with rasterio.open(raster_path) as raster:
        for position, (geometry_m, length_m) in enumerate(zip(edges["geometry_m"], edges["length_m"])):
            corridor = geometry_m.buffer(buffer_m)
            if not prepared_park.intersects(corridor):
                continue
            reach = corridor.intersection(park_shape_m)
            fraction, _ = canopy_fraction(raster, reach)
            if fraction == 0.0:
                continue
            reach_density = config.CANOPY_FRACTION_TO_DENSITY_SLOPE * fraction
            added[position] = reach_density * max(length_m, config.DENSITY_LENGTH_FLOOR_M)

    edges = edges.copy()
    edges["tree_deciduous"] = edges["tree_deciduous"].to_numpy() + added
    return edges
