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
  - Items carrying a `video_id` (YouTube watch items from the `kind: youtube`
    fetcher) **always**, via the transcript path below — a video judged
    without its transcript is judged on the title alone, which is no judgment
    at all. This path is gated by the video id itself, not the article
    allowlist: a watch URL is never fetched as HTML, so no youtube.com
    allowlist entry is needed.

Long-but-vague snippets at or above the threshold are an accepted loss: they
are not deep-read and are judged on the snippet alone.

## Transcript deep-read (videos)

A stage-1-selected video's transcript is fetched keylessly through
`youtube-transcript-api` and reduced **deterministically** to a compact,
bounded excerpt block — evenly-spaced windows of the transcript text capped
at `TRANSCRIPT_MAX_CHARS`, with no model pass — so stage-2 remains the only
summarizer and cost doesn't grow per video.

Datacenter IPs (GitHub Actions runners) are blocked from the transcript
endpoint (`RequestBlocked`); when `YT_TRANSCRIPT_PROXY_URL` is set (an
outbound HTTP proxy, e.g. a rotating residential one), transcript requests
route through it — and *only* transcript requests: article deep-reads stay
direct so proxy bandwidth stays minimal. Failures are isolated — the item
stays judgable on its snippet alone.

The caption origin
(`is_generated`: auto-generated vs manual) is surfaced in the block's header
line. The block attaches to the item's enrichment exactly like an HTML
enrichment (keyed by the item's URL). Each transcript fetch counts against
the shared `TOOL_CALL_CAP`; video items never take the HTML deep-read path,
so no allowlist entry is needed for youtube.com and a watch URL can never
burn an article fetch.

## Isolate-and-continue

A fetch that fails, exceeds a cap, or is disallowed by the allowlist leaves
that item's enrichment empty — the item remains judgable on its snippet alone.
One bad item never crashes the run. Transcript failures (captions disabled,
no matching transcript, video unplayable, request blocked by YouTube — the
`CouldNotRetrieveTranscript` family) map the same way: a per-item error
string in `PrefetchResult.errors`, and the run continues.
"""

from __future__ import annotations

import os
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
    TRANSCRIPT_MAX_CHARS,
    USER_AGENT,
)
from fetchers.common import Item
from youtube_transcript_api.proxies import GenericProxyConfig
from youtube_transcript_api import (
    CouldNotRetrieveTranscript,
    NoTranscriptFound,
    RequestBlocked,
    TranscriptsDisabled,
    VideoUnplayable,
    YouTubeTranscriptApi,
)


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


# ---------------------------------------------------------------------------
# Transcript deep-read (videos)
# ---------------------------------------------------------------------------

# Marker between excerpt windows in a reduced transcript block: unambiguous,
# compact, and stable, so a bounded block's pieces stay evenly spaced and the
# cap arithmetic in reduce_transcript stays exact.
TRANSCRIPT_SEPARATOR = "\n[…]\n"

# Language preference for transcript lookup, most-preferred first. Manual
# captions are preferred over auto-generated within each code
# (TranscriptList.find_transcript's documented order); if none of these match,
# the first transcript YouTube lists is used so a non-English channel still
# gets substance rather than a per-item error.
TRANSCRIPT_LANGUAGES = ("en", "en-US", "en-GB")


def reduce_transcript(text: str, cap: int = TRANSCRIPT_MAX_CHARS) -> str:
    """Reduce a transcript to at most `cap` chars, deterministically.

    Text within the cap passes through unchanged. Longer text is split into
    the fewest evenly-spaced contiguous windows that fit the cap (head, mid,
    …, tail — so the video's beginning and end are always represented) and
    joined with the `[…]` separator. Pure arithmetic, no randomness: the
    same transcript always reduces to the same block.
    """
    if len(text) <= cap:
        return text
    n = -(-len(text) // cap)                       # window count (ceil div)
    sep = len(TRANSCRIPT_SEPARATOR)
    window = (cap - (n - 1) * sep) // n
    if window <= 0:                                # pathological tiny cap
        return text[:cap]
    # Evenly spaced window starts spanning the whole text: the first window
    # opens at the head, the last closes at the tail.
    span = len(text) - window
    starts = [round(i * span / (n - 1)) for i in range(n)]
    windows = [text[s:s + window] for s in starts]
    return TRANSCRIPT_SEPARATOR.join(windows)


def _transcript_proxy_url() -> str | None:
    """Return the optional outbound proxy URL for transcript fetches.

    Read from `YT_TRANSCRIPT_PROXY_URL` at call time (like OPENROUTER_API_KEY
    and the other deployment secrets — this module imports no env at import
    time). Example value for a DataImpulse rotating residential proxy:
    `http://<login>__cr.us:<password>@gw.dataimpulse.com:823`. Unset →
    transcript requests go direct, the pre-proxy behavior.
    """
    return os.environ.get("YT_TRANSCRIPT_PROXY_URL") or None


def fetch_transcript_excerpt(video_id: str) -> tuple[str, str | None]:
    """Fetch one video's transcript and reduce it to a bounded excerpt block.

    Returns ``(block, None)`` on success — the block opens with a header
    surfacing the caption origin (auto-generated vs manual), then the capped
    excerpt — or ``("", error)`` on failure. Never raises: every
    `CouldNotRetrieveTranscript` (the base of TranscriptsDisabled /
    NoTranscriptFound / VideoUnplayable / RequestBlocked) and any unexpected
    exception maps to a per-item error string, so the item stays judgable on
    its snippet alone and the run continues.
    """
    # The proxy applies here — and only here: this function is the whole
    # transcript seam. Article deep-reads (fetch_full_article) never see it,
    # so proxy bandwidth cost stays a few hundred KB per video at most.
    # A fresh YouTubeTranscriptApi per call also means a fresh requests
    # Session per video, which is what makes a rotating proxy actually
    # rotate (a shared session would pin one proxy IP).
    try:
        proxy_url = _transcript_proxy_url()
        api = (
            YouTubeTranscriptApi(
                proxy_config=GenericProxyConfig(
                    http_url=proxy_url, https_url=proxy_url)
            )
            if proxy_url else YouTubeTranscriptApi()
        )
        transcript_list = api.list(video_id)
        try:
            transcript = transcript_list.find_transcript(TRANSCRIPT_LANGUAGES)
        except CouldNotRetrieveTranscript:
            # No preferred language: fall back to whatever YouTube lists, so
            # a non-English channel is judged on its actual transcript.
            transcript = next(iter(transcript_list), None)
        if transcript is None:
            return "", "Error: transcript unavailable: no transcripts listed"
        fetched = transcript.fetch()
    except CouldNotRetrieveTranscript as e:
        return "", f"Error: transcript unavailable: {type(e).__name__}"
    except Exception as e:                         # noqa: BLE001 — isolate-and-continue
        return "", f"Error: transcript fetch failed: {type(e).__name__}: {e}"

    text = re.sub(r"\s+", " ", " ".join(s.text for s in fetched)).strip()
    if not text:
        return "", "Error: transcript unavailable: empty transcript"
    origin = "auto-generated" if fetched.is_generated else "manual"
    header = f"[Video transcript — {origin} captions]"
    # The cap bounds the whole block (header included), not just the excerpt.
    excerpt = reduce_transcript(
        text, TRANSCRIPT_MAX_CHARS - len(header) - 1)
    return f"{header}\n{excerpt}", None


def prefetch(items: list[Item], sources: Iterable[Source]) -> PrefetchResult:
    """Run the pre-fetch stage: fetch full text for the deep-read items.

    Builds the static allowlist from source homepages plus the runtime
    allowlist from item linked URLs, selects the deep-read targets via the
    thin-snippet policy, and fetches each within the per-call fetch budget.
    Items carrying a `video_id` take the transcript path instead (never the
    HTML path, so no youtube.com allowlist entry is needed); each transcript
    fetch counts against the same shared TOOL_CALL_CAP as article fetches.
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

    # Transcript targets: every selected video item, in incoming order,
    # deduped by video id (one shared video can't fetch twice). Videos run
    # before articles: a video without its transcript has no substance at
    # all, whereas an article item always keeps its snippet to be judged on.
    transcript_targets: list[tuple[str, str]] = []   # (item url, video id)
    seen_video_ids: set[str] = set()
    for it in items:
        if it.video_id and it.video_id not in seen_video_ids:
            seen_video_ids.add(it.video_id)
            transcript_targets.append((it.url, it.video_id))

    # Article targets: the thin-snippet policy over the non-video items only,
    # so a watch URL can never consume an article fetch slot.
    text_items = [it for it in items if not it.video_id]
    deep_read_urls = list(select_deep_read_urls(text_items, allowlist))
    total = len(transcript_targets) + len(deep_read_urls)
    print(
        f"[prefetch] {total} deep-read target(s) "
        f"({len(transcript_targets)} transcript, {len(deep_read_urls)} article; "
        f"cap {TOOL_CALL_CAP})",
        file=sys.stderr, flush=True,
    )

    for url, video_id in transcript_targets:
        if fetches_used >= TOOL_CALL_CAP:
            errors[url] = f"Error: fetch cap reached ({TOOL_CALL_CAP} per run)"
            continue
        fetches_used += 1
        print(
            f"[prefetch]   {fetches_used}/{total} transcript {video_id}",
            file=sys.stderr, flush=True,
        )
        block, err = fetch_transcript_excerpt(video_id)
        if err:
            errors[url] = err
        else:
            enrichments[url] = block

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
