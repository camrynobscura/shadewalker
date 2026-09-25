"""Order-of-magnitude latency canary for /route's core work.

This is a TRIPWIRE, not a benchmark (decided 2026-08-30, `latency-guard`).
The threshold is deliberately ~5x the measured typical cost so that noise
-- a busy laptop, a cold cache -- can never fail it: silence must mean
"no disaster", so that red means fire. What it exists to catch is the
class this repo has actually hit: a one-line change turning seconds into
minutes while every correctness test stays green (`np.load` laziness once
made a 15s audit take 29 minutes; the same class landing in routing would
otherwise surface only at a deploy).

Division of labor, so nobody grows this test into what it isn't:
- 20-30% creep: NOT this test's job -- re-run the measurement recipe in
  history/quick-fixes.md §4 when it matters (pre-deploy, perf work).
- the LIVE site getting slow: UptimeRobot monitor on a fixed short
  /route URL with response-time alerting (PLAN.md `hosting` to-do) --
  no local test can see production.

Subjects are the four anchor-band pairs (known routable, spread over
real chokepoints); each is timed as the production request shape -- one
snap + the full four-preset ladder, the only shape the frontend sends.
Runs only where the real export lives (the citywide tier skips cleanly
elsewhere, including CI), so it fires exactly at the checkpoints where
someone is watching a terminal: verify-suite and pre-deploy.
"""
import statistics
import time

import pytest

from tests.test_route_regressions import ANCHOR_PINS

TREE_WEIGHT_LADDER = [0.0, 5.0, 15.0, 40.0]
MEDIAN_CEILING_S = 2.0  # typical ladder measured ~0.4-0.8s; 5x headroom


@pytest.mark.citywide
def test_route_ladder_median_stays_sane(citywide_store):
    pairs = [(p.values[0], p.values[1]) for p in ANCHOR_PINS]

    # One un-timed warmup so first-touch costs (index warm-up, page-in of
    # the freshly loaded graph) don't land in the first sample.
    frm, to = pairs[0]
    citywide_store.snap_pair(frm[0], frm[1], to[0], to[1])

    ladder_times = []
    for frm, to in pairs:
        started = time.perf_counter()
        pair = citywide_store.snap_pair(frm[0], frm[1], to[0], to[1])
        assert pair is not None, f"canary pair {frm}->{to} no longer snaps"
        for weight in TREE_WEIGHT_LADDER:
            result = citywide_store.route(pair[0], pair[1], tree_weight=weight, month=7, hour=13)
            assert result is not None, f"canary pair {frm}->{to} no longer routes"
        ladder_times.append(time.perf_counter() - started)

    median_s = statistics.median(ladder_times)
    print(f"\n[canary] ladder times: "
          + " ".join(f"{t * 1000:.0f}ms" for t in sorted(ladder_times))
          + f" -- median {median_s * 1000:.0f}ms (ceiling {MEDIAN_CEILING_S:.1f}s)")
    assert median_s < MEDIAN_CEILING_S, (
        f"median 4-preset route ladder took {median_s:.2f}s against a "
        f"{MEDIAN_CEILING_S:.1f}s ceiling that sits ~5x above normal -- "
        "this is an order-of-magnitude regression (the np.load-laziness "
        "class), not noise. Find the change that did it before deploying; "
        "do NOT raise the ceiling to green this."
    )
