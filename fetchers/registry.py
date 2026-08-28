"""Open-ended `kind → fetcher` registry.

The pipeline dispatches source fetching through this registry instead of a
hardcoded if/elif in main.py. Adding a new source kind is exactly two steps,
never a pipeline edit:

    1. Write one fetcher module under fetchers/ that exposes
       ``def fetch(source) -> FetchResult``.
    2. Register it here (or have the module register itself) via
       ``register("mykind", my_module.fetch)``.

Dispatch (``fetch_one``) is kind-agnostic: it looks the source's ``kind`` up
in the registry and either runs the mapped fetcher or returns an isolated
per-source ``FetchResult`` failure for unknown kinds, so one bad source never
crashes the run (isolate-and-continue).

Several kinds are registered at launch: ``rss`` (arXiv and other feeds) and
``youtube`` (keyless per-channel Atom listing, deliberately a separate kind —
never the shared ``rss`` kind), plus the bespoke ``huggingface_papers``,
``airelease_tracker``, and ``reddit_rss_api``. The registry supports further
kinds with no pipeline edits when a category ever declares one.
"""

from __future__ import annotations

from typing import Callable

from categories import Source
from fetchers.common import FetchResult

# kind -> fetcher; a fetcher is ``Callable[[Source], FetchResult]``.
FETCHERS: dict[str, Callable[[Source], FetchResult]] = {}


def register(kind: str, fetcher: Callable[[Source], FetchResult]) -> None:
    """Register a fetcher for a source kind.

    Re-registering an existing kind overwrites the previous fetcher; this is
    intentional so a kind can be swapped without a registry rewrite, but it
    makes a duplicate-registration bug loud rather than silent.
    """
    if not kind:
        raise ValueError("kind must be a non-empty string")
    FETCHERS[kind] = fetcher


def fetch_one(source: Source) -> FetchResult:
    """Dispatch a single source to its registered fetcher.

    Unknown kinds produce a clear, isolated failure for that source (a
    FetchResult with success=False and a descriptive error) rather than raising,
    so the surrounding pipeline can isolate-and-continue past a misconfigured
    source.
    """
    fetcher = FETCHERS.get(source.kind)
    if fetcher is None:
        return FetchResult(
            source.name,
            False,
            error=f"unknown source kind: {source.kind!r} (no fetcher registered)",
        )
    return fetcher(source)


# Launch registrations. ``rss`` is the shipped general feed kind;
# ``youtube`` is deliberately separate (per-channel Atom, never routed
# through ``rss``). huggingface_papers (JSON-API field mapping),
# airelease_tracker (HTML-selector scraping), and reddit_rss_api are bespoke
# kinds that stay **distinct** mechanisms while sharing one config-schema
# contract. Each adds a source kind with no pipeline edit here or in main.py.
from fetchers import (  # noqa: E402
    airelease_tracker,
    huggingface_papers,
    reddit_rss_api,
    rss,
    youtube,
)

register("rss", rss.fetch)
register("youtube", youtube.fetch)
register("huggingface_papers", huggingface_papers.fetch)
register("airelease_tracker", airelease_tracker.fetch)
register("reddit_rss_api", reddit_rss_api.fetch)
