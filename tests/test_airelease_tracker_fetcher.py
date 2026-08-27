"""Offline tests for the AI Release Tracker HTML-scraping fetcher (ticket 04).

The seam under test is the fetcher's public interface — ``fetch(source)``
against a source of kind ``airelease_tracker`` carrying the shared
fetcher-config. These tests feed the fetcher a **captured HTML string** (via a
stubbed ``requests.get``) and assert the mapped ``Item``s: title, article URL,
the snippet (provider), and the published date parsed from the displayed date
string (e.g. ``"Wed, Aug 26 2026"``) into a timezone-aware value. They also pin
the isolate-and-continue behavior (an HTTP error is returned as a failure
result, never raised) and the registry registration (the kind dispatches
through the existing kind-to-fetcher seam with no pipeline edit).

Per the spec's testing decision, the tests assert external, deterministic
behavior — the resulting items — and never couple to selector expressions or
parsing internals.
"""

from __future__ import annotations

import datetime
import main
from categories import Source
from fetchers.config_schema import FetcherConfig
from fetchers.airelease_tracker import fetch

urldom = "https://" + "aireleasetracker" + ".com"
PAGE_URL = urldom + "/latest"

# Captured /latest HTML: server-rendered model-release cards. Each is an anchor
# (``<a href="/model/{provider}/{slug}">``) wrapping a title span
# (``text-white truncate``), a provider span (``text-gray-500 truncate``), and
# a right-aligned date div whose text is a human-readable date string.
CAPTURED_HTML = (
    "<div>"
    '<a href="/model/qwen/qwen25">'
    '<span class="text-white truncate">Qwen2.5</span>'
    '<span class="text-gray-500 truncate">Qwen \u00b7 Alibaba</span>'
    "<div>Wed, Aug 26 2026</div>"
    "</a>"
    '<a href="/model/deepseek/deepseek-v3">'
    '<span class="text-white truncate">DeepSeek-V3</span>'
    '<span class="text-gray-500 truncate">DeepSeek</span>'
    "<div>Mon, Aug 24 2026</div>"
    "</a>"
    "</div>"
)


def _source():
    """An airelease_tracker source carrying the shared fetcher-config whose
    item/title/link/date values are CSS selectors against the rendered HTML."""
    return Source(
        name="AI Release Tracker",
        tier=4,
        kind="airelease_tracker",
        url=PAGE_URL,
        homepage=PAGE_URL,
        fetcher_config=FetcherConfig(
            url=PAGE_URL,
            item='a[href^="/model/"]',
            title="span.text-white.truncate",
            link="a",
            date="div",
        ),
    )


class _FakeResponse:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text


# --- registry seam (AC 1): registered, no pipeline edit --------------------
def test_kind_registered_on_registry():
    """The kind is registered on the kind-to-fetcher registry — a
    registration, not a pipeline edit, and it stays distinct from the HF JSON
    kind."""
    from fetchers.registry import FETCHERS
    assert "airelease_tracker" in FETCHERS
    assert FETCHERS["airelease_tracker"] is fetch
    assert "huggingface_papers" in FETCHERS        # the JSON kind stays distinct
    assert FETCHERS["airelease_tracker"] is not FETCHERS["huggingface_papers"]


# --- field mapping (AC 3): title, url, snippet, parsed timezone-aware date --
def test_maps_each_release_card_to_an_item(monkeypatch):
    """Feeding the fetcher a captured HTML string yields one Item per release
    card with the right title, absolute article URL, provider-as-snippet, and
    provenance. The link is expanded from the anchor's relative ``/model/...``
    href against the source page URL."""
    monkeypatch.setattr(
        "fetchers.airelease_tracker.requests.get",
        lambda *a, **k: _FakeResponse(200, CAPTURED_HTML),
    )
    result = fetch(_source())
    assert result.success is True
    assert result.error is None
    assert len(result.items) == 2

    first, second = result.items
    # title
    assert first.title == "Qwen2.5"
    assert second.title == "DeepSeek-V3"
    # absolute article URL built from the container href
    assert first.url == urldom + "/model/qwen/qwen25"
    assert second.url == urldom + "/model/deepseek/deepseek-v3"
    # snippet = provider
    assert first.content_snippet == "Qwen \u00b7 Alibaba"
    assert second.content_snippet == "DeepSeek"
    # provenance
    assert first.source_name == "AI Release Tracker"


def test_published_date_is_parsed_tz_aware_not_raw_string(monkeypatch):
    """The displayed date string (e.g. "Wed, Aug 26 2026") is parsed into a
    timezone-aware ISO-8601 value — NOT left as the raw label — so the filter
    can compare it against the window cutoff like any other source."""
    monkeypatch.setattr(
        "fetchers.airelease_tracker.requests.get",
        lambda *a, **k: _FakeResponse(200, CAPTURED_HTML),
    )
    result = fetch(_source())
    first, second = result.items
    # timezone-aware ISO (UTC), not the raw "Wed, Aug 26 2026" label
    assert first.published == "2026-08-26T00:00:00+00:00"
    assert first.published.endswith("+00:00")
    assert "Wed, Aug 26 2026" not in first.published
    assert second.published == "2026-08-24T00:00:00+00:00"


class _FrozenUtcNow(datetime.datetime):
    """A datetime pinned to a fixed "now" so the time-window filter's cutoff is
    deterministic for the out-of-window test (no dependence on the real clock)."""
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 8, 26, 12, 0, 0, tzinfo=tz or datetime.timezone.utc)


def test_out_of_window_release_is_ordinary_drop(monkeypatch):
    """An out-of-window release is an ordinary drop: the fetcher just emits the
    parsed published date, and the standard time-window filter drops it
    identically to any other source's old item — releases are never special-
    cased in, regardless of date."""
    monkeypatch.setattr(
        "fetchers.airelease_tracker.requests.get",
        lambda *a, **k: _FakeResponse(200, CAPTURED_HTML),
    )
    monkeypatch.setattr(main, "datetime", _FrozenUtcNow)  # "now" = 2026-08-26
    result = fetch(_source())
    assert result.success is True and len(result.items) == 2
    # The shared filter uses the parsed tz-aware published date exactly like
    # every other source: recent (Aug 26) kept, older (Aug 24) dropped.
    assert main.filter_recent([result.items[0]], days=2) == [result.items[0]]
    assert main.filter_recent([result.items[1]], days=2) == []
    assert main.filter_recent(result.items, days=100) == result.items


# --- isolate-and-continue (AC 4): HTTP errors returned, not raised ---------
def test_http_error_is_returned_not_raised(monkeypatch):
    """An HTTP error from the page becomes a failure FetchResult rather than
    raising, so the run isolates-and-continues past this source."""
    monkeypatch.setattr(
        "fetchers.airelease_tracker.requests.get",
        lambda *a, **k: _FakeResponse(503, ""),
    )
    result = fetch(_source())
    assert result.success is False
    assert result.error is not None
    assert "503" in result.error


# --- browser-like headers (AC 4) ------------------------------------------
def test_sends_browser_like_headers(monkeypatch):
    """The fetcher sends the browser-like User-Agent and a full header set, so
    bot-sensitive hosts respond with a full body."""
    captured = {}

    def fake_get(url, headers=None, timeout=None, allow_redirects=None):
        captured["url"] = url
        captured["headers"] = headers
        return _FakeResponse(200, CAPTURED_HTML)

    monkeypatch.setattr("fetchers.airelease_tracker.requests.get", fake_get)
    fetch(_source())

    assert captured["url"] == PAGE_URL
    assert "User-Agent" in captured["headers"]
    assert captured["headers"]["User-Agent"].startswith("Mozilla/5.0")
    assert captured["headers"].get("Accept")
    assert captured["headers"].get("Accept-Language")
    assert captured["headers"].get("Accept-Encoding")
