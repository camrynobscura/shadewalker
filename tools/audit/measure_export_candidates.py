"""Gate 2 instrument: how should the building-shade table be packed in the export?

WHY THIS EXISTS
---------------
The shade table is 288 uint8 per edge -- 140.7 MB citywide by
construction -- riding in a file whose settled rules are "one file, JSON,
inspectable, streamed into server RAM at startup". Synthetic
measurements (2026-09-08) ruled out plain int lists (3x startup) and
left three candidates. This writes each of them from the same real build
and measures what the box will pay: bytes shipped, GraphStore.load()
wall time, peak RSS of the loading process, and the field's own decode
cost.

CANDIDATES (each in its own directory under --out-dir)
------------------------------------------------------
  baseline/    today's format -- rows and meta keys stripped. The control.
  a_base64/    288 bytes per edge as base64 in the edge record (what the
               build writes now, interim); all-zero rows omitted.
  a_daylight/  same, but only the hours the sun table lights (6-20 in the
               2026 anchor table: 15 hours x 12 months = 180 bytes); the
               server would expand to 288 on load.
  c_sidecar/   the JSON without rows + citywide_shade.npy, a (288, n)
               uint8 array in edge order. Reverses "one file, not binary";
               deploy.sh ships data/export whole so it rides along;
               GraphStore's *.json.gz glob ignores it today.

Every candidate decodes to the SAME table -- checked here, so the choice
is about cost only. Before #109 GraphStore did not read the field, so
"load" was what that loader paid for the bigger JSON and the decode
column what the field would add; since #109 the loader decodes it, so a
re-run's "load" includes the decode. Mac numbers are relative; the box
measurement is deploy day's (a Mac peak is not a Linux budget).

    uv run python tools/audit/measure_export_candidates.py
    uv run python tools/audit/measure_export_candidates.py --export path/to/other/citywide.json.gz
"""

import argparse
import base64
import datetime as dt
import gzip
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)

from pipeline import export                     # noqa: E402

SLOTS = 288
SIDECAR = "citywide_shade.npy"


def _dir_bytes(path) -> int:
    return sum(os.path.getsize(os.path.join(path, f)) for f in os.listdir(path))


def _write_json(dir_path, payload):
    os.makedirs(dir_path, exist_ok=True)
    out = os.path.join(dir_path, f"{export.CITYWIDE_NAME}.json.gz")
    t0 = time.perf_counter()
    export._write_atomically(Path(out), payload)
    return time.perf_counter() - t0


def _load_once(dir_path) -> tuple[float, float]:
    """(wall seconds, peak RSS MB) of GraphStore().load() in a fresh process."""
    env = dict(os.environ, SHADEWALKER_EXPORT_DIR=os.path.abspath(dir_path), PYTHONPATH=REPO)
    cmd = ["/usr/bin/time", "-l", os.path.join(REPO, ".venv", "bin", "python"), "-c",
           "from server.graph_store import GraphStore; GraphStore().load()"]
    t0 = time.perf_counter()
    done = subprocess.run(cmd, env=env, capture_output=True, text=True, cwd=REPO)
    wall = time.perf_counter() - t0
    if done.returncode != 0:
        raise RuntimeError(f"load failed in {dir_path}:\n{done.stderr[-2000:]}")
    match = re.search(r"(\d+)\s+maximum resident set size", done.stderr)
    peak_bytes = int(match.group(1)) if match else 0
    return wall, peak_bytes / 1e6


def _load_best(dir_path, runs: int):
    results = [_load_once(dir_path) for _ in range(runs)]
    return min(w for w, _ in results), max(r for _, r in results)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    # The real export is the default; --export points at a practice build.
    parser.add_argument("--export", default="data/export/citywide.json.gz")
    parser.add_argument("--out-dir", default=f"data/audits/{dt.date.today().isoformat()}/export_candidates")
    parser.add_argument("--runs", type=int, default=2, help="load() runs per candidate; best wall, worst RSS")
    args = parser.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)

    t0 = time.perf_counter()
    with gzip.open(args.export, "rt") as fh:
        payload = json.load(fh)
    edges = payload["edges"]
    n = len(edges)
    print(f"loaded {args.export}: {n:,} edges in {time.perf_counter() - t0:.0f}s")

    # The full table, edge order, from the build's base64 rows.
    t0 = time.perf_counter()
    table = np.zeros((n, SLOTS), dtype=np.uint8)
    lit = 0
    for i, edge in enumerate(edges):
        row = edge.get("building_shade")
        if row:
            table[i] = np.frombuffer(base64.b64decode(row), dtype=np.uint8)
            lit += 1
    decode_a = time.perf_counter() - t0
    sun_table = payload["meta"]["sun_table"]
    hours = sorted({h for row in sun_table for h, v in enumerate(row) if v is not None})
    h0, h1 = hours[0], hours[-1]
    day_cols = np.array([(m * 24) + h for m in range(12) for h in range(h0, h1 + 1)])
    print(f"rows on {lit:,} edges ({100 * lit / n:.1f}%); decode {decode_a:.2f}s; daylight hours {h0}-{h1} "
          f"({len(day_cols)} of {SLOTS} slots); table {table.nbytes / 1e6:.1f} MB")

    report = [f"# Export packing candidates -- {dt.date.today().isoformat()}\n",
              f"Source `{args.export}`: {n:,} edges, rows on {lit:,} ({100 * lit / n:.1f}%); "
              f"daylight hours {h0}-{h1}; full table {table.nbytes / 1e6:.1f} MB. "
              f"load() = today's GraphStore (field NOT read yet), best of {args.runs} runs; "
              f"RSS = peak of the loading process (Mac, relative only).\n",
              "| candidate | files | bytes on disk | write s | load() s | load peak RSS MB | field decode s | decoded == table |",
              "|---|---|---|---|---|---|---|---|"]

    def row_for(label, dir_path, write_s, decode_s, identical, files):
        wall, rss = _load_best(dir_path, args.runs)
        report.append(f"| {label} | {files} | {_dir_bytes(dir_path) / 1e6:.1f} MB | {write_s:.0f} | {wall:.1f} | "
                      f"{rss:.0f} | {decode_s:.2f} | {'yes' if identical else 'NO'} |")
        print(report[-1])

    meta_shade = {k: payload["meta"][k] for k in ("sun_table", "shade_slots")}

    # baseline: strip everything
    for edge in edges:
        edge.pop("building_shade", None)
    for k in ("sun_table", "shade_slots"):
        payload["meta"].pop(k, None)
    d = os.path.join(args.out_dir, "baseline")
    w = _write_json(d, payload)
    row_for("baseline (today's format)", d, w, 0.0, True, "json.gz")

    # a: base64 of 288 (the build's own format, re-written here for a fair timing)
    payload["meta"].update(meta_shade)
    for i, edge in enumerate(edges):
        if table[i].any():
            edge["building_shade"] = base64.b64encode(table[i].tobytes()).decode("ascii")
    d = os.path.join(args.out_dir, "a_base64")
    w = _write_json(d, payload)
    t0 = time.perf_counter()
    check = np.zeros_like(table)
    for i, edge in enumerate(edges):
        r = edge.get("building_shade")
        if r:
            check[i] = np.frombuffer(base64.b64decode(r), dtype=np.uint8)
    row_for("a: base64 x 288 in JSON", d, w, time.perf_counter() - t0, np.array_equal(check, table), "json.gz")

    # a_daylight: base64 of the lit hours only
    for i, edge in enumerate(edges):
        edge.pop("building_shade", None)
        if table[i].any():
            edge["building_shade"] = base64.b64encode(table[i, day_cols].tobytes()).decode("ascii")
    payload["meta"]["shade_slots"] = dict(meta_shade["shade_slots"],
                                          encoding=f"building_shade = base64 of {len(day_cols)} uint8: hours {h0}-{h1} "
                                                   f"of each month, month-major; absent = all zero")
    d = os.path.join(args.out_dir, "a_daylight")
    w = _write_json(d, payload)
    t0 = time.perf_counter()
    check = np.zeros_like(table)
    for i, edge in enumerate(edges):
        r = edge.get("building_shade")
        if r:
            check[i, day_cols] = np.frombuffer(base64.b64decode(r), dtype=np.uint8)
    row_for(f"a-daylight: base64 x {len(day_cols)} in JSON", d, w, time.perf_counter() - t0,
            np.array_equal(check, table), "json.gz")

    # c: sidecar
    for edge in edges:
        edge.pop("building_shade", None)
    payload["meta"]["shade_slots"] = dict(meta_shade["shade_slots"],
                                          encoding=f"{SIDECAR} beside this file: uint8 (288, edge_count) in edge order")
    d = os.path.join(args.out_dir, "c_sidecar")
    w = _write_json(d, payload)
    t0 = time.perf_counter()
    np.save(os.path.join(d, SIDECAR), np.ascontiguousarray(table.T))
    w += time.perf_counter() - t0
    t0 = time.perf_counter()
    loaded = np.load(os.path.join(d, SIDECAR))
    row_for("c: JSON + .npy sidecar", d, w, time.perf_counter() - t0, np.array_equal(loaded.T, table),
            "json.gz + npy")

    report.append(f"\nSidecar gz-compressibility, for the rsync/deploy question: "
                  f"{os.path.getsize(os.path.join(d, SIDECAR)) / 1e6:.1f} MB raw")
    text = "\n".join(report)
    path = os.path.join(args.out_dir, "report.md")
    with open(path, "w") as fh:
        fh.write(text + "\n")
    print(f"\n{text}\n\nreport: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
