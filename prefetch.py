"""Pre-fetch enrichment stage — the deterministic read boundary under curation.

The curation model is a pure summarizer that never touches the network.
Everything it needs must already be in the prompt text it receives. This stage
runs *before* the model: it deterministically fetches and extracts the full plain
text of the
articles the day's items warrant deep-reading, gated by an allowlist built from
each source's homepage (static) plus the items' linked URLs (runtime), and it
re-checks redirects so a fetch that ends at a non-allowlisted host is rejected.

The allowlist + fetch / extract / redirect-recheck logic is lifted unchanged
from the original in-loop `fetch_full_article` tool in curator.py; it has moved
out of curation and into this Python pre-fetch stage so that no in-process or
in-model fetch tool remains. The capped budget survives as the pre-fetch
limits: at most `TOOL_CALL_CAP` fetches per pre-fetch call, `MAX_RETURN_CHARS`
characters returned per fetch, and a `MAX_BYTES` response cap. Exceeding any
cap fails that one item's enrichment without crashing the run.

## Deep-read policy (thin-snippet)

Which items get deep-read:

  - External-linked items **always** (a feed item with a `linked_url` points
    at an article on another site; that article, not the feed teaser, is what
    has the substance) — the `linked_url` is fetched.
  - Any item whose snippet is below `DEEP_READ_SNIPPET_CHARS` — its own `url`
    is fetched.

Long-but-vague snippets at or above the threshold are an accepted loss: they
are not deep-read and are judged on the snippet alone.

## Isolate-and-continue

A fetch that fails, exceeds a cap, or is disallowed by the allowlist leaves
that item's enrichment empty — the item remains judgable on its snippet alone.
One bad item never crashes the run.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from typing import Iterable
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from categories import Source
from config import (
    DEEP_READ_SNIPPET_CHARS,
    HTTP_TIMEOUT_SECONDS,
    MAX_BYTES,
    MAX_REDIRECTS,
    MAX_RETURN_CHARS,
    TOOL_CALL_CAP,
    USER_AGENT,
)
from fetchers.common import Item


@dataclass
class PrefetchResult:
    """Outcome of one pre-fetch stage run.

    `enrichments` maps an article URL to its extracted plain text. `errors`
    maps URLs whose fetch failed / was disallowed / hit a cap to a short
    human-readable reason (consumed by prompt assembly / diagnostics, never
    used to crash the run). `fetches_used` is the number of fetches actually
    performed (bounded by TOOL_CALL_CAP).
    """
    enrichments: dict[str, str] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    fetches_used: int = 0


# The static allowlist is built from `homepage` only (not `url`): the `url`
# field is the feed/API endpoint, which there is no reason to fetch as an
# article the way a homepage host is. Homepage hosts are what appear in
# Item.url values.
def build_static_allowlist(sources: Iterable[Source]) -> set[str]:
    """Extract exact hostnames from each source's `homepage`.

    Exact-host match (no subdomain fuzzing): keeps "evil.openai.com" out and
    avoids the substack.com gotcha where any *.substack.com would be reachable.
    """
    hosts: set[str] = set()
    for s in sources:
        if not s.homepage:
            continue
        host = urlparse(s.homepage).hostname
        if host:
            hosts.add(host.lower())
    return hosts


def build_runtime_allowlist(items: Iterable[Item]) -> set[str]:
    """Extract exact hostnames from each item's linked URL.

    Aggregator feeds carry a `linked_url` for items whose link resolves to an
    external article. These hosts are only allowed for this run, so the
    external article is fetchable without opening up the whole internet.
    """
    hosts: set[str] = set()
    for it in items:
        if it.linked_url:
            host = urlparse(it.linked_url).hostname
            if host:
                hosts.add(host.lower())
    return hosts


def select_deep_read_urls(items: list[Item], allowlist: set[str]) -> list[str]:
    """Return the ordered list of URLs to deep-read this run.

    Thin-snippet policy:
      - External-linked items always: their `linked_url` is the target (the
        linked host was added to the allowlist by the caller).
      - Any item whose snippet is below DEEP_READ_SNIPPET_CHARS: its own `url`.

    Two changes from the original relevance-ordered pass keep the thin items
    from starving:
      - URLs whose host is not on the allowlist are skipped here rather than
        after the fact, so a disallowed URL can no longer burn a fetch slot
        that a reachable, allowlisted item needed.
      - Targets are ordered thinnest-snippet-first (tiebroken by the incoming
        order), so an item the summarizer is about to drop for a thin snippet
        gets enriched before an already-long abstract.
    """
    targets: list[tuple[int, int, str]] = []  # (snippet_len, order, url)
    seen: set[str] = set()
    for order, it in enumerate(items):
        if it.linked_url and it.linked_url not in seen:
            target = it.linked_url
        elif len(it.content_snippet or "") < DEEP_READ_SNIPPET_CHARS and it.url not in seen:
            target = it.url
        else:
            continue
        host = (urlparse(target).hostname or "").lower()
        if not host or host not in allowlist:
            continue
        seen.add(target)
        # External-linked items are always deep-read; rank them by the same
        # thin-snippet urgency so ordering stays stable across both kinds.
        snippet_len = len(it.content_snippet or "")
        targets.append((snippet_len, order, target))
    targets.sort()
    return [u for _, _, u in targets]


def fetch_full_article(url: str, allowlist: set[str]) -> str:
    """Fetch and extract the plain text of a single article URL.

    Applies the allowlist gate (scheme, host) and re-checks the redirect target
    host before returning any text, so a fetch ending at a non-allowlisted host
    is rejected. Enforces the per-fetch caps: `MAX_BYTES` response bound before
    decoding, then `MAX_RETURN_CHARS` on the returned text. Returns the plain
    text on success, or an "Error: ..." string (matching the original tool's
    contract) when the URL is disallowed or the fetch fails — never raises.
    """
    if not url or not isinstance(url, str):
        return "Error: missing or invalid url argument"

    try:
        parsed = urlparse(url)
    except Exception as e:
        return f"Error: could not parse URL: {e}"

    if parsed.scheme not in ("http", "https"):
        return f"Error: scheme not allowed: {parsed.scheme!r}"

    host = (parsed.hostname or "").lower()
    if not host:
        return "Error: missing hostname in URL"
    if host not in allowlist:
        return f"Error: hostname not in allowlist: {host}"

    session = requests.Session()
    session.max_redirects = MAX_REDIRECTS

    try:
        with session.get(
            url,
            timeout=HTTP_TIMEOUT_SECONDS,
            stream=True,
            allow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        ) as resp:
            final_host = (urlparse(resp.url).hostname or "").lower()
            if final_host not in allowlist:
                return (
                    f"Error: redirect ended at non-allowlisted host: "
                    f"{final_host}"
                )

            if resp.status_code >= 400:
                return f"Error: HTTP {resp.status_code}"

            content_type = (resp.headers.get("content-type") or "").lower()
            if not (
                content_type.startswith("text/html")
                or content_type.startswith("text/plain")
            ):
                return f"Error: unsupported content type: {content_type or '(none)'}"

            buf = bytearray()
            for chunk in resp.iter_content(chunk_size=8192):
                if not chunk:
                    continue
                buf.extend(chunk)
                if len(buf) > MAX_BYTES:
                    return f"Error: response exceeded {MAX_BYTES} bytes"

            encoding = resp.encoding or "utf-8"
            text = bytes(buf).decode(encoding, errors="replace")

            if content_type.startswith("text/html"):
                soup = BeautifulSoup(text, "html.parser")
                for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
                    tag.decompose()
                text = soup.get_text(separator="\n")
                text = re.sub(r"\n[ \t]*\n+", "\n\n", text).strip()

            if len(text) > MAX_RETURN_CHARS:
                text = text[:MAX_RETURN_CHARS] + (
                    f"\n\n[truncated at {MAX_RETURN_CHARS} chars]"
                )

            return text or "Error: empty response body"
    except requests.exceptions.TooManyRedirects:
        return f"Error: too many redirects (>{MAX_REDIRECTS})"
    except requests.exceptions.Timeout:
        return f"Error: request timed out after {HTTP_TIMEOUT_SECONDS}s"
    except requests.RequestException as e:
        return f"Error: fetch failed: {e}"


def prefetch(items: list[Item], sources: Iterable[Source]) -> PrefetchResult:
    """Run the pre-fetch stage: fetch full text for the deep-read items.

    Builds the static allowlist from source homepages plus the runtime
    allowlist from item linked URLs, selects the deep-read targets via the
    thin-snippet policy, and fetches each within the per-call fetch budget.
    Exceeding
    the fetch cap simply stops deep-reading; a per-fetch failure or disallowed
    host leaves that item's enrichment empty (isolate-and-continue). Returns a
    PrefetchResult mapping URLs to extracted text alongside per-URL errors.
    """
    static_hosts = build_static_allowlist(sources)
    runtime_hosts = build_runtime_allowlist(items)
    allowlist = static_hosts | runtime_hosts

    enrichments: dict[str, str] = {}
    errors: dict[str, str] = {}
    fetches_used = 0

    deep_read_urls = list(select_deep_read_urls(items, allowlist))
    total = len(deep_read_urls)
    print(
        f"[prefetch] {total} deep-read target(s) (cap {TOOL_CALL_CAP})",
        file=sys.stderr, flush=True,
    )
    for url in deep_read_urls:
        if fetches_used >= TOOL_CALL_CAP:
            errors[url] = f"Error: fetch cap reached ({TOOL_CALL_CAP} per run)"
            continue
        fetches_used += 1
        print(
            f"[prefetch]   {fetches_used}/{total} {url[:90]}",
            file=sys.stderr, flush=True,
        )
        text = fetch_full_article(url, allowlist)
        if text.startswith("Error:"):
            errors[url] = text
        else:
            enrichments[url] = text

    return PrefetchResult(
        enrichments=enrichments,
        errors=errors,
        fetches_used=fetches_used,
    )
