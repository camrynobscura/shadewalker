"""Tests for pipeline/fetch/socrata.py's auth-header logic and
get_with_retry()'s transient-failure handling.

fetch_all_rows() itself (pagination, caching, the real live request) stays
out of pytest's scope on purpose -- no network calls here. get_with_retry()
is public (parks.py and boundaries.py share it) and gets mock-based
coverage: a single transient 503 from Socrata must not kill a run.
"""

import requests

from pipeline import config
from pipeline.fetch import socrata


def test_auth_headers_includes_token_when_configured(monkeypatch):
    monkeypatch.setattr(config, "SOCRATA_APP_TOKEN", "fake-token-123")
    assert socrata.auth_headers() == {"X-App-Token": "fake-token-123"}


def test_auth_headers_empty_when_token_unset(monkeypatch):
    monkeypatch.setattr(config, "SOCRATA_APP_TOKEN", None)
    assert socrata.auth_headers() == {}


class _FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            error = requests.exceptions.HTTPError(f"{self.status_code} error")
            error.response = self
            raise error


def test_get_with_retry_succeeds_without_retrying_on_a_clean_response(monkeypatch):
    monkeypatch.setattr(socrata.requests, "get", lambda *args, **kwargs: _FakeResponse(200))
    response = socrata.get_with_retry("http://example.test", {}, {})
    assert response.status_code == 200


def test_get_with_retry_recovers_from_a_transient_503(monkeypatch):
    # The second attempt succeeding is what lets a run survive a one-off
    # blip instead of dying outright.
    responses = iter([_FakeResponse(503), _FakeResponse(200)])
    monkeypatch.setattr(socrata.requests, "get", lambda *args, **kwargs: next(responses))
    monkeypatch.setattr(socrata.time, "sleep", lambda seconds: None)

    response = socrata.get_with_retry("http://example.test", {}, {})
    assert response.status_code == 200


def test_get_with_retry_gives_up_after_max_retries_of_5xx(monkeypatch):
    monkeypatch.setattr(socrata.requests, "get", lambda *args, **kwargs: _FakeResponse(503))
    monkeypatch.setattr(socrata.time, "sleep", lambda seconds: None)

    try:
        socrata.get_with_retry("http://example.test", {}, {})
        assert False, "expected HTTPError to propagate"
    except requests.exceptions.HTTPError:
        pass


def test_get_with_retry_does_not_retry_a_4xx(monkeypatch):
    # A malformed query or a rejected app token fails identically no
    # matter how many times it's retried -- burning the retry budget on
    # it would just delay the real error, not fix anything.
    calls = []

    def fake_get(*args, **kwargs):
        calls.append(1)
        return _FakeResponse(400)

    monkeypatch.setattr(socrata.requests, "get", fake_get)

    try:
        socrata.get_with_retry("http://example.test", {}, {})
        assert False, "expected HTTPError to propagate"
    except requests.exceptions.HTTPError:
        pass
    assert len(calls) == 1
