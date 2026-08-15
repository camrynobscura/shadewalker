"""The external-engine DISCOVERY harness (FIXES item 5).

Compares fresh random NONE-priority routes against independent engines to
surface leads -- the technique that found the id-collision, Williamsburg,
phantom-connector, and Queensboro bugs. Split deliberately in two:

- REGRESSION pins (the objective half) live in test_route_regressions.py's
  v19 anchor section and run in the default citywide tier with no network.
- DISCOVERY (this file) hits live third-party servers, so it is opt-in
  twice over: marked `external` AND skipped unless --run-external is
  passed (see conftest.py) -- a stray `-m` expression can never trigger
  live traffic from CI or a default run.

A lead is homework for a human, never a failure: engines are advisory
(see tests/external_engines.py's module docstring), so this test only
fails when the harness itself couldn't do its job (nothing compared at
all). Results go to a dated report under data/audits/, and every
confirmed lead prints the triage ritual: what's physically at the gap,
the way-membership audit, then a field check with a walking-directions
link (the route, not a single point).

Run it:

    uv run pytest --run-external tests/test_external_validation.py -s

Options: --external-pairs N (default 30), --external-seed S (default:
fresh entropy, printed for reproduction). Budget ~1-2s per compared pair
(politeness sleeps) on top of the ~15s citywide load.
"""

import datetime
import json
import random
from pathlib import Path

import pytest

from external_engines import (
    classify_flag,
    google_walking_directions_url,
    haversine_m,
    osrm_foot_length_m,
    our_none_priority_length_m,
    polite_pause,
    valhalla_no_ferry_length_m,
    ARBITER_AGREE_RATIO,
)

# Same-ish-length pairs: long enough that a detour is unambiguous, short
# enough that both engines' path-choice variance stays small relative to
# the flag thresholds. Carried over from the proven ad-hoc batches.
STRAIGHT_LINE_MIN_M = 1500.0
STRAIGHT_LINE_MAX_M = 6000.0

REPORTS_DIR = Path(__file__).parent.parent / "data" / "audits"


def _sample_candidate_pair(rng, store):
    """One random node pair with a plausible straight-line separation, or
    None if this draw missed the band."""
    ids = store._sampled_node_ids  # cached below; avoids re-listing 520k nodes per draw
    ia, ib = rng.sample(ids, 2)
    lon1, lat1 = store._node_lonlat[ia]
    lon2, lat2 = store._node_lonlat[ib]
    straight = haversine_m(lat1, lon1, lat2, lon2)
    if not (STRAIGHT_LINE_MIN_M <= straight <= STRAIGHT_LINE_MAX_M):
        return None
    return (float(lat1), float(lon1)), (float(lat2), float(lon2))


@pytest.mark.external
def test_random_batch_against_external_engines(citywide_store, request):
    n_pairs = request.config.getoption("--external-pairs")
    seed = request.config.getoption("--external-seed")
    if seed is None:
        seed = random.SystemRandom().randrange(10**8)
    print(f"\nexternal discovery seed: {seed} (reproduce with --external-seed={seed})")

    store = citywide_store
    store._sampled_node_ids = list(store._id_to_idx.values())
    rng = random.Random(seed)

    rows = []
    skips = {}
    leads = []
    explained = []
    compared = 0
    attempts = 0
    while compared < n_pairs and attempts < n_pairs * 25:
        attempts += 1
        pair = _sample_candidate_pair(rng, store)
        if pair is None:
            continue
        a, b = pair

        ours, our_err = our_none_priority_length_m(store, a, b)
        if ours is None:
            # Cross-component pairs and far snaps aren't length comparisons;
            # tallied rather than dropped silently -- the cross-component
            # rate is itself a signal for FIXES item 1's orphaned scraps.
            skips[our_err] = skips.get(our_err, 0) + 1
            continue

        theirs, their_err = osrm_foot_length_m(a, b)
        polite_pause()
        if theirs is None:
            skips[their_err] = skips.get(their_err, 0) + 1
            continue

        compared += 1
        ratio = ours / theirs
        flag = classify_flag(ours, theirs)
        row = {
            "a": a, "b": b,
            "ours_m": round(ours), "osrm_m": round(theirs),
            "ratio": round(ratio, 3), "flag": flag,
        }

        if flag:
            # Arbitration (the ferry lesson): Valhalla-no-ferry agreeing
            # with US means the disagreement is OSRM-specific, not a lead.
            valhalla, valhalla_err = valhalla_no_ferry_length_m(a, b)
            polite_pause()
            row["valhalla_no_ferry_m"] = round(valhalla) if valhalla else None
            row["valhalla_err"] = valhalla_err
            agrees = valhalla is not None and (
                1 / ARBITER_AGREE_RATIO <= valhalla / ours <= ARBITER_AGREE_RATIO
            )
            if agrees:
                row["verdict"] = "explained (Valhalla-no-ferry agrees with us; OSRM-specific)"
                explained.append(row)
            else:
                row["verdict"] = "LEAD -- needs human triage"
                leads.append(row)

        rows.append(row)
        mark = f"  <-- {row.get('verdict', flag)}" if flag else ""
        print(f"  [{compared:3d}/{n_pairs}] ours={ours:6.0f}m osrm={theirs:6.0f}m ratio={ratio:.2f}{mark}")

    # Report file first, so it exists even if the assertions below fire.
    today = datetime.date.today().isoformat()
    stamp = datetime.datetime.now().strftime("%H%M%S")
    out_dir = REPORTS_DIR / today
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"external_discovery_{stamp}_n{n_pairs}_s{seed}.json"
    report = {
        "seed": seed, "requested_pairs": n_pairs, "compared": compared,
        "attempts": attempts, "skips": skips,
        "leads": len(leads), "explained_flags": len(explained),
        "rows": rows,
    }
    with open(out_path, "w") as f:
        json.dump(report, f, indent=1)

    print(f"\ncompared {compared}/{n_pairs} (attempts {attempts}; skips: {skips or 'none'})")
    print(f"flags: {len(leads)} lead(s), {len(explained)} explained by the arbiter")
    print(f"report: {out_path}")

    for i, lead in enumerate(leads, 1):
        a, b = lead["a"], lead["b"]
        print(f"\nLEAD {i}: ours={lead['ours_m']}m osrm={lead['osrm_m']}m "
              f"valhalla-no-ferry={lead['valhalla_no_ferry_m']}m (ratio {lead['ratio']})")
        print(f"  pair: {a[0]},{a[1]} -> {b[0]},{b[1]}")
        print(f"  route: {google_walking_directions_url(a, b)}")
        print("  triage ritual (see HISTORY 2026-08-15, Queensboro):")
        print("   1. draw both routes; find WHERE they diverge (the gap, not the detour)")
        print("   2. check what's physically at the gap: real OSM ways/tags, not assumptions")
        print("   3. if a connection looks missing: way-membership audit before any bridge")
        print("   4. field check with start/end pins + the directions link above")

    # The only failure mode is the harness not doing its job at all.
    if compared == 0:
        pytest.fail(
            f"no pairs compared after {attempts} attempts (skips: {skips}) -- "
            "external engines down, or the citywide graph produced no usable pairs"
        )
