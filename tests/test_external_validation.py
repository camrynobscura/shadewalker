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
    arbitrate,
    classify_flag,
    google_walking_directions_url,
    haversine_m,
    osrm_route_uses_ferry,
    our_none_priority_length_m,
    polite_pause,
    primary_comparison,
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

        theirs, primary_engine, their_err = primary_comparison(a, b, ours)
        if theirs is None:
            skips[their_err] = skips.get(their_err, 0) + 1
            continue

        compared += 1
        ratio = ours / theirs
        flag = classify_flag(ours, theirs)
        row = {
            "a": a, "b": b,
            "ours_m": round(ours), "theirs_m": round(theirs),
            "primary_engine": primary_engine,
            "ratio": round(ratio, 3), "flag": flag,
        }

        if flag:
            # Ferry short-circuit: OSRM rides ferries and we don't, so an
            # OURS_LONG flag against OSRM is most often just a ferry. Ask
            # OSRM's OWN route (steps carry mode=ferry) before spending an
            # arbiter call -- 11 of the 1,000-route campaign's 13 flags were
            # this. Only meaningful when OSRM was the primary and OSRM read
            # short; a ferry can't explain an OURS_SHORT flag.
            ferry = None
            if primary_engine == "osrm" and flag.startswith("OURS_LONG"):
                ferry, _ferry_err = osrm_route_uses_ferry(a, b)
                polite_pause()  # stay polite after the extra OSRM call
            if ferry:
                row["ferry"] = True
                row["verdict"] = "explained (OSRM route uses a ferry; we don't)"
                explained.append(row)
            else:
                # Arbitration (the ferry lesson generalized): a second engine
                # agreeing with US means the disagreement is
                # primary-engine-specific, not a lead. See
                # external_engines.arbitrate for the Valhalla->BRouter
                # fallback and the no-self-arbitration rule.
                arbiter, arbiter_engine, arbiter_err, agrees = arbitrate(
                    a, b, ours, primary_engine
                )
                row["arbiter_m"] = round(arbiter) if arbiter else None
                row["arbiter_engine"] = arbiter_engine
                row["arbiter_err"] = arbiter_err
                if agrees:
                    row["verdict"] = f"explained ({arbiter_engine} agrees with us; {primary_engine}-specific)"
                    explained.append(row)
                else:
                    row["verdict"] = "LEAD -- needs human triage"
                    leads.append(row)

        rows.append(row)
        mark = f"  <-- {row.get('verdict', flag)}" if flag else ""
        print(f"  [{compared:3d}/{n_pairs}] ours={ours:6.0f}m {primary_engine}={theirs:6.0f}m ratio={ratio:.2f}{mark}")

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
        print(f"\nLEAD {i}: ours={lead['ours_m']}m {lead['primary_engine']}={lead['theirs_m']}m "
              f"{lead['arbiter_engine']}={lead['arbiter_m']}m (ratio {lead['ratio']})")
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
