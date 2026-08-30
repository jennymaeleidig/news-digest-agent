"""Per-source in-window diagnostic: why did my digest come back thin?

Usage: python -m scripts.diag_filter <category id>

Fetches the category's sources live, then reports per source: total items,
how many pass the recency window, how many also pass the topic filter, and
the three newest published dates seen. This is how you tell a filter bug
from a slow feed: correct dates + zero in-window items means the source
genuinely published nothing inside the window (weekend, cadence), not that
the pipeline dropped it.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

from main import discover_categories, fetch_all, collect_items, filter_recent, filter_relevant
from config import ITEM_AGE_LIMIT_DAYS


def main(cat_id: str) -> int:
    cats = discover_categories()
    cat = [c for c in cats if c.id == cat_id][0]
    now = datetime.now(timezone.utc)
    print(f"now = {now.isoformat()}  (global window = {ITEM_AGE_LIMIT_DAYS}d)")
    for r in fetch_all(list(cat.sources)):
        if not r.success:
            print(f"{r.source_name:35s} FETCH FAIL: {r.error}")
            continue
        dates = sorted((it.published for it in r.items if it.published), reverse=True)
        in_window = filter_recent(r.items, {r.source_name: ITEM_AGE_LIMIT_DAYS})
        rel = filter_relevant(in_window, [s for s in cat.sources if s.name == r.source_name])
        newest = dates[0] if dates else "(none)"
        print(f"{r.source_name:35s} total={len(r.items):4d} in-window={len(in_window):3d} relevant={len(rel):3d}  newest={newest}")
        for d in dates[:3]:
            print(f"{'':35s}   sample: {d}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
