"""Shared RSS fetcher behavior (``fetchers.rss.fetch``).

Seam: ``fetchers.rss.fetch`` — one Source in, one FetchResult out. All tests
are offline: ``requests.get`` is monkeypatched and ``feedparser.parse`` is
stubbed on the fetcher module, mirroring the youtube-fetcher suite. Live
feed resolution is a smoke-test concern, per the spec's Testing Decisions.

The Politics & News category (ticket 06) drives the two behaviors pinned
here: democracynow permalinks ship as ``http://`` and are normalized to
``https://`` so dedup (``seen_items``, keyed by URL) matches across runs;
and the Substack-hosted sources (usermag, dropsite, kenklippenstein) ride
this same fetch path, which sends the full header set CDN-fronted feeds
require (no 403).
"""

from __future__ import annotations

import pytest

from conftest import make_source
from fetchers.rss import fetch


class FakeResponse:
    content = b"<rss/>"
    status_code = 200


def parsed(entries):
    return type("Parsed", (), {"bozo": False, "entries": entries,
                               "bozo_exception": None})()


def dnow_entry(link):
    return {
        "title": "Headline",
        "link": link,
        "published": "Sat, 01 Jun 2024 12:00:00 +0000",
        "summary": "<p>Detail.</p>",
    }


@pytest.fixture
def rss_fetch(monkeypatch):
    """Patch the fetcher's HTTP + parse boundaries; returns a runner."""
    import fetchers.rss as rss_module

    state = {"headers": None, "parsed": parsed([])}

    def fake_get(url, headers=None, **kwargs):
        state["headers"] = headers
        return FakeResponse()

    monkeypatch.setattr(rss_module.requests, "get", fake_get)
    monkeypatch.setattr(rss_module.feedparser, "parse",
                        lambda *a, **k: state["parsed"])

    def run(source, entries):
        state["parsed"] = parsed(entries)
        return fetch(source), state["headers"]

    return run


class TestHttpsNormalization:
    def test_http_permalinks_are_normalized_to_https(self, rss_fetch):
        source = make_source(
            name="democracynow", kind="rss",
            url="https://www.democracynow.org/democracynow.rss",
            homepage="https://www.democracynow.org/")
        result, _ = rss_fetch(source, [
            dnow_entry("http://www.democracynow.org/2024/6/1/story"),
            dnow_entry("https://www.democracynow.org/2024/6/1/other"),
        ])
        assert result.success
        assert [i.url for i in result.items] == [
            "https://www.democracynow.org/2024/6/1/story",
            "https://www.democracynow.org/2024/6/1/other",
        ]

    def test_normalized_permalinks_are_not_flagged_as_linked_out(
            self, rss_fetch):
        # The scheme upgrade must not trip the external-link (linked_url)
        # gate: an http:// permalink on the homepage's own host stays a
        # first-party item, not an external deep-read target.
        source = make_source(
            name="democracynow", kind="rss",
            url="https://www.democracynow.org/democracynow.rss",
            homepage="https://www.democracynow.org/")
        result, _ = rss_fetch(source, [
            dnow_entry("http://www.democracynow.org/2024/6/1/story"),
        ])
        assert result.items[0].linked_url is None


class TestFullHeaderRequests:
    def test_fetch_sends_the_full_header_set(self, rss_fetch):
        # The Substack-hosted sources ride this path: the full header set a
        # normal client sends, not a stripped-down one (no 403).
        source = make_source(name="usermag", kind="rss",
                             url="https://www.usermag.co/feed",
                             homepage="https://www.usermag.co/")
        _, headers = rss_fetch(source, [])
        assert headers is not None
        assert "User-Agent" in headers
        assert "Accept" in headers
        assert "Accept-Language" in headers
        assert "Accept-Encoding" in headers
