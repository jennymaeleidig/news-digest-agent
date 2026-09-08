"""Generic RSS fetcher.

One source in, one FetchResult out. Errors are returned, not raised, so
main.py can keep going when a single feed breaks.

Requests are sent with feedparser's bytes via `requests` rather than
letting feedparser fetch the URL itself, so we can attach the full set of
headers a normal HTTP client would send (Accept, Accept-Language,
Accept-Encoding). Some CDN-fronted feeds (Substack/Cloudflare) 403 on
header sets that look stripped-down, even when the User-Agent is fine.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import feedparser
import requests

from categories import Source
from config import (
    HTTP_TIMEOUT_SECONDS,
    RSS_FETCH_ATTEMPTS,
    RSS_RETRY_BACKOFF_SECONDS,
    SNIPPET_CHARS,
    USER_AGENT,
)
from fetchers.common import FetchResult, Item, strip_html

# Advertise only encodings `requests` can decode without extra deps
# (gzip, deflate). Brotli would need the `brotli` package.
_REQUEST_HEADERS = {
    "Accept": (
        "application/rss+xml, application/atom+xml, "
        "application/xml;q=0.9, text/xml;q=0.8, */*;q=0.5"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
}


def _https_normalize(url: str) -> str:
    """Upgrade an ``http://`` permalink to ``https://``.

    Some feeds (e.g. democracynow) publish ``http://`` permalinks even though
    the https origin serves the same page. Normalizing at fetch time keeps a
    source's item URLs on one scheme so cross-run comparisons (run-log
    debugging, per-source health) match instead of carrying both spellings.
    """
    if url.startswith("http://"):
        return "https://" + url[len("http://"):]
    return url


def _parse_published(entry) -> str:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        return datetime(*parsed[:6], tzinfo=timezone.utc).isoformat()
    return entry.get("published") or entry.get("updated") or ""


def _entry_content(entry) -> str:
    if "content" in entry and entry.content:
        return entry.content[0].get("value", "")
    return entry.get("summary") or entry.get("description") or ""


# Bounded retry on transient causes — the same policy as the reddit
# fetcher (fetchers/reddit_rss_api.py) and configured alongside it in
# config.py. Some hosts rate-limit by client IP pool, so a 429 from one
# runner can pass from the next request moments later; retrying only
# 429/5xx-blips/network errors (never 403/404 — the answer won't change)
# recovers those without hammering a genuinely-down host.
_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


def _fetch_response(url: str, headers: dict) -> requests.Response:
    """GET ``url`` with bounded retry on transient causes.

    Raises the last RequestException, or a requests.HTTPError carrying the
    final status, only once the retry budget is exhausted. A deterministic
    status (403/404 and other non-retryable 4xx) raises immediately.
    """
    last_exc: Exception | None = None
    for attempt in range(1, RSS_FETCH_ATTEMPTS + 1):
        try:
            resp = requests.get(
                url,
                headers=headers,
                timeout=HTTP_TIMEOUT_SECONDS,
                allow_redirects=True,
            )
        except requests.RequestException as e:
            last_exc = e
        else:
            if resp.status_code < 400:
                return resp
            if resp.status_code not in _RETRYABLE_STATUSES:
                resp.raise_for_status()  # deterministic: fail now
            last_exc = requests.HTTPError(f"HTTP {resp.status_code}", response=resp)
        if attempt < RSS_FETCH_ATTEMPTS:
            time.sleep(RSS_RETRY_BACKOFF_SECONDS * attempt)
    raise last_exc  # type: ignore[misc]


def fetch(source: Source) -> FetchResult:
    if source.kind != "rss":
        return FetchResult(source.name, False, error=f"not an RSS source: kind={source.kind}")

    # Per-source User-Agent override (e.g. PBS 202s-and-empties the shared
    # browser-impersonation string); the shared config UA otherwise.
    headers = {"User-Agent": source.user_agent or USER_AGENT, **_REQUEST_HEADERS}
    try:
        resp = _fetch_response(source.url, headers)
    except requests.HTTPError as e:
        # Exhausted retry budget or a deterministic status; keep the terse
        # "HTTP <code>" shape the smoke tests and source-health records show.
        status = e.response.status_code if e.response is not None else None
        return FetchResult(
            source.name, False,
            error=f"HTTP {status}" if status else f"fetch failed: {e}",
        )
    except requests.RequestException as e:
        return FetchResult(source.name, False, error=f"fetch failed: {e}")

    parsed = feedparser.parse(resp.content)

    if parsed.bozo and not parsed.entries:
        return FetchResult(source.name, False, error=f"feed parse error: {parsed.bozo_exception}")

    # A well-formed channel that legitimately carries zero entries is a
    # success-with-note, not a failure — and crucially, not the 200-but-empty
    # bot-block signature the smoke tests exist to catch. arXiv is the
    # motivating case: it declares <skipDays> (Sat/Sun and occasional
    # holidays) and serves a valid, empty channel on those days (feedparser
    # flattens <skipDays> to a 'skipdays'/'day' key pair on the channel).
    # Anything else with zero entries keeps the plain zero-item result the
    # smoke tests treat as a suspected block.
    note = None
    if not parsed.entries and ("skipdays" in parsed.feed or "day" in parsed.feed):
        rebuilt = parsed.feed.get("lastbuilddate") or parsed.feed.get("updated") or ""
        # feedparser flattens the repeated <skipDays><day> elements and keeps
        # only the last one on `feed.day` — pull the full list from the raw
        # XML so the note can say "Saturday, Sunday" instead of just "Sunday".
        skip_days = re.findall(
            r"<day>\s*([^<]+?)\s*</day>",
            resp.content.decode(resp.encoding or "utf-8", errors="replace"),
        )
        days = f" (skipDays declares: {', '.join(skip_days)})" if skip_days else ""
        note = (
            "feed channel is valid but contains no entries — source declares "
            f"skip days{days}; lastBuildDate {rebuilt or 'unknown'}"
        )

    # The static allowlist is built from source homepages; an aggregator feed
    # (e.g. radarai.top) links out to external articles, which would otherwise
    # be unfetchable. When an item's link lands on a different host than the
    # source's homepage, mark it as `linked_url` — "the item points at an
    # external article" — so the pre-fetch stage
    # allowlist-gates and deep-reads the actual article instead of the teaser.
    homepage_host = None
    if source.homepage:
        homepage_host = (urlparse(source.homepage).hostname or "").lower()

    items: list[Item] = []
    for entry in parsed.entries:
        title = (entry.get("title") or "").strip()
        url = _https_normalize((entry.get("link") or "").strip())
        if not title or not url:
            continue
        linked_url = None
        if homepage_host:
            host = (urlparse(url).hostname or "").lower()
            if host and host != homepage_host:
                linked_url = url
        snippet = strip_html(_entry_content(entry))[:SNIPPET_CHARS]
        items.append(Item(
            title=title,
            source_name=source.name,
            url=url,
            published=_parse_published(entry),
            content_snippet=snippet,
            linked_url=linked_url,
        ))

    return FetchResult(source.name, True, items, note=note)