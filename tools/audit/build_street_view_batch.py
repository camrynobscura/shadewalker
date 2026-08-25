"""Build a Street View spot-check batch for sidewalk -> street matching.

WHY THIS EXISTS
---------------
The only machine-readable ground truth for "did we pick the right street"
is the set of sidewalks carrying their OWN OSM name, and it is 0.56% of
sidewalks, drawn 29% from Manhattan and 65% from Queens while Brooklyn --
a third of the city's pavement -- contributes 1.3%. Every precision figure
resting on it should be treated as unestablished.

Human eyes on Street View are the better instrument, and the user has asked
to be given these batches rather than have decisions made on weaker
evidence. This makes one.

DESIGN
------
  - STRATIFIED BY BOROUGH to the real distribution, so Brooklyn and Staten
    Island actually appear.
  - Mid-block coordinates. Never an endpoint: a junction is where every
    match looks ambiguous, and a batch built on endpoints once sent the
    reviewer to corners and corrupted their verdicts.
  - One checkable claim per spot: "this sidewalk belongs to <street>".
  - Shuffled, with the grouping hidden behind a collapsed section, so the
    reviewer judges the pavement instead of confirming a number.

Writes a self-contained HTML file. Read-only.
"""
import argparse
import collections
import html
import os
import random

import numpy as np

BOROUGHS = {"1": "Manhattan", "2": "Bronx", "3": "Brooklyn",
            "4": "Queens", "5": "Staten Island"}

PAGE = """<!doctype html><meta charset="utf-8">
<title>Sidewalk Match Spot Check</title>
<style>
 body{font:15px/1.6 ui-monospace,SF Mono,Menlo,monospace;max-width:52rem;
      margin:0 auto;padding:32px 20px 80px;background:#e6f2e9;color:#0b2418}
 h1{font-size:1.5rem;margin:0 0 6px}
 .sub{color:#4a7360;margin:0 0 24px}
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
<h1>Sidewalk Match Spot Check</h1>
<p class="sub">For each: <b>is this sidewalk actually along that street?</b>
Open Street View and judge. Mark yes / no / unclear &mdash; unclear is a real answer.</p>
<ol>
__ROWS__
</ol>
<details><summary>Which group each came from &mdash; open only after judging</summary>
__KEY__
</details>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--table",
                    default="data/audits/2026-08-23/sidewalk_kerb_match.npz")
    ap.add_argument("--out",
                    default="data/audits/2026-08-23/street_view_batch.html")
    ap.add_argument("--n", type=int, default=36)
    ap.add_argument("--seed", type=int, default=20260823)
    args = ap.parse_args()

    # np.load on an .npz is LAZY -- each subscript re-inflates the whole
    # column out of the zip. Only ~36 rows are read here so it is not a
    # runtime problem, but the same pattern in a per-edge loop cost half an
    # hour in test_blockface_side.py. Materialise once, everywhere.
    with np.load(args.table, allow_pickle=False) as npz:
        t = {name: npz[name] for name in npz.files}
    rng = random.Random(args.seed)

    # A spot is only checkable if there is a named street to check against
    # and the edge is long enough to stand on.
    usable = ((t["kind"] == "footway/sidewalk") & (t["length_m"] >= 15.0)
              & (t["kerb_dist"] <= 5.0) & (t["street"] != "")
              & ~t["in_park"] & np.isfinite(t["mid_lon"]))
    print(f"checkable sidewalks: {int(usable.sum()):,}")

    # Stratify to the REAL borough mix, not the convenient one.
    share = {}
    for code in BOROUGHS:
        b = usable & (t["boro"] == code)
        share[code] = float(t["length_m"][b].sum())
    total = sum(share.values())

    # Allocate exactly n by largest remainder, with a floor of 2 per borough.
    # Shuffling and then truncating to n -- which is what this did -- drops
    # spots at random, and the floor means the per-borough counts can sum
    # above n, so the truncation could delete Staten Island's entire
    # representation. That silently defeats the stratification this tool
    # exists to provide.
    want = {}
    for code in BOROUGHS:
        exact = args.n * share[code] / total if total else 0.0
        want[code] = (max(2, int(exact)), exact % 1.0)
    while sum(v[0] for v in want.values()) < args.n:
        code = max(want, key=lambda c: want[c][1])
        want[code] = (want[code][0] + 1, -1.0)
    while sum(v[0] for v in want.values()) > args.n:
        code = max((c for c in want if want[c][0] > 2),
                   key=lambda c: want[c][0], default=None)
        if code is None:
            break
        want[code] = (want[code][0] - 1, want[code][1])

    rows = []
    for code, name in BOROUGHS.items():
        pool = list(np.flatnonzero(usable & (t["boro"] == code)))
        for i in rng.sample(pool, min(want[code][0], len(pool))):
            rows.append({"i": int(i), "boro": name,
                         "street": str(t["street"][i]),
                         "lon": float(t["mid_lon"][i]),
                         "lat": float(t["mid_lat"][i]),
                         "dist": float(t["kerb_dist"][i]),
                         "side": str(t["side"][i])})
    rng.shuffle(rows)

    body = []
    for n, r in enumerate(rows, 1):
        link = f"https://www.google.com/maps?layer=c&cbll={r['lat']:.5f},{r['lon']:.5f}"
        body.append(
            f'<li><span class="n">{n}</span>'
            f'<span><span class="st">{html.escape(r["street"])}</span><br>'
            f'<span class="meta">{r["dist"]:.1f}m from the kerb &middot; '
            f'{r["lat"]:.5f}, {r["lon"]:.5f}</span></span>'
            f'<a class="go" href="{link}" target="_blank" rel="noopener">Open</a></li>')

    by_boro = collections.defaultdict(list)
    for n, r in enumerate(rows, 1):
        by_boro[r["boro"]].append(str(n))
    key = ["<table><tr><th>borough</th><th>which numbers</th></tr>"]
    for name, nums in sorted(by_boro.items()):
        key.append(f"<tr><td>{name}</td><td>{', '.join(nums)}</td></tr>")
    key.append("</table>")

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as fh:
        fh.write(PAGE.replace("__ROWS__", "\n".join(body))
                     .replace("__KEY__", "\n".join(key)))
    print(f"{len(rows)} spots -> {args.out}")
    for name, nums in sorted(by_boro.items()):
        print(f"   {name:16s} {len(nums)}")


if __name__ == "__main__":
    main()
