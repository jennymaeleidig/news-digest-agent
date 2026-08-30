"""Reddit subreddit JSON fetcher via the community reddit-rss-api proxy.

Native ``www.reddit.com/r/<sub>.rss`` is not reliable here: it serves an
unauthenticated client once and then 403s repeated requests from the same IP
(and 403s datacenter IPs outright — the digest runs on a GitHub Actions
runner). So the Reddit source reads from the community-maintained
``reddit-rss-api`` service instead: a Deno HTTP endpoint that delegates to
Reddit's RSS on our behalf and returns structured JSON — one entry per post
with its title, canonical link, ``isoDate``, and (for text posts) the full
``message`` body.

The fetcher is a single JSON fetch plus the shared dot-path field map — the
same mechanism as ``huggingface_papers``, with the field paths carried in the
source's shared ``fetcher_config`` (item/title/link/date). The post body
(``message``) becomes the snippet, which native Reddit RSS did not provide.

Reliability note: this is a community-maintained third-party service (one
public instance, no SLA). It intermittently serves bad gateways (502/503)
and resets connections on scheduled runs — while a single smoke-test probe
passes, because the blips are transient. Fetches therefore retry up to
REDDIT_FETCH_ATTEMPTS times (fresh HTTP request per attempt, linear
backoff) on transient causes only: 5xx, 429, and network-level exceptions.
A 403/404 or invalid JSON fails immediately. An exhausted retry surfaces
as an isolated per-source ``FetchResult`` failure in the source-health
footer — it never stops the run.
"""

from __future__ import annotations

import time

import requests

from categories import Source
from config import (
    HTTP_TIMEOUT_SECONDS,
    REDDIT_FETCH_ATTEMPTS,
    REDDIT_RETRY_BACKOFF_SECONDS,
    SNIPPET_CHARS,
    USER_AGENT,
)
from fetchers.common import FetchResult, Item

# HTTP statuses worth retrying: rate limiting and gateway/server blips —
# the shapes the community proxy actually exhibits under load. Anything
# else (403 bot-block, 404 gone) is deterministic; retrying cannot change
# the answer.
_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}

# Browser-like header set, mirroring the RSS fetcher's hygiene so bot-sensitive
# hosts respond with a full body rather than a stripped one.
_REQUEST_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*;q=0.5",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
}


def _dig(data, path: str):
    """Resolve a dot-path against JSON-ish data; ``$`` (or empty) is the value
    itself. Returns None when any key along the path is missing."""
    if path in ("", "$"):
        return data
    cur = data
    for key in path.split("."):
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def _map_entries(entries, config, source_name: str) -> list[Item]:
    """Map each JSON entry to an Item using the shared config's field paths.

    title/link/date come from the configured dot-paths; the snippet comes from
    the post body (``message``, optional — text posts have it, media posts may
    not). Entries missing a required field are skipped rather than crashing the
    mapping.
    """
    items: list[Item] = []
    for entry in entries:
        title = _dig(entry, config.title)
        link = _dig(entry, config.link)
        published = _dig(entry, config.date)
        body = _dig(entry, "message")
        if not title or not link:
            continue
        items.append(Item(
            title=str(title).strip(),
            source_name=source_name,
            url=str(link).strip(),
            published=str(published) if published else "",
            content_snippet=str(body).strip()[:SNIPPET_CHARS] if body else "",
        ))
    return items


def _fetch_once(url: str) -> requests.Response:
    """One HTTP attempt; raises on network-level failure."""
    return requests.get(
        url,
        headers=_REQUEST_HEADERS,
        timeout=HTTP_TIMEOUT_SECONDS,
        allow_redirects=True,
    )


def fetch(source: Source) -> FetchResult:
    if source.kind != "reddit_rss_api":
        return FetchResult(
            source.name, False,
            error=f"not a reddit_rss_api source: kind={source.kind}",
        )

    config = source.fetcher_config
    if config is None:
        return FetchResult(
            source.name, False,
            error="reddit_rss_api source missing fetcher_config",
        )

    # Bounded retry on transient causes: the community proxy intermittently
    # serves 502/503 under load and occasionally resets connections mid-run.
    # One fresh request per attempt (fresh TCP/TLS), linear backoff between
    # attempts — the same shape as the transcript retry in prefetch.py.
    last_error: str | None = None
    resp: requests.Response | None = None
    for attempt in range(1, REDDIT_FETCH_ATTEMPTS + 1):
        try:
            resp = _fetch_once(config.url)
        except requests.RequestException as e:
            last_error = f"fetch failed: {e}"
        else:
            last_error = f"HTTP {resp.status_code}" if resp.status_code >= 400 else None
            # Non-retryable status (403 bot-block, 404 gone): the answer
            # won't change — fail now instead of burning the backoff.
            if resp.status_code < 400 or resp.status_code not in _RETRYABLE_STATUSES:
                break
        if attempt < REDDIT_FETCH_ATTEMPTS:
            time.sleep(REDDIT_RETRY_BACKOFF_SECONDS * attempt)

    if resp is None or resp.status_code >= 400:
        return FetchResult(source.name, False, error=last_error)

    try:
        payload = resp.json()
    except ValueError as e:
        return FetchResult(source.name, False, error=f"invalid JSON: {e}")

    entries = _dig(payload, config.item)
    if not isinstance(entries, list):
        return FetchResult(
            source.name, False,
            error=f"item path {config.item!r} did not resolve to a list",
        )

    items = _map_entries(entries, config, source.name)
    return FetchResult(source.name, True, items)
