"""Fetch-only smoke test for a category's sources — no model, no email, no state.

Unlike scripts/smoke_test_fetchers.py (the CI datacenter-IP gate across all
categories), this is an operator tool for one category: it fetches every
source exactly once through the real fetcher registry and reports per-source
status. Safe to run from a laptop.

For ``kind: youtube`` sources it additionally attempts one transcript excerpt
on the first listed video with a watch URL, so the deep-read seam
(youtube-transcript-api) is exercised end to end. A transcript failure is
reported as a warning, not a failure — per isolate-and-continue the item
would stay judgable on its snippet alone.

Exit code: 0 if every fetch succeeded and mapped to non-empty items (a
success that mapped to zero items is tolerated only when the feed itself
documents the emptiness — `FetchResult.note`, e.g. arXiv's <skipDays> — and
is reported as a WARN), 1 otherwise (transcript warnings alone never fail
the run).

Usage:
    python -m scripts.smoke_fetch_category <category.json | category id>
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

from categories import Category, load_category
from fetchers.common import FetchResult
from fetchers.registry import fetch_one
from fetchers.youtube import extract_video_id
from prefetch import fetch_transcript_excerpt


def check_fetch(source, result: FetchResult | None) -> list[str]:
    """Return the list of problems with a single source's fetch.

    Empty list means the fetch is healthy: it succeeded *and* mapped to
    non-empty items — OR it succeeded empty with `result.note` set, meaning
    the feed itself documents the emptiness (arXiv's <skipDays>: a valid,
    empty channel on declared skip days). The smoke test surfaces that case
    as a WARN, not a failure — but a 200-but-empty response *without* the
    note stays a failure this smoke test must surface: a bot-blocked host
    often still returns HTTP 200 with a stripped/empty body rather than a
    4xx, and that body does not come with a valid channel + skipDays.
    """
    if result is None:
        return [f"{source.name}: fetcher returned no result"]
    if not result.success:
        return [f"{source.name}: fetch failed: {result.error}"]
    if not result.items:
        return [
            f"{source.name}: returned a full body but mapped to zero items — "
            f"likely a bot-blocked or empty response"
        ]
    return []


def smoke_category(
    category: Category,
    fetcher_registry=fetch_one,
    transcript_fn=fetch_transcript_excerpt,
) -> tuple[bool, list[str]]:
    """Smoke-test one category: every source, exactly one dispatch each.

    No retries, no second chances — so the smoke test cannot hammer a host.
    For youtube sources, one transcript excerpt is attempted on the first
    item with an extractable video id (a live/Shorts-style entry can top a
    feed with a URL that is not a /watch link); a transcript failure is a
    printed warning, never a failure. Returns ``(ok, failures)``.
    """
    print(f"smoke: {category.id!r} — fetching {len(category.sources)} sources "
          "(one dispatch each, no retries, no state, no email)")
    failures: list[str] = []
    for source in category.sources:
        result = fetcher_registry(source)
        problems = check_fetch(source, result)
        note = result.note if (result is not None and result.success) else None
        if problems and not note:
            failures.extend(problems)
            print(f"  FAIL  {source.name:<22} kind={source.kind:<8} "
                  f"tier={source.tier}  -> {problems[0]}")
            continue
        if problems and note:
            # Documented-empty feed (e.g. arXiv skip days): expected emptiness,
            # surfaced but never failed on.
            print(f"  WARN  {source.name:<22} kind={source.kind:<8} "
                  f"tier={source.tier}  -> {note}")
        else:
            print(f"  OK    {source.name:<22} kind={source.kind:<8} "
                  f"tier={source.tier}  -> {len(result.items)} items")
        if source.kind != "youtube" or not result.items:
            continue
        video_id = next(
            (vid for item in result.items
             if (vid := extract_video_id(item.url)) is not None),
            None)
        if video_id is None:
            failures.append(
                f"{source.name}: no item URL yielded a video id across "
                f"{len(result.items)} items (first: {result.items[0].url!r})")
            print(f"  FAIL  {source.name:<22} no /watch URL among "
                  f"{len(result.items)} items (first: {result.items[0].url})")
            continue
        excerpt, err = transcript_fn(video_id)
        if err:
            print(f"  WARN  {source.name:<22} transcript unavailable on "
                  f"video {video_id}: {err} (item stays judgable on snippet)")
        else:
            print(f"  OK    {source.name:<22} transcript excerpt on video "
                  f"{video_id}: {len(excerpt)} chars")
    return (not failures, failures)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        print(f"usage: {sys.argv[0]} <category.json | category id>",
              file=sys.stderr)
        return 2
    arg = argv[0]
    path = arg if arg.endswith(".json") else f"categories/{arg}.json"
    load_dotenv()
    proxy = os.environ.get("YT_TRANSCRIPT_PROXY_URL")
    print("transcript proxy: "
          + ("configured" if proxy else "not set (direct connection)"))
    ok, failures = smoke_category(load_category(path))
    if not ok:
        print(f"smoke: FAILED — {len(failures)} problem(s)", file=sys.stderr)
        return 1
    print("smoke: all sources fetched non-empty items")
    return 0


if __name__ == "__main__":
    sys.exit(main())
