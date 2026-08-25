"""Tests for pipeline/fetch/planimetrics.py's paging, caching and layer names.

No network: get_with_retry is monkeypatched, the same approach
test_pipeline_fetch_socrata.py takes with its own retry tests.

What is worth pinning here, and why each one bit at some point:

  - `$order=:id`. Pavement Edge has no `objectid` column, so ordering on
    that returns 400 and the fetch dies. The param is easy to "tidy" into
    something more conventional years later.
  - A short page ends paging. Getting this wrong either truncates a layer
    or loops forever.
  - The cache is GZIPPED and round-trips. These layers are 182MB gzipped;
    a change to plain .json would quietly cost a gigabyte of disk.
  - The write is ATOMIC and leaves no .tmp behind when it fails. A
    half-written 182MB cache read back as JSON is a confusing failure a
    long way from its cause.
  - An unknown layer name raises rather than silently fetching nothing.
"""

import gzip
import json

import pytest

from pipeline.fetch import planimetrics


class _FakeResponse:
    def __init__(self, rows):
        self._rows = rows

    def json(self):
        return self._rows


def _fake_source(total_rows: int, calls: list):
    """A stand-in Socrata that serves `total_rows` in PAGE-sized pages and
    records the params of every request it saw."""
    def fake_get(url, params=None, headers=None):
        calls.append(params)
        start = params["$offset"]
        end = min(start + params["$limit"], total_rows)
        return _FakeResponse([{"i": i} for i in range(start, end)])
    return fake_get


def test_pages_until_a_short_page_and_orders_by_id(monkeypatch, tmp_path):
    calls = []
    total = planimetrics.PAGE * 2 + 7
    monkeypatch.setattr(planimetrics, "get_with_retry", _fake_source(total, calls))

    rows = planimetrics.fetch_all("fake-id", "pavement_edge")

    assert len(rows) == total
    assert [r["i"] for r in rows] == list(range(total))
    # three requests: two full pages then the short one that ends it
    assert len(calls) == 3
    assert [c["$offset"] for c in calls] == [0, planimetrics.PAGE,
                                             planimetrics.PAGE * 2]
    # `:id`, not `objectid` -- Pavement Edge has no objectid and 400s on it
    assert all(c["$order"] == ":id" for c in calls)


def test_load_writes_a_gzipped_cache_and_reads_it_back(monkeypatch, tmp_path):
    monkeypatch.setattr(planimetrics.config, "RAW_DIR", tmp_path)
    monkeypatch.setattr(planimetrics, "get_with_retry", _fake_source(3, []))

    first = planimetrics.load("cscl")
    assert first == [{"i": 0}, {"i": 1}, {"i": 2}]

    path = planimetrics.cache_path("cscl")
    assert path.exists()
    assert path.suffix == ".gz"
    with gzip.open(path, "rt") as fh:          # genuinely gzip, not just named .gz
        assert json.load(fh) == first

    # second call must not hit the network at all
    def explode(*args, **kwargs):
        raise AssertionError("load() re-fetched instead of using the cache")
    monkeypatch.setattr(planimetrics, "get_with_retry", explode)
    assert planimetrics.load("cscl") == first


def test_refresh_bypasses_the_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(planimetrics.config, "RAW_DIR", tmp_path)
    monkeypatch.setattr(planimetrics, "get_with_retry", _fake_source(2, []))
    planimetrics.load("cscl")

    monkeypatch.setattr(planimetrics, "get_with_retry", _fake_source(5, []))
    assert len(planimetrics.load("cscl", refresh=True)) == 5


def test_a_failed_write_leaves_no_cache_and_no_tmp(monkeypatch, tmp_path):
    monkeypatch.setattr(planimetrics.config, "RAW_DIR", tmp_path)
    monkeypatch.setattr(planimetrics, "get_with_retry", _fake_source(2, []))

    def die(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(planimetrics.json, "dump", die)

    with pytest.raises(OSError):
        planimetrics.load("pavement_edge")

    path = planimetrics.cache_path("pavement_edge")
    assert not path.exists()
    # the temp file must be cleaned up too, or the next run trips over it
    assert list(path.parent.glob("*.tmp")) == []


def test_unknown_layer_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(planimetrics.config, "RAW_DIR", tmp_path)
    with pytest.raises(KeyError, match="unknown planimetrics layer"):
        planimetrics.load("roadbed")
