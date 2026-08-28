"""CI smoke test: every source, in every category, from the datacenter IP.

The scheduled run executes from a GitHub Actions runner (a datacenter IP), so
a fetch that works on a local residential IP is not sufficient evidence a
source holds up where the digest *actually* runs. This workflow discovers
every category (via the same discover_categories() the run entry point uses —
a new category is covered automatically) and smoke-tests **all** of its
sources, one stage per category, in declared order: RSS feeds, keyless
YouTube Atom feeds, and the network-backed JSON/HTML fetchers alike.

Why "non-empty items" is the bar: a datacenter block often still returns HTTP
200 with a stripped/empty body (a CAPTCHA page, an empty JSON array, or an
RSS parse yielding zero entries) rather than a 4xx. The fetchers already
convert HTTP errors and unparseable bodies into failure ``FetchResult``s, so
this smoke test's extra job is to surface the 200-but-empty case — the one a
local IP would not reproduce. That is what makes a local-OK-but-datacenter-
blocked scrape fail loudly here instead of silently passing.

For youtube sources it also attempts one transcript excerpt per channel (the
first listed video with a watch URL). A transcript failure prints a warning
but never fails the job — per isolate-and-continue the item stays judgable on
its snippet alone — but the warning is the datacenter-IP signal (e.g.
RequestBlocked) that a laptop run cannot provide.

There are no retry loops anywhere — each fetcher is dispatched exactly once —
so this cannot hammer any host it exercises. It runs within a 10-minute
timeout and the same dependency set as the digest workflows.

The per-source logic is shared with scripts/smoke_fetch_category.py (the
single-category operator tool); this script is the all-categories shell that
exits non-zero on any failure.
"""

from __future__ import annotations

import sys

from categories import Category
from fetchers.registry import fetch_one
from main import discover_categories
from prefetch import fetch_transcript_excerpt

from scripts.smoke_fetch_category import smoke_category


def run_all(
    categories: list[Category],
    fetcher_registry=fetch_one,
    transcript_fn=fetch_transcript_excerpt,
) -> tuple[bool, list[str]]:
    """Smoke-test every category, one stage each, in declared order.

    Returns ``(ok, failures)`` aggregated across categories. A category that
    fails does not stop the later categories from being tested.
    """
    ok_all = True
    failures: list[str] = []
    for index, category in enumerate(categories, start=1):
        print(f"\n── Stage {index}/{len(categories)} · {category.id} "
              f"({category.name}) " + "─" * 8)
        ok, problems = smoke_category(category,
                                      fetcher_registry=fetcher_registry,
                                      transcript_fn=transcript_fn)
        ok_all = ok_all and ok
        failures.extend(problems)
    return ok_all, failures


def main(argv: list[str] | None = None) -> int:
    categories = discover_categories()
    if not categories:
        print("smoke test: no categories discovered", file=sys.stderr)
        return 1
    ok, failures = run_all(categories)
    for problem in failures:
        print(f"  FAIL  {problem}")
    if ok:
        print(f"\nsmoke test: all {len(categories)} categories' sources passed "
              "from the datacenter IP")
        return 0
    print(f"\nsmoke test: FAILED — {len(failures)} problem(s) across "
          f"{len(categories)} categories", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
