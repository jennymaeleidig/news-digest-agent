"""Hugging Face Daily Papers JSON fetcher.

A bespoke ``huggingface_papers`` source kind that hits the Daily Papers JSON
endpoint (``https://huggingface.co/api/daily_papers``) and maps each entry to
an Item. No RSS, no DOM scraping, no headless browser — just one JSON fetch
and a field map.

Like every fetcher it returns a ``FetchResult`` and isolates-and-continues:
an HTTP error, a transport exception, or a malformed body becomes a failure
result for this source rather than a raised exception, so one broken source
never stops the run. The request sends a browser-like User-Agent and a full
header set so bot-sensitive hosts respond with a full body.

The item's published date is taken from ``paper.submittedOnDailyAt`` — the
Daily-Papers *feature day* — NOT ``paper.publishedAt`` (the arXiv date), so
the time-window filter reflects the day the paper was surfaced
on Daily Papers rather than when it first appeared on arXiv.

Configuration comes through the shared fetcher-config schema (see
``fetchers/config_schema.py``): the same ``url`` + ``item``/``title``/
``link``/``date`` shape every bespoke feedless kind shares. This kind
interprets those strings as dot-paths into the JSON response (``$`` = the
document root). ``link`` names the paper id (``paper.id``), which this kind
renders into the full article URL; ``date`` names the Daily-Papers feature day
(``paper.submittedOnDailyAt``). The field-mapping mechanism stays specific to
this JSON-API kind — a HTML-scraping kind reads the same strings as selectors
against a document instead.
"""

from __future__ import annotations

import json

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

# A shared, kind-specific constant: the Daily Papers article host, used to
# render a paper id into its canonical article URL.
_PAPERS_URL = "https:" + "//huggingface.co/papers/"


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

    This is the JSON-API field-mapping mechanism (specific to this kind): it
    resolves ``title``/``link``/``date`` as dot-paths into each entry and
    renders the ``link`` value (a paper id) into the full article URL. Entries
    missing a required field are skipped rather than crashing the mapping.
    """
    items: list[Item] = []
    for entry in entries:
        title = _dig(entry, config.title)
        paper_id = _dig(entry, config.link)
        published = _dig(entry, config.date)
        summary = _dig(entry, "summary")
        if not title or not paper_id:
            continue
        items.append(Item(
            title=str(title).strip(),
            source_name=source_name,
            url=f"{_PAPERS_URL}{paper_id}",
            published=str(published) if published else "",
            content_snippet=str(summary).strip()[:SNIPPET_CHARS] if summary else "",
        ))
    return items


def fetch(source: Source) -> FetchResult:
    if source.kind != "huggingface_papers":
        return FetchResult(
            source.name, False,
            error=f"not a huggingface_papers source: kind={source.kind}",
        )

    config = source.fetcher_config
    if config is None:
        return FetchResult(
            source.name, False,
            error="huggingface_papers source missing fetcher_config",
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
