"""Ticket 02 — keyless per-channel YouTube Atom-feed listing fetcher.

Seams:
  - ``fetchers.youtube.fetch`` — the public fetcher interface, mirroring the
    RSS fetcher's seam (one Source in, one FetchResult out).
  - ``fetchers.registry.fetch_one`` — the kind→fetcher dispatch seam.

All tests are offline: ``requests.get`` is monkeypatched (declared patchable
in conftest) and ``feedparser.parse`` is stubbed on the fetcher module, so no
network is touched. The live verification of the three shipped channel ids is
a smoke-test concern, per the existing fetcher smoke-test pattern.
"""

from __future__ import annotations

import pytest

from conftest import make_source
from fetchers.common import FetchResult


CHANNEL_ID = "UCQoOmu6mKZkXTnwZcpD8Ciw"


def youtube_source(**kw):
    return make_source(name="jason-schreier", kind="youtube",
                       url=kw.pop("url", CHANNEL_ID), **kw)


class FakeResponse:
    def __init__(self, content=b"<feed/>", status_code=200):
        self.content = content
        self.status_code = status_code


def feed_result(entries, bozo=False):
    """A stand-in for feedparser's parsed feed."""
    return type("Parsed", (), {"bozo": bozo, "entries": entries,
                               "bozo_exception": None})()


def entry(title, video_id, published="Sat, 01 Jun 2024 12:00:00 +0000"):
    return {
        "title": title,
        "link": f"https://www.youtube.com/watch?v={video_id}",
        "published": published,
        "summary": f"<p>About {title}.</p>",
    }


@pytest.fixture
def feed(monkeypatch):
    """Patch the fetcher's HTTP + parse boundaries; returns a setter."""
    import fetchers.youtube as yt

    state = {"response": FakeResponse(), "parsed": feed_result([])}

    def set_response(response):
        state["response"] = response

    def set_parsed(parsed):
        state["parsed"] = parsed

    monkeypatch.setattr(yt.requests, "get",
                        lambda *a, **k: state["response"])
    monkeypatch.setattr(yt.feedparser, "parse",
                        lambda *a, **k: state["parsed"])
    return type("Feed", (), {"set_response": staticmethod(set_response),
                             "set_parsed": staticmethod(set_parsed)})()


class TestListing:
    def test_entries_map_to_items_with_watch_urls_and_video_ids(self, feed):
        from fetchers.youtube import fetch

        feed.set_parsed(feed_result([
            entry("Baldur's Gate 3 postmortem", "dQw4w9WgXcQ"),
            entry("Studio deep dive", "abc123_-XYZ4"),
        ]))
        result = fetch(youtube_source())
        assert result.success
        assert result.error is None
        assert [i.url for i in result.items] == [
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://www.youtube.com/watch?v=abc123_-XYZ4",
        ]
        assert [i.video_id for i in result.items] == ["dQw4w9WgXcQ", "abc123_-XYZ4"]
        assert all(i.source_name == "jason-schreier" for i in result.items)
        assert all("Baldur" in i.title or "Studio" in i.title for i in result.items)

    def test_requests_the_per_channel_atom_feed(self, feed, monkeypatch):
        import fetchers.youtube as yt

        seen = {}
        monkeypatch.setattr(
            yt.requests, "get",
            lambda url, **k: seen.setdefault("url", url) and FakeResponse())

        from fetchers.youtube import fetch
        fetch(youtube_source())
        assert seen["url"] == (
            f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}")

    def test_zero_entries_is_an_empty_success_not_a_failure(self, feed):
        from fetchers.youtube import fetch

        feed.set_parsed(feed_result([]))
        result = fetch(youtube_source())
        assert result.success
        assert result.items == []
        assert result.error is None

    def test_http_error_is_a_failure(self, feed):
        from fetchers.youtube import fetch

        feed.set_response(FakeResponse(status_code=404))
        result = fetch(youtube_source())
        assert not result.success
        assert "404" in result.error

    def test_wrong_kind_is_a_failure(self):
        from fetchers.youtube import fetch

        result = fetch(make_source(name="x", kind="rss", url="https://e.com/rss"))
        assert not result.success
        assert "youtube" in result.error


class TestVideoIdExtraction:
    def test_watch_url_yields_id(self):
        from fetchers.youtube import extract_video_id

        assert extract_video_id(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_watch_url_with_extra_params(self):
        from fetchers.youtube import extract_video_id

        assert extract_video_id(
            "https://www.youtube.com/watch?t=90&v=abc123_-XYZ4") == "abc123_-XYZ4"

    def test_non_watch_url_yields_none(self):
        from fetchers.youtube import extract_video_id

        assert extract_video_id("https://example.com/page") is None


class TestRegistryDispatch:
    def test_youtube_kind_dispatches_through_the_registry(self, feed, monkeypatch):
        from fetchers.registry import fetch_one

        # Hermetic: patch the HTTP boundary so dispatch never touches network.
        import fetchers.youtube as yt
        monkeypatch.setattr(yt.requests, "get", lambda *a, **k: FakeResponse())

        result = fetch_one(youtube_source())
        # Reached the real (keyless, network) fetcher path: a wrong kind would
        # fail with "unknown source kind", not the youtube fetcher's error.
        assert result is not None
        assert "unknown source kind" not in (result.error or "")

    def test_unregistered_kind_isolate_and_continues(self):
        from fetchers.registry import fetch_one

        result = fetch_one(make_source(name="bad", kind="newsletter",
                                       url="https://e.com"))
        assert isinstance(result, FetchResult)
        assert not result.success
        assert "newsletter" in result.error
