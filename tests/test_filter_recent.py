"""Unit tests: the recency filter is the only dedup, so its semantics are
load-bearing. Pins them exactly:

- the window is a strict rolling window off each item's published timestamp,
  inclusive of the boundary;
- a per-source window (from the category/source override map) overrides the
  global default; sources absent from the map get the global default;
- items with missing or unparseable dates are KEPT (warned, not dropped) —
  a malformed feed date must not silently empty a digest;
- date-only timestamps (midnight UTC) are treated as their calendar day's
  end, so a feed that stamps dates without times isn't penalized up to a
  full day of staleness.

Fully offline: Items are constructed directly.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fetchers.common import Item
from main import filter_recent

NOW = datetime.now(timezone.utc)  # real clock: filter_recent uses the same one


def _iso(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat()


def _item(published: str, name: str = "src") -> Item:
    return Item(
        title="t", source_name=name, url=f"https://e.test/{published}/{name}",
        published=published, content_snippet="",
    )


def test_window_inclusive_boundary():
    # ~24h old is inside the window (t >= cutoff); just past it is out.
    # (Exactly-24.0h is racy against the moving clock inside filter_recent,
    # so the test straddles the boundary by seconds, not microseconds.)
    items = [_item(_iso(24.0 - 3.6)), _item(_iso(24.0 + 3.6))]
    assert len(filter_recent(items, 1)) == 1


def test_per_source_window_overrides_global():
    # "slow" gets a 3-day window, "fast" the 1-day global default.
    items = [_item(_iso(48), name="slow"), _item(_iso(48), name="fast")]
    kept = filter_recent(items, {"slow": 3, "fast": 1})
    assert [it.source_name for it in kept] == ["slow"]


def test_missing_and_unparseable_dates_are_kept():
    items = [
        _item(""),
        _item("not-a-date"),
    ]
    assert len(filter_recent(items, 1)) == 2


def test_date_only_timestamp_counts_to_end_of_day():
    # Yesterday's date, no time component: parsed naively it is midnight —
    # up to ~36h before NOW. As a date without a time it means *some time
    # that day*, so it stays eligible through the end of yesterday (i.e. it
    # is inside the 1-day window iff NOW is still within yesterday+24h).
    yesterday = (NOW - timedelta(days=1)).date().isoformat()
    day_before = (NOW - timedelta(days=2)).date().isoformat()
    items = [_item(yesterday), _item(day_before)]
    kept = filter_recent(items, 1)
    assert [it.published for it in kept] == [yesterday]


def test_unparseable_kept_even_in_per_source_map():
    items = [_item("garbage", name="s")]
    assert len(filter_recent(items, {"s": 1})) == 1
