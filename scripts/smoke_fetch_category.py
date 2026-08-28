"""Manual fetch-only smoke test for one category's sources.

Unlike scripts/smoke_test_fetchers.py (the CI datacenter-IP gate for ai-ml's
three network-backed fetchers), this is an operator tool: it fetches every
source in one category exactly once through the real fetcher registry and
reports per-source status. No model calls, no email, no state writes —
safe to run from a laptop.

For ``kind: youtube`` sources it additionally attempts one transcript excerpt
on the first listed video, so the deep-read seam (youtube-transcript-api) is
exercised end to end. A transcript failure is reported as a warning, not a
failure — per isolate-and-continue the item would stay judgable on its
snippet alone.

Exit code: 0 if every fetch succeeded and mapped to non-empty items, 1
otherwise (transcript warnings alone never fail the run).
"""

from __future__ import annotations

import sys

from categories import load_category
from fetchers.registry import fetch_one
from fetchers.youtube import extract_video_id
from prefetch import fetch_transcript_excerpt


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        print(f"usage: {sys.argv[0]} <category.json | category id>", file=sys.stderr)
        return 2
    arg = argv[0]
    path = arg if arg.endswith(".json") else f"categories/{arg}.json"

    category = load_category(path)
    print(f"smoke: {category.id!r} — fetching {len(category.sources)} sources "
          "(one dispatch each, no retries, no state, no email)")
    failures: list[str] = []
    for source in category.sources:
        result = fetch_one(source)
        if not result.success:
            failures.append(f"{source.name}: fetch failed: {result.error}")
            print(f"  FAIL  {source.name:<22} kind={source.kind:<8} "
                  f"tier={source.tier}  -> {result.error}")
            continue
        if not result.items:
            failures.append(
                f"{source.name}: full body but zero items — likely bot-blocked "
                "or an empty response")
            print(f"  FAIL  {source.name:<22} kind={source.kind:<8} "
                  f"tier={source.tier}  -> 0 items (body empty/blocked?)")
            continue
        print(f"  OK    {source.name:<22} kind={source.kind:<8} "
              f"tier={source.tier}  -> {len(result.items)} items")
        if source.kind != "youtube" or not result.items:
            continue
        # Live/Shorts-style entries can top a feed with a URL that is not a
        # /watch link — scan for the first item that yields a video id.
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
        excerpt, err = fetch_transcript_excerpt(video_id)
        if err:
            print(f"  WARN  {source.name:<22} transcript unavailable on "
                  f"video {video_id}: {err} (item stays judgable on snippet)")
        else:
            print(f"  OK    {source.name:<22} transcript excerpt on video "
                  f"{video_id}: {len(excerpt)} chars")

    if failures:
        print(f"smoke: {category.id!r} FAILED — {len(failures)} problem(s)",
              file=sys.stderr)
        return 1
    print(f"smoke: {category.id!r} — all sources fetched non-empty items")
    return 0


if __name__ == "__main__":
    sys.exit(main())
