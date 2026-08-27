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
public instance, no SLA). A proxy outage surfaces as an isolated per-source
``FetchResult`` failure in the source-health footer — it never stops the run.
"""

from __future__ import annotations

import requests

from categories import Source
from config import HTTP_TIMEOUT_SECONDS, SNIPPET_CHARS, USER_AGENT
from fetchers.common import FetchResult, Item

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

    try:
        resp = requests.get(
            config.url,
            headers=_REQUEST_HEADERS,
            timeout=HTTP_TIMEOUT_SECONDS,
            allow_redirects=True,
        )
    except requests.RequestException as e:
        return FetchResult(source.name, False, error=f"fetch failed: {e}")

    if resp.status_code >= 400:
        return FetchResult(source.name, False, error=f"HTTP {resp.status_code}")

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
