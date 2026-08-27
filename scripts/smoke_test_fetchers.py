"""CI smoke-test for the three new network-backed fetchers.

This script is the CI's datacenter-IP smoke test for ticket 05. The daily
scheduled run executes from a GitHub Actions runner (a datacenter IP), so a
fetch that works on a local residential IP is not sufficient evidence a source
works where the digest *actually* runs. This smoke test invokes each of the
three new network-backed fetchers **unauthenticated** from that exact
environment and fails unless every one returns a full body that maps to
**non-empty** items.

The three fetchers under test:

  - Hugging Face Daily Papers  (``huggingface_papers`` JSON-API fetcher)
  - AI Release Tracker         (``airelease_tracker`` HTML-scraping fetcher)
  - radarai.top                (native ``rss`` feed, radarai.top/en/feed.xml)

Reddit r/LocalLLaMA is deliberately **not** smoke-tested: it is read through a
community-maintained third-party proxy (the ``reddit_rss_api`` fetcher), and a
tier-4 commentary source degrading is a source-health event the run already
surfaces — not a reason to block the CI job on a stranger's service.
There are no retry loops anywhere in this script — each fetcher is called
exactly once — so it cannot hammer any host it exercises.

Why "non-empty items" is the bar: a datacenter block often still returns HTTP
200 with a stripped/empty body (a CAPTCHA page, an empty JSON array, or an
RSS parse yielding zero entries) rather than a 4xx. The fetchers already
convert HTTP errors and unparseable bodies into failure ``FetchResult``s, so
this smoke test's extra job is to surface the 200-but-empty case — the one a
local IP would not reproduce. That is what makes a local-OK-but-datacenter-
blocked scrape fail loudly here instead of silently passing.

The forkable logic lives in ``run_smoke`` / ``check_fetch`` so it can be
unit-tested offline with injected sources and a fake fetcher registry (see
``tests/test_smoke_test_fetchers.py``); ``main`` is a thin I/O shell that
loads the real category and exits non-zero on any failure.
"""

from __future__ import annotations

import sys
from typing import Callable, Sequence

from categories import Category, load_category
from fetchers.common import FetchResult
from fetchers.registry import fetch_one

# The three network-backed fetchers the smoke test covers, keyed by the source
# ``name`` declared in categories/ai-ml.json. radarai.top is covered as one of
# the new network-backed sources; Reddit r/LocalLLaMA is intentionally excluded
# (community third-party proxy — not a host we gate the CI job on).
SMOKED_SOURCE_NAMES = (
    "Hugging Face Daily Papers",
    "AI Release Tracker",
    "radarai.top (AI)",
)

CATEGORY_PATH = "categories/ai-ml.json"


def check_fetch(source_name: str, result: FetchResult) -> list[str]:
    """Return the list of problems with a single source's fetch.

    Empty list means the fetch is healthy: it succeeded *and* mapped to
    non-empty items. A non-empty list means the fetch is a failure this smoke
    test must surface — including the 200-but-zero-items case, the datacenter
    block a residential IP would not reproduce.
    """
    if result is None:
        return [f"{source_name}: fetcher returned no result"]
    problems: list[str] = []
    if not result.success:
        problems.append(f"{source_name}: fetch failed: {result.error}")
    elif not result.items:
        problems.append(
            f"{source_name}: returned a full body but mapped to zero items — "
            f"likely a bot-blocked or empty response (local-OK is not enough; "
            f"this must also hold from the datacenter IP)"
        )
    return problems


def run_smoke(
    sources: Sequence,
    fetcher_registry: Callable = fetch_one,
) -> tuple[bool, list[str], dict[str, FetchResult | None]]:
    """Smoke-test the in-scope network-backed sources (by name).

    Each source in ``sources`` whose ``name`` is one of ``SMOKED_SOURCE_NAMES``
    is dispatched **exactly once** through ``fetcher_registry`` (default: the
    real kind-to-fetcher registry). Returns ``(ok, problems, results)``:

      - ``ok`` is True only when every in-scope source passes
        ``check_fetch`` (success + non-empty items) — no retries, no
        second chances.
      - ``problems`` lists one entry per failing source, so the CI log names
        the blocked fetcher.
      - ``results`` maps each in-scope source name to its (single) fetch
        result, so the caller can report status without re-dispatching.

    Sources outside the smoked set are ignored — they are not part of the
    datacenter-IP evidence for the three new fetchers.
    """
    problems: list[str] = []
    results: dict[str, FetchResult | None] = {}
    for source in sources:
        if source.name not in SMOKED_SOURCE_NAMES:
            continue
        result = fetcher_registry(source)
        results[source.name] = result
        problems.extend(check_fetch(source.name, result))
    return (len(problems) == 0, problems, results)


def _load_smoked_sources(category: Category) -> list:
    """Select the in-scope sources from the category, in declared order."""
    return [s for s in category.sources if s.name in SMOKED_SOURCE_NAMES]


def main(argv: list[str] | None = None) -> int:
    """Load the real ai-ml category and smoke-test its three network-backed
    fetchers against the live network, exiting non-zero on any failure."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) > 1:
        print(f"usage: {sys.argv[0]} [category.json]", file=sys.stderr)
        return 2
    path = argv[0] if argv else CATEGORY_PATH

    category = load_category(path)
    sources = _load_smoked_sources(category)
    if len(sources) != len(SMOKED_SOURCE_NAMES):
        missing = set(SMOKED_SOURCE_NAMES) - {s.name for s in sources}
        print(
            f"smoke test: category {category.id!r} is missing in-scope sources: "
            f"{sorted(missing)}",
            file=sys.stderr,
        )
        return 1

    # Each fetcher is dispatched exactly once, inside run_smoke (never
    # retried) — so the smoke test cannot hammer a host. The one dispatch's
    # results drive the status report below.
    ok, problems, results = run_smoke(sources)
    for src in sources:
        status = "OK" if not check_fetch(src.name, results[src.name]) else "FAIL"
        print(f"  {status}  {src.name}  {'-> ' + src.url if src.url else ''}")
    for problem in problems:
        print(f"  FAIL  {problem}")

    if ok:
        print("smoke test: all three network-backed fetchers passed from the datacenter IP")
        return 0
    print("smoke test: FAILED — a network-backed fetcher is blocked or empty from the datacenter IP", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
