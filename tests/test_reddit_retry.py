"""Regression tests: bounded retry on the reddit-rss-api fetcher.

The community reddit-rss-api proxy intermittently returns bad gateways
(HTTP 502/503) and resets connections — observed on scheduled digest runs
while the smoke test passed (a single healthy probe). One-shot fetching
surfaced those blips as a missing Reddit source in that day's digest.

Policy under test: up to REDDIT_FETCH_ATTEMPTS attempts, a FRESH HTTP
request per attempt, short linear backoff, retrying only transient causes —
5xx gateway responses, 429, and requests.RequestException. Deterministic
failures (403/404 and other non-transient statuses) fail immediately.
Fully offline: requests.get is stubbed at the module seam.
"""

from __future__ import annotations

import pytest
import requests

import fetchers.reddit_rss_api as reddit_mod
from categories import Source
from config import REDDIT_FETCH_ATTEMPTS
from fetchers.config_schema import FetcherConfig
from fetchers.reddit_rss_api import fetch


def _source() -> Source:
    return Source(
        name="Reddit r/LocalLLaMA",
        tier=3,
        kind="reddit_rss_api",
        url="https://reddit-rss-api.example/r/LocalLLaMA",
        homepage="https://reddit.com/r/LocalLLaMA",
        sections=("General news",),
        fetcher_config=FetcherConfig(
            url="https://reddit-rss-api.example/r/LocalLLaMA",
            item="threads",
            title="title",
            link="url",
            date="isoDate",
        ),
    )


class _StubResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


_OK_PAYLOAD = {"threads": [
    {"title": "A post", "url": "https://example.com/a", "isoDate": "2026-08-30T00:00:00Z"},
]}


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(reddit_mod.time, "sleep", lambda s: None)


def test_attempts_bounded_and_sane():
    assert 2 <= REDDIT_FETCH_ATTEMPTS <= 5


def test_retries_bad_gateway_then_succeeds(monkeypatch):
    calls = []

    def stub_get(url, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            return _StubResponse(502)
        return _StubResponse(200, _OK_PAYLOAD)

    monkeypatch.setattr(reddit_mod.requests, "get", stub_get)
    result = fetch(_source())

    assert result.success
    assert len(result.items) == 1
    assert len(calls) == 2


def test_exhausts_attempts_on_persistent_bad_gateway(monkeypatch):
    calls = []

    def stub_get(url, **kwargs):
        calls.append(url)
        return _StubResponse(502)

    monkeypatch.setattr(reddit_mod.requests, "get", stub_get)
    result = fetch(_source())

    assert not result.success
    assert "502" in (result.error or "")
    assert len(calls) == REDDIT_FETCH_ATTEMPTS


def test_backs_off_between_attempts(monkeypatch):
    sleeps = []
    monkeypatch.setattr(reddit_mod.time, "sleep", sleeps.append)

    def stub_get(url, **kwargs):
        return _StubResponse(503)

    monkeypatch.setattr(reddit_mod.requests, "get", stub_get)
    fetch(_source())

    # Linear backoff between attempts: (ATTEMPTS - 1) sleeps, growing.
    assert len(sleeps) == REDDIT_FETCH_ATTEMPTS - 1
    assert sleeps == sorted(sleeps)
    assert all(s > 0 for s in sleeps)


def test_does_not_retry_deterministic_4xx(monkeypatch):
    calls = []

    def stub_get(url, **kwargs):
        calls.append(url)
        return _StubResponse(403)

    monkeypatch.setattr(reddit_mod.requests, "get", stub_get)
    result = fetch(_source())

    assert not result.success
    assert "403" in (result.error or "")
    assert len(calls) == 1


def test_retries_transient_network_exception_then_succeeds(monkeypatch):
    calls = []

    def stub_get(url, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            raise requests.ConnectionError("reset by peer")
        return _StubResponse(200, _OK_PAYLOAD)

    monkeypatch.setattr(reddit_mod.requests, "get", stub_get)
    result = fetch(_source())

    assert result.success
    assert len(calls) == 2


def test_transient_429_is_retried(monkeypatch):
    calls = []

    def stub_get(url, **kwargs):
        calls.append(url)
        if len(calls) < REDDIT_FETCH_ATTEMPTS:
            return _StubResponse(429)
        return _StubResponse(200, _OK_PAYLOAD)

    monkeypatch.setattr(reddit_mod.requests, "get", stub_get)
    result = fetch(_source())

    assert result.success
    assert len(calls) == REDDIT_FETCH_ATTEMPTS
