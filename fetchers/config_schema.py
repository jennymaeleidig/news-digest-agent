"""Shared fetcher-config schema for bespoke feedless source kinds.

A small data-contract prefactor so future feedless sites slot in as config
rather than each bespoke kind inventing its own ad-hoc config shape.

Each bespoke feedless source kind's configuration takes the same shape: a
``url`` plus the ``item``/``title``/``link``/``date`` field paths or selectors
used to locate those values within the kind's own fetched representation. The
schema pins only this config-shape contract; it deliberately leaves parsing
mechanics to each consuming kind — a JSON-API kind reads ``title``/``link``/
``date`` as field paths into its JSON, an HTML-scraping kind reads them as
selectors against its document. Consuming kinds stay distinct (JSON-API field
mapping vs HTML-selector scraping); no single shared-kind fetch framework, no
HTML-selector ``webpage`` kind, and no headless browser are introduced here.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FetcherConfig:
    """The one config shape a bespoke feedless source kind consumes.

    This is a pure data contract: it names *where* each piece of an item
    lives (field paths or selectors) but performs no fetching or parsing
    itself. Each consuming kind interprets the strings through its own
    mechanism.
    """

    url: str    # the feed / endpoint / page URL this kind fetches
    item: str   # path/selector to one container item within the fetched data
    title: str  # path/selector for the item's title
    link: str   # path/selector for the item's link/URL
    date: str   # path/selector for the item's published date
