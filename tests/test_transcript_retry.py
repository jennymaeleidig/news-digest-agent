"""Regression tests: bounded retry on the transcript deep-read seam.

The transcript seam had zero retries: with GenericProxyConfig the api mounts
no urllib3 Retry (retries_when_blocked is 0), and direct connections never
get one — so a transient TLS EOF, a connection reset, or a rotating-proxy IP
that YouTube has flagged (IpBlocked) failed the item on attempt 1, every
time. Deterministic failures (TranscriptsDisabled, NoTranscriptFound,
VideoUnplayable) must still NOT be retried: the captions are not coming back.

Policy under test: up to TRANSCRIPT_ATTEMPTS attempts, one FRESH api instance
per attempt (fresh TCP/TLS; a rotating proxy hands out a fresh IP), short
backoff, retrying only RequestBlocked (incl. IpBlocked) and transient network
exceptions. Fully offline: the api is stubbed at the module seam.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import prefetch as prefetch_mod
from prefetch import (
    TRANSCRIPT_ATTEMPTS,
    fetch_transcript_excerpt,
)
from youtube_transcript_api import (
    IpBlocked,
    NoTranscriptFound,
    RequestBlocked,
    TranscriptsDisabled,
)


class _StubFetched:
    is_generated = False

    def __iter__(self):
        return iter([SimpleNamespace(text="hello "), SimpleNamespace(text="world")])


class _StubTranscript:
    def fetch(self):
        return _StubFetched()


class _StubList:
    def find_transcript(self, languages):
        return _StubTranscript()


class _StubApi:
    """Stub YouTubeTranscriptApi: one instance per attempt; `script` holds one
    entry per instance — an exception instance to raise from list(), or 'ok'.
    """

    script: list = []
    instances = 0

    def __init__(self, proxy_config=None):
        self.i = _StubApi.instances
        self.proxy_config = proxy_config
        _StubApi.instances += 1
        _instances.append(self)

    def list(self, video_id):
        step = _StubApi.script[self.i]
        if step != "ok":
            raise step
        return _StubList()


@pytest.fixture
def stub_api(monkeypatch):
    monkeypatch.delenv("YT_TRANSCRIPT_PROXY_URL", raising=False)
    monkeypatch.setattr(prefetch_mod, "YouTubeTranscriptApi", _StubApi)
    monkeypatch.setattr(prefetch_mod.time, "sleep", lambda s: None)
    _StubApi.script = []
    _StubApi.instances = 0
    _instances.clear()
    return _StubApi


# Every instance the code under test constructed, in order (so tests can
# assert which pool each attempt used).
_instances: list = []


def test_attempts_constant_is_bounded():
    assert 2 <= TRANSCRIPT_ATTEMPTS <= 5


# --- path alternation (env unset: direct only, unchanged) -------------------

def test_no_proxy_all_attempts_direct(stub_api):
    stub_api.script = [IpBlocked(video_id="v")] * TRANSCRIPT_ATTEMPTS
    fetch_transcript_excerpt("vid1234567")
    assert all(api.proxy_config is None for api in _instances)


def test_proxy_set_alternates_pools(stub_api, monkeypatch):
    monkeypatch.setenv("YT_TRANSCRIPT_PROXY_URL", "http://user:pass@gw.test:823")
    stub_api.script = [IpBlocked(video_id="v")] * TRANSCRIPT_ATTEMPTS
    fetch_transcript_excerpt("vid1234567")
    kinds = ["proxy" if api.proxy_config is not None else "direct"
             for api in _instances]
    # primary pool first, then the other pool, alternating — two independent
    # IP pools get flagged independently, so a block falls over to the other.
    assert kinds == ["proxy", "direct", "proxy"]


# --- block vs transient vs deterministic ------------------------------------

def test_ipblocked_is_retried_with_fresh_instance_and_succeeds(stub_api):
    stub_api.script = [IpBlocked(video_id="v"), "ok"]
    block, err = fetch_transcript_excerpt("vid1234567")
    assert err is None and "hello" in block
    assert _StubApi.instances == 2            # fresh api instance per attempt


def test_requestblocked_exhausts_attempts_then_errors(stub_api):
    stub_api.script = [IpBlocked(video_id="v")] * TRANSCRIPT_ATTEMPTS
    block, err = fetch_transcript_excerpt("vid1234567")
    assert block == "" and "IpBlocked" in err
    assert _StubApi.instances == TRANSCRIPT_ATTEMPTS


def test_transient_ssl_error_is_retried_and_succeeds(stub_api):
    stub_api.script = [
        ConnectionError("SSLError: UNEXPECTED_EOF_WHILE_READING"), "ok"]
    block, err = fetch_transcript_excerpt("vid1234567")
    assert err is None and "hello" in block


def test_transient_error_exhausts_attempts_then_errors(stub_api):
    stub_api.script = [ConnectionError("reset by peer")] * TRANSCRIPT_ATTEMPTS
    block, err = fetch_transcript_excerpt("vid1234567")
    assert block == "" and "transcript fetch failed" in err
    assert _StubApi.instances == TRANSCRIPT_ATTEMPTS


def test_deterministic_failure_is_never_retried(stub_api):
    stub_api.script = [TranscriptsDisabled(video_id="v")] * TRANSCRIPT_ATTEMPTS
    block, err = fetch_transcript_excerpt("vid1234567")
    assert block == "" and "TranscriptsDisabled" in err
    assert _StubApi.instances == 1            # captions are not coming back


def test_no_transcript_found_is_never_retried(stub_api):
    stub_api.script = [NoTranscriptFound(video_id="v", requested_language_codes=["en"],
                                         transcript_data="none")] * TRANSCRIPT_ATTEMPTS
    block, err = fetch_transcript_excerpt("vid1234567")
    assert block == "" and "NoTranscriptFound" in err
    assert _StubApi.instances == 1
