"""Offline tests for the huggingface_papers JSON fetcher (ticket 03).

The seam under test is the fetcher's public interface — ``fetch(source)``
against a source of kind ``huggingface_papers`` carrying the shared
fetcher-config. These tests feed the fetcher a captured JSON response (via a
stubbed ``requests.get``) and assert the mapped ``Item``s: title, article URL,
the abstract as snippet, and the published date taken from the Daily-Papers
feature day (``paper.submittedOnDailyAt``) rather than the arXiv date. They
also pin the isolate-and-continue behavior (an HTTP error is returned as a
failure result, never raised) and the registry registration (the kind
dispatches through the existing kind-to-fetcher seam with no pipeline edit).

Per the spec's testing decision, the tests assert external, deterministic
behavior — the resulting items — and never couple to parsing mechanics such as
which internal helper resolves a JSON path.
"""

from __future__ import annotations

from categories import Source
from fetchers.config_schema import FetcherConfig
from fetchers.huggingface_papers import fetch

ENDPOINT = "https:" + "//huggingface.co/api/daily_papers"

# A captured Daily Papers response (shape: list of {title, summary,
# paper{id, submittedOnDailyAt, publishedAt, ...}}). publishedAt is the arXiv
# date and must NOT drive the item's published date.
CAPTURED = [
    {
        "title": "LoRA: Low-Rank Adaptation of Large Language Models",
        "summary": "We propose Low-Rank Adaptation, which freezes pretrained weights.",
        "paper": {
            "id": "2106.09685",
            "submittedOnDailyAt": "2026-08-25T13:00:00.000Z",
            "publishedAt": "2021-06-17T14:59:12.000Z",
        },
    },
    {
        "title": "Chain-of-Thought Prompting Elicits Reasoning",
        "summary": "We examine how chain-of-thought prompting enables reasoning.",
        "paper": {
            "id": "2201.11903",
            "submittedOnDailyAt": "2026-08-26T09:00:00.000Z",
            "publishedAt": "2022-01-28T18:59:16.000Z",
        },
    },
]


def _source():
    """A huggingface_papers source carrying the shared fetcher-config. The
    config's item/title/link/date field paths are the contract the kind reads;
    link names the paper id that this kind renders into a full article URL and
    date names the Daily-Papers feature day."""
    return Source(
        name="Hugging Face Papers",
        tier=3,
        kind="huggingface_papers",
        url=ENDPOINT,
        homepage="https://huggingface.co/papers",
        fetcher_config=FetcherConfig(
            url=ENDPOINT,
            item="$",                       # the response array itself
            title="title",
            link="paper.id",
            date="paper.submittedOnDailyAt",
        ),
    )


class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


# --- registry seam (AC 1): registered, no pipeline edit --------------------
def test_kind_registered_on_registry():
    """The kind is registered on the kind-to-fetcher registry — a
    registration, not a pipeline edit."""
    from fetchers.registry import FETCHERS
    assert "huggingface_papers" in FETCHERS
    assert FETCHERS["huggingface_papers"] is fetch


# --- field mapping (AC 3): title, url, snippet, feature-day date -----------
def test_maps_each_entry_to_an_item(monkeypatch):
    """Feeding the fetcher a captured JSON response yields one Item per entry
    with the right title, article URL, abstract-as-snippet, and provenance."""
    monkeypatch.setattr(
        "fetchers.huggingface_papers.requests.get",
        lambda *a, **k: _FakeResponse(200, CAPTURED),
    )
    result = fetch(_source())
    assert result.success is True
    assert result.error is None
    assert len(result.items) == 2

    first, second = result.items
    # title
    assert first.title == "LoRA: Low-Rank Adaptation of Large Language Models"
    assert second.title == "Chain-of-Thought Prompting Elicits Reasoning"
    # article URL rendered from the paper id
    assert first.url == "https:" + "//huggingface.co/papers/2106.09685"
    assert second.url == "https:" + "//huggingface.co/papers/2201.11903"
    # abstract as snippet
    assert first.content_snippet.startswith("We propose Low-Rank Adaptation")
    # provenance
    assert first.source_name == "Hugging Face Papers"


def test_published_uses_feature_day_not_arxiv_date(monkeypatch):
    """The item's published date is the Daily-Papers feature day
    (submittedOnDailyAt), NOT the arXiv date (publishedAt) — so window
    filtering and seen-dedup reflect the feature day."""
    monkeypatch.setattr(
        "fetchers.huggingface_papers.requests.get",
        lambda *a, **k: _FakeResponse(200, CAPTURED),
    )
    result = fetch(_source())
    first = result.items[0]
    assert first.published == "2026-08-25T13:00:00.000Z"   # submittedOnDailyAt
    assert "2021-06-17" not in first.published             # arXiv date excluded


# --- isolate-and-continue (AC 4): HTTP errors returned, not raised ---------
def test_http_error_is_returned_not_raised(monkeypatch):
    """An HTTP error from the endpoint becomes a failure FetchResult rather
    than raising, so the run isolates-and-continues past this source."""
    monkeypatch.setattr(
        "fetchers.huggingface_papers.requests.get",
        lambda *a, **k: _FakeResponse(503, None),
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
        return _FakeResponse(200, CAPTURED)

    monkeypatch.setattr("fetchers.huggingface_papers.requests.get", fake_get)
    fetch(_source())

    assert captured["url"] == ENDPOINT
    assert "User-Agent" in captured["headers"]
    assert captured["headers"]["User-Agent"].startswith("Mozilla/5.0")
    assert captured["headers"].get("Accept")
    assert captured["headers"].get("Accept-Language")
    assert captured["headers"].get("Accept-Encoding")
