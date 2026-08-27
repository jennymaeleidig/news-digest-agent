"""Curation step — thin wrapper that drives GitHub Copilot CLI.

Curation no longer runs an in-process model tool-use loop against raw provider
SDKs (providers/anthropic.py, providers/gemini.py — deleted). Instead the
curator shells out to GitHub Copilot CLI in programmatic mode:

    copilot -p "PROMPT" -s --no-ask-user --allow-tool=...

The prompt reaches Copilot as text only: the category's curation prompt file
(categories/prompts/<id>.md) is read for the driving instructions, the day's
items are formatted into the user message, and the two are combined into the
`-p` prompt. Curation runs **one Copilot call per digest section**: items are
partitioned by their source's assigned section and each non-empty section is
curated separately, so section coverage is an orchestration guarantee rather
than a model balancing judgment. No tool definition is passed, and no
network-capable tool is granted (COPILOT_ALLOW_TOOLS is empty by default), so
Copilot is a pure summarizer that never touches the network — an untrusted
article's text cannot cause it to fetch arbitrary hosts.

Local runs need a one-time interactive Copilot login: run `copilot`, then the
`/login` command (or `gh auth login`). CI authenticates via the
`COPILOT_GITHUB_TOKEN` env var (auth-in-CI wiring lands with the run seam,
ticket 07).

Copilot is a flat seat: it reports no token counts, so token accounting is
dropped here. This is the rationale for the run-log redesign, whose shape
(duration / item counts / prompt size / errors) is built by the run-log
redesign ticket — not here.

Input is bounded because copilot's argv handling degrades sharply (and
ultimately crashes with a V8 boot error) on very large prompts: the item count
is capped to CURATION_MAX_ITEMS and the assembled prompt to
CURATION_PROMPT_MAX_CHARS, with full-text enrichments dropped before items and
the most-relevant items (tier, then recency) kept first. See `_build_prompt`.

This is a thin wrapper around an external dependency (real Copilot auth +
model); per the spec's Testing Decisions, the subprocess itself is accepted as
untested and is exercised manually/CI instead.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone

from categories import Category, Section
from config import (
    COPILOT_TIMEOUT_SECONDS,
    CURATION_MAX_ITEMS,
    CURATION_MAX_ITEMS_PER_SOURCE,
    CURATION_PROMPT_MAX_CHARS,
)
from fetchers.common import Item
from prefetch import PrefetchResult, prefetch


# The Copilot CLI binary. Local runs need a one-time interactive login (run
# `copilot`, then `/login`, or `gh auth login`); the
# same path is what CI uses.
COPILOT_BIN = "copilot"

# Tools Copilot may use. Empty by default: a *pure summarizer* that never
# touches the network. The tool gates that matter (the article allowlist) are
# supplied by the deterministic Python pre-fetch stage, not by Copilot, so no
# network-capable tool is ever granted here. (If a future operator wants to
# grant a harmless read-only tool, add its name to this tuple — the invocation
# always renders `--allow-tool=` accordingly.)
COPILOT_ALLOW_TOOLS: tuple[str, ...] = ()

# Conservative estimate of the fixed message banner overhead inside the prompt
# (``Today is <date>.`` + ``There are N items below…`` + ``Items:`` + the
# separator blank lines), used to keep the greedy budget-fitter safely under
# CURATION_PROMPT_MAX_CHARS. Over-estimating by ~200 chars is intentional: it
# only makes the assembled prompt a little smaller than the cap.
_BANNER_OVERHEAD = 700


class CopilotError(RuntimeError):
    """Raised when the Copilot CLI subprocess fails to produce a digest.

    Propagates to main.py's curate_error handling, which turns it into the
    broken-agent email rather than a silent gap.
    """


@dataclass
class CurateResult:
    digest_markdown: str
    items_input: int
    items_output: int
    prompt_size: int = 0   # chars of the assembled `-p` prompt Copilot received


def _copilot_command(prompt: str) -> list[str]:
    """Build the programmatic-mode Copilot CLI invocation.

    `-p` carries the full assembled prompt text; `-s` (session/stream flag),
    `--no-ask-user`, and `--allow-tool=<csv>` keep it non-interactive and
    bound to the granted tool set (empty by default).
    """
    args = [COPILOT_BIN, "-p", prompt, "-s", "--no-ask-user"]
    args.append("--allow-tool=" + ",".join(COPILOT_ALLOW_TOOLS))
    return args


def _enrichment_for(it: Item, enrichments: dict[str, str]) -> str | None:
    """Return the pre-fetched full text attached to an item, if any.

    Enrichments are keyed by the URL that was actually fetched: an ordinary
    item by its own `url`, an HN-style linked item by its `linked_url` (the
    external article that was deep-read).
    """
    return enrichments.get(it.url) or (
        enrichments.get(it.linked_url) if it.linked_url else None
    )


def _item_lines(
    it: Item, index: int, enrichment: str | None,
    tier: int | None = None, section: str | None = None,
) -> list[str]:
    """Render one item's lines of the user message (shared by the banner
    builder and the budget fitter so both measure the same text). The source
    carries its trust tier in parentheses and its assigned digest section when
    known. The trailing empty string is the blank separator line between
    items."""
    head = f"[{index}] {it.source_name}"
    if tier is not None:
        head += f" (tier {tier})"
    head += f" | {it.published or 'no date'}"
    lines = [head]
    if section:
        lines.append(f"Section: {section}")
    lines += [
        f"Title: {it.title}",
        f"URL: {it.url}",
    ]
    if it.linked_url:
        lines.append(f"Linked: {it.linked_url}")
    lines.append(f"Snippet: {it.content_snippet or '(empty)'}")
    if enrichment:
        lines.append("")
        lines.append(f"Full text of {it.title}:")
        lines.append(enrichment)
    lines.append("")
    return lines


def build_user_message(
    items: list[Item],
    today: str,
    enrichments: dict[str, str] | None = None,
    tier_by_source: dict[str, int] | None = None,
    section_by_source: dict[str, str] | None = None,
) -> str:
    """Format the day's items into the user message pasted into the prompt.

    Each item's snippet, source (tagged with its trust tier), and URL are
    always included. When the pre-fetch stage has attached full article text
    to an item (keyed by URL / linked URL), that plain text is pasted into the
    prompt after the snippet, so Copilot's only textual context comes from the
    pre-fetched plain text.
    """
    enrichments = enrichments or {}
    tier_by_source = tier_by_source or {}
    section_by_source = section_by_source or {}
    parts = [
        f"Today is {today}.",
        "",
        f"There are {len(items)} items below from the last 24 to 48 hours, "
        "after URL-level dedup against previously covered items. Each item "
        "has a source (with its trust tier in parentheses and its assigned "
        "digest section), title, URL, publish date, and a content snippet "
        "(which may be empty for some sources). The full text of any deep-read "
        "item has already been attached to it; judge each item from the "
        "material given.",
        "",
        "Items:",
        "",
    ]
    for i, it in enumerate(items, start=1):
        tier = tier_by_source.get(it.source_name)
        section = section_by_source.get(it.source_name)
        parts.extend(
            _item_lines(it, i, _enrichment_for(it, enrichments), tier, section)
        )
    return "\n".join(parts)


def _tier_by_source(category: Category) -> dict[str, int]:
    """Map each configured source name to its Kagi trust tier (1-4)."""
    return {s.name: s.tier for s in category.sources}


def _section_by_source(category: Category) -> dict[str, str]:
    """Map each configured source name to its assigned digest section.

    Sources without a section (absent / None) drop out, so an un-delegated
    source simply has no ``Section:`` tag and the model is free to place it.
    """
    return {s.name: s.section for s in category.sources if s.section}


def _order_by_relevance(items: list[Item], category: Category) -> list[Item]:
    """Order items most-relevant-first so the budget fitter keeps the best N.

    Relevance = Kagi trust tier first (lower tier number = stronger primary
    signal; an item's source maps to its category tier), then recency within a
    tier (newest first). Items from an unknown source default to tier 4, so a
    misconfigured source's items are dropped before real ones when capped.
    """
    tier_by_source = _tier_by_source(category)

    def key(it: Item) -> tuple:
        tier = tier_by_source.get(it.source_name, 4)
        try:
            pub = datetime.fromisoformat(it.published)
            if pub.tzinfo is None:
                pub = pub.replace(tzinfo=timezone.utc)
            ts = pub.timestamp()
        except (TypeError, ValueError):
            ts = 0.0
        return (tier, -ts)

    return sorted(items, key=key)


def _cap_per_source(items: list[Item], max_per_source: int) -> list[Item]:
    """Bound how many items any single source contributes to the prompt.

    A busy arXiv day produces far more items than every other source combined,
    and the flat most-relevant-first order then lets arXiv fill the entire
    prompt while the release and news sources are pushed past the item cap and
    never curated. Capping each source here — in the incoming relevance order,
    so the strongest items per source survive — keeps every source (hence every
    digest section) represented, deterministically.
    """
    kept: list[Item] = []
    counts: dict[str, int] = {}
    for it in items:
        n = counts.get(it.source_name, 0)
        if n >= max_per_source:
            continue
        kept.append(it)
        counts[it.source_name] = n + 1
    return kept


def _group_by_section(
    items: list[Item],
    section_by_source: dict[str, str],
    section_order: tuple[str, ...],
) -> dict[str, list[Item]]:
    """Partition items into their assigned digest sections.

    Returns a ``section -> items`` map in no particular grouping order (the
    caller walks ``section_order``). Items whose source has no section, or an
    unknown one, land in the last section (the catch-all), matching
    ``_reassemble_by_section``'s fallback.
    """
    groups: dict[str, list[Item]] = {s: [] for s in section_order}
    for it in items:
        section = section_by_source.get(it.source_name)
        if section not in groups:
            section = section_order[-1]
        groups[section].append(it)
    return groups


def _section_prompt(prompt_text: str, section: str) -> str:
    """Scope the shared curation prompt to one digest section.

    Each Copilot call curates exactly one section: it sees only that section's
    items and is told to return only that section's content as a flat list, so
    section coverage is an orchestration guarantee rather than a model's
    cross-section balancing judgment. Written positively (what to produce),
    because the curation prompt file already fixes the per-entry format.
    """
    return (
        f"You are curating one digest section: **{section}**."
        f" Produce only {section}: a flat list of item entries (an H3 title"
        f" link plus its summary), in rough importance order. The pipeline"
        f" adds the section heading and the source line, so write neither."
        f" If no item below earns a place in {section}, produce nothing.\n\n"
        + prompt_text
    )


def _build_prompt(
    prompt_text: str,
    items: list[Item],
    today: str,
    enrichments: dict[str, str] | None = None,
    tier_by_source: dict[str, int] | None = None,
    section_by_source: dict[str, str] | None = None,
) -> tuple[str, int]:
    """Assemble the single `-p` prompt Copilot receives, bounded to the budget.

    The category's curation prompt file provides the driving instructions; the
    day's items (with any pre-fetched full text attached) are appended as the
    material to summarize. Prompt text only — no tool definition, no network
    role for Copilot.

    Input is bounded because copilot's argv handling degrades sharply (and
    ultimately crashes with a V8 boot error) on very large prompts:
      - the item count is capped to CURATION_MAX_ITEMS (most-relevant first —
        the caller orders items via _order_by_relevance);
      - when the assembled prompt would exceed CURATION_PROMPT_MAX_CHARS, the
        cheapest material is dropped first — an item's full-text enrichment
        goes before the item itself, and the lowest-priority (trailing) items
        go last.

    Returns ``(prompt, items_sent)`` where ``items_sent`` is the number of
    items actually fed to Copilot (≤ CURATION_MAX_ITEMS), so the caller can
    report the true fed count in the run log.
    """
    enrichments = enrichments or {}
    tier_by_source = tier_by_source or {}
    section_by_source = section_by_source or {}
    items = items[:CURATION_MAX_ITEMS]

    budget = CURATION_PROMPT_MAX_CHARS - len(prompt_text) - _BANNER_OVERHEAD
    chosen: list[tuple[Item, str | None]] = []   # (item, enrichment or None)
    used = 0
    index = 1
    for it in items:
        enr = _enrichment_for(it, enrichments)
        tier = tier_by_source.get(it.source_name)
        section = section_by_source.get(it.source_name)
        with_len = len("\n".join(_item_lines(it, index, enr, tier, section)))
        bare_len = len("\n".join(_item_lines(it, index, None, tier, section)))
        if used + with_len <= budget:
            chosen.append((it, enr))
            used += with_len
        elif enr and used + bare_len <= budget:
            # Keep the item but drop its full text (enrichments go first).
            chosen.append((it, None))
            used += bare_len
        else:
            # Even the bare item won't fit; everything after is lower priority.
            break
        index += 1

    kept = [it for it, _ in chosen]
    # Rebuild the enrichment map over only the kept items, honouring which
    # items kept (vs. dropped) their full text.
    kept_enrich: dict[str, str] = {}
    for it, enr in chosen:
        if enr is None:
            continue
        key = it.url if it.url in enrichments else (
            it.linked_url if it.linked_url and it.linked_url in enrichments else None
        )
        if key is not None:
            kept_enrich[key] = enrichments[key]

    return (
        prompt_text + "\n\n"
        + build_user_message(kept, today, kept_enrich, tier_by_source, section_by_source)
    ).strip(), len(kept)


def _demote_stray_heading_bodies(markdown: str) -> str:
    """Strip `## ` / `# ` prefixes from lines that follow an item title.

    The model occasionally emits an item summary as `## body text…`
    instead of a plain paragraph, which markdown then renders as <h2>.
    This catches the deterministic pattern: a heading-prefixed line
    immediately after a `### [Title](url)` item title is body text.
    """
    lines = markdown.splitlines()
    item_title = re.compile(r"^###\s+\[.+\]\(.+\)\s*$")
    stray = re.compile(r"^#{1,2}\s+(?!\[)(.+)$")
    for i in range(1, len(lines)):
        if item_title.match(lines[i - 1]):
            m = stray.match(lines[i])
            if m:
                lines[i] = m.group(1)
    return "\n".join(lines)


def _normalize_item_headings(markdown: str) -> str:
    """Pin every item-title heading to `###`.

    The model can drift an item title to `##` / `#` (or `####`), which
    markdown renders as a larger/smaller heading than the sibling `###`
    titles — most visibly in email clients that size `<h2>` well above `<h3>`
    (e.g. Apple Mail). Item titles are the only link-bearing headings in the
    digest (`### [Title](url)`), so rewriting any `#{1,4}` heading that
    contains a markdown link to a uniform `###` collapses the drift. Section
    headers (`## Research`, `## Tools and Frameworks`) carry no link and are
    left untouched.
    """
    linked_heading = re.compile(r"^#{1,4}\s+(\[.+\]\(.+\))\s*$")
    lines = markdown.splitlines()
    for i, line in enumerate(lines):
        m = linked_heading.match(line)
        if m:
            lines[i] = f"### {m.group(1)}"
    return "\n".join(lines)


def _insert_source_lines(
    markdown: str,
    items: list[Item],
    tier_by_source: dict[str, int],
) -> str:
    """Deterministically attach a source/tier line under every item heading.

    The digest's titles are verbatim, so each `### [Title](url)` heading can be
    matched back to its item by exact title and given a canonical
    `*Source: <name> — tier <n>*` line — inserted when missing, replaced when
    the model emitted a different one. This backstops the prompt, so the
    per-entry source/tier annotation never depends on the model getting the
    format right. Unmatched headings (e.g. section headers, which carry no
    link, or a model-typoed title) are left untouched.
    """
    by_title = {it.title: it for it in items}
    heading = re.compile(r"^#{1,4}\s+\[(.*)\]\([^)]*\)\s*$")
    model_source = re.compile(r"^\*Source:")

    lines = markdown.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        m = heading.match(line)
        it = by_title.get(m.group(1)) if m else None
        out.append(line)
        i += 1
        if it is None:
            continue
        # Swallow a model-emitted source line so the canonical one wins.
        if i < len(lines) and model_source.match(lines[i]):
            i += 1
        tier = tier_by_source.get(it.source_name)
        if tier is not None:
            out.append(f"*Source: {it.source_name} — tier {tier}*")
        else:
            out.append(f"*Source: {it.source_name}*")
    return "\n".join(out)


def _sections_blurb(sections: tuple[Section, ...]) -> str:
    """Render the category's section definitions for the curation prompt.

    Section names, descriptions, and order are injected here from the category
    config rather than hardcoded in the prompt file, so the JSON stays the
    single source of truth for what sections exist, what belongs in each, and
    their digest order.
    """
    lines = ["# Sections", ""]
    for sec in sections:
        if sec.description:
            lines.append(f"- **{sec.name}** \u2014 {sec.description}")
        else:
            lines.append(f"- **{sec.name}**")
    return "\n".join(lines)


def _trim_summary_lines(body: list[str]) -> str:
    """Trim leading/trailing blank lines from an item's body and rejoin.

    Interior blank lines (paragraph breaks in a multi-paragraph summary) are
    preserved, so a model-written summary keeps its shape.
    """
    first, last = 0, len(body)
    while first < last and not body[first].strip():
        first += 1
    while last > first and not body[last - 1].strip():
        last -= 1
    return "\n".join(body[first:last])


def _reassemble_by_section(
    markdown: str,
    items: list[Item],
    tier_by_source: dict[str, int],
    section_by_source: dict[str, str],
    section_order: tuple[str, ...],
) -> str:
    """Deterministically regroup digest items into their assigned sections.

    Section placement is a pipeline guarantee, not a model choice: Copilot's
    job is to *select* items and write their summaries; the pipeline owns the
    structure. This parses the model's markdown for item headings, matches each
    back to its input item (verbatim title first, then URL), keeps the model's
    summary, and re-emits everything grouped under the item's canonical
    ``## <Section>`` heading in fixed section order — with the canonical
    verbatim title/URL from the input item and the canonical ``*Source: …*``
    line. Items the model emitted that match no input item (a non-verbatim or
    invented title) are dropped, which also hard-enforces verbatim titles.
    Sections with no items are skipped.
    """
    by_title = {it.title: it for it in items}
    by_url = {it.url: it for it in items}

    item_heading = re.compile(r"^#{1,4}\s+\[(.+)\]\(([^)]*)\)\s*$")
    source_line = re.compile(r"^\s*\*Source:")
    section_heading = re.compile(r"^#{1,4}\s+(?!\[)")

    # (input item, model-written summary) pairs, in the model's output order.
    parsed: list[tuple[Item, str]] = []
    current: Item | None = None
    body: list[str] = []
    for line in markdown.splitlines():
        m = item_heading.match(line)
        if m:
            if current is not None:
                parsed.append((current, _trim_summary_lines(body)))
            title, url = m.group(1), m.group(2)
            current = by_title.get(title) or by_url.get(url)
            body = []
        elif current is not None and not section_heading.match(line):
            if not source_line.match(line):
                body.append(line)
    if current is not None:
        parsed.append((current, _trim_summary_lines(body)))

    groups: dict[str, list[tuple[Item, str]]] = {
        s: [] for s in section_order
    }
    for it, summary in parsed:
        section = section_by_source.get(it.source_name)
        if section not in groups:
            section = section_order[-1]   # unknown => catch-all last section
        groups[section].append((it, summary))

    out: list[str] = []
    for section in section_order:
        entries = groups[section]
        if not entries:
            continue
        out.append(f"## {section}")
        out.append("")
        for it, summary in entries:
            out.append(f"### [{it.title}]({it.url})")
            if summary:
                out.append(summary)
            tier = tier_by_source.get(it.source_name)
            if tier is not None:
                out.append(f"*Source: {it.source_name} — tier {tier}*")
            else:
                out.append(f"*Source: {it.source_name}*")
            out.append("")
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)


def _count_items_in_digest(markdown: str) -> int:
    # Item headings are the link-bearing `### [Title](url)` lines the
    # reassembler emits; section headings (`## Releases`) carry no link.
    return sum(1 for line in markdown.splitlines() if re.match(r"^#{2,4}\s+\[", line))


def _empty_result() -> CurateResult:
    return CurateResult(
        digest_markdown="",
        items_input=0,
        items_output=0,
        prompt_size=0,
    )


def _run_copilot(prompt: str) -> str:
    """Run one Copilot CLI call and return its stdout as markdown.

    Raises CopilotError on a missing binary, a timeout, or a non-zero exit,
    isolating this one section call's failure to the caller.
    """
    command = _copilot_command(prompt)
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=COPILOT_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        raise CopilotError(
            f"copilot CLI not found on PATH ({COPILOT_BIN!r}). "
            "Install GitHub Copilot CLI and log in once (run `copilot`, then "
            "the `/login` command, or `gh auth login`)."
        ) from None
    except subprocess.TimeoutExpired:
        raise CopilotError(
            f"copilot CLI timed out after {COPILOT_TIMEOUT_SECONDS}s"
        ) from None

    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        raise CopilotError(
            f"copilot CLI exited {proc.returncode}"
            + (f": {stderr}" if stderr else "")
        )
    return proc.stdout


def _postprocess(
    markdown: str,
    items: list[Item],
    tier_by_source: dict[str, int],
    section_by_source: dict[str, str],
    section_order: tuple[str, ...],
) -> str:
    """Normalize one (section-scoped) model output into canonical digest markdown.

    The same deterministic post-pipeline as before, now applied per section:
    pin item headings to `###`, demote stray heading bodies, attach the
    canonical source/tier line, and regroup under the canonical section heading
    in fixed order (all items in this call already belong to that section).
    """
    digest_md = _normalize_item_headings(markdown)
    digest_md = _demote_stray_heading_bodies(digest_md)
    digest_md = _insert_source_lines(digest_md, items, tier_by_source)
    return _reassemble_by_section(
        digest_md, items, tier_by_source, section_by_source, section_order
    )


def curate(
    items: list[Item],
    category: Category,
    *,
    today: str | None = None,
) -> CurateResult:
    """Run curation for one category by driving the Copilot CLI once per section.

    Section coverage is an orchestration guarantee, not a model balancing
    judgment: the day's items are grouped by their source's assigned section
    and each non-empty section gets its own Copilot call over only that
    section's items. The per-section outputs are post-processed deterministically
    (verbatim titles, canonical source/tier lines, canonical section order) and
    concatenated into the final digest. Raises CopilotError if any section's
    subprocess fails; main.py turns that into the broken-agent email.
    """
    if today is None:
        today = datetime.now(timezone.utc).date().isoformat()

    if not items:
        return _empty_result()

    # Deterministic read boundary: the Python pre-fetch stage deep-reads the
    # items that warrant it BEFORE any Copilot call, gated by the source-
    # homepage + linked-URL allowlist, and pastes the plain text into the
    # prompt. Items are relevance-ordered (tier, then recency) and capped per
    # source once, then partitioned into sections — so each section call is
    # both bounded and already contains only its own material.
    ordered = _order_by_relevance(items, category)
    balanced = _cap_per_source(ordered, CURATION_MAX_ITEMS_PER_SOURCE)
    prefetch_result: PrefetchResult = prefetch(balanced, category.sources)
    prompt_text = category.prompt_path.read_text(encoding="utf-8")
    # Inject the category's section definitions from config (the single source
    # of truth) so the prompt file never hardcodes section names.
    prompt_text = prompt_text + "\n\n" + _sections_blurb(category.sections)
    tier_by_source = _tier_by_source(category)
    section_by_source = _section_by_source(category)
    section_order = tuple(sec.name for sec in category.sections)
    groups = _group_by_section(balanced, section_by_source, section_order)

    digest_parts: list[str] = []
    total_sent = 0
    total_chars = 0
    for section in section_order:
        section_items = groups[section]
        if not section_items:
            continue
        prompt, items_sent = _build_prompt(
            _section_prompt(prompt_text, section),
            section_items, today, prefetch_result.enrichments,
            tier_by_source, section_by_source,
        )
        section_md = _postprocess(
            _run_copilot(prompt), section_items, tier_by_source, section_by_source,
            section_order,
        )
        if section_md:
            digest_parts.append(section_md)
        total_sent += items_sent
        total_chars += len(prompt)

    digest_markdown = "\n\n".join(digest_parts)
    return CurateResult(
        digest_markdown=digest_markdown,
        items_input=total_sent,
        items_output=_count_items_in_digest(digest_markdown),
        prompt_size=total_chars,
    )
