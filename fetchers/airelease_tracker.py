"""AI Release Tracker fetcher — consumes the official machine-readable dataset.

The tracker publishes a JSON dataset at ``/models.json`` (linked from the site's
own "Machine-readable index" as "the dataset as JSON"): one entry per tracked
model with its release date, provider, weight-access, parameter count, context
window, and published benchmark scores. This kind reads that dataset instead of
scraping the server-rendered ``/latest`` HTML, which carries only a title, a
provider, and a date — no description.

Reading the dataset gives curation a real, self-contained snippet per release
(provider + access + parameter count + context window + headline benchmark
scores), so a release item no longer reaches the summarizer as a bare model
name that must be dropped as "too thin".

Configuration comes through the shared fetcher-config schema: the same
``url`` + ``item``/``title``/``link``/``date`` shape every bespoke feedless
kind shares. This kind interprets those strings as dot-paths into the JSON
response — the same mechanism as ``huggingface_papers``:

  - ``item``  — path to the list of models (``"models"``).
  - ``title`` — path to the model name.
  - ``link``  — path to the model slug, which this kind renders into the
                canonical article URL ``/model/{company}/{slug}`` (the
                ``company`` and ``slug`` fields are kind-specific constants).
  - ``date``  — path to the ISO release date (``releaseDate``).

The snippet is synthesized from kind-specific fields (companyName, access,
parameters, contextWindow, releaseDateLabel, benchmarks) rather than carried by
the schema, because the dataset's value is spread across several fields no one
flat path names.
"""

from __future__ import annotations

import json
import urllib.parse
from datetime import datetime, timezone

import requests

from categories import Source
from config import HTTP_TIMEOUT_SECONDS, SNIPPET_CHARS, USER_AGENT
from fetchers.common import FetchResult, Item

# Browser-like header set, mirroring the other fetchers' hygiene so bot-
# sensitive hosts respond with a full body rather than a stripped one.
_REQUEST_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*;q=0.5",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
}

# A shared, kind-specific constant: the canonical model-article URL host. The
# link field yields the model slug; the ``company`` field (also kind-specific)
# forms the rest of the path, matching the site's /model/{company}/{slug}
# scheme.
_MODEL_URL_BASE = "https:" + "//aireleasetracker.com/model/"

# How many headline benchmark scores the synthesized snippet carries. The
# dataset lists a model's strongest published numbers first, so the first few
# entries are the headline ones.
_SNIPPET_BENCHMARKS = 6


def _dig(data, path: str):
    """Resolve a dot-path against JSON data; ``$`` (or empty) is the value
    itself. Returns None when any key along the path is missing."""
    if path in ("", "$"):
        return data
    cur = data
    for key in path.split("."):
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def _parse_release_date(value) -> str:
    """Turn an ISO date like ``"2026-08-26"`` into a timezone-aware ISO-8601
    timestamp (UTC), matching what ``filter_recent`` compares. A missing or
    unparseable value is returned as-is (tolerance, not a crash)."""
    if not value:
        return ""
    text = str(value).strip()
    # The dataset's releaseDate is a bare day ("2026-08-26"); normalize to the
    # same tz-aware ISO the other fetchers emit. A full timestamp passes through.
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(text, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.isoformat()
        except ValueError:
            continue
    return text


def _build_snippet(entry: dict) -> str:
    """Synthesize a self-contained description from the dataset's per-model
    fields: provider, weight-access, parameter count, context window, release
    label, and the first few headline benchmark scores."""
    company = (entry.get("companyName") or entry.get("company") or "").strip()
    access = (entry.get("access") or "").strip()
    parameters = (entry.get("parameters") or "").strip()
    context = (entry.get("contextWindow") or "").strip()
    released = (entry.get("releaseDateLabel") or entry.get("releaseDate") or "").strip()

    parts: list[str] = []
    if company:
        parts.append(company)
    if access:
        parts.append(access)
    if parameters:
        parts.append(f"{parameters} parameters")
    if context:
        parts.append(f"{context} context window")
    head = " ".join(parts)
    if released:
        head += f"; released {released}" if head else f"Released {released}"
    if head:
        head += "."

    # Headline published benchmark scores (the dataset lists strongest first).
    benchmarks = entry.get("benchmarks")
    bench_strs: list[str] = []
    if isinstance(benchmarks, dict):
        for key, b in list(benchmarks.items())[:_SNIPPET_BENCHMARKS]:
            if not isinstance(b, dict):
                continue
            name = (b.get("name") or key).strip()
            value = b.get("value")
            unit = (b.get("unit") or "").strip()
            if value is None:
                continue
            bench_strs.append(f"{name} {value}{unit}")

    text = head
    if bench_strs:
        text += (" " if text else "") + "Benchmarks: " + ", ".join(bench_strs) + "."
    return text.strip()[:SNIPPET_CHARS]


def _map_models(entries, config, source_name: str) -> list[Item]:
    """Map each JSON model entry to an Item.

    title/link/date come from the configured dot-paths (link = slug); the full
    article URL is rendered from the kind-specific company + slug fields, and
    the snippet is synthesized from the dataset's spec/benchmark fields."""
    items: list[Item] = []
    for entry in entries:
        title = _dig(entry, config.title)
        slug = _dig(entry, config.link)
        published = _dig(entry, config.date)
        if not title or not slug:
            continue
        company = str(entry.get("company") or "").strip()
        url = _MODEL_URL_BASE + urllib.parse.quote(company) + "/" + urllib.parse.quote(str(slug).strip())
        items.append(Item(
            title=str(title).strip(),
            source_name=source_name,
            url=url,
            published=_parse_release_date(published),
            content_snippet=_build_snippet(entry),
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

    items = _map_models(entries, config, source.name)
    return FetchResult(source.name, True, items)
