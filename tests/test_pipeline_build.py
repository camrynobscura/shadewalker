"""Tests for pipeline/build.py's readback check -- the last gate before a
build's file is trusted.

The export's atomicity guarantees a whole file, not the right one; the
readback re-opens what was written and compares it with what was built.
Since PLAN `building-shadows` it also insists the building-shade table
rode along: a build whose shade step silently produced nothing must fail
here, not as a shadeless month on the live site.
"""

import gzip
import json

import pytest

from pipeline import build


def _write(tmp_path, payload):
    path = tmp_path / "citywide.json.gz"
    with gzip.open(path, "wt") as fh:
        json.dump(payload, fh)
    return path


def _good_payload():
    table = [[None] * 24 for _ in range(12)]
    table[6][13] = [178.6, 70.7]
    return {
        "meta": {"node_count": 2, "edge_count": 2, "sun_table": table},
        "nodes": {"a": [-74.0, 40.7], "b": [-74.001, 40.701]},
        "edges": [{"u": "a", "v": "b", "building_shade": "AAAA"},
                  {"u": "b", "v": "a"}],
    }


def test_readback_accepts_a_matching_file_with_shade(tmp_path):
    assert build._readback_matches(_write(tmp_path, _good_payload()), 2, 2)


@pytest.mark.parametrize("break_it", [
    lambda p: p["nodes"].pop("b"),
    lambda p: p["edges"].pop(),
    lambda p: p["meta"].__setitem__("edge_count", 99),
    lambda p: p["meta"].pop("sun_table"),
    lambda p: p["meta"].__setitem__("sun_table", [[None] * 24] * 11),
    lambda p: p["edges"][0].pop("building_shade"),
], ids=["missing node", "missing edge", "meta count", "no sun table",
        "sun table 11 months", "no edge has shade"])
def test_readback_rejects_each_mismatch(tmp_path, break_it):
    payload = _good_payload()
    break_it(payload)
    assert not build._readback_matches(_write(tmp_path, payload), 2, 2)
