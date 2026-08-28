"""Keyless per-channel YouTube listing fetcher.

Lists a channel's recent uploads through its per-channel Atom feed
(``https://www.youtube.com/feeds/videos.xml?channel_id=<CHANNEL_ID>``) — no
API key, no OAuth, no scraping. This mirrors the RSS fetcher: one source in,
one FetchResult out; errors are returned, not raised, so a single broken
source never stops the run.

The source's ``url`` carries the channel id (the 24-char ``UC…`` id, not the
handle). A defensive zero-entry guard returns an empty success rather than a
failure; per the spec, no ``/videos``-page scrape fallback is added — the
correct channel ids return entries reliably.

Each entry maps to an Item with the watch URL and the video id extracted from
that watch URL (kept on ``Item.video_id`` for the transcript deep-read path).
"""

from __future__ import annotations

from urllib.parse import urlparse, parse_qs

import feedparser
import requests

from categories import Source
from config import HTTP_TIMEOUT_SECONDS, SNIPPET_CHARS
from fetchers.common import FetchResult, Item, strip_html
from fetchers.rss import (
    _REQUEST_HEADERS,
    _entry_content,
    _parse_published,
)

FEED_URL_TEMPLATE = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"


def _feed_url(channel_id: str) -> str:
    """Build the per-channel Atom feed URL from a channel id (or a full feed
    URL, passed through unchanged)."""
    if channel_id.startswith("http"):
        return channel_id
    return FEED_URL_TEMPLATE.format(channel_id=channel_id)


def extract_video_id(url: str) -> str | None:
    """Extract the video id from a YouTube watch URL; None otherwise."""
    parsed = urlparse(url or "")
    if parsed.hostname not in ("www.youtube.com", "youtube.com", "m.youtube.com"):
        return None
    if parsed.path != "/watch":
        return None
    return (parse_qs(parsed.query).get("v") or [None])[0]


def fetch(source: Source) -> FetchResult:
    if source.kind != "youtube":
        return FetchResult(source.name, False,
                           error=f"not a youtube source: kind={source.kind}")

    try:
        resp = requests.get(
            _feed_url(source.url),
            headers=_REQUEST_HEADERS,
            timeout=HTTP_TIMEOUT_SECONDS,
            allow_redirects=True,
        )
    except requests.RequestException as e:
        return FetchResult(source.name, False, error=f"fetch failed: {e}")

    if resp.status_code >= 400:
        return FetchResult(source.name, False, error=f"HTTP {resp.status_code}")

    parsed = feedparser.parse(resp.content)

    if parsed.bozo and not parsed.entries:
        return FetchResult(source.name, False,
                           error=f"feed parse error: {parsed.bozo_exception}")

    # Defensive zero-entry guard: an empty feed is an empty success, not a
    # failure — and no /videos-page scrape fallback runs (spec: stay keyless).
    items: list[Item] = []
    for entry in parsed.entries:
        title = (entry.get("title") or "").strip()
        url = (entry.get("link") or "").strip()
        if not title or not url:
            continue
        snippet = strip_html(_entry_content(entry))[:SNIPPET_CHARS]
        items.append(Item(
            title=title,
            source_name=source.name,
            url=url,
            published=_parse_published(entry),
            content_snippet=snippet,
            video_id=extract_video_id(url),
        ))

    return FetchResult(source.name, True, items)
