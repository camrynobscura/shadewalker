"""Tests for pipeline/fetch/buildings.py: paging, the gzipped atomic cache,
and the rules that decide which footprints cast shade.

No network: get_with_retry is monkeypatched, as test_pipeline_fetch_planimetrics.py
does. The row rules use the real config constants on purpose -- a test row
just over the cap, or carrying the excluded status, follows the constant
if it ever moves.
"""

import gzip
import json

import pytest

from pipeline import config
from pipeline.fetch import buildings


class _FakeResponse:
    def __init__(self, rows):
        self._rows = rows

    def json(self):
        return self._rows


def _fake_source(total_rows: int, calls: list):
    def fake_get(url, params=None, headers=None):
        calls.append(params)
        start = params["$offset"]
        end = min(start + params["$limit"], total_rows)
        return _FakeResponse([{"bin": str(i), "height_roof": "20"}
                              for i in range(start, end)])
    return fake_get


def _row(**overrides) -> dict:
    row = {"bin": "1000000", "height_roof": "45", "feature_code": "2100",
           "last_status_type": "Constructed", "the_geom": {"type": "MultiPolygon",
                                                            "coordinates": []}}
    row.update(overrides)
    return row


# ── fetch + cache ────────────────────────────────────────────────────────────

def test_pages_until_a_short_page_selecting_columns_ordered_by_id(monkeypatch):
    calls = []
    total = buildings.PAGE * 2 + 5
    monkeypatch.setattr(buildings, "get_with_retry", _fake_source(total, calls))

    rows = buildings.fetch_all()

    assert len(rows) == total
    assert [r["bin"] for r in rows] == [str(i) for i in range(total)]
    assert len(calls) == 3
    assert [c["$offset"] for c in calls] == [0, buildings.PAGE, buildings.PAGE * 2]
    assert all(c["$order"] == ":id" for c in calls)
    # the column list is the contract with the shade step
    for column in ("the_geom", "height_roof", "ground_elevation",
                   "feature_code", "last_status_type", "last_edited_date"):
        assert column in calls[0]["$select"].split(",")


def test_load_writes_a_gzipped_versioned_cache_and_reads_it_back(monkeypatch, tmp_path):
    monkeypatch.setattr(buildings.config, "RAW_DIR", tmp_path)
    monkeypatch.setattr(buildings, "get_with_retry", _fake_source(3, []))

    first = buildings.load()
    assert [r["bin"] for r in first] == ["0", "1", "2"]

    path = buildings.cache_path()
    assert path.exists()
    assert path.name == f"buildings_v{config.BUILDINGS_CACHE_VERSION}.json.gz"
    with gzip.open(path, "rt") as fh:
        assert json.load(fh) == first

    def explode(*args, **kwargs):
        raise AssertionError("load() re-fetched instead of using the cache")
    monkeypatch.setattr(buildings, "get_with_retry", explode)
    assert buildings.load() == first


def test_refresh_bypasses_the_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(buildings.config, "RAW_DIR", tmp_path)
    monkeypatch.setattr(buildings, "get_with_retry", _fake_source(2, []))
    buildings.load()

    monkeypatch.setattr(buildings, "get_with_retry", _fake_source(5, []))
    assert len(buildings.load(refresh=True)) == 5


def test_a_failed_write_leaves_no_cache_and_no_tmp(monkeypatch, tmp_path):
    monkeypatch.setattr(buildings.config, "RAW_DIR", tmp_path)
    monkeypatch.setattr(buildings, "get_with_retry", _fake_source(2, []))

    def die(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(buildings.json, "dump", die)

    with pytest.raises(OSError):
        buildings.load()

    path = buildings.cache_path()
    assert not path.exists()
    assert list(path.parent.glob("*.tmp")) == []


# ── which rows cast shade ────────────────────────────────────────────────────

def test_height_ft_parses_strings_and_rejects_garbage():
    assert buildings.height_ft({"height_roof": "45.5"}) == 45.5
    assert buildings.height_ft({"height_roof": ""}) is None
    assert buildings.height_ft({}) is None
    assert buildings.height_ft({"height_roof": "tall"}) is None


def test_usable_keeps_ordinary_rows_and_every_shading_feature_code():
    rows = [_row(bin="1", feature_code="2100"),   # building
            _row(bin="2", feature_code="5110"),   # garage
            _row(bin="3", feature_code="1001"),   # gas-station canopy
            _row(bin="4", feature_code="2110"),   # skybridge
            _row(bin="5", last_status_type="Marked for Demolition"),
            _row(bin="6", last_status_type="Investigate Demolition")]
    assert [r["bin"] for r in buildings.usable(rows)] == ["1", "2", "3", "4", "5", "6"]


def test_usable_drops_over_cap_rows_rather_than_clipping_them():
    cap = config.BUILDING_HEIGHT_CAP_FT
    rows = [_row(bin="ok", height_roof=str(cap)),
            _row(bin="garbage", height_roof=str(cap + 1)),
            _row(bin="bin-as-height", height_roof="2130353")]
    kept = buildings.usable(rows)
    assert [r["bin"] for r in kept] == ["ok"]
    assert kept[0]["height_roof"] == str(cap)   # untouched, not clipped


def test_usable_drops_non_positive_and_missing_heights():
    rows = [_row(bin="zero", height_roof="0"),
            _row(bin="negative", height_roof="-3"),
            _row(bin="blank", height_roof=""),
            {"bin": "absent", "feature_code": "2100"},
            _row(bin="fine", height_roof="12")]
    assert [r["bin"] for r in buildings.usable(rows)] == ["fine"]


def test_usable_drops_demolished_and_placeholder_rows():
    status = next(iter(config.BUILDING_EXCLUDED_STATUSES))
    code = next(iter(config.BUILDING_EXCLUDED_FEATURE_CODES))
    rows = [_row(bin="gone", last_status_type=status),
            _row(bin="placeholder", feature_code=code),
            _row(bin="fine")]
    assert [r["bin"] for r in buildings.usable(rows)] == ["fine"]


def test_usable_logs_one_tally_line_counting_each_drop_once(caplog):
    cap = config.BUILDING_HEIGHT_CAP_FT
    status = next(iter(config.BUILDING_EXCLUDED_STATUSES))
    code = next(iter(config.BUILDING_EXCLUDED_FEATURE_CODES))
    rows = [_row(bin="a"),
            _row(bin="b", height_roof="0"),
            _row(bin="c", height_roof=str(cap * 10)),
            _row(bin="d", last_status_type=status),
            _row(bin="e", feature_code=code),
            # fails two rules; must be counted under the first only
            _row(bin="f", height_roof="-1", last_status_type=status)]
    with caplog.at_level("INFO", logger="pipeline.fetch.buildings"):
        kept = buildings.usable(rows)
    assert [r["bin"] for r in kept] == ["a"]
    tallies = [m for m in caplog.messages if "[buildings]" in m]
    assert len(tallies) == 1
    line = tallies[0]
    assert "6 rows, 1 kept" in line
    assert "2 non-positive height" in line
    assert f"1 over {cap} ft cap" in line
    assert "1 excluded status" in line
    assert "1 placeholder code" in line
