"""Tests for pipeline/fetch/streets.py's handling of the two ways osmnx can
fail to hand back a usable graph: "no data here" (real for grid tiles that
only clip a borough's real coastline at their edge, still mostly open
water) and transient connection failures (real: three separate
ConnectionRefusedErrors during the Brooklyn run, each recovering within
seconds). The two need opposite handling -- the first means skip this tile
for good, the second means the same request would likely work if asked
again shortly -- so each gets its own tests here. Also covers the
CYCLEWAY_FILTER, FOOT_OVERRIDES_ACCESS_FILTER, NAMED_SIDEWALK_FILTER, and
ANY_SIDEWALK_FILTER unions (fetch_streets now makes five real Overpass
queries, not one -- see the module for why), and the
compose-before-simplify fix (each of the five is fetched unsimplified and
simplified once after composing, not simplified independently before
composing -- see fetch_streets()'s own docstring for the real bug, High
Bridge, this fixes)."""

import re

import networkx as nx
import pytest
import requests
from osmnx._errors import InsufficientResponseError
from shapely.geometry import LineString, box

from pipeline import config
from pipeline.config import Bbox
from pipeline.fetch import park_trails, streets

BBOX = Bbox(lat_min=40.0, lat_max=40.1, lon_min=-74.0, lon_max=-73.9)


def _mock_fetch(monkeypatch, tmp_path, graph_from_bbox):
    monkeypatch.setattr(streets, "STREETS_DIR", tmp_path)
    monkeypatch.setattr(streets.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(streets.ox, "graph_from_bbox", graph_from_bbox)
    monkeypatch.setattr(streets.ox, "save_graphml", lambda graph, path: None)
    # Defaults to "no trails here" (the common real case) unless a test
    # overrides it -- unlike interior_sidewalks.fetch_interior_sidewalks,
    # nothing else in this suite implicitly relies on a real cache file
    # existing on disk, so every test needs this mocked, not just the ones
    # that care about park trails specifically.
    monkeypatch.setattr(
        park_trails, "fetch_park_trails",
        lambda **kwargs: {"type": "FeatureCollection", "features": []},
    )


def _by_filter(
    main_fn,
    cycleway_fn=None,
    access_override_fn=None,
    named_sidewalk_fn=None,
    any_sidewalk_fn=None,
    parking_aisle_fn=None,
    barrier_fn=None,
):
    """Dispatch a graph_from_bbox mock by which of the seven real queries
    fetch_streets makes -- WALK_FILTER (main), CYCLEWAY_FILTER (the
    shared-path union), FOOT_OVERRIDES_ACCESS_FILTER (the
    foot-designated-despite-access=no/private union), NAMED_SIDEWALK_FILTER
    (the named-park-path union), ANY_SIDEWALK_FILTER (every sidewalk,
    narrowed to park reach afterwards), PARKING_AISLE_FILTER (every
    parking aisle, narrowed to through-paths afterwards), or BARRIER_FILTER
    (fence/wall/hedge ways, used to veto an interior-sidewalk connection
    that would cross one). Defaults the six narrower queries to "nothing
    here" (ValueError, the common real case) unless a test supplies its
    own function for one."""
    def dispatch(**kwargs):
        if kwargs.get("custom_filter") == streets.CYCLEWAY_FILTER:
            if cycleway_fn is not None:
                return cycleway_fn(**kwargs)
            raise ValueError("no foot-designated cycleways here")
        if kwargs.get("custom_filter") == streets.FOOT_OVERRIDES_ACCESS_FILTER:
            if access_override_fn is not None:
                return access_override_fn(**kwargs)
            raise ValueError("no foot-designated access=no/private ways here")
        if kwargs.get("custom_filter") == streets.NAMED_SIDEWALK_FILTER:
            if named_sidewalk_fn is not None:
                return named_sidewalk_fn(**kwargs)
            raise ValueError("no named sidewalk-tagged park paths here")
        if kwargs.get("custom_filter") == streets.ANY_SIDEWALK_FILTER:
            if any_sidewalk_fn is not None:
                return any_sidewalk_fn(**kwargs)
            raise ValueError("no sidewalk-tagged ways here")
        if kwargs.get("custom_filter") == streets.PARKING_AISLE_FILTER:
            if parking_aisle_fn is not None:
                return parking_aisle_fn(**kwargs)
            raise ValueError("no parking aisles here")
        if kwargs.get("custom_filter") == getattr(streets, "BARRIER_FILTER", object()):
            if barrier_fn is not None:
                return barrier_fn(**kwargs)
            raise ValueError("no barrier ways here")
        return main_fn(**kwargs)
    return dispatch


class _FakeParkReach:
    """Stands in for canopy.citywide_park_reach_m()'s prepared geometry.

    Takes the same .contains(point) call the real one does, answering from
    a plain lon/lat box rather than the real citywide park union -- these
    tests are about fetch_streets()'s wiring, not about park geometry
    (pipeline/graph/boundary.py's own tests cover that). Reprojection into
    METRIC_CRS happens before .contains() is called, so the box is
    expressed in projected meters.
    """

    def __init__(self, min_x, min_y, max_x, max_y):
        self.bounds = (min_x, min_y, max_x, max_y)

    def contains(self, point):
        min_x, min_y, max_x, max_y = self.bounds
        return min_x <= point.x <= max_x and min_y <= point.y <= max_y


def _park_reach_around(lon, lat, half_width_m=200.0):
    """A fake park-reach shape covering half_width_m around one lon/lat --
    built by reprojecting that point the same way streets.py does, so the
    box lands where the real code will look for it."""
    x, y = streets._TO_METRIC_CRS(lon, lat)
    return _FakeParkReach(x - half_width_m, y - half_width_m, x + half_width_m, y + half_width_m)


def test_fetch_streets_returns_none_when_overpass_returns_no_data(monkeypatch, tmp_path):
    def raise_it(**kwargs):
        raise InsufficientResponseError("No data elements in server response.")

    _mock_fetch(monkeypatch, tmp_path, _by_filter(raise_it))
    assert streets.fetch_streets(BBOX, "test-water-tile") is None


def test_fetch_streets_returns_none_when_no_nodes_survive_polygon_clipping(monkeypatch, tmp_path):
    def raise_it(**kwargs):
        raise ValueError("Found no graph nodes within the requested polygon.")

    _mock_fetch(monkeypatch, tmp_path, _by_filter(raise_it))
    assert streets.fetch_streets(BBOX, "test-water-tile") is None


def test_fetch_streets_retries_on_connection_error_then_succeeds(monkeypatch, tmp_path):
    # fetch_streets() now runs every result through simplify_graph() once
    # (see the module for why), which always returns a new graph object --
    # so this checks the result's shape rather than its identity.
    fake_graph = nx.MultiDiGraph()
    calls = {"count": 0}

    def flaky(**kwargs):
        calls["count"] += 1
        if calls["count"] < streets.MAX_FETCH_RETRIES:
            raise requests.exceptions.ConnectionError("connection refused")
        return fake_graph

    _mock_fetch(monkeypatch, tmp_path, _by_filter(flaky))
    result = streets.fetch_streets(BBOX, "test-flaky-tile")
    assert list(result.nodes) == []
    assert calls["count"] == streets.MAX_FETCH_RETRIES


def test_fetch_streets_retries_on_mid_response_drop_then_succeeds(monkeypatch, tmp_path):
    # ChunkedEncodingError is a connection dying mid-response rather than
    # at connect time; it is a RequestException sibling of ConnectionError,
    # not a subclass, so an unlisted catch lets it kill a whole borough run
    # (Queens tile 76/155, 2026-08-15).
    fake_graph = nx.MultiDiGraph()
    calls = {"count": 0}

    def flaky(**kwargs):
        calls["count"] += 1
        if calls["count"] < streets.MAX_FETCH_RETRIES:
            raise requests.exceptions.ChunkedEncodingError("Connection broken: IncompleteRead")
        return fake_graph

    _mock_fetch(monkeypatch, tmp_path, _by_filter(flaky))
    result = streets.fetch_streets(BBOX, "test-midstream-drop-tile")
    assert list(result.nodes) == []
    assert calls["count"] == streets.MAX_FETCH_RETRIES


def test_fetch_streets_raises_after_exhausting_retries(monkeypatch, tmp_path):
    def always_fails(**kwargs):
        raise requests.exceptions.ConnectionError("connection refused")

    _mock_fetch(monkeypatch, tmp_path, _by_filter(always_fails))
    with pytest.raises(requests.exceptions.ConnectionError):
        streets.fetch_streets(BBOX, "test-persistent-failure-tile")


def test_fetch_streets_asks_osmnx_for_all_components_with_the_walk_filter(monkeypatch, tmp_path):
    # Three kwargs where a one-line "cleanup" silently changes what data
    # exists, and no data-level test in CI can catch any of them (the
    # pilot tile happens not to depend on them):
    #   - retain_all=True is what keeps neighborhoods that merely *look*
    #     disconnected through one tile's peephole (Red Hook: expressway
    #     trench + water on three sides) from being deleted at fetch time.
    #     The keep-or-drop decision belongs to graph_store.load()'s global
    #     prune, which sees the whole merged borough.
    #   - custom_filter=WALK_FILTER is the centerline model; swapping back
    #     to network_type="walk" reintroduces the unnamed-sidewalk trap
    #     documented in CLAUDE.md.
    #   - simplify=False keeps each of the three filters' own fetch from
    #     simplifying in isolation, before it's had a chance to see the
    #     other two filters' ways -- see _fetch_with_retry's own comment
    #     for the real bug (High Bridge) this caused.
    seen = {}

    def record_kwargs(**kwargs):
        seen.update(kwargs)
        return nx.MultiDiGraph()

    _mock_fetch(monkeypatch, tmp_path, _by_filter(record_kwargs))
    streets.fetch_streets(BBOX, "test-fetch-kwargs-tile")

    assert seen.get("retain_all") is True
    assert seen.get("custom_filter") == streets.WALK_FILTER
    assert seen.get("simplify") is False


def test_fetch_streets_unions_in_foot_designated_cycleways(monkeypatch, tmp_path):
    # Some NYC bridges (confirmed real: Brooklyn Bridge's Brooklyn-side
    # landing) model their pedestrian path as a shared foot+bike cycleway
    # rather than a footway -- WALK_FILTER's highway allowlist doesn't
    # include cycleway at all, so without this union the landing would be
    # missing from the fetched data entirely, same as it was before this
    # fix (see PLAN.md).
    main_graph = nx.MultiDiGraph()
    main_graph.add_node("m1", x=0.0, y=0.0)

    cycleway_graph = nx.MultiDiGraph()
    cycleway_graph.add_node("c1", x=1.0, y=1.0)

    def cycleway_fn(**kwargs):
        assert kwargs.get("custom_filter") == streets.CYCLEWAY_FILTER
        return cycleway_graph

    _mock_fetch(monkeypatch, tmp_path, _by_filter(lambda **kwargs: main_graph, cycleway_fn))
    result = streets.fetch_streets(BBOX, "test-cycleway-union-tile")

    assert "m1" in result.nodes
    assert "c1" in result.nodes


def test_fetch_streets_works_with_no_cycleways_in_the_area(monkeypatch, tmp_path):
    # The common case: most tiles have zero foot=designated cycleways --
    # that must not be treated as an error, or block the main result.
    main_graph = nx.MultiDiGraph()
    main_graph.add_node("m1", x=0.0, y=0.0)

    _mock_fetch(monkeypatch, tmp_path, _by_filter(lambda **kwargs: main_graph))
    result = streets.fetch_streets(BBOX, "test-no-cycleway-tile")

    assert list(result.nodes) == ["m1"]


def test_fetch_streets_unions_in_foot_designated_access_override_ways(monkeypatch, tmp_path):
    # Confirmed real on Queensboro Bridge: its own pedestrian walkway is
    # split across several segments of the same physical path, some
    # tagged access=no + foot=designated, others with no access tag at
    # all -- WALK_FILTER's own access clause drops exactly the access=no
    # segments (see PLAN.md). Without this union those segments would be
    # missing from the fetched data entirely, fragmenting the path.
    main_graph = nx.MultiDiGraph()
    main_graph.add_node("m1", x=0.0, y=0.0)

    override_graph = nx.MultiDiGraph()
    override_graph.add_node("a1", x=2.0, y=2.0)

    def access_override_fn(**kwargs):
        assert kwargs.get("custom_filter") == streets.FOOT_OVERRIDES_ACCESS_FILTER
        return override_graph

    _mock_fetch(
        monkeypatch, tmp_path,
        _by_filter(lambda **kwargs: main_graph, access_override_fn=access_override_fn),
    )
    result = streets.fetch_streets(BBOX, "test-access-override-union-tile")

    assert "m1" in result.nodes
    assert "a1" in result.nodes


def test_fetch_streets_simplifies_once_after_composing_all_five_results(monkeypatch, tmp_path):
    # The actual bug fix, pinned directly. Real case (High Bridge,
    # confirmed 2026-07-18): a node shared between WALK_FILTER's and
    # CYCLEWAY_FILTER's own results looks like a plain pass-through to
    # each filter's own isolated simplification pass, so simplifying each
    # fetch BEFORE composing them can drop a real intersection's
    # connection to one side entirely (see PLAN.md/HISTORY.md for the
    # full mechanism). Simplifying is un-mockable in the small, no-network
    # unit tests elsewhere in this file (they bypass osmnx's real
    # graph_from_bbox, so its real simplify=True behavior never runs) --
    # so this test doesn't reproduce the bug on real data, it pins the
    # mechanism of the fix: simplify_graph() is called exactly once, on
    # the graph that already has all five filters' nodes composed in,
    # not once per filter before composing.
    main_graph = nx.MultiDiGraph()
    main_graph.add_node("m1", x=0.0, y=0.0)
    cycleway_graph = nx.MultiDiGraph()
    cycleway_graph.add_node("c1", x=1.0, y=1.0)
    override_graph = nx.MultiDiGraph()
    override_graph.add_node("a1", x=2.0, y=2.0)
    named_sidewalk_graph = nx.MultiDiGraph()
    named_sidewalk_graph.add_node("n1", x=3.0, y=3.0)
    any_sidewalk_graph = nx.MultiDiGraph()
    any_sidewalk_graph.add_node("s1", x=-73.95, y=40.05)
    any_sidewalk_graph.add_node("s2", x=-73.9501, y=40.0501)
    any_sidewalk_graph.add_edge("s1", "s2")

    calls = []

    def fake_simplify(graph, **kwargs):
        calls.append(set(graph.nodes))
        return graph

    monkeypatch.setattr(streets.ox.simplification, "simplify_graph", fake_simplify)
    _mock_fetch(
        monkeypatch, tmp_path,
        _by_filter(
            lambda **kwargs: main_graph,
            lambda **kwargs: cycleway_graph,
            lambda **kwargs: override_graph,
            lambda **kwargs: named_sidewalk_graph,
            lambda **kwargs: any_sidewalk_graph,
        ),
    )
    streets.fetch_streets(
        BBOX, "test-simplify-once-tile", park_reach=_park_reach_around(-73.95, 40.05)
    )

    assert len(calls) == 1
    assert calls[0] == {"m1", "c1", "a1", "n1", "s1", "s2"}


def test_fetch_streets_works_with_no_access_override_ways_in_the_area(monkeypatch, tmp_path):
    # The common case: most tiles have zero access=no/private-but-
    # foot-designated ways -- that must not be treated as an error, or
    # block the main result.
    main_graph = nx.MultiDiGraph()
    main_graph.add_node("m1", x=0.0, y=0.0)

    _mock_fetch(monkeypatch, tmp_path, _by_filter(lambda **kwargs: main_graph))
    result = streets.fetch_streets(BBOX, "test-no-access-override-tile")

    assert list(result.nodes) == ["m1"]


def test_fetch_streets_unions_in_named_sidewalk_tagged_park_paths(monkeypatch, tmp_path):
    # Confirmed real: Central Park's own "Central Park Outer Loop" is
    # tagged footway=sidewalk right at its W65th/Central Park West
    # entrance (it IS, in OSM's tagging convention, the "sidewalk" of the
    # park's own internal drive road) -- WALK_FILTER's blanket
    # footway!=sidewalk exclusion was dropping it there, forcing a ~700m
    # detour to a different entrance for two points only ~100m apart in
    # reality (see PLAN.md). Without this union, named interior park
    # paths tagged as a sidewalk would be missing from the fetched data
    # entirely, same as before this fix.
    main_graph = nx.MultiDiGraph()
    main_graph.add_node("m1", x=0.0, y=0.0)

    named_sidewalk_graph = nx.MultiDiGraph()
    named_sidewalk_graph.add_node("n1", x=3.0, y=3.0)

    def named_sidewalk_fn(**kwargs):
        assert kwargs.get("custom_filter") == streets.NAMED_SIDEWALK_FILTER
        return named_sidewalk_graph

    _mock_fetch(
        monkeypatch, tmp_path,
        _by_filter(lambda **kwargs: main_graph, named_sidewalk_fn=named_sidewalk_fn),
    )
    result = streets.fetch_streets(BBOX, "test-named-sidewalk-union-tile")

    assert "m1" in result.nodes
    assert "n1" in result.nodes


def test_fetch_streets_works_with_no_named_sidewalk_park_paths_in_the_area(monkeypatch, tmp_path):
    # The common case: most tiles have zero named footway=sidewalk ways --
    # that must not be treated as an error, or block the main result.
    main_graph = nx.MultiDiGraph()
    main_graph.add_node("m1", x=0.0, y=0.0)

    _mock_fetch(monkeypatch, tmp_path, _by_filter(lambda **kwargs: main_graph))
    result = streets.fetch_streets(BBOX, "test-no-named-sidewalk-tile")

    assert list(result.nodes) == ["m1"]


def test_walk_filter_excludes_sidewalk_but_not_crossing():
    # A marked pedestrian crossing bridges a real gap -- e.g. connecting a
    # bridge's own footway to the street grid at its landing (confirmed
    # real on all 3 Manhattan<->Brooklyn bridges checked; see PLAN.md) --
    # rather than duplicating a street's own centerline the way a
    # parallel sidewalk does. The two used to be excluded together;
    # that silently disconnected every such landing. This pins the
    # distinction so a future "cleanup" can't quietly reintroduce it.
    footway_clause = re.search(r'\["footway"!~"([^"]+)"\]', streets.WALK_FILTER)
    assert footway_clause is not None
    excluded = footway_clause.group(1).split("|")
    assert "sidewalk" in excluded
    assert "crossing" not in excluded


def test_walk_filter_admits_bridleway():
    # NYC's bridle paths are walked and run on daily -- Central Park's
    # reservoir loop, Prospect Park's bridle path (100% of that park's
    # fixable coverage gap) -- and a citywide tag survey found ZERO of the
    # city's 32.45km tagged foot=no, with 37% carrying no foot tag at all.
    # That's why bridleway sits in this broad allowlist rather than getting
    # a narrow foot=designated-only query like CYCLEWAY_FILTER: narrowing
    # would silently drop ~12km of real walkable path. Pinned so a future
    # "why is a horse path in the walk filter?" cleanup has to read the
    # reasoning first.
    highway_clause = re.search(r'\["highway"~"([^"]+)"\]', streets.WALK_FILTER)
    assert highway_clause is not None
    assert "bridleway" in highway_clause.group(1).split("|")
    # The restricted mileage is excluded by access, not by omission -- so
    # that clause has to stay for admitting bridleway to remain safe.
    assert '["access"!~"private|no"]' in streets.WALK_FILTER


def test_walk_filter_admits_track():
    # highway=track: OSM's own pedestrian-navigation guidelines
    # (wiki.openstreetmap.org/wiki/Guidelines_for_pedestrian_navigation)
    # list track as a default "pedestrian (distinct) way," the same
    # category as footway/path/steps -- not something needing a special
    # foot=yes override the way a shared carriageway does. A full
    # citywide tag survey (FIXES.md item 1f, 2026-08-09) found 497 real
    # NYC track ways; 323 (65%) already pass this filter's existing
    # foot/access clauses unchanged. Manually checked every real risk
    # cluster in that admitted set against satellite imagery (an untagged
    # paved path network in Pelham Bay, Rockaway/Tilden Beach's service
    # roads, a beach-path access=customers cluster, Ocean Breeze Park) --
    # all confirmed real public paths, not private facilities slipping
    # through on a missing access tag.
    highway_clause = re.search(r'\["highway"~"([^"]+)"\]', streets.WALK_FILTER)
    assert highway_clause is not None
    assert "track" in highway_clause.group(1).split("|")
    # Same safety net bridleway's own test re-asserts above: admitting a
    # new type only stays safe as long as this clause keeps excluding
    # genuinely private/gated ways.
    assert '["access"!~"private|no"]' in streets.WALK_FILTER


def test_walk_filter_excludes_foot_private():
    # Found during the highway=track survey (FIXES.md item 1f,
    # 2026-08-09): 4 real ways (near Marine Park/Bergen Beach) are
    # tagged foot=private -- explicitly not public for pedestrians -- but
    # this clause only ever excluded foot=no, so these would have slipped
    # straight through untouched. Applies to every highway type this
    # filter admits, not just track.
    foot_clause = re.search(r'\["foot"!~"([^"]+)"\]', streets.WALK_FILTER)
    assert foot_clause is not None
    excluded = foot_clause.group(1).split("|")
    assert "no" in excluded
    assert "private" in excluded


def test_cycleway_filter_requires_an_explicit_foot_allowance():
    # Scoped on purpose: broadening this to admit every cycleway (not
    # just explicitly foot-allowed ones) would start routing pedestrians
    # down ordinary bike-only lanes. v19 widened designated ->
    # designated|yes (anchored, so foot=no/foot=designated;no etc. can't
    # sneak through a substring match): ~95km of real named greenways
    # citywide carry foot=yes, measured 2026-08-14 (Cunningham Park
    # Greenway was the found case, FIXES item 1).
    assert '"highway"="cycleway"' in streets.CYCLEWAY_FILTER
    assert '"foot"~"^(designated|yes)$"' in streets.CYCLEWAY_FILTER


def test_foot_overrides_access_filter_requires_an_explicit_foot_override():
    # Scoped tightly on purpose: this should only admit ways where foot
    # access is explicitly designated/yes despite a general access
    # restriction -- not every access=no/private way, which would
    # reintroduce genuinely gated/private ways WALK_FILTER's own clause
    # exists to keep out.
    assert '"foot"~"designated|yes"' in streets.FOOT_OVERRIDES_ACCESS_FILTER
    assert '"access"~"private|no"' in streets.FOOT_OVERRIDES_ACCESS_FILTER


def test_foot_overrides_access_filter_excludes_golf_ways():
    # Confirmed real citywide (FIXES.md item 1e's follow-up, 2026-08-09):
    # of 222 ways this filter matches citywide, 29 carry a "golf" tag
    # (golf=path/cartpath) -- real golf-course cart paths, where
    # foot=designated/yes marks the walking lane of the path (as opposed
    # to the cart lane), not "the general public may enter." One of these
    # (a Marine Park golf cart path) was already confirmed live in
    # data/tiles/r7c14.json.gz -- an access=private golf course path
    # routable in production. The other 193 matches (the original
    # Queensboro Bridge case, Columbia's College Walk, Fulton Mall, gated
    # communities that block cars but explicitly admit pedestrians) don't
    # carry a golf tag and should stay admitted.
    assert '"golf"!~"."' in streets.FOOT_OVERRIDES_ACCESS_FILTER


def test_foot_overrides_access_filter_admits_track():
    # One real case found during the highway=track survey (FIXES.md item
    # 1f, 2026-08-09): way 1240381845 (40.63868,-73.87592) is tagged
    # access=no + foot=yes + horse=yes -- the exact Queensboro Bridge
    # pattern this filter exists for -- but fell through untouched
    # because track wasn't in this filter's own highway allowlist,
    # even after being added to WALK_FILTER's.
    highway_clause = re.search(r'\["highway"~"([^"]+)"\]', streets.FOOT_OVERRIDES_ACCESS_FILTER)
    assert highway_clause is not None
    assert "track" in highway_clause.group(1).split("|")


def test_named_sidewalk_filter_requires_both_sidewalk_and_a_name():
    # Scoped tightly on purpose: this should only admit footway=sidewalk
    # ways that ALSO have a real name -- not every sidewalk, which would
    # reintroduce the thousands of unnamed sidewalk fragments
    # WALK_FILTER's own footway!=sidewalk clause exists to keep out.
    assert '"footway"="sidewalk"' in streets.NAMED_SIDEWALK_FILTER
    assert '["name"]' in streets.NAMED_SIDEWALK_FILTER


def test_parking_aisle_filter_matches_service_parking_aisle():
    assert '"service"="parking_aisle"' in streets.PARKING_AISLE_FILTER


def test_parking_aisle_filter_does_not_exclude_by_access():
    # Deliberately broader than WALK_FILTER's own access clause: checked
    # directly against 8 real, confirmed through-path lots citywide
    # (2026-08-08) -- 6 of 8 carry access=private/customers on at least
    # some aisles, one (a Home Depot) on every single aisle way, despite
    # being an obvious, heavily-used public shortcut. Gating on access here
    # would exclude most of the real cases this filter exists to find --
    # _through_path_parking_aisles()'s own connectivity check is what
    # decides real vs. dead-end, not this tag.
    assert "access" not in streets.PARKING_AISLE_FILTER


def test_parking_aisle_filter_excludes_building_passages():
    # tunnel=building_passage means the aisle runs through or under a
    # building -- a private indoor/underground garage lane, not an open
    # lot. Real and citywide (38 ways, confirmed 2026-08-08 near Times
    # Square: a private, motor_vehicle-only lane tunneling under a hotel),
    # but categorically different from the open-air through-paths this
    # filter exists to find -- a building's internal driveway isn't a
    # public-feeling shortcut the way a Home Depot parking lot is,
    # regardless of how many street connections it has.
    assert '"tunnel"!~"building_passage"' in streets.PARKING_AISLE_FILTER
    # Still excludes explicitly foot-prohibited aisles -- a much more
    # direct pedestrian-specific signal than access=private/customers.
    assert '"foot"!~"no"' in streets.PARKING_AISLE_FILTER


def _cache_roundtrip(monkeypatch, tmp_path, cached_graph, recorded_bbox_signature):
    """Put a graph in the cache with a given recorded fetch_bbox, then call
    fetch_streets and report whether it re-fetched. Uses osmnx's real
    save/load so the attribute genuinely survives a GraphML round trip
    rather than being asserted against a mock's in-memory dict."""
    monkeypatch.setattr(streets, "STREETS_DIR", tmp_path)
    monkeypatch.setattr(streets.time, "sleep", lambda seconds: None)

    if recorded_bbox_signature is not None:
        cached_graph.graph["fetch_bbox"] = recorded_bbox_signature
    cached_graph.graph["crs"] = "epsg:4326"
    # Must match fetch_streets()'s own filename exactly, including the
    # park-reach variant suffix -- this helper calls it without a
    # park_reach, so the cache it writes has to carry that suffix too.
    # Getting this wrong makes the "must re-fetch" tests below pass for the
    # wrong reason (file never found at all, guard never consulted).
    path = tmp_path / f"cachetile_v{streets.GRAPH_CACHE_VERSION}_noparkreach.graphml"
    streets.ox.save_graphml(cached_graph, path)

    fetched = {"count": 0}

    def record_fetch(**kwargs):
        fetched["count"] += 1
        fresh = nx.MultiDiGraph()
        fresh.add_node(2222, x=0.0, y=0.0)  # int ids: osmnx casts them on load
        return fresh

    monkeypatch.setattr(streets.ox, "graph_from_bbox", _by_filter(record_fetch))
    result = streets.fetch_streets(BBOX, "cachetile")
    return result, fetched["count"]


def _cached_graph():
    graph = nx.MultiDiGraph()
    # Integer node id on purpose: osmnx's load_graphml casts ids to int
    # (real OSM node ids are integers), so a string id here fails to load
    # and every cache test would pass for the wrong reason.
    graph.add_node(1111, x=0.0, y=0.0)
    return graph


def test_fetch_streets_reuses_a_cache_whose_recorded_bbox_matches(monkeypatch, tmp_path):
    result, fetches = _cache_roundtrip(
        monkeypatch, tmp_path, _cached_graph(), streets._bbox_signature(BBOX)
    )
    assert fetches == 0
    assert 1111 in result.nodes


def test_fetch_streets_refetches_a_cache_recorded_for_a_different_bbox(monkeypatch, tmp_path):
    # The real failure: commit a461e36 shifted CITY_BBOX.lat_min by exactly
    # one tile row, so r17c14's cache kept its filename while its contents
    # became the tile one row north (South Bronx streets served as Astoria
    # for twelve days). Same shift reproduced here.
    shifted = Bbox(
        lat_min=BBOX.lat_min + config.TILE_SIZE_LAT_DEG,
        lat_max=BBOX.lat_max + config.TILE_SIZE_LAT_DEG,
        lon_min=BBOX.lon_min,
        lon_max=BBOX.lon_max,
    )
    result, fetches = _cache_roundtrip(
        monkeypatch, tmp_path, _cached_graph(), streets._bbox_signature(shifted)
    )
    assert fetches > 0, "a cache for a different area must not be reused"
    assert 1111 not in result.nodes


def test_fetch_streets_refetches_a_cache_with_no_recorded_bbox(monkeypatch, tmp_path):
    # Written before this check existed, so nothing ever verified the area
    # it covers -- unverifiable has to mean re-fetch, or the guard is
    # trivially bypassed by every pre-existing file.
    result, fetches = _cache_roundtrip(monkeypatch, tmp_path, _cached_graph(), None)
    assert fetches > 0
    assert 1111 not in result.nodes


def test_fetch_streets_records_the_fetch_bbox_it_used(monkeypatch, tmp_path):
    # Without this the guard above can never fire on a freshly written file.
    saved = {}

    def capture(graph, path):
        saved["fetch_bbox"] = graph.graph.get("fetch_bbox")

    main_graph = nx.MultiDiGraph()
    main_graph.add_node("m1", x=0.0, y=0.0)
    _mock_fetch(monkeypatch, tmp_path, _by_filter(lambda **kwargs: main_graph))
    monkeypatch.setattr(streets.ox, "save_graphml", capture)

    streets.fetch_streets(BBOX, "test-records-bbox-tile")

    assert saved["fetch_bbox"] == streets._bbox_signature(BBOX)


def test_any_sidewalk_filter_matches_sidewalks_with_or_without_a_name():
    # The deliberate counterpart to the test above: this filter drops the
    # name requirement (a park's entrance paths are as often unnamed as
    # named in OSM), which is only safe because _park_reach_sidewalks()
    # narrows its result to park reach before anything is composed in.
    assert '"footway"="sidewalk"' in streets.ANY_SIDEWALK_FILTER
    assert '["name"]' not in streets.ANY_SIDEWALK_FILTER
    # Every other clause stays identical to the named variant -- this is
    # that filter minus one bracket, not a separately-drifting copy.
    assert streets.ANY_SIDEWALK_FILTER == streets.NAMED_SIDEWALK_FILTER.replace('["name"]', '')


def test_fetch_streets_keeps_park_reach_sidewalks_and_drops_the_rest(monkeypatch, tmp_path):
    # The core of the fix. ANY_SIDEWALK_FILTER matches every walkable
    # footway=sidewalk way, the vast majority of which are the unnamed
    # duplicates of a street centerline we already have -- only the ones
    # reaching a real park may be composed in.
    main_graph = nx.MultiDiGraph()
    main_graph.add_node("m1", x=-73.95, y=40.05)

    sidewalks = nx.MultiDiGraph()
    # In park reach: a park-entrance path, the case this exists for.
    sidewalks.add_node("in1", x=-73.9500, y=40.0500)
    sidewalks.add_node("in2", x=-73.9501, y=40.0501)
    sidewalks.add_edge("in1", "in2")
    # Several km away: an ordinary street's sidewalk, must be dropped.
    sidewalks.add_node("out1", x=-73.9900, y=40.0900)
    sidewalks.add_node("out2", x=-73.9901, y=40.0901)
    sidewalks.add_edge("out1", "out2")

    _mock_fetch(
        monkeypatch, tmp_path,
        _by_filter(lambda **kwargs: main_graph, any_sidewalk_fn=lambda **kwargs: sidewalks),
    )
    result = streets.fetch_streets(
        BBOX, "test-park-reach-tile", park_reach=_park_reach_around(-73.95, 40.05)
    )

    assert {"in1", "in2"} <= set(result.nodes)
    assert not {"out1", "out2"} & set(result.nodes)


def test_fetch_streets_survives_a_tile_where_no_sidewalk_reaches_a_park(monkeypatch, tmp_path):
    # Not an edge case -- this is most of the city. Any tile with no park
    # inside PARK_REACH_BUFFER_M drops every segment the sidewalk query
    # returned, and _park_reach_sidewalks() hands back None rather than an
    # empty graph (composing an edgeless graph in would contribute stray
    # nodes with no edges, the same shape as the Bronx r22c19 case
    # run_tile.py already guards against). Untested, this would have
    # crashed on the first park-free tile of a 276-tile citywide run.
    main_graph = nx.MultiDiGraph()
    main_graph.add_node("m1", x=-73.95, y=40.05)

    sidewalks = nx.MultiDiGraph()
    sidewalks.add_node("far1", x=-73.9900, y=40.0900)
    sidewalks.add_node("far2", x=-73.9901, y=40.0901)
    sidewalks.add_edge("far1", "far2")

    _mock_fetch(
        monkeypatch, tmp_path,
        _by_filter(lambda **kwargs: main_graph, any_sidewalk_fn=lambda **kwargs: sidewalks),
    )
    result = streets.fetch_streets(
        BBOX, "test-parkless-tile", park_reach=_park_reach_around(-73.95, 40.05)
    )

    assert list(result.nodes) == ["m1"]


def test_fetch_streets_narrows_sidewalks_before_simplifying_not_after(monkeypatch, tmp_path):
    # Load-bearing ordering, measured: filtering before the single simplify
    # pass costs +0% edges in a park-free area (output byte-identical to not
    # running the query at all), while filtering afterwards costs +14% --
    # sidewalk ways present at simplify time preserve intersection nodes
    # that would otherwise collapse, and deleting their edges later strands
    # those nodes in the graph forever. So the out-of-reach sidewalk must
    # never reach simplify_graph() at all, not merely be absent from the
    # final result.
    main_graph = nx.MultiDiGraph()
    main_graph.add_node("m1", x=-73.95, y=40.05)

    sidewalks = nx.MultiDiGraph()
    sidewalks.add_node("in1", x=-73.9500, y=40.0500)
    sidewalks.add_node("in2", x=-73.9501, y=40.0501)
    sidewalks.add_edge("in1", "in2")
    sidewalks.add_node("out1", x=-73.9900, y=40.0900)
    sidewalks.add_node("out2", x=-73.9901, y=40.0901)
    sidewalks.add_edge("out1", "out2")

    seen = []

    def fake_simplify(graph, **kwargs):
        seen.append(set(graph.nodes))
        return graph

    monkeypatch.setattr(streets.ox.simplification, "simplify_graph", fake_simplify)
    _mock_fetch(
        monkeypatch, tmp_path,
        _by_filter(lambda **kwargs: main_graph, any_sidewalk_fn=lambda **kwargs: sidewalks),
    )
    streets.fetch_streets(
        BBOX, "test-prefilter-tile", park_reach=_park_reach_around(-73.95, 40.05)
    )

    assert len(seen) == 1
    assert not {"out1", "out2"} & seen[0]


def test_fetch_streets_skips_the_sidewalk_query_without_a_park_reach_shape(monkeypatch, tmp_path):
    # No park data available (the pilot tile, CI) must still produce a real
    # graph -- the query is skipped entirely rather than run unfiltered,
    # which would re-admit every duplicate street sidewalk citywide.
    main_graph = nx.MultiDiGraph()
    main_graph.add_node("m1", x=-73.95, y=40.05)

    asked = []

    def record_sidewalk_query(**kwargs):
        asked.append(kwargs.get("custom_filter"))
        raise AssertionError("ANY_SIDEWALK_FILTER must not be queried without park reach")

    _mock_fetch(
        monkeypatch, tmp_path,
        _by_filter(lambda **kwargs: main_graph, any_sidewalk_fn=record_sidewalk_query),
    )
    result = streets.fetch_streets(BBOX, "test-no-park-reach-tile")

    assert list(result.nodes) == ["m1"]
    assert asked == []


def test_fetch_streets_caches_park_reach_and_plain_results_under_different_names(monkeypatch, tmp_path):
    # The two results genuinely differ, so they must never share a cache
    # entry: a tile fetched once without park data (pilot/CI) would
    # otherwise be reused forever afterwards as if it had the park-reach
    # sidewalks in it, silently un-fixing the bug on any machine that
    # happened to run it that way first.
    main_graph = nx.MultiDiGraph()
    main_graph.add_node("m1", x=-73.95, y=40.05)

    saved = []
    _mock_fetch(monkeypatch, tmp_path, _by_filter(lambda **kwargs: main_graph))
    monkeypatch.setattr(streets.ox, "save_graphml", lambda graph, path: saved.append(path))

    streets.fetch_streets(BBOX, "test-cache-variant-tile", park_reach=_park_reach_around(-73.95, 40.05))
    streets.fetch_streets(BBOX, "test-cache-variant-tile")

    assert len(saved) == 2
    assert saved[0] != saved[1]


# FIXES.md item 1b: parking_aisle ways are excluded from WALK_FILTER
# entirely (real duplicates/dead-ends are the common case), but some form
# a real through-path across a large lot -- confirmed citywide (646
# substantial lots) and validated directly against 8 real examples
# (2026-08-08): Lowe's Gowanus, several Staten Island big-box stores, and
# Aviator Sports/Floyd Bennett Field all connect to the surrounding street
# network at 2+ distinct points. _through_path_parking_aisles() is the
# connectivity check that tells a real through-path apart from a dead-end
# spur into a single row of parking spaces -- a graph-topology question,
# not a tag lookup, unlike 1a/1d.

def test_through_path_parking_aisles_keeps_a_cluster_touching_the_street_twice():
    graph = nx.MultiDiGraph()
    graph.add_node("street_a")
    graph.add_node("street_b")

    aisles = nx.MultiDiGraph()
    aisles.add_edge("street_a", "mid")
    aisles.add_edge("mid", "street_b")

    result = streets._through_path_parking_aisles(graph, aisles, "test-tile")

    assert set(result.edges()) == {("street_a", "mid"), ("mid", "street_b")}


def test_through_path_parking_aisles_drops_a_dead_end_cluster():
    # Only one attachment point (street_a) -- street_a to a "dead_end"
    # node with no other exit is a spur, not a shortcut.
    graph = nx.MultiDiGraph()
    graph.add_node("street_a")

    aisles = nx.MultiDiGraph()
    aisles.add_edge("street_a", "mid")
    aisles.add_edge("mid", "dead_end")

    assert streets._through_path_parking_aisles(graph, aisles, "test-tile") is None


def test_through_path_parking_aisles_drops_a_cluster_with_no_street_connection():
    graph = nx.MultiDiGraph()
    graph.add_node("street_a")

    aisles = nx.MultiDiGraph()
    aisles.add_edge("isolated_1", "isolated_2")

    assert streets._through_path_parking_aisles(graph, aisles, "test-tile") is None


def test_through_path_parking_aisles_evaluates_each_cluster_independently():
    # Two unrelated lots in the same fetched aisle graph: one a real
    # through-path (street_a <-> street_b), the other a dead-end off
    # street_c. Only the real one's edges should survive.
    graph = nx.MultiDiGraph()
    graph.add_node("street_a")
    graph.add_node("street_b")
    graph.add_node("street_c")

    aisles = nx.MultiDiGraph()
    aisles.add_edge("street_a", "mid1")
    aisles.add_edge("mid1", "street_b")
    aisles.add_edge("street_c", "mid2")
    aisles.add_edge("mid2", "dead_end")

    result = streets._through_path_parking_aisles(graph, aisles, "test-tile")

    assert set(result.edges()) == {("street_a", "mid1"), ("mid1", "street_b")}
    assert "dead_end" not in result.nodes


def test_through_path_parking_aisles_returns_none_when_given_none():
    graph = nx.MultiDiGraph()
    assert streets._through_path_parking_aisles(graph, None, "test-tile") is None


def test_fetch_streets_unions_in_through_path_parking_aisles(monkeypatch, tmp_path):
    # street_a and street_b are deliberately NOT connected within the main
    # street graph itself -- each sits on its own separate little edge, so
    # any path between them can only come from the aisle union. "mid" is a
    # plain degree-2 chain node that simplify_graph() correctly collapses
    # into a single street_a<->street_b edge, same as it would for any real
    # intermediate OSM node -- hence checking connectivity, not that
    # specific node, survived.
    main_graph = nx.MultiDiGraph()
    main_graph.add_node("street_a", x=-73.95, y=40.05)
    main_graph.add_node("other_a", x=-73.949, y=40.049)
    main_graph.add_edge("street_a", "other_a")
    main_graph.add_node("street_b", x=-73.951, y=40.051)
    main_graph.add_node("other_b", x=-73.952, y=40.052)
    main_graph.add_edge("street_b", "other_b")

    aisles = nx.MultiDiGraph()
    aisles.add_node("street_a", x=-73.95, y=40.05)
    aisles.add_node("mid", x=-73.9505, y=40.0505)
    aisles.add_node("street_b", x=-73.951, y=40.051)
    aisles.add_edge("street_a", "mid")
    aisles.add_edge("mid", "street_b")

    _mock_fetch(
        monkeypatch, tmp_path,
        _by_filter(lambda **kwargs: main_graph, parking_aisle_fn=lambda **kwargs: aisles),
    )
    result = streets.fetch_streets(BBOX, "test-aisle-tile")

    # street_a/mid/street_b are themselves plain degree-2 pass-through
    # points once composed (each with exactly one neighbor on either
    # side), so simplify_graph() collapses the whole chain into a single
    # edge and none of them necessarily survive as nodes -- other_a and
    # other_b are the real endpoints (degree 1), guaranteed to survive,
    # and connectivity between them can only exist via the aisle path.
    assert nx.has_path(result.to_undirected(), "other_a", "other_b")


def test_fetch_streets_works_with_no_parking_aisles_in_the_area(monkeypatch, tmp_path):
    # Most tiles have none -- the common real case, same as every other
    # narrower query.
    main_graph = nx.MultiDiGraph()
    main_graph.add_node("m1", x=-73.95, y=40.05)

    _mock_fetch(monkeypatch, tmp_path, _by_filter(lambda **kwargs: main_graph))
    result = streets.fetch_streets(BBOX, "test-no-aisle-tile")

    assert list(result.nodes) == ["m1"]


def test_fetch_streets_drops_dead_end_parking_aisles(monkeypatch, tmp_path):
    main_graph = nx.MultiDiGraph()
    main_graph.add_node("street_a", x=-73.95, y=40.05)

    aisles = nx.MultiDiGraph()
    aisles.add_node("street_a", x=-73.95, y=40.05)
    aisles.add_node("dead_end", x=-73.9505, y=40.0505)
    aisles.add_edge("street_a", "dead_end")

    _mock_fetch(
        monkeypatch, tmp_path,
        _by_filter(lambda **kwargs: main_graph, parking_aisle_fn=lambda **kwargs: aisles),
    )
    result = streets.fetch_streets(BBOX, "test-dead-end-aisle-tile")

    assert "dead_end" not in result.nodes


# FIXES.md item 1a: NYC's Interior Sidewalk Centerline data (real off-ROW
# walking paths in parks, NYCHA, hospital/school campuses, and ordinary
# residential complexes) arrives as independently-digitized line segments
# from an ArcGIS source, not OSM ways -- so there's no shared node id to
# rely on the way the other five filters above get for free. Three
# functions handle this: _interior_sidewalks_for_tile() narrows the
# citywide cache to one tile, _build_interior_sidewalk_graph() stitches
# that tile's segments into one shape (merging endpoints that coincide
# within INTERIOR_SIDEWALK_MERGE_TOLERANCE_M, since two segments meeting
# at the same real point don't necessarily share a coordinate exactly),
# and _snap_interior_sidewalks() connects the result's loose ends onto the
# real street network.
#
# The snap distance (INTERIOR_SIDEWALK_SNAP_MAX_M = 5.0) and the decision
# to check for a real barrier (fence/wall/hedge) crossing the connection
# were both settled against real data, not guessed: median real distance
# from a loose end to the nearest street is 0.6m, 95% are within 5m, and
# the two datasets are independently digitized enough that snapping to
# the nearest existing NODE instead of the nearest EDGE would misplace
# 63% of real connections by >3m (confirmed live, Holmes Towers, NYCHA,
# Manhattan). The barrier check is real but known-incomplete -- OSM's
# fence/wall tagging is crowdsourced and nowhere near complete, and no
# professionally-surveyed dataset covers ordinary fencing (checked; only
# a narrow "Retaining Wall" layer exists) -- so 5m was chosen specifically
# to keep the unverifiable "invisible fence" risk small, not to maximize
# how many real connections get captured.

def _segment_feature(coords):
    return {"type": "Feature", "properties": {}, "geometry": {"type": "LineString", "coordinates": coords}}


def test_interior_sidewalks_for_tile_keeps_segments_inside_the_bbox():
    geojson = {"type": "FeatureCollection", "features": [
        _segment_feature([[-73.95, 40.05], [-73.949, 40.051]]),
    ]}

    result = streets._interior_sidewalks_for_tile(geojson, BBOX)

    assert result == [[(-73.95, 40.05), (-73.949, 40.051)]]


def test_interior_sidewalks_for_tile_drops_segments_outside_the_bbox():
    geojson = {"type": "FeatureCollection", "features": [
        _segment_feature([[10.0, 10.0], [10.001, 10.001]]),  # nowhere near BBOX
    ]}

    assert streets._interior_sidewalks_for_tile(geojson, BBOX) == []


def test_interior_sidewalks_for_tile_keeps_a_segment_crossing_the_boundary():
    # One end outside BBOX (lon_max=-73.9), one end inside -- a real
    # segment straddling a tile edge should still count as belonging here,
    # same reasoning WALK_FILTER's own tile fetches rely on.
    geojson = {"type": "FeatureCollection", "features": [
        _segment_feature([[-73.95, 40.05], [-73.8, 40.05]]),
    ]}

    assert streets._interior_sidewalks_for_tile(geojson, BBOX) != []


def test_build_interior_sidewalk_graph_returns_none_for_no_segments():
    assert streets._build_interior_sidewalk_graph([], "test-tile") is None


def test_build_interior_sidewalk_graph_merges_exactly_touching_endpoints():
    segments = [
        [(-73.95, 40.05), (-73.949, 40.05)],
        [(-73.949, 40.05), (-73.948, 40.05)],  # shares an exact coordinate
    ]

    result = streets._build_interior_sidewalk_graph(segments, "test-tile")

    assert result.number_of_nodes() == 3  # not 4 -- the shared point is one node
    assert nx.is_connected(result.to_undirected())


def test_build_interior_sidewalk_graph_merges_endpoints_within_tolerance():
    # ~0.3m apart (well under INTERIOR_SIDEWALK_MERGE_TOLERANCE_M=1.0) --
    # independently-digitized segments meeting at the same real point
    # essentially never share an exact coordinate.
    segments = [
        [(-73.95, 40.05), (-73.949, 40.05)],
        [(-73.949, 40.0500027), (-73.948, 40.05)],
    ]

    result = streets._build_interior_sidewalk_graph(segments, "test-tile")

    assert result.number_of_nodes() == 3
    assert nx.is_connected(result.to_undirected())


def test_build_interior_sidewalk_graph_does_not_merge_endpoints_beyond_tolerance():
    # ~5m apart -- clearly beyond the 1m tolerance, so these are two real,
    # separate loose ends, not one shared point.
    segments = [
        [(-73.95, 40.05), (-73.949, 40.05)],
        [(-73.949, 40.05005), (-73.948, 40.05)],
    ]

    result = streets._build_interior_sidewalk_graph(segments, "test-tile")

    assert result.number_of_nodes() == 4
    assert not nx.is_connected(result.to_undirected())


def test_build_interior_sidewalk_graph_gives_every_edge_a_synthetic_osmid():
    # Real bug (2026-08-09): these edges came from ArcGIS data, not OSM, so
    # they never had an osmid at all -- osmnx's own to_undirected()
    # (centerline.build_edge_table, called later in the real pipeline)
    # requires one on every edge, and crashed with a bare KeyError the
    # first time this ran end-to-end on a real tile (Marine Park, r7c14).
    # Distinct per edge, and negative -- real OSM ids are always large
    # positive ints, confirmed against real cached tiles -- so nothing
    # here can collide with a real osmid once composed with the street
    # graph, same reasoning as this function's synthetic node ids.
    segments = [
        [(-73.95, 40.05), (-73.949, 40.05)],
        [(-73.949, 40.05), (-73.948, 40.05)],
    ]

    result = streets._build_interior_sidewalk_graph(segments, "test-tile")

    osmids = [data["osmid"] for _, _, data in result.edges(data=True)]
    assert all(isinstance(osmid, int) and osmid < 0 for osmid in osmids)
    assert len(set(osmids)) == len(osmids)  # each edge gets its own, not shared


def test_build_interior_sidewalk_graph_respects_custom_start_ids():
    # FIXES.md item 1g needs this: once park trails are a second synthetic
    # source composed into the same tile, its ids have to continue from
    # wherever interior sidewalks (or any earlier source) left off, not
    # restart at -1 and collide with an id that already means a different
    # real-world point.
    segments = [[(-73.95, 40.05), (-73.949, 40.05)]]

    result = streets._build_interior_sidewalk_graph(
        segments, "test-tile", start_id=-100, start_edge_osmid=-200,
    )

    assert max(result.nodes) == -100  # the first node gets exactly the given start
    osmids = [data["osmid"] for _, _, data in result.edges(data=True)]
    assert osmids == [-200]


def test_build_interior_sidewalk_graph_respects_a_custom_merge_tolerance():
    # ~5m apart -- beyond the default 1m interior-sidewalk tolerance
    # (see test_build_interior_sidewalk_graph_does_not_merge_endpoints_
    # beyond_tolerance above), but within a wider tolerance a different
    # synthetic source might legitimately need.
    segments = [
        [(-73.95, 40.05), (-73.949, 40.05)],
        [(-73.949, 40.05005), (-73.948, 40.05)],
    ]

    result = streets._build_interior_sidewalk_graph(segments, "test-tile", merge_tolerance_m=10.0)

    assert result.number_of_nodes() == 3
    assert nx.is_connected(result.to_undirected())


def test_uncovered_trail_segments_returns_the_whole_line_when_nothing_covers_it():
    trail = LineString([(0, 0), (100, 0)])
    covered = box(500, 500, 600, 600)  # nowhere near the trail

    result = streets._uncovered_trail_segments(trail, covered, min_length_m=10)

    assert len(result) == 1
    assert result[0].equals(trail)


def test_uncovered_trail_segments_returns_empty_when_fully_covered():
    trail = LineString([(0, 0), (100, 0)])
    covered = box(-10, -10, 110, 10)  # comfortably contains the whole trail

    assert streets._uncovered_trail_segments(trail, covered, min_length_m=10) == []


def test_uncovered_trail_segments_splits_into_multiple_pieces_when_partially_covered():
    # A real, not hypothetical, shape: an 8m buffer against a trail that's
    # only covered in its middle stretch leaves two disconnected leftover
    # pieces, not one -- FIXES.md item 1g's own three-park survey found
    # exactly this pattern (a trail dipping in and out of OSM coverage
    # along its length).
    trail = LineString([(0, 0), (300, 0)])
    covered = box(90, -5, 210, 5)  # covers the middle third only

    result = streets._uncovered_trail_segments(trail, covered, min_length_m=10)

    assert len(result) == 2
    lengths = sorted(piece.length for piece in result)
    assert lengths == pytest.approx([90, 90])


def test_uncovered_trail_segments_drops_pieces_below_the_minimum_length():
    # Same middle-covered shape, but one leftover end is a short 5m sliver
    # (digitization noise -- the buffer's edge doesn't land exactly on a
    # real gap boundary) and the other is a real 90m gap.
    trail = LineString([(0, 0), (300, 0)])
    covered = box(5, -5, 210, 5)  # covers everything except a 5m sliver and a 90m gap

    result = streets._uncovered_trail_segments(trail, covered, min_length_m=10)

    assert len(result) == 1
    assert result[0].length == pytest.approx(90)


def _trail_feature(coords, trail_class="Class IV : Highly Developed"):
    return {"type": "Feature", "properties": {"class": trail_class},
            "geometry": {"type": "LineString", "coordinates": coords}}


def test_park_trails_for_tile_keeps_class_iv_and_v_only():
    geojson = {"type": "FeatureCollection", "features": [
        _trail_feature([[-73.95, 40.05], [-73.949, 40.05]], "Class IV : Highly Developed"),
        _trail_feature([[-73.95, 40.06], [-73.949, 40.06]], "Class V : Fully Developed"),
        _trail_feature([[-73.95, 40.07], [-73.949, 40.07]], "Class III : Developed/Improved"),
    ]}

    result = streets._park_trails_for_tile(geojson, BBOX)

    assert len(result) == 2


def test_park_trails_for_tile_drops_segments_outside_the_bbox():
    geojson = {"type": "FeatureCollection", "features": [
        _trail_feature([[10.0, 10.0], [10.001, 10.001]]),  # nowhere near BBOX
    ]}

    assert streets._park_trails_for_tile(geojson, BBOX) == []


def test_park_trails_for_tile_explodes_a_multilinestring_into_separate_parts():
    # A real shape in the raw data: one row's `shape` can be a
    # MultiLineString with more than one disconnected part.
    geojson = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"class": "Class IV : Highly Developed"},
         "geometry": {"type": "MultiLineString", "coordinates": [
             [[-73.95, 40.05], [-73.949, 40.05]],
             [[-73.94, 40.05], [-73.939, 40.05]],
         ]}},
    ]}

    result = streets._park_trails_for_tile(geojson, BBOX)

    assert len(result) == 2


def _street_graph_with_one_edge():
    graph = nx.MultiDiGraph()
    graph.add_node("s1", x=-73.9500, y=40.0500)
    graph.add_node("s2", x=-73.9490, y=40.0500)
    graph.add_edge("s1", "s2", osmid=555)
    return graph


def _interior_graph_with_one_loose_end(lon, lat):
    interior = nx.MultiDiGraph()
    interior.add_node("loose_end", x=lon, y=lat)
    interior.add_node("anchor", x=lon, y=lat + 0.001)  # far away, not near any street
    interior.add_edge("loose_end", "anchor", osmid=-1)  # as _build_interior_sidewalk_graph would set
    return interior


def test_snap_interior_sidewalks_connects_a_loose_end_within_range():
    # ~3.3m from the s1-s2 edge -- comfortably under the 5m snap distance.
    graph = _street_graph_with_one_edge()
    interior = _interior_graph_with_one_loose_end(-73.9495, 40.05003)

    result = streets._snap_interior_sidewalks(graph, interior, None, "test-tile")

    undirected = result.to_undirected()
    assert nx.has_path(undirected, "loose_end", "s1")
    assert nx.has_path(undirected, "loose_end", "s2")


def test_snap_interior_sidewalks_leaves_a_far_loose_end_disconnected():
    # ~22m from the s1-s2 edge -- well beyond the 5m snap distance, a real
    # digitization gap rather than something to force-connect.
    graph = _street_graph_with_one_edge()
    interior = _interior_graph_with_one_loose_end(-73.9495, 40.05020)

    result = streets._snap_interior_sidewalks(graph, interior, None, "test-tile")

    undirected = result.to_undirected()
    assert not nx.has_path(undirected, "loose_end", "s1")
    assert "loose_end" in result.nodes  # kept, just not connected to the street


def test_snap_interior_sidewalks_skips_a_connection_blocked_by_a_real_barrier():
    # Same geometry as the "connects" case above, but a fence crosses the
    # straight line between the loose end and its snap point -- known
    # incomplete data (OSM's own fence tagging is crowdsourced), but a
    # real, mapped barrier here is real evidence the two points aren't
    # actually walkably connected, distance notwithstanding.
    graph = _street_graph_with_one_edge()
    interior = _interior_graph_with_one_loose_end(-73.9495, 40.05003)

    barriers = nx.MultiDiGraph()
    barriers.add_node("b1", x=-73.9496, y=40.050015)
    barriers.add_node("b2", x=-73.9494, y=40.050015)
    barriers.add_edge("b1", "b2")

    result = streets._snap_interior_sidewalks(graph, interior, barriers, "test-tile")

    assert not nx.has_path(result.to_undirected(), "loose_end", "s1")


def test_snap_interior_sidewalks_avoids_id_collision_with_a_prior_synthetic_source():
    # FIXES.md item 1g: once park trails are snapped in AFTER interior
    # sidewalks (a second call in the same tile), `graph` (the
    # destination) already contains negative synthetic ids from that
    # first pass. This call's own new split-node numbering must not
    # restart at -1 and silently collide with an id that already means a
    # different real-world point once composed -- nx.compose would
    # silently merge the two into one node instead of raising anything.
    graph = _street_graph_with_one_edge()
    graph.add_node(-1, x=-73.9500, y=40.06)  # simulates a node an earlier
                                              # synthetic source already placed
    graph.add_edge("s1", -1, osmid=-1, length=1000.0)

    interior = _interior_graph_with_one_loose_end(-73.9495, 40.05003)

    result = streets._snap_interior_sidewalks(graph, interior, None, "test-tile")

    # The pre-existing node's own identity must survive untouched -- if
    # the new split node collided with it, this would instead read back
    # as wherever the split landed on the s1-s2 edge (~40.05), not 40.06.
    assert result.nodes[-1]["y"] == 40.06


def test_snap_interior_sidewalks_respects_a_custom_snap_max():
    # Same ~22m-away loose end that the default 5m threshold leaves
    # disconnected (see test_snap_interior_sidewalks_leaves_a_far_loose_
    # end_disconnected above) -- park trails need a larger threshold,
    # since a trimmed trail's own endpoint can legitimately sit close to
    # PARK_TRAIL_OVERLAP_BUFFER_M (8m) from the nearest covered edge by
    # construction (see PARK_TRAIL_SNAP_MAX_M's own comment).
    graph = _street_graph_with_one_edge()
    interior = _interior_graph_with_one_loose_end(-73.9495, 40.05020)

    result = streets._snap_interior_sidewalks(graph, interior, None, "test-tile", snap_max_m=25.0)

    undirected = result.to_undirected()
    assert nx.has_path(undirected, "loose_end", "s1")


def test_snap_interior_sidewalks_evaluates_each_loose_end_independently():
    # close and far are two SEPARATE, unconnected little interior paths
    # (not two ends of the same one) -- a single shared edge between them
    # would make this test unsatisfiable by construction: once "close"
    # snaps to the street, "far" would become transitively connected to it
    # regardless of what the snapping logic actually does.
    graph = _street_graph_with_one_edge()
    interior = nx.MultiDiGraph()
    interior.add_node("close", x=-73.9495, y=40.05003)
    interior.add_node("close_anchor", x=-73.9495, y=40.052)
    interior.add_edge("close", "close_anchor")
    interior.add_node("far", x=-73.9495, y=40.05020)
    interior.add_node("far_anchor", x=-73.9495, y=40.052)
    interior.add_edge("far", "far_anchor")

    result = streets._snap_interior_sidewalks(graph, interior, None, "test-tile")

    undirected = result.to_undirected()
    assert nx.has_path(undirected, "close", "s1")
    assert not nx.has_path(undirected, "far", "s1")


def test_snap_interior_sidewalks_splits_the_same_street_edge_for_two_different_loose_ends():
    # Two unrelated interior paths (e.g. two separate courtyard entrances
    # onto the same block) both land near the s1-s2 edge, at two different
    # points along it. The second split must not silently ignore or
    # overwrite the first -- both connections need to survive
    # independently, and the two loose ends should also end up connected
    # to each other via the (now twice-split) street edge.
    graph = _street_graph_with_one_edge()
    interior = nx.MultiDiGraph()
    interior.add_node("close_1", x=-73.9497, y=40.05003)
    interior.add_node("anchor_1", x=-73.9497, y=40.052)
    interior.add_edge("close_1", "anchor_1")
    interior.add_node("close_2", x=-73.9493, y=40.05003)
    interior.add_node("anchor_2", x=-73.9493, y=40.052)
    interior.add_edge("close_2", "anchor_2")

    result = streets._snap_interior_sidewalks(graph, interior, None, "test-tile")

    undirected = result.to_undirected()
    assert nx.has_path(undirected, "close_1", "s1")
    assert nx.has_path(undirected, "close_1", "s2")
    assert nx.has_path(undirected, "close_2", "s1")
    assert nx.has_path(undirected, "close_2", "s2")
    assert nx.has_path(undirected, "close_1", "close_2")


def test_snap_interior_sidewalks_gives_every_new_edge_an_osmid():
    # Real bug (2026-08-09): a split street sub-segment and the new
    # connector edge to the interior sidewalk's loose end were both being
    # added without an osmid, which crashed centerline.build_edge_table()
    # (via osmnx's own to_undirected()) the first time this ran end-to-end
    # on a real tile with a true parallel edge (Marine Park, r7c14) --
    # never caught by the narrower unit tests above, which only check
    # connectivity, not this attribute. A split sub-segment genuinely is
    # still part of the original street (s1-s2, osmid=555 from the fixture
    # above), so it should keep that real id; the brand-new connector edge
    # to the interior sidewalk never existed in OSM at all, so it needs its
    # own synthetic one instead.
    graph = _street_graph_with_one_edge()
    interior = _interior_graph_with_one_loose_end(-73.9495, 40.05003)

    result = streets._snap_interior_sidewalks(graph, interior, None, "test-tile")

    for u, v, k, data in result.edges(keys=True, data=True):
        assert data.get("osmid") is not None, f"edge ({u}, {v}) is missing osmid"

    split_segment_osmids = {
        data["osmid"] for u, v, k, data in result.edges(keys=True, data=True)
        if "geometry" in data
    }
    assert split_segment_osmids == {555}  # the real street's own id, preserved


def test_snap_interior_sidewalks_result_survives_build_edge_table():
    # The actual regression test for the real bug: running the snapped
    # result through the same downstream step the real pipeline does
    # (centerline.build_edge_table, which calls osmnx's to_undirected())
    # must not crash. Two loose ends snapping to the same street edge
    # (see the split test above) is what actually produces a true
    # parallel-edge scenario like the one that crashed on r7c14.
    from pipeline.graph.centerline import build_edge_table

    graph = _street_graph_with_one_edge()
    graph.graph["crs"] = "epsg:4326"
    interior = nx.MultiDiGraph()
    interior.add_node("close_1", x=-73.9497, y=40.05003)
    interior.add_node("anchor_1", x=-73.9497, y=40.052)
    interior.add_edge("close_1", "anchor_1", osmid=-1)
    interior.add_node("close_2", x=-73.9493, y=40.05003)
    interior.add_node("anchor_2", x=-73.9493, y=40.052)
    interior.add_edge("close_2", "anchor_2", osmid=-2)

    result = streets._snap_interior_sidewalks(graph, interior, None, "test-tile")

    build_edge_table(result)  # must not raise


def test_missing_park_trail_graph_returns_none_for_no_trail_lines():
    graph = _street_graph_with_one_edge()

    assert streets._missing_park_trail_graph(graph, [], "test-tile") is None


def test_missing_park_trail_graph_returns_none_when_everything_is_already_covered():
    graph = _street_graph_with_one_edge()  # s1 (-73.9500, 40.0500) -> s2 (-73.9490, 40.0500)
    trail = LineString([(-73.9500, 40.0500), (-73.9490, 40.0500)])  # exactly on top of it

    assert streets._missing_park_trail_graph(graph, [trail], "test-tile") is None


def test_missing_park_trail_graph_builds_a_graph_for_a_genuinely_missing_trail():
    graph = _street_graph_with_one_edge()
    trail = LineString([(-73.9000, 40.1000), (-73.8990, 40.1000)])  # nowhere near the street

    result = streets._missing_park_trail_graph(graph, [trail], "test-tile")

    assert result is not None
    assert result.number_of_edges() == 1


def test_missing_park_trail_graph_continues_synthetic_ids_from_the_existing_graph():
    # FIXES.md item 1g: if interior sidewalks already snapped a synthetic
    # source into this tile, park trails' own new ids must continue past
    # whatever's already there, not restart at -1 (see
    # _snap_interior_sidewalks' own comment on the same collision risk).
    graph = _street_graph_with_one_edge()
    graph.add_node(-5, x=-73.8, y=40.2)  # simulates a node an earlier synthetic source placed
    trail = LineString([(-73.9000, 40.1000), (-73.8990, 40.1000)])

    result = streets._missing_park_trail_graph(graph, [trail], "test-tile")

    assert all(n <= -6 for n in result.nodes)


def test_fetch_streets_unions_in_interior_sidewalks(monkeypatch, tmp_path):
    # street_a and street_b are deliberately NOT connected within the main
    # street graph itself (each on its own separate stub edge to
    # other_a/other_b), same test shape as the parking-aisle union test
    # above -- so any connectivity between other_a and other_b can only
    # come from the interior-sidewalk union + snap.
    from pipeline.fetch import interior_sidewalks

    main_graph = nx.MultiDiGraph()
    main_graph.add_node("street_a", x=-73.9500, y=40.0500)
    main_graph.add_node("other_a", x=-73.9501, y=40.0499)
    main_graph.add_edge("street_a", "other_a")
    main_graph.add_node("street_b", x=-73.9490, y=40.0500)
    main_graph.add_node("other_b", x=-73.9489, y=40.0499)
    main_graph.add_edge("street_b", "other_b")

    # One interior segment running roughly parallel to the (nonexistent)
    # street_a<->street_b connection, ~3.3m offset -- comfortably within
    # the 5m snap distance at both ends.
    interior_geojson = {"type": "FeatureCollection", "features": [
        _segment_feature([[-73.9500, 40.05003], [-73.9490, 40.05003]]),
    ]}
    monkeypatch.setattr(interior_sidewalks, "fetch_interior_sidewalks", lambda **kwargs: interior_geojson)

    _mock_fetch(monkeypatch, tmp_path, _by_filter(lambda **kwargs: main_graph))
    result = streets.fetch_streets(BBOX, "test-interior-sidewalk-tile")

    assert nx.has_path(result.to_undirected(), "other_a", "other_b")


def test_fetch_streets_works_with_no_interior_sidewalks_in_the_area(monkeypatch, tmp_path):
    # Most tiles have none -- the common real case, same as every other
    # narrower query.
    from pipeline.fetch import interior_sidewalks

    main_graph = nx.MultiDiGraph()
    main_graph.add_node("m1", x=-73.95, y=40.05)

    monkeypatch.setattr(
        interior_sidewalks, "fetch_interior_sidewalks",
        lambda **kwargs: {"type": "FeatureCollection", "features": []},
    )
    _mock_fetch(monkeypatch, tmp_path, _by_filter(lambda **kwargs: main_graph))
    result = streets.fetch_streets(BBOX, "test-no-interior-sidewalk-tile")

    assert list(result.nodes) == ["m1"]


def test_fetch_streets_unions_in_missing_park_trail_segments(monkeypatch, tmp_path):
    # Same test shape as test_fetch_streets_unions_in_interior_sidewalks:
    # street_a and street_b are deliberately unconnected within the main
    # graph, so any connectivity between other_a and other_b can only
    # come from the trail union + snap.
    main_graph = nx.MultiDiGraph()
    main_graph.add_node("street_a", x=-73.9500, y=40.0500)
    main_graph.add_node("other_a", x=-73.9501, y=40.0499)
    main_graph.add_edge("street_a", "other_a")
    main_graph.add_node("street_b", x=-73.9490, y=40.0500)
    main_graph.add_node("other_b", x=-73.9489, y=40.0499)
    main_graph.add_edge("street_b", "other_b")

    trail_geojson = {"type": "FeatureCollection", "features": [
        _trail_feature([[-73.9500, 40.05003], [-73.9490, 40.05003]]),
    ]}

    _mock_fetch(monkeypatch, tmp_path, _by_filter(lambda **kwargs: main_graph))
    monkeypatch.setattr(park_trails, "fetch_park_trails", lambda **kwargs: trail_geojson)
    result = streets.fetch_streets(BBOX, "test-park-trail-tile")

    assert nx.has_path(result.to_undirected(), "other_a", "other_b")


def test_fetch_streets_works_with_no_park_trails_in_the_area(monkeypatch, tmp_path):
    # Most tiles have none -- the common real case, same as every other
    # narrower query. (_mock_fetch's default already covers this, but an
    # explicit test documents the behavior same as every other source.)
    main_graph = nx.MultiDiGraph()
    main_graph.add_node("m1", x=-73.95, y=40.05)

    _mock_fetch(monkeypatch, tmp_path, _by_filter(lambda **kwargs: main_graph))
    result = streets.fetch_streets(BBOX, "test-no-park-trail-tile")

    assert list(result.nodes) == ["m1"]


def test_fetch_streets_does_not_duplicate_an_already_covered_park_trail(monkeypatch, tmp_path):
    # The one behavior park trails need that interior sidewalks never
    # did: most real trails mostly duplicate a path OSM already has
    # (FIXES.md item 1g's own three-park survey found 82-92% overlap), so
    # a trail sitting right on top of an existing street must NOT get
    # spliced in as a second, redundant edge.
    main_graph = nx.MultiDiGraph()
    main_graph.add_node("street_a", x=-73.9500, y=40.0500)
    main_graph.add_node("street_b", x=-73.9490, y=40.0500)
    main_graph.add_edge("street_a", "street_b")

    trail_geojson = {"type": "FeatureCollection", "features": [
        _trail_feature([[-73.9500, 40.0500], [-73.9490, 40.0500]]),  # exactly on top
    ]}

    _mock_fetch(monkeypatch, tmp_path, _by_filter(lambda **kwargs: main_graph))
    monkeypatch.setattr(park_trails, "fetch_park_trails", lambda **kwargs: trail_geojson)
    result = streets.fetch_streets(BBOX, "test-covered-park-trail-tile")

    assert set(result.nodes) == {"street_a", "street_b"}


def test_fetch_streets_leaves_a_too_far_interior_sidewalk_unconnected(monkeypatch, tmp_path):
    # A real digitization gap (~22m from the nearest street, well beyond
    # the 5m snap distance) should still be added to the graph, just not
    # wired into the surrounding street network -- same treatment as a
    # dead-end parking aisle cluster, not silently dropped and not
    # force-connected.
    from pipeline.fetch import interior_sidewalks

    main_graph = nx.MultiDiGraph()
    main_graph.add_node("street_a", x=-73.9500, y=40.0500)
    main_graph.add_node("street_b", x=-73.9490, y=40.0500)
    main_graph.add_edge("street_a", "street_b")

    interior_geojson = {"type": "FeatureCollection", "features": [
        _segment_feature([[-73.9495, 40.05020], [-73.9495, 40.06]]),
    ]}
    monkeypatch.setattr(interior_sidewalks, "fetch_interior_sidewalks", lambda **kwargs: interior_geojson)

    _mock_fetch(monkeypatch, tmp_path, _by_filter(lambda **kwargs: main_graph))
    result = streets.fetch_streets(BBOX, "test-far-interior-sidewalk-tile")

    # The interior segment's nodes get synthetic ids assigned internally
    # (see _build_interior_sidewalk_graph), so identify them by set
    # difference rather than a name this test doesn't control.
    new_nodes = set(result.nodes) - set(main_graph.nodes)
    assert new_nodes  # the far segment was still added, not dropped
    undirected = result.to_undirected()
    assert not any(nx.has_path(undirected, node, "street_a") for node in new_nodes)


def test_fetch_streets_fetches_barrier_ways_once_for_both_interior_sidewalks_and_park_trails(monkeypatch, tmp_path):
    # interior sidewalks (1a) and park trails (1g) each need barrier data
    # for their own snap pass -- same bbox/filter either way, so a tile
    # where both apply should cost one Overpass barrier-ways request, not
    # two. Found while investigating why the v18 citywide re-fetch ran
    # slower than v9's baseline: every tile with both sources was asking
    # Overpass for identical data twice.
    from pipeline.fetch import interior_sidewalks

    main_graph = nx.MultiDiGraph()
    main_graph.add_node("street_a", x=-73.9500, y=40.0500)
    main_graph.add_node("street_b", x=-73.9490, y=40.0500)
    main_graph.add_edge("street_a", "street_b")

    interior_geojson = {"type": "FeatureCollection", "features": [
        _segment_feature([[-73.9495, 40.05020], [-73.9495, 40.06]]),
    ]}
    monkeypatch.setattr(interior_sidewalks, "fetch_interior_sidewalks", lambda **kwargs: interior_geojson)

    trail_geojson = {"type": "FeatureCollection", "features": [
        _trail_feature([[-73.9480, 40.0700], [-73.9470, 40.0700]]),
    ]}

    barrier_calls = {"count": 0}

    def barrier_fn(**kwargs):
        barrier_calls["count"] += 1
        raise ValueError("no barrier ways here")

    _mock_fetch(monkeypatch, tmp_path, _by_filter(lambda **kwargs: main_graph, barrier_fn=barrier_fn))
    monkeypatch.setattr(park_trails, "fetch_park_trails", lambda **kwargs: trail_geojson)
    streets.fetch_streets(BBOX, "test-shared-barrier-fetch-tile")

    assert barrier_calls["count"] == 1


# ---- closure zones (FIXES.md item 0b, v19) --------------------------------
# The imported synthetic layers must not re-add paths through a known
# construction closure (East River Park: OSM's mappers DELETED the paths,
# so no tag can infer the closure -- measured 2026-08-14, 0/32 ghost edges
# inside any landuse=construction polygon). Zones are curated polygons in
# pipeline/closure_zones.json; the clip runs in METRIC_CRS.


def _square_zone_m(lon, lat, half_m):
    """A test closure zone: a square centered on lon/lat, half_m meters
    to each side, in METRIC_CRS like the real loaded zones."""
    from shapely.geometry import Polygon

    x, y = streets._TO_METRIC_CRS(lon, lat)
    return Polygon([(x - half_m, y - half_m), (x + half_m, y - half_m),
                    (x + half_m, y + half_m), (x - half_m, y + half_m)])


def test_clip_closure_zones_drops_a_segment_fully_inside(monkeypatch):
    monkeypatch.setattr(streets, "_CLOSURE_ZONES_M", [_square_zone_m(-73.99, 40.7, 200.0)])
    inside = LineString([(-73.9901, 40.7), (-73.9899, 40.7)])  # ~17m, centered
    assert streets._clip_closure_zones(inside) == []


def test_clip_closure_zones_keeps_the_outside_remnants(monkeypatch):
    monkeypatch.setattr(streets, "_CLOSURE_ZONES_M", [_square_zone_m(-73.99, 40.7, 100.0)])
    # ~640m west-to-east straight through the 200m-wide zone
    crossing = LineString([(-73.9938, 40.7), (-73.9862, 40.7)])
    kept = streets._clip_closure_zones(crossing)
    assert len(kept) == 2
    for piece in kept:
        for lon, lat in piece.coords:
            x, _ = streets._TO_METRIC_CRS(lon, lat)
            zx, _ = streets._TO_METRIC_CRS(-73.99, 40.7)
            assert abs(x - zx) >= 99.0  # every kept point is outside the zone


def test_clip_closure_zones_leaves_far_segments_untouched(monkeypatch):
    monkeypatch.setattr(streets, "_CLOSURE_ZONES_M", [_square_zone_m(-73.99, 40.7, 100.0)])
    far = LineString([(-73.95, 40.72), (-73.949, 40.72)])
    assert streets._clip_closure_zones(far) == [far]


def test_clip_closure_zones_drops_boundary_slivers(monkeypatch):
    # A remnant shorter than CLOSURE_ZONE_MIN_REMNANT_M is digitization
    # noise at the polygon's edge, not a usable path.
    monkeypatch.setattr(streets, "_CLOSURE_ZONES_M", [_square_zone_m(-73.99, 40.7, 100.0)])
    # ends ~5m past the zone's western edge: remnant ~5m < 10m floor
    x_west_edge = -73.99 - 100.0 / (111320.0 * 0.7578)  # rough deg-per-m at 40.7
    barely_out = LineString([(x_west_edge - 0.00006, 40.7), (-73.99, 40.7)])
    kept = streets._clip_closure_zones(barely_out)
    assert kept == [] or all(
        streets.transform(streets._TO_METRIC_CRS, piece).length
        >= streets.CLOSURE_ZONE_MIN_REMNANT_M
        for piece in kept
    )


def test_real_closure_zones_file_covers_east_river_park():
    # The shipped file must contain the ESCR zone: the point below is one
    # of the 32 confirmed ghost-edge locations (FIXES item 0b's answer
    # key, data/audits/2026-08-13/erp_phantoms.json).
    from shapely.geometry import Point as _Point

    assert len(streets._CLOSURE_ZONES_M) >= 1
    ghost = _Point(streets._TO_METRIC_CRS(-73.972923, 40.723911))
    assert any(zone.contains(ghost) for zone in streets._CLOSURE_ZONES_M)


def test_interior_sidewalks_for_tile_clips_closure_zones(monkeypatch):
    monkeypatch.setattr(streets, "_CLOSURE_ZONES_M", [_square_zone_m(-73.99, 40.7, 100.0)])
    geojson = {"features": [
        {"geometry": {"type": "LineString",
                      "coordinates": [[-73.9901, 40.7], [-73.9899, 40.7]]}},  # inside
        {"geometry": {"type": "LineString",
                      "coordinates": [[-73.95, 40.72], [-73.949, 40.72]]}},   # far away
    ]}
    tile_bbox = Bbox(lat_min=40.6, lat_max=40.8, lon_min=-74.1, lon_max=-73.9)
    segments = streets._interior_sidewalks_for_tile(geojson, tile_bbox)
    assert segments == [[(-73.95, 40.72), (-73.949, 40.72)]]
