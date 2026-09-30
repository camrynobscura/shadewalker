"""Spot-check batch: does this block really carry TWO walking surfaces?

THE QUESTION
------------
Some block faces are assigned more sidewalk than they physically span --
several pieces stacked along the same stretch of ground rather than laid
end to end. Two possibilities, and they need opposite handling:

  REAL   the block genuinely has two walking surfaces (a plaza edge beside
         the sidewalk, a service road with its own pavement, a divided path
         through a median). The extra length is true, and shade maths has
         to divide by pavement people can actually walk.
  FAKE   a mis-match, and the extra length is not there at all.

They are identical in the data. A person looking at Street View settles it
in seconds.

WHY NOT THE CSCL RATIO
----------------------
The obvious test -- assigned length vs the block's own CSCL centerline
length -- is invalid for this. A centerline segment can be far shorter than
the run of kerb conflated to it, so that ratio flags ordinary blocks (it
reported 13% of the city, and measure_edge_block_alignment.py then showed
edges are aligned and only 2.0% of length is miscredited).

This instead compares a face's total assigned pavement against the ground
its pieces actually cover -- both measured from our data, no CSCL. Pieces
laid end to end give ~1. Pieces stacked in parallel give ~2.

DESIGN
------
  - BLIND. Controls (ordinary faces) are mixed in and shuffled, so the
    reviewer judges what is there rather than confirming a suspicion.
  - The viewing point is the MIDPOINT of the face's longest piece, so it is
    on real pavement and never at a junction, where everything looks
    ambiguous.
  - One question per spot, answerable from one look.

Read-only. Instant.
"""
import argparse
import collections
import html
import os
import random
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)

from pipeline.graph.naming import (  # noqa: E402
    _K as LON_SCALE, _LAT_M as LAT_SCALE,
)

BOROUGHS = {"1": "Manhattan", "2": "Bronx", "3": "Brooklyn",
            "4": "Queens", "5": "Staten Island"}

# Total assigned pavement divided by the ground the pieces cover. 1.0 means
# laid end to end. This much above it means stacked side by side.
STACKED_RATIO = 1.8

# Below this the bounding box of a couple of midpoints is mostly noise.
MIN_EXTENT_M = 20.0

PAGE = """<!doctype html><meta charset="utf-8">
<title>Double Pavement Spot Check</title>
<style>
 body{font:15px/1.6 ui-monospace,SF Mono,Menlo,monospace;max-width:52rem;
      margin:0 auto;padding:32px 20px 80px;background:#e6f2e9;color:#0b2418}
 h1{font-size:1.5rem;margin:0 0 6px}
 .sub{color:#4a7360;margin:0 0 8px}
 .ask{background:#fff;border:1px solid #9dc7ac;padding:12px 16px;margin:0 0 24px}
 ol{list-style:none;padding:0;margin:0}
 li{display:grid;grid-template-columns:2rem 1fr auto;gap:12px;align-items:baseline;
    padding:11px 13px;border:1px solid #cfe5d6;border-bottom:0;background:#fff}
 li:last-child{border-bottom:1px solid #cfe5d6}
 .n{color:#4a7360;font-size:.75rem;font-weight:600}
 .st{font-weight:600}
 .meta{font-size:.68rem;color:#4a7360}
 a.go{font-size:.62rem;letter-spacing:.1em;text-transform:uppercase;font-weight:700;
      text-decoration:none;background:#007a4d;color:#fff;padding:6px 10px;white-space:nowrap}
 details{margin-top:24px;background:#eff8f1;border:1px solid #9dc7ac;padding:12px 16px}
 summary{cursor:pointer;font-weight:600;font-size:.82rem}
 table{border-collapse:collapse;width:100%;font-size:.75rem;margin-top:10px}
 td,th{text-align:left;padding:5px 10px 5px 0;border-bottom:1px solid #cfe5d6}
</style>
<h1>Double Pavement Spot Check</h1>
<p class="sub">__COUNT__ spots. Some are ordinary blocks, mixed in on purpose
&mdash; don't assume anything is unusual.</p>
<div class="ask"><b>For each one: how many separate walking surfaces run
along this side of the street?</b><br>
One ordinary sidewalk &rarr; <b>ONE</b>.<br>
A sidewalk plus a second path beside it (a plaza edge, a service-road
pavement, a separate walkway) &rarr; <b>TWO</b>.<br>
Can't tell from the imagery &rarr; <b>UNCLEAR</b>, which is a real answer.</div>
<ol>
__ROWS__
</ol>
<details><summary>Which ones I flagged &mdash; open only after judging</summary>
__KEY__
</details>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--table",
                    default="data/audits/2026-08-23/sidewalk_kerb_match.npz")
    ap.add_argument("--out",
                    default="data/audits/2026-08-23/double_pavement_batch.html")
    ap.add_argument("--suspects", type=int, default=7)
    ap.add_argument("--controls", type=int, default=3)
    ap.add_argument("--seed", type=int, default=20260823)
    args = ap.parse_args()

    with np.load(args.table, allow_pickle=False) as npz:
        t = {name: npz[name] for name in npz.files}
    rng = random.Random(args.seed)

    usable = ((t["kind"] == "footway/sidewalk") & (t["length_m"] >= 5.0)
              & (t["kerb_dist"] <= 5.0) & (t["blockface"] != "")
              & t["conflated"] & ~t["in_park"]
              & np.isfinite(t["mid_lon"]) & np.isfinite(t["mid_lat"]))

    by_face = collections.defaultdict(list)
    for i in np.flatnonzero(usable):
        by_face[t["blockface"][i]].append(int(i))

    length, mid_lon, mid_lat = t["length_m"], t["mid_lon"], t["mid_lat"]
    suspects, controls = [], []
    for face, rows in by_face.items():
        if len(rows) < 2:
            continue
        total = float(sum(length[i] for i in rows))
        lons = np.array([mid_lon[i] for i in rows]) * LON_SCALE
        lats = np.array([mid_lat[i] for i in rows]) * LAT_SCALE
        span = float(np.hypot(lons.max() - lons.min(), lats.max() - lats.min()))
        # The table stores MIDPOINTS, not endpoints, so the midpoint span
        # falls short of the ground actually covered by about half a piece
        # at each end. Without that correction two 40m pieces laid end to
        # end span 40m and total 80m, scoring exactly 2.0 -- so every
        # ordinary two-piece block gets flagged as doubled pavement, which
        # is what this selector did on its first run (7,723 of 8,993).
        # Adding the mean piece length puts end-to-end back at ~1.0 while
        # leaving genuinely stacked pieces (span ~0) at ~2.0.
        extent = span + total / len(rows)
        if extent < MIN_EXTENT_M:
            continue
        longest = max(rows, key=lambda i: length[i])
        record = {"face": face, "rows": rows, "total": total,
                  "extent": extent, "ratio": total / extent,
                  "lon": float(mid_lon[longest]), "lat": float(mid_lat[longest]),
                  "street": str(t["street"][longest]),
                  "boro": BOROUGHS.get(str(t["boro"][longest]), "?")}
        if record["ratio"] >= STACKED_RATIO:
            suspects.append(record)
        elif record["ratio"] <= 1.2:
            controls.append(record)

    print(f"faces with >=2 pieces and real extent : "
          f"{len(suspects) + len(controls):,}")
    print(f"  stacked (ratio >= {STACKED_RATIO})        : {len(suspects):,}")
    print(f"  ordinary (ratio <= 1.2, controls)  : {len(controls):,}")

    # Spread the suspects across boroughs rather than letting one borough
    # supply them all -- a defect confirmed only in Queens is a weaker
    # finding than the same defect confirmed in four boroughs.
    picked = []
    by_boro = collections.defaultdict(list)
    for record in suspects:
        by_boro[record["boro"]].append(record)
    for group in by_boro.values():
        rng.shuffle(group)
    boro_order = sorted(by_boro, key=lambda b: -len(by_boro[b]))
    while len(picked) < args.suspects and any(by_boro.values()):
        for name in boro_order:
            if by_boro[name] and len(picked) < args.suspects:
                picked.append(by_boro[name].pop())

    chosen = picked + rng.sample(controls, min(args.controls, len(controls)))
    rng.shuffle(chosen)

    body = []
    for n, r in enumerate(chosen, 1):
        link = f"https://www.google.com/maps?layer=c&cbll={r['lat']:.5f},{r['lon']:.5f}"
        body.append(
            f'<li><span class="n">{n}</span>'
            f'<span><span class="st">{html.escape(r["street"] or "unnamed")}</span>'
            f'<br><span class="meta">{html.escape(r["boro"])} &middot; '
            f'{r["lat"]:.5f}, {r["lon"]:.5f}</span></span>'
            f'<a class="go" href="{link}" target="_blank" rel="noopener">Open</a></li>')

    flagged = {id(r) for r in picked}
    key = ["<table><tr><th>#</th><th>street</th><th>my guess</th>"
           "<th>pavement / ground covered</th></tr>"]
    for n, r in enumerate(chosen, 1):
        label = "TWO surfaces" if id(r) in flagged else "control (ordinary)"
        key.append(f"<tr><td>{n}</td><td>{html.escape(r['street'] or 'unnamed')}"
                   f"</td><td>{label}</td>"
                   f"<td>{r['total']:.0f}m over {r['extent']:.0f}m "
                   f"= {r['ratio']:.2f}x</td></tr>")
    key.append("</table>")

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as fh:
        fh.write(PAGE.replace("__ROWS__", "\n".join(body))
                     .replace("__KEY__", "\n".join(key))
                     .replace("__COUNT__", str(len(chosen))))
    print(f"\n{len(chosen)} spots ({len(picked)} flagged, "
          f"{len(chosen) - len(picked)} controls) -> {args.out}")
    for name in sorted({r["boro"] for r in chosen}):
        print(f"   {name:16s} {sum(1 for r in chosen if r['boro'] == name)}")


if __name__ == "__main__":
    main()
