"""Offline tests for the CI smoke-test of the three network-backed fetchers.

The seam under test is the smoke test's orchestration logic — ``run_smoke`` —
with an injected fake fetcher registry and synthetic sources, so this suite
stays fully offline. The smoke test's core guarantee is that a source which a
local (residential) IP would accept but the GitHub Actions datacenter IP
blocks is surfaced as a **failure**: the smoke test must pass only when each
network-backed fetcher returns a full body that maps to **non-empty** items.

These tests feed ``run_smoke`` fake ``FetchResult``s and assert the pass/fail
decision and the surfaced problems:
  - a healthy fetch (success + items) passes;
  - a fetch failure (success=False) is surfaced;
  - a 200 that maps to zero items (the bot-blocked/empty-body tell) is
    surfaced as a failure, exactly like a fetch error;
  - sources outside the three in-scope network-backed fetchers are skipped,
    not smeared into the pass/fail decision.

The live network call itself is not unit-tested — that is the CI job's job —
and the smoke test never retries a host, so it cannot hammer any of the
endpoints it exercises.
"""

from __future__ import annotations

import pytest

from categories import Source
from fetchers.common import FetchResult
from scripts.smoke_test_fetchers import SMOKED_SOURCE_NAMES, check_fetch, run_smoke


def _source(name):
    """A minimal Source carrying only the name the smoke test keys on."""
    return Source(name=name, tier=3, kind="rss", url="https://example.com/feed")


@pytest.fixture
def three_sources():
    """The three in-scope network-backed sources, in the category's order."""
    return [_source("Hugging Face Daily Papers"),
            _source("AI Release Tracker"),
            _source("radarai.top (AI)")]


def _registry(*results):
    """A fake fetcher-registry dispatching per source name to a fixed result.
    Sources without a result fail — isolating dispatch, mirroring fetch_one."""
    by_name = {src_name: res for src_name, res in results}

    def reg(source):
        return by_name.get(source.name, FetchResult(source.name, False, error="no result"))
    return reg


def _ok(name, n=3):
    return FetchResult(name, success=True, items=[object() for _ in range(n)])


# --- healthy path (AC 2): each fetcher returns a full body with non-empty items
def test_all_three_healthy_passes(three_sources):
    reg = _registry(
        ("Hugging Face Daily Papers", _ok("Hugging Face Daily Papers")),
        ("AI Release Tracker", _ok("AI Release Tracker")),
        ("radarai.top (AI)", _ok("radarai.top (AI)")),
    )
    ok, problems, results = run_smoke(three_sources, reg)
    assert ok is True
    assert problems == []


# --- surfaced datacenter-block (AC 3): a 200-to-zero-items fetch must fail
def test_zero_items_is_surfaced_as_failure(three_sources):
    """A full body that maps to zero items — the bot-blocked / empty-response
    tell a local IP would not reproduce — is surfaced as a failure, so a
    scrape that only works locally cannot silently pass."""
    empty = FetchResult("Hugging Face Daily Papers", success=True, items=[])
    reg = _registry(
        ("Hugging Face Daily Papers", empty),
        ("AI Release Tracker", _ok("AI Release Tracker")),
        ("radarai.top (AI)", _ok("radarai.top (AI)")),
    )
    ok, problems, results = run_smoke(three_sources, reg)
    assert ok is False
    assert any("Hugging Face Daily Papers" in p and "zero items" in p for p in problems)


# --- fetch failure (AC 1/AC 3): a failed fetch is surfaced too
def test_fetch_failure_is_surfaced(three_sources):
    failing = FetchResult("AI Release Tracker", success=False, error="HTTP 503")
    reg = _registry(
        ("Hugging Face Daily Papers", _ok("Hugging Face Daily Papers")),
        ("AI Release Tracker", failing),
        ("radarai.top (AI)", _ok("radarai.top (AI)")),
    )
    ok, problems, results = run_smoke(three_sources, reg)
    assert ok is False
    assert any("AI Release Tracker" in p and "HTTP 503" in p for p in problems)


def test_all_failing_reports_each_problem(three_sources):
    reg = _registry(
        ("Hugging Face Daily Papers", FetchResult("Hugging Face Daily Papers", False, error="HTTP 403")),
        ("AI Release Tracker", FetchResult("AI Release Tracker", True, items=[])),
        ("radarai.top (AI)", FetchResult("radarai.top (AI)", False, error="HTTP 404")),
    )
    ok, problems, results = run_smoke(three_sources, reg)
    assert ok is False
    # one problem per in-scope source
    assert len(problems) == 3
    assert any("Hugging Face Daily Papers" in p for p in problems)
    assert any("AI Release Tracker" in p and "zero items" in p for p in problems)
    assert any("radarai.top (AI)" in p for p in problems)


# --- out-of-scope sources are skipped, not counted (AC scope: three fetchers)
def test_out_of_scope_sources_are_skipped(three_sources):
    """Sources outside the three network-backed fetchers (e.g. Reddit, which
    429s on rapid repeats) are not part of the smoke test's pass/fail."""
    extra = [_source("Reddit r/LocalLLaMA")]
    reg = _registry(
        ("Hugging Face Daily Papers", _ok("Hugging Face Daily Papers")),
        ("AI Release Tracker", _ok("AI Release Tracker")),
        ("radarai.top (AI)", _ok("radarai.top (AI)")),
        ("Reddit r/LocalLLaMA", FetchResult("Reddit r/LocalLLaMA", False, error="HTTP 429")),
    )
    ok, problems, results = run_smoke(three_sources + extra, reg)
    assert ok is True
    assert problems == []
    assert "Reddit r/LocalLLaMA" not in SMOKED_SOURCE_NAMES


# --- single-dispatch (AC 4): never hammer a host with retries ---------------
def test_each_fetcher_dispatched_exactly_once(three_sources):
    """Each network-backed fetcher is called exactly once — no retry loops —
    so the smoke test cannot hammer any host it exercises."""
    calls: list[str] = []

    def registry(source):
        calls.append(source.name)
        return _ok(source.name)

    run_smoke(three_sources, registry)
    # one dispatch per in-scope source, no repeats
    assert sorted(calls) == sorted(SMOKED_SOURCE_NAMES)
    assert len(calls) == len(SMOKED_SOURCE_NAMES)


# --- results mapping (reporting without re-dispatch) -----------------------
def test_run_smoke_returns_per_source_results(three_sources):
    reg = _registry(
        ("Hugging Face Daily Papers", _ok("Hugging Face Daily Papers")),
        ("AI Release Tracker", _ok("AI Release Tracker")),
        ("radarai.top (AI)", _ok("radarai.top (AI)")),
    )
    ok, problems, results = run_smoke(three_sources, reg)
    assert ok is True
    assert set(results.keys()) == set(SMOKED_SOURCE_NAMES)
    assert all(results[n].success for n in SMOKED_SOURCE_NAMES)


# --- check_fetch unit seam ------------------------------------------------
def test_check_fetch_healthy_is_empty():
    assert check_fetch("S", _ok("S")) == []


def test_check_fetch_failure_is_reported():
    problems = check_fetch("S", FetchResult("S", False, error="HTTP 500"))
    assert len(problems) == 1
    assert "S" in problems[0] and "HTTP 500" in problems[0]


def test_check_fetch_zero_items_is_reported():
    problems = check_fetch("S", FetchResult("S", True, items=[]))
    assert len(problems) == 1
    assert "S" in problems[0] and "zero items" in problems[0]


def test_dispatched_none_result_is_surfaced(three_sources):
    """A source that the fetcher registry dispatches to None is surfaced as a
    failure, not a crash."""
    reg = lambda source: None
    ok, problems, results = run_smoke(three_sources, reg)
    assert ok is False
    assert len(problems) == len(three_sources)
    assert any("no result" in p for p in problems)
