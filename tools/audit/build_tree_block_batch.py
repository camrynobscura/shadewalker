"""Spot-check batch: which street is this tree actually on?

WHY A HUMAN HAS TO SETTLE THIS
------------------------------
Our rule assigns a tree to the block of its NEAREST KERB. NYC's planting-
space records name a street too, and the two disagree on ~31% of trees.
Neither computation can adjudicate:

  - our side looks physically right: median 0.89m from the kerb we pick,
    p90 1.81m, and only 1.0% of trees have another street's kerb
    comparably close.
  - the city's side cannot be a physical ground truth: on disagreements
    its named segment is a median 14.3m away, is within 3m only 0.7% of
    the time, and is not within 25m at all for 37% of them. It reads as an
    administrative/address assignment, not a location.

So the geometry says us and the paperwork says them, and no further
measurement separates a real mis-assignment from a records convention.
One look at Street View does.

DESIGN
------
  - BLIND. The two candidate streets are shown as A and B in random order,
    so the reviewer cannot tell which one is ours and neither can bias the
    answer.
  - Only CLEAR conflicts: our kerb within CLOSE_M, the city's segment
    beyond FAR_M. A tree genuinely between two kerbs proves nothing either
    way.
  - Species and trunk size are shown, because the reviewer has to find the
    right tree, not just the right corner.
  - Spread across boroughs: a defect confirmed only in Queens is a weaker
    finding than the same defect in four boroughs.

Read-only. Needs the caches test_tree_block_assignment.py uses.
"""
import argparse
import collections
import html
import json
import os
import random
import sys

from shapely.geometry import Point

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)

from pipeline.fetch import socrata  # noqa: E402
from pipeline.graph.naming import _to_m  # noqa: E402
from tools.audit import fetch_planimetrics  # noqa: E402
from pipeline.graph.blockface import (  # noqa: E402
    build_face_lookup, build_kerb_index,
)
from tools.audit.test_tree_block_assignment import (  # noqa: E402
    normalise_id, same_street,
)

BOROUGHS = {"1": "Manhattan", "2": "Bronx", "3": "Brooklyn",
            "4": "Queens", "5": "Staten Island"}

SEARCH_M = 40.0
CLOSE_M = 3.0    # our kerb must be this close for us to be making a real claim
FAR_M = 10.0     # the city's segment must be at least this far for a clear conflict

PAGE = """<!doctype html><meta charset="utf-8">
<title>Tree Block Spot Check</title>
<style>
 body{font:15px/1.6 ui-monospace,SF Mono,Menlo,monospace;max-width:54rem;
      margin:0 auto;padding:32px 20px 80px;background:#e6f2e9;color:#0b2418}
 h1{font-size:1.5rem;margin:0 0 6px}
 .ask{background:#fff;border:1px solid #9dc7ac;padding:12px 16px;margin:0 0 24px}
 ol{list-style:none;padding:0;margin:0}
 li{padding:13px 15px;border:1px solid #cfe5d6;border-bottom:0;background:#fff}
 li:last-child{border-bottom:1px solid #cfe5d6}
 .hd{display:flex;justify-content:space-between;align-items:baseline;gap:12px}
 .n{color:#4a7360;font-size:.75rem;font-weight:700}
 .tree{font-size:.72rem;color:#4a7360;margin:2px 0 8px}
 .opts{display:flex;gap:10px;flex-wrap:wrap}
 .opt{border:1px solid #9dc7ac;padding:5px 11px;font-weight:600;font-size:.85rem;
      background:#f4fbf6}
 a.go{font-size:.62rem;letter-spacing:.1em;text-transform:uppercase;font-weight:700;
      text-decoration:none;background:#007a4d;color:#fff;padding:6px 10px;white-space:nowrap}
 details{margin-top:24px;background:#eff8f1;border:1px solid #9dc7ac;padding:12px 16px}
 summary{cursor:pointer;font-weight:600;font-size:.82rem}
 table{border-collapse:collapse;width:100%;font-size:.75rem;margin-top:10px}
 td,th{text-align:left;padding:5px 10px 5px 0;border-bottom:1px solid #cfe5d6}
</style>
<h1>Tree Block Spot Check</h1>
<div class="ask"><b>For each tree: which street is it on?</b> Answer <b>A</b>
or <b>B</b> &mdash; or <b>UNCLEAR</b> if the imagery won't tell you, which is
a real answer.<br><br>
Two datasets disagree about these trees and I can't tell which is right by
computation. The order of A and B is random and I don't tell you which is
mine, so your answer isn't nudged either way.<br><br>
The link drops you at the tree's exact coordinate. The species and trunk
size are there to help you pick out the right tree, since there may be
several nearby.</div>
<ol>
__ROWS__
</ol>
<details><summary>Answer key &mdash; open only after judging all __COUNT__</summary>
__KEY__
</details>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trees",
                    default="data/raw/socrata/trees_citywide_v3.json")
    ap.add_argument("--out",
                    default="data/audits/2026-08-23/tree_block_batch.html")
    ap.add_argument("--n", type=int, default=12)
    ap.add_argument("--seed", type=int, default=20260824)
    args = ap.parse_args()
    rng = random.Random(args.seed)

    link = {}
    for row in socrata.fetch_all_rows(
            "hn5i-inap", "tpstructure = 'Full'",
            "globalid, plantingspaceglobalid", "globalid",
            "tree_planting_link_v1"):
        if row.get("globalid") and row.get("plantingspaceglobalid"):
            link[row["globalid"].upper()] = row["plantingspaceglobalid"].upper()

    spaces = {}
    for row in socrata.fetch_all_rows(
            "82zj-84is", "physicalid IS NOT NULL",
            "globalid, physicalid, streetname", "globalid",
            "planting_spaces_v1"):
        segment = normalise_id(row.get("physicalid"))
        if segment is not None and row.get("globalid"):
            spaces[row["globalid"].upper()] = (
                segment, (row.get("streetname") or "").strip())

    kerb_index, kerbs, kerb_face, kerb_conflated = build_kerb_index(
        fetch_planimetrics.load("pavement_edge"))
    faces = build_face_lookup(fetch_planimetrics.load("cscl"))
    face_segment = {f: normalise_id(m.segment_id) for f, m in faces.items()}

    with open(args.trees) as fh:
        trees = json.load(fh)

    by_boro = collections.defaultdict(list)
    for tree in trees:
        gid = str(tree.get("globalid") or "").upper()
        space = link.get(gid)
        known = spaces.get(space) if space else None
        if not known:
            continue
        coords = (tree.get("location") or {}).get("coordinates")
        if not coords:
            continue
        point = Point(*_to_m(coords[0], coords[1]))
        nearest = {}
        best = None
        for position in kerb_index.query(point.buffer(SEARCH_M)):
            if not kerb_conflated[position]:
                continue
            face = kerb_face[position]
            segment = face_segment.get(face)
            if segment is None:
                continue
            d = point.distance(kerbs[position])
            if segment not in nearest or d < nearest[segment]:
                nearest[segment] = d
            if best is None or d < best[0]:
                best = (d, face, segment)
        if best is None or best[2] == known[0]:
            continue
        # A clear conflict only: we are confident AND the city is far away.
        city_d = nearest.get(known[0])
        if best[0] > CLOSE_M or (city_d is not None and city_d < FAR_M):
            continue
        ours = (faces[best[1]].street or "").strip()
        # _norm_street, NOT a raw comparison: the two sources spell the same
        # street differently (COOPER STREET vs COOPER ST, BRONX PARK SOUTH vs
        # BRONX PARK S). A raw check let 8 of 12 same-street pairs through
        # into the first build of this batch, which would have asked the
        # reviewer to choose between two spellings of one street.
        if not ours or not known[1]:
            continue
        if same_street(ours, known[1]):
            continue
        boro = BOROUGHS.get(faces[best[1]].boro, "?")
        by_boro[boro].append({
            "lat": coords[1], "lon": coords[0], "boro": boro,
            "ours": ours, "theirs": known[1],
            "our_d": best[0], "city_d": city_d,
            "species": (tree.get("genusspecies") or "?").split(" - ")[-1],
            "dbh": tree.get("dbh") or "?",
        })
        if sum(len(v) for v in by_boro.values()) >= args.n * 40:
            break

    for group in by_boro.values():
        rng.shuffle(group)
    picked = []
    order = sorted(by_boro, key=lambda b: -len(by_boro[b]))
    while len(picked) < args.n and any(by_boro.values()):
        for name in order:
            if by_boro[name] and len(picked) < args.n:
                picked.append(by_boro[name].pop())
    rng.shuffle(picked)

    print(f"clear conflicts found: {sum(len(v) for v in by_boro.values()) + len(picked):,}")
    body, key = [], ["<table><tr><th>#</th><th>A</th><th>B</th>"
                     "<th>which was OURS</th><th>distances</th></tr>"]
    for n, r in enumerate(picked, 1):
        flip = rng.random() < 0.5
        a, b = (r["theirs"], r["ours"]) if flip else (r["ours"], r["theirs"])
        mine = "B" if flip else "A"
        link_url = (f"https://www.google.com/maps?layer=c&"
                    f"cbll={r['lat']:.6f},{r['lon']:.6f}")
        body.append(
            f'<li><div class="hd"><span class="n">{n}</span>'
            f'<a class="go" href="{link_url}" target="_blank" rel="noopener">'
            f'Street View</a></div>'
            f'<div class="tree">{html.escape(r["species"])} &middot; '
            f'{html.escape(str(r["dbh"]))}in trunk &middot; {r["boro"]} '
            f'&middot; {r["lat"]:.6f}, {r["lon"]:.6f}</div>'
            f'<div class="opts"><span class="opt">A &nbsp; '
            f'{html.escape(a)}</span>'
            f'<span class="opt">B &nbsp; {html.escape(b)}</span></div></li>')
        city_d = "not within 40m" if r["city_d"] is None else f"{r['city_d']:.1f}m"
        key.append(f"<tr><td>{n}</td><td>{html.escape(a)}</td>"
                   f"<td>{html.escape(b)}</td><td><b>{mine}</b></td>"
                   f"<td>ours {r['our_d']:.1f}m &middot; "
                   f"city's street {city_d}</td></tr>")
    key.append("</table><p>“Ours” is the nearest-kerb rule. The other is "
               "NYC's planting-space record.</p>")

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as fh:
        fh.write(PAGE.replace("__ROWS__", "\n".join(body))
                     .replace("__KEY__", "\n".join(key))
                     .replace("__COUNT__", str(len(picked))))
    print(f"{len(picked)} spots -> {args.out}")
    for name in sorted({r["boro"] for r in picked}):
        print(f"   {name:16s} {sum(1 for r in picked if r['boro'] == name)}")


if __name__ == "__main__":
    main()
