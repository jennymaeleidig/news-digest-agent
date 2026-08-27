"""Multi-category news digest agent — entry point.

Discovers every category config (categories/*.json) and runs each category's
full fetch → filter → curate → email pipeline (see run_category) fully before
starting the next; per-category failures are isolated so one bad category never
halts the rest. One shared schedule (the workflow) fires all categories.

Per-category run order (inside run_category):
  1. Fetch from every source (failures isolated, run continues)
  2. Filter items: time window, then dedup against that category's seen_items
  3. Curate via Copilot CLI (timed), using the category's own prompt file
  4. Build digest body, append source-health footer
  5. Send email (always — quiet days look the same as broken agent days),
     routed to the category's own recipient
  6. Persist state and observability (all keyed by category id):
       - source_health.json   always
       - run_log.jsonl        always (duration/item counts/prompt size/errors;
                              no token columns — Copilot is a flat seat)
       - seen_items.json      only if email sent AND curate succeeded
                              (a failed send must not lose items, and
                              an error email must not mark them seen)
  7. Return a CategoryRunOutcome; exit 1 if any category's curation or email
     failed, else 0.
"""

from __future__ import annotations

import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from dotenv import load_dotenv

from categories import Category, Source, load_category
from config import ITEM_AGE_LIMIT_DAYS
from curator import CurateResult, curate
from emailer import digest_subject, empty_digest_body, resolve_recipient, send_digest
from fetchers.common import FetchResult, Item
from fetchers.registry import fetch_one
from state import (
    append_run_log_row,
    load_seen_items,
    load_source_health,
    prune_expired,
    record_source_run,
    save_seen_items,
    save_source_health,
)


# Copilot / email exceptions routinely embed account identifiers (org UUIDs,
# request IDs) and the recipient address back from the subprocess or API. Strip
# those before the message hits run_log.jsonl or the digest email body — the
# repo is public, so anything written to disk is published.
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")
_UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_API_KEY_RE = re.compile(r"\b(?:sk-[A-Za-z0-9_-]+|AIza[A-Za-z0-9_-]+|re_[A-Za-z0-9_-]+)\b")
_REQ_ID_RE = re.compile(r"\breq_[A-Za-z0-9]+\b")


# Categories are discovered from the categories/ directory (see
# discover_categories); the single launch category is one of them. No category
# is hardcoded in the pipeline.
CATEGORIES_DIR = Path(__file__).parent / "categories"


# Fetcher registry, curator/shell runner, and emailer are passed explicitly to
# run_category so the test harness (ticket 08) can inject fakes without any
# network or CI. The same stage types flow through here as type aliases.
FetcherLike = Callable[[Source], FetchResult]      # e.g. fetchers.registry.fetch_one
CuratorLike = Callable[[list[Item], Category], CurateResult]  # e.g. curator.curate
EmailerLike = Callable[[str, str, str], str]       # e.g. emailer.send_digest


def sanitize_error(e: BaseException) -> str:
    msg = str(e)
    msg = _EMAIL_RE.sub("[email]", msg)
    msg = _API_KEY_RE.sub("[apikey]", msg)
    msg = _UUID_RE.sub("[uuid]", msg)
    msg = _REQ_ID_RE.sub("[reqid]", msg)
    return f"{type(e).__name__}: {msg}"


# Fetch dispatch lives in fetchers.registry: an open-ended `kind -> fetcher`
# map. Adding a new source kind is one fetcher module plus one registration —
# no pipeline edit. RSS is the only kind registered at launch; unknown kinds
# return an isolated per-source FetchResult failure rather than crashing the run.


def fetch_all(sources: list[Source]) -> list[FetchResult]:
    return [fetch_one(s) for s in sources]


def collect_items(results: list[FetchResult]) -> list[Item]:
    out: list[Item] = []
    for r in results:
        if r.success:
            out.extend(r.items)
    return out


def filter_recent(items: list[Item], days: int | dict[str, int]) -> list[Item]:
    """Keep only items whose published date is within the recency window.

    ``days`` is either a single integer applied to every item, or a
    ``source_name -> days`` map giving a per-source window (sources absent from
    the map fall back to the global ``ITEM_AGE_LIMIT_DAYS``). Per-source
    windows let a slow-moving canonical feed (a release tracker) stay eligible
    longer than a fast news feed without loosening the global default.
    """
    now = datetime.now(timezone.utc)
    if isinstance(days, dict):
        default_cutoff = now - timedelta(days=ITEM_AGE_LIMIT_DAYS)
        cutoffs = {name: now - timedelta(days=d) for name, d in days.items()}
    else:
        default_cutoff = now - timedelta(days=days)
        cutoffs = {}

    out: list[Item] = []
    for it in items:
        if not it.published:
            print(f"warn: missing date, keeping item: {it.source_name} | {it.url}", file=sys.stderr)
            out.append(it)
            continue
        try:
            t = datetime.fromisoformat(it.published)
        except ValueError:
            print(f"warn: unparseable date {it.published!r}, keeping item: {it.source_name} | {it.url}", file=sys.stderr)
            out.append(it)
            continue
        cutoff = cutoffs.get(it.source_name, default_cutoff)
        if t >= cutoff:
            out.append(it)
    return out


def filter_unseen(items: list[Item], seen: dict[str, str]) -> list[Item]:
    return [it for it in items if it.url not in seen]


def filter_relevant(items: list[Item], sources: list[Source]) -> list[Item]:
    """Drop items whose source declares a `topics` allow-list but whose
    title/abstract/snippet matches none of the terms.

    A source with no topics keeps every item. The match is a case-insensitive
    substring scan of the item's title plus its snippet (arXiv feeds carry the
    abstract in the RSS description, so this captures arXiv abstracts too).
    This lets a broad feed (e.g. arXiv cs.AI+cs.LG) be scoped to a topic —
    "the LLM stuff" — without a bespoke fetcher.
    """
    topics_by_source: dict[str, tuple[str, ...]] = {
        s.name: s.topics for s in sources
    }
    out: list[Item] = []
    for it in items:
        topics = topics_by_source.get(it.source_name)
        if not topics:
            out.append(it)
            continue
        hay = f"{it.title}\n{it.content_snippet}".lower()
        if any(t.lower() in hay for t in topics):
            out.append(it)
    return out


def build_health_footer(results: list[FetchResult]) -> str:
    failures = [r for r in results if not r.success]
    if not failures:
        return ""
    lines = ["", "---", "", "*Source health:*", ""]
    for r in failures:
        lines.append(f"- {r.source_name}: {r.error}")
    return "\n".join(lines)


@dataclass
class CategoryRunOutcome:
    """Structured per-category run outcome returned by ``run_category``.

    The composition seam (ticket 08 asserts against this). Carries the
    category-scoped state deltas so the harness can verify namespacing and the
    seen-items-only-on-successful-send guard without poking the real data/
    files.
    """
    category_id: str
    items_input: int                       # items fed to the curator (after filters)
    items_output: int                      # items in the produced digest
    results: tuple[FetchResult, ...]       # per-source fetch results
    digest_md: str
    subject: str
    recipient: str
    curate_error: str | None
    email_error: str | None
    email_sent: bool
    marked_seen: tuple[str, ...] = ()      # state delta: urls marked seen this run
    health_records: int = 0                # state delta: source-health rows written
    run_log_row: dict = field(default_factory=dict)  # state delta: the logged row

    @property
    def ok(self) -> bool:
        """True when neither curation nor email failed for this category."""
        return self.curate_error is None and self.email_error is None


class StateStore:
    """Default category-scoped state operations used by run_category.

    Injectable: a test harness passes a fake object with the same methods to
    observe/capture state deltas instead of touching the real data/ files.
    ``category_id`` namespaces each operation so no category leaks into
    another's namespace.
    """
    def __init__(self, category_id: str):
        self.category_id = category_id

    def load_seen(self) -> dict[str, object]:
        return prune_expired(load_seen_items(self.category_id))

    def save_seen(self, seen: dict[str, object]) -> None:
        save_seen_items(self.category_id, seen)

    def load_health(self) -> dict:
        return load_source_health(self.category_id)

    def save_health(self, health: dict) -> None:
        save_source_health(self.category_id, health)

    def record_source(self, health: dict, name: str, success: bool, error: str | None) -> None:
        record_source_run(health, name, success, error)

    def log_run(self, row: dict) -> None:
        append_run_log_row(row)


def empty_curate_result() -> CurateResult:
    return CurateResult(
        digest_markdown="",
        items_input=0,
        items_output=0,
        prompt_size=0,
    )


def run_category(
    category: Category,
    *,
    fetcher_registry: FetcherLike = fetch_one,
    curate_fn: CuratorLike = curate,
    emailer: EmailerLike = send_digest,
    state: StateStore | None = None,
    today: str | None = None,
) -> CategoryRunOutcome:
    """Run one category's full fetch → filter → curate → email pipeline.

    The locked per-category orchestration seam (spec decision 8). All stage
    dependencies are injectable so tests can supply fakes with no network or
    CI:
      - fetcher_registry: a ``kind -> FetchResult`` dispatcher (default
        fetchers.registry.fetch_one).
      - curate_fn: the curator/shell runner (default curator.curate); accepts
        ``(items, category, *, today=...)``.
      - emailer: ``(markdown, subject, recipient) -> message_id`` (default
        emailer.send_digest).
      - state: category-scoped state operator (default a StateStore bound to
        category.id); carries load/save of seen + health and the run-log row.

    Each stage's failures are captured (sanitized via sanitize_error) into the
    returned outcome rather than raised, so the caller can isolate-and-
    continue to the next category. Account-identifier redaction and the seen-
    items-only-on-successful-send guard hold inside this function per
    category.
    """
    if state is None:
        state = StateStore(category.id)
    if today is None:
        today = datetime.now(timezone.utc).date().isoformat()

    sources = list(category.sources)
    seen = state.load_seen()
    health = state.load_health()

    # fetch -> filter (time window, then relevance, then dedup against seen)
    # -> curate. The relevance filter drops items from a `topics`-scoped source
    # that don't match, so a broad feed is narrowed before curation (and before
    # any dedup), keeping the prompt focused on what the reader asked for.
    # The time window is per-source: a source may override the global age limit
    # (a release tracker's "latest" list spans weeks, so it needs a longer
    # window than a 7-day news feed).
    results = [fetcher_registry(s) for s in sources]
    raw = collect_items(results)
    age_limit_by_source = {
        s.name: (s.age_limit_days if s.age_limit_days is not None else ITEM_AGE_LIMIT_DAYS)
        for s in sources
    }
    fresh = filter_recent(raw, age_limit_by_source)
    relevant = filter_relevant(fresh, sources)
    unseen = filter_unseen(relevant, seen)

    curate_error: str | None = None
    curate_started = time.monotonic()
    if unseen:
        try:
            result = curate_fn(unseen, category, today=today)
        except Exception as e:
            curate_error = sanitize_error(e)
            result = empty_curate_result()
    else:
        result = empty_curate_result()
    curate_duration = time.monotonic() - curate_started

    if curate_error:
        digest_md = (
            f"Agent error during curation:\n\n"
            f"```\n{curate_error}\n```\n\n"
            f"No digest produced this run."
        )
    elif result.digest_markdown.strip():
        digest_md = result.digest_markdown.strip()
    else:
        digest_md = empty_digest_body()
    digest_md += build_health_footer(results)

    subject = digest_subject(category, today)
    recipient = resolve_recipient(category)
    email_error: str | None = None
    message_id: str | None = None
    try:
        message_id = emailer(digest_md, subject, recipient)
    except Exception as e:
        email_error = sanitize_error(e)
        print(f"email send failed: {email_error}", file=sys.stderr)

    # Always: record + save source health (state written even on failure).
    for r in results:
        state.record_source(health, r.source_name, r.success, r.error)
    state.save_health(health)

    # Always: log the run row (written even on failure so post-mortem data
    # survives). Copilot is a flat seat and reports no token counts, so the
    # row records duration, item counts, prompt size, and errors, and carries
    # the category field so each category's runs are attributable.
    run_log_row = {
        "category": category.id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "items_input": result.items_input,
        "items_output": result.items_output,
        "sources_succeeded": sum(1 for r in results if r.success),
        "sources_failed": sum(1 for r in results if not r.success),
        "duration_seconds": round(curate_duration, 2),
        "prompt_size": result.prompt_size,
        "curate_error": curate_error,
        "email_error": email_error,
    }
    state.log_run(run_log_row)

    # Mark items seen only when the digest email landed AND curation actually
    # ran. An error email still sets message_id, but its items were never
    # curated — leaving curate_error out here would silently burn them so the
    # next (recovered) run never sees them again.
    marked_seen: list[str] = []
    if message_id is not None and curate_error is None:
        now_iso = datetime.now(timezone.utc).isoformat()
        section_by_source = {s.name: s.section for s in sources}
        for it in unseen:
            seen[it.url] = {
                "date": now_iso,
                "section": section_by_source.get(it.source_name),
                "source": it.source_name,
            }
            marked_seen.append(it.url)
        state.save_seen(seen)

    return CategoryRunOutcome(
        category_id=category.id,
        items_input=result.items_input,
        items_output=result.items_output,
        results=tuple(results),
        digest_md=digest_md,
        subject=subject,
        recipient=recipient,
        curate_error=curate_error,
        email_error=email_error,
        email_sent=message_id is not None,
        marked_seen=tuple(marked_seen),
        health_records=sum(len(s.get("recent_runs", [])) for s in health.get("sources", {}).values()),
        run_log_row=run_log_row,
    )


def discover_categories(directory: str | Path = CATEGORIES_DIR) -> list[Category]:
    """Discover and load every category config in the categories/ directory.

    Sorted by filename for deterministic order. A category whose config fails
    to load (schema/JSON error) raises here so the operator is surfaced the
    bad config rather than having an entire category silently skipped;
    per-category *ran* failures are isolated inside the run loop.
    """
    base = Path(directory)
    paths = sorted(base.glob("*.json"))
    if not paths:
        raise FileNotFoundError(f"no category configs (*.json) found in {base}")
    return [load_category(p) for p in paths]


def main() -> int:
    load_dotenv()
    run_started = time.monotonic()
    today_date = datetime.now(timezone.utc).date().isoformat()

    # Discover every category config and run each fully (fetch → filter →
    # curate → email) before starting the next. One shared schedule fires this
    # single job; each category gets its own prompt file, its own state
    # namespace, and its own email routing. A category run failing does not
    # prevent the next from running (isolate-and-continue across categories).
    categories = discover_categories()
    exit_code = 0
    for category in categories:
        try:
            outcome = run_category(category, today=today_date)
        except Exception as e:
            # Only unexpected hard failures land here; run_category already
            # captures curate/email errors into the outcome. Isolate-and-
            # continue so one broken category never halts the rest.
            print(
                f"[{category.id}] category run failed: {sanitize_error(e)}",
                file=sys.stderr,
            )
            exit_code = 1
            continue

        succ = sum(1 for r in outcome.results if r.success)
        total = len(outcome.results)
        print(
            f"[{outcome.category_id}] digest: {outcome.items_output} items "
            f"from {succ}/{total} sources"
            + (f" | curate_error={outcome.curate_error}" if outcome.curate_error else "")
            + (f" | email_error={outcome.email_error}" if outcome.email_error else "")
        )
        if not outcome.ok:
            exit_code = 1

    run_duration = time.monotonic() - run_started
    print(f"digest complete: {len(categories)} categories in {run_duration:.1f}s")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())