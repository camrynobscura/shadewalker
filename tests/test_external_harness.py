"""Unit tests for the external-engine harness fallback logic
(external_engines.primary_comparison / arbitrate).

The whole point of the BRouter fallback is to behave correctly when OSRM or
Valhalla is DOWN -- which can't be exercised with live engines. So the two
functions take their engine callables as arguments, and these tests drive
them with fakes (no network, default tier). pause_fn is a no-op here so the
suite never sleeps.
"""
from external_engines import arbitrate, primary_comparison

A = (40.70, -73.98)
B = (40.71, -73.97)
NOOP = lambda: None


def _fixed(value, err=None):
    """An engine callable that always returns the same (value, err)."""
    def fn(a, b):
        return value, err
    return fn


def _boom(marker):
    """An engine callable that fails the test if it is ever called."""
    def fn(a, b):
        raise AssertionError(f"{marker} should not have been called")
    return fn


# ── primary_comparison ──────────────────────────────────────────────────────

def test_primary_uses_osrm_when_it_works_and_never_calls_brouter():
    length, engine, err = primary_comparison(
        A, B, 1000, osrm_fn=_fixed(1234.0), brouter_fn=_boom("brouter"), pause_fn=NOOP
    )
    assert (length, engine, err) == (1234.0, "osrm", None)


def test_primary_falls_back_to_brouter_when_osrm_is_down():
    length, engine, err = primary_comparison(
        A, B, 1000,
        osrm_fn=_fixed(None, "osrm error: timeout"),
        brouter_fn=_fixed(1300.0), pause_fn=NOOP,
    )
    assert (length, engine, err) == (1300.0, "brouter", None)


def test_primary_does_not_fall_back_on_a_snap_move():
    # A per-pair snap is a real skip, not an outage -- BRouter can't be
    # snap-guarded, so it must NOT paper over it.
    length, engine, err = primary_comparison(
        A, B, 1000,
        osrm_fn=_fixed(None, "osrm snap moved 91m"),
        brouter_fn=_boom("brouter"), pause_fn=NOOP,
    )
    assert length is None
    assert err == "osrm snap moved 91m"


def test_primary_reports_osrm_reason_when_both_are_down():
    length, engine, err = primary_comparison(
        A, B, 1000,
        osrm_fn=_fixed(None, "osrm code: NoRoute"),
        brouter_fn=_fixed(None, "brouter error: 500"), pause_fn=NOOP,
    )
    assert length is None
    assert err == "osrm code: NoRoute"  # the primary's reason, for the skip tally


# ── arbitrate ───────────────────────────────────────────────────────────────

def test_arbiter_uses_valhalla_when_up_and_never_calls_brouter():
    arb, engine, err, agrees = arbitrate(
        A, B, 1000, "osrm",
        valhalla_fn=_fixed(1040.0), brouter_fn=_boom("brouter"), pause_fn=NOOP,
    )
    assert (arb, engine, agrees) == (1040.0, "valhalla-no-ferry", True)


def test_arbiter_falls_back_to_brouter_when_valhalla_is_down():
    arb, engine, err, agrees = arbitrate(
        A, B, 1000, "osrm",
        valhalla_fn=_fixed(None, "valhalla error: 502"),
        brouter_fn=_fixed(1050.0), pause_fn=NOOP,
    )
    assert (arb, engine, agrees) == (1050.0, "brouter", True)


def test_brouter_never_self_arbitrates_when_it_was_the_primary():
    # If BRouter was already the primary, there is no independent second
    # engine -- the pair stays an honest unarbitrated lead, not a self-agree.
    arb, engine, err, agrees = arbitrate(
        A, B, 1000, "brouter",
        valhalla_fn=_fixed(None, "valhalla error: 502"),
        brouter_fn=_boom("brouter"), pause_fn=NOOP,
    )
    assert arb is None
    assert agrees is False
    assert engine == "valhalla-no-ferry"


def test_arbiter_disagreement_leaves_it_a_lead():
    arb, engine, err, agrees = arbitrate(
        A, B, 1000, "osrm",
        valhalla_fn=_fixed(1400.0), brouter_fn=_boom("brouter"), pause_fn=NOOP,
    )
    assert agrees is False  # 1.4x is well outside ARBITER_AGREE_RATIO
