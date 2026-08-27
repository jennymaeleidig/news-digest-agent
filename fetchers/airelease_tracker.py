"""AI Release Tracker HTML-scraping fetcher.

A bespoke ``airelease_tracker`` source kind that scrapes the server-rendered
``/latest`` release list with ``requests`` + ``BeautifulSoup`` — **no headless
browser, no JSON API** — and maps each model-release card to an Item. Each
item's published date is parsed from the displayed date string (e.g.
``"Wed, Aug 26 2026"``) into a timezone-aware value, so an out-of-window
release then drops exactly like any other source's old item — releases are
never special-cased into the digest.

Like every fetcher it returns a ``FetchResult`` and isolates-and-continues: an
HTTP error, a transport exception, or a malformed body becomes a failure result
for this source rather than a raised exception, so one broken source never
stops the run. The request sends a browser-like User-Agent and a full header
set so bot-sensitive hosts respond with a full body.

Configuration comes through the shared fetcher-config schema (see
``fetchers/config_schema.py``): the same ``url`` + ``item``/``title``/``link``/
``date`` shape every bespoke feedless kind shares. This kind interprets those
strings as **CSS selectors** against the rendered HTML — an HTML-selector
scraping mechanism, deliberately distinct from the JSON-API field mapping of
``huggingface_papers``:

  - ``item``  — selector locating each release container (an anchor like
                ``<a href="/model/{provider}/{slug}">``).
  - ``title`` — selector, resolved *within* each container, for the release's
                title element; its text is the item title.
  - ``link``  — selector, resolved *within* each container, for the element
                carrying the article ``href``. When it selects no element, the
                container's own ``href`` is used — the common anchor-container
                layout, where the release link lives on the container itself.
  - ``date``  — selector, resolved *within* each container, for the published
                date element; its display text is parsed into a timezone-aware
                ISO-8601 timestamp.

The config schema deliberately has no ``snippet`` field, so this kind derives
each item's snippet from a kind-specific element constant — the provider span
(``.text-gray-500.truncate``) inside the container. Keeping the provider's text
as the snippet gives curation a short, on-topic descriptor per release.
"""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from categories import Source
from config import HTTP_TIMEOUT_SECONDS, SNIPPET_CHARS, USER_AGENT
from fetchers.common import FetchResult, Item

# Browser-like header set, mirroring the other fetchers' hygiene so bot-
# sensitive hosts respond with a full body rather than a stripped one.
_REQUEST_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
}

# Kind-specific element constant: the provider span inside each release
# container. The config schema has no snippet field, so the provider's text
# doubles as the item's snippet — a short, on-topic descriptor per release.
_PROVIDER_SELECTOR = "span.text-gray-500.truncate"

# Date formats the /latest page may render. The displayed date is not an ISO
# timestamp (e.g. "Wed, Aug 26 2026"), so the kind tries each format and falls
# back to the raw string if none match — the same tolerance the RSS fetcher
# exercises with an unparseable feed date.
_DATE_FORMATS = (
    "%a, %b %d %Y",   # "Wed, Aug 26 2026"
    "%a %b %d %Y",    # "Wed Aug 26 2026"
    "%b %d, %Y",      # "Aug 26, 2026"
    "%b %d %Y",       # "Aug 26 2026"
)


def _parse_published(text: str) -> str:
    """Parse a displayed date string into a timezone-aware ISO-8601 value.

    The /latest page shows dates like ``"Wed, Aug 26 2026"`` — not ISO
    timestamps — so this parses the human-readable string into
    ``YYYY-MM-DDTHH:MM:SS+00:00`` (UTC), which ``filter_recent``'s
    ``fromisoformat`` can compare against the window cutoff. An unparseable
    string is returned as-is, matching the RSS fetcher's tolerance, so an oddly
    formatted item is kept rather than silently dropped.
    """
    raw = text.strip()
    for fmt in _DATE_FORMATS:
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            continue
    return raw


def _resolve_href(container, link_selector: str) -> str | None:
    """Resolve an item container's article URL.

    ``link_selector`` is a CSS selector resolved *within* the container for the
    element carrying the ``href``. In the anchor-container layout the release
    link lives on the container itself, where a descendant selector selects
    nothing — so the container's own ``href`` is the fallback.
    """
    if link_selector:
        el = container.select_one(link_selector)
        if el is not None and el.get("href"):
            return el["href"]
    return container.get("href")


def _map_containers(containers, config, source_name: str) -> list:
    """Map each release container to an Item using the config's CSS selectors.

    Selectors are resolved *within* each container (for title/link/date) so the
    same expression can select one element per card. Containers missing a title
    or a resolvable link are skipped rather than crashing the mapping. The
    snippet comes from the kind-specific provider element constant.
    """
    items: list = []
    for container in containers:
        title_el = container.select_one(config.title) if config.title else None
        if title_el is None:
            continue
        title = title_el.get_text(strip=True)
        href = _resolve_href(container, config.link)
        if not href:
            continue

        date_el = container.select_one(config.date) if config.date else None
        date_text = date_el.get_text(strip=True) if date_el else ""
        provider_el = container.select_one(_PROVIDER_SELECTOR)
        provider = provider_el.get_text(strip=True) if provider_el else ""

        items.append(Item(
            title=title,
            source_name=source_name,
            url=urljoin(config.url, href),
            published=_parse_published(date_text),
            content_snippet=provider[:SNIPPET_CHARS],
        ))
    return items


def fetch(source: Source) -> FetchResult:
    if source.kind != "airelease_tracker":
        return FetchResult(
            source.name, False,
            error="not an airelease_tracker source: kind=" + source.kind,
        )

    config = source.fetcher_config
    if config is None:
        return FetchResult(
            source.name, False,
            error="airelease_tracker source missing fetcher_config",
        )

    try:
        resp = requests.get(
            config.url,
            headers=_REQUEST_HEADERS,
            timeout=HTTP_TIMEOUT_SECONDS,
            allow_redirects=True,
        )
    except requests.RequestException as e:
        return FetchResult(source.name, False, error="fetch failed: " + str(e))

    if resp.status_code >= 400:
        return FetchResult(source.name, False, error="HTTP " + str(resp.status_code))

    soup = BeautifulSoup(resp.text, "html.parser")
    containers = soup.select(config.item)
    items = _map_containers(containers, config, source.name)
    return FetchResult(source.name, True, items)
