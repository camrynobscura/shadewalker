"""Unit tests for the external-engine harness fallback logic
(external_engines.primary_comparison / arbitrate).

The whole point of the BRouter fallback is to behave correctly when OSRM or
Valhalla is down -- which can't be exercised with live engines. So the two
functions take their engine callables as arguments, and these tests drive
them with fakes (no network, default tier). pause_fn is a no-op here so the
suite never sleeps.
"""
import pytest

from external_engines import (
    arbitrate,
    coerce_osrm_node_id,
    gap_probe_snap_ok,
    in_nyc_bbox,
    osrm_route_uses_ferry,
    primary_comparison,
    valhalla_no_ferry_length_m,
)

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


class _Resp:
    """A minimal stand-in for a requests Response (status_code + .json())."""
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def _script(*outcomes):
    """A post/get callable that returns/raises the next scripted outcome per
    call. An outcome is either a _Resp or an Exception instance (raised)."""
    calls = {"n": 0}

    def fn(*args, **kwargs):
        outcome = outcomes[calls["n"]]
        calls["n"] += 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    fn.calls = calls
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
    # snap-guarded, so it must not paper over it.
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


# ── valhalla retry ───────────────────────────────────────────────────────────

_VALHALLA_OK = {"trip": {"summary": {"length": 1.5}}}  # 1.5 km -> 1500 m


def test_valhalla_retries_once_after_a_transient_error_then_succeeds():
    post = _script(ConnectionError("boom"), _Resp(_VALHALLA_OK))
    length, err = valhalla_no_ferry_length_m(A, B, post_fn=post, pause_fn=NOOP)
    assert (length, err) == (1500.0, None)
    assert post.calls["n"] == 2  # first failed, retry succeeded


def test_valhalla_retries_on_a_5xx_then_succeeds():
    post = _script(_Resp({}, status_code=503), _Resp(_VALHALLA_OK))
    length, err = valhalla_no_ferry_length_m(A, B, post_fn=post, pause_fn=NOOP)
    assert (length, err) == (1500.0, None)
    assert post.calls["n"] == 2


def test_valhalla_gives_up_after_exhausting_retries():
    post = _script(ConnectionError("down"), ConnectionError("still down"))
    length, err = valhalla_no_ferry_length_m(A, B, post_fn=post, pause_fn=NOOP, retries=1)
    assert length is None
    assert "valhalla error" in err
    assert post.calls["n"] == 2  # initial + one retry, no more


def test_valhalla_does_not_retry_a_clean_no_route():
    # A 200 that simply carries no trip is a genuine "no route", not
    # transient -- retrying would just waste a call.
    post = _script(_Resp({"error": "No path could be found"}))
    length, err = valhalla_no_ferry_length_m(A, B, post_fn=post, pause_fn=NOOP)
    assert length is None
    assert "No path" in err
    assert post.calls["n"] == 1  # no retry


# ── ferry auto-explain ───────────────────────────────────────────────────────

def _osrm_steps_resp(*modes):
    steps = [{"mode": m} for m in modes]
    return _Resp({"code": "Ok", "routes": [{"legs": [{"steps": steps}]}]})


def test_osrm_route_uses_ferry_detects_a_ferry_leg():
    get = _script(_osrm_steps_resp("walking", "ferry", "walking"))
    used, err = osrm_route_uses_ferry(A, B, get_fn=get)
    assert (used, err) == (True, None)


def test_osrm_route_uses_ferry_false_when_all_walking():
    get = _script(_osrm_steps_resp("walking", "walking"))
    used, err = osrm_route_uses_ferry(A, B, get_fn=get)
    assert (used, err) == (False, None)


def test_osrm_route_uses_ferry_reports_a_bad_code():
    get = _script(_Resp({"code": "NoRoute"}))
    used, err = osrm_route_uses_ferry(A, B, get_fn=get)
    assert used is None
    assert "NoRoute" in err


# ── oracle rules ─────────────────────────────────────────────────────────────

def test_gap_probe_snap_ok_rejects_a_snap_that_reaches_half_the_gap():
    # 12m gap: a 5m snap is fine, a 6m snap (>= 0.5x) is ABSENT_OR_PRUNED.
    assert gap_probe_snap_ok(5.0, 12.0) is True
    assert gap_probe_snap_ok(6.0, 12.0) is False
    assert gap_probe_snap_ok(0.0, 0.0) is False  # degenerate gap never trusted


def test_in_nyc_bbox_accepts_nyc_and_rejects_switzerland():
    assert in_nyc_bbox(40.7128, -74.0060) is True   # lower Manhattan
    assert in_nyc_bbox(46.9481, 7.4474) is False     # Bern, the annotation-garbage case


def test_coerce_osrm_node_id_handles_float_and_string_forms():
    assert coerce_osrm_node_id(1234567890.0) == 1234567890
    assert coerce_osrm_node_id("42990458") == 42990458
    assert coerce_osrm_node_id(42990458) == 42990458


def test_coerce_osrm_node_id_rejects_a_non_integral_value():
    with pytest.raises(ValueError):
        coerce_osrm_node_id(1234.5)
