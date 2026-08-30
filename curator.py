"""Curation step — thin wrapper that drives the OpenRouter chat-completions API.

Curation no longer shells out to GitHub Copilot CLI (superseded after the
Copilot seat hit its monthly request quota); it POSTs to OpenRouter's
OpenAI-compatible `/api/v1/chat/completions` endpoint over HTTPS.

The model is pinned, not selected per run: an ``OPENROUTER_MODEL``
environment variable overrides ``config.OPENROUTER_MODEL`` (default
`z-ai/glm-5.3-flash`). Dynamic selection was removed — the discount ranking
had no stable public API and drifted between models, and a sloppy sale-priced
pick regressed digest quality.

The prompt reaches the model as text only: the category's curation prompt file
(categories/prompts/<id>.md) is read for the driving instructions, the day's
items are formatted into the user message, and the two are combined into a
single chat `user` message. Curation runs **one API call per digest section**:
an item from a multi-section source is offered as a candidate to every Section
its source is mapped to and each section's candidates are curated separately,
so section coverage is an orchestration guarantee rather than a model balancing
judgment. A deterministic no-double-pick guard (``_candidates_for_section``)
keeps one URL in exactly one Section of the digest: a URL picked in an earlier
Section is excluded from every later Section's candidate set. No tools or plugins are
requested, so the model is a pure summarizer that never touches the network —
an untrusted article's text cannot cause it to fetch arbitrary hosts.
Authentication is the `OPENROUTER_API_KEY` bearer token.

OpenRouter reports per-request token usage, so token accounting returns here:
prompt/completion totals ride on CurateResult into the run log.

Input is bounded so a day's items plus full-text enrichments fit a model
context window and the owner's budget. The item count is capped to
CURATION_MAX_ITEMS and the assembled prompt to CURATION_PROMPT_MAX_BYTES (UTF-8
bytes), with full-text enrichments dropped before items and the most-relevant
items (tier, then recency) kept first. See `_build_prompt`.

This is a thin wrapper around an external dependency (real OpenRouter auth +
model); per the spec's Testing Decisions, the HTTP call itself is accepted as
untested and is exercised manually/CI instead.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests

from categories import Category, Section
from config import (
    CURATION_MAX_ITEMS,
    CURATION_PROMPT_MAX_BYTES,
    CURATION_SELECT_MAX_ITEMS,
    OPENROUTER_BASE_URL,
    OPENROUTER_MODEL,
    OPENROUTER_TIMEOUT_SECONDS,
)
from fetchers.common import Item
from prefetch import prefetch


# Curation requests carry a single user message and request no tools/plugins,
# so the model stays a *pure summarizer* that never touches the network. The
# network-capable reads (the article allowlist) are supplied by the
# deterministic Python pre-fetch stage, not by the model.
#
# App-attribution headers OpenRouter asks for so this app shows up under its
# own name (optional; harmless).
_APP_TITLE = "news-digest-agent"
_APP_REFERER = "https://github.com/jennymaeleidig/news-digest-agent"

# Conservative estimate of the fixed message banner overhead inside the prompt
# (``Today is <date>.`` + ``There are N items below…`` + ``Items:`` + the
# separator blank lines), used to keep the greedy budget-fitter safely under
# CURATION_PROMPT_MAX_BYTES. Over-estimating by ~200 bytes is intentional: it
# only makes the assembled prompt a little smaller than the cap.
_BANNER_OVERHEAD = 700


def _prompt_bytes(s: str) -> int:
    """UTF-8 byte length of a prompt fragment.

    The budget is measured in bytes, not characters, because a multibyte
    string of N characters can need up to 4N bytes in UTF-8 — and bytes are
    the honest measure of how close an assembled prompt gets to a fixed token
    budget on a worst-case (multibyte-heavy) day.
    """
    return len(s.encode("utf-8"))


class OpenRouterError(RuntimeError):
    """Raised when an OpenRouter API call fails to produce a digest.

    Propagates to main.py's curate_error handling, which turns it into the
    broken-agent email rather than a silent gap.
    """


@dataclass
class CurateResult:
    digest_markdown: str
    items_input: int
    items_output: int
    prompt_size: int = 0         # chars of the assembled prompt the model received
    model: str = ""              # the model id curation actually ran against
    prompt_tokens: int = 0       # sum of usage.prompt_tokens across section calls
    completion_tokens: int = 0   # sum of usage.completion_tokens across section calls
    # The Section each URL was actually picked into (url -> section name),
    # accumulated from stage-1's selections in declaration order by the
    # no-double-pick guard. URLs never picked are absent. Kept for
    # observability/debugging of the no-double-pick guard (ticket 08).
    picked_section_by_url: dict[str, str] = field(default_factory=dict)


def _request_headers(api_key: str) -> dict[str, str]:
    """Headers shared by every OpenRouter call (auth + app attribution)."""
    headers = {
        "Content-Type": "application/json",
        "X-Title": _APP_TITLE,
        "HTTP-Referer": _APP_REFERER,
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _pick_model() -> str:
    """Return the pinned curation model.

    The model is fixed rather than chosen per run: dynamic selection was
    removed after the discount ranking drifted between models and a sloppy
    sale-priced pick regressed digest quality. `OPENROUTER_MODEL` in the
    environment overrides the config default (config.OPENROUTER_MODEL), so the
    .env / repo secret is the single place the operator pins it.
    """
    return os.environ.get("OPENROUTER_MODEL") or OPENROUTER_MODEL



def _enrichment_for(it: Item, enrichments: dict[str, str]) -> str | None:
    """Return the pre-fetched full text attached to an item, if any.

    Enrichments are keyed by the URL that was actually fetched: an ordinary
    item by its own `url`, an external-linked item by its `linked_url` (the
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
    prompt after the snippet, so the model's only textual context comes from the
    pre-fetched plain text.
    """
    enrichments = enrichments or {}
    tier_by_source = tier_by_source or {}
    section_by_source = section_by_source or {}
    parts = [
        f"Today is {today}.",
        "",
        f"There are {len(items)} items below from the last 24 hours, "
        "after URL-level dedup against items already assigned to an earlier "
        "section of this digest. Each item "
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


def _sections_by_source(category: Category) -> dict[str, tuple[str, ...]]:
    """Map each configured source name to every Section it is mapped to.

    A multi-section source's items are offered as candidates in each of these
    Sections' per-section passes (see ``_group_by_section``); a source with a
    single mapped Section behaves exactly as before.
    """
    return {s.name: s.sections for s in category.sources if s.sections}


def _order_by_relevance(items: list[Item], category: Category) -> list[Item]:
    """Order items most-relevant-first for a stable numbered selection list.

    Relevance = Kagi trust tier first (lower tier number = stronger primary
    signal; an item's source maps to its category tier), then recency within a
    tier (newest first). Ordering no longer drops anything — stage-1 selection
    sees every item — it just gives the numbered list a sensible order.
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


def _group_by_section(
    items: list[Item],
    sections_by_source: dict[str, tuple[str, ...]],
    section_order: tuple[str, ...],
) -> dict[str, list[Item]]:
    """Offer items as candidates to every Section their source is mapped to.

    An item from a multi-section source is appended to *each* mapped Section's
    candidate list, so a strong item can be placed by stage-1 into any of its
    mapped Sections rather than being forced into one (the no-double-pick
    guard, ``_candidates_for_section``, keeps it out of the Sections it loses
    the race for). Returns a ``section -> items`` map in no particular
    grouping order (the caller walks ``section_order``). Items whose source
    maps to no known Section land in the last section (the catch-all),
    matching ``_reassemble_by_section``'s fallback.
    """
    groups: dict[str, list[Item]] = {s: [] for s in section_order}
    for it in items:
        mapped = [
            s for s in sections_by_source.get(it.source_name, ())
            if s in groups
        ]
        if not mapped:
            mapped = [section_order[-1]]
        for section in mapped:
            groups[section].append(it)
    return groups


def _candidates_for_section(
    section_items: list[Item],
    picked_urls: set[str],
) -> list[Item]:
    """The no-double-pick guard: drop items already picked in an earlier
    Section.

    Sections are curated in declared order, and ``picked_urls`` accumulates
    the URLs stage-1 actually selected in the Sections already processed, so a
    URL picked in an earlier Section is excluded from this Section's candidate
    set and one URL can never be selected into more than one Section of the
    same digest. Deterministic: same input items and picked set, same output
    list, in input order. This is pipeline logic, not the model's judgment —
    the model never sees (and so can never re-pick) an already-picked URL.
    """
    return [it for it in section_items if it.url not in picked_urls]


def _section_prompt(prompt_text: str, section: Section) -> str:
    """Scope the shared curation prompt to one digest section.

    Each section call curates exactly one section: it sees only that section's
    items and is told to return only that section's content as a flat list, so
    section coverage is an orchestration guarantee rather than a model's
    cross-section balancing judgment. The section's own definition (name +
    description) is appended so the model knows what belongs in it, but no
    other section's definition — each call only curates one section. Written
    positively (what to produce), because the curation prompt file already
    fixes the per-entry format.
    """
    return (
        f"You are curating the **{section.name}** section. Produce {section.name}"
        f" entries as a flat list (each an H3 title link plus its summary),"
        f" in rough importance order. The pipeline adds the section heading"
        f" and the source line. Return entries only for items that earn a"
        f" place in {section.name}.\n\n"
        + prompt_text
        + "\n\n"
        + "# Sections\n\n"
        + _section_blurb(section)
    )


def _selection_prompt(
    section: Section,
    items: list[Item],
    tier_by_source: dict[str, int],
    today: str,
    max_items: int,
) -> str:
    """Stage 1: ask the model to choose items by title alone.

    Every candidate's title + source + tier is listed (numbered); the model
    returns the numbers that earn a place, one per line. `max_items` is the
    per-section ceiling (section.max_items, or the global default) named in the
    instruction and hard-clipped by the caller after parsing. Titles-only keeps
    this call cheap and lets every candidate be seen — deterministic per-source
    cuts are gone, so a busy feed can no longer starve the others.
    """
    lines = [
        f"Today is {today}.",
        "",
        f"You are selecting the **{section.name}** section. Below are "
        f"{len(items)} items, numbered. Choose the items that genuinely earn "
        f"a place in **{section.name}** and return their numbers, one per "
        f"line, in importance order. At most {max_items}. "
        f"Return bare numbers only — no prose, no title text, no explanation.",
        "",
        "# Sections",
        "",
        _section_blurb(section),
        "",
        "Items:",
        "",
    ]
    for i, it in enumerate(items, start=1):
        tier = tier_by_source.get(it.source_name)
        tier_text = f" (tier {tier})" if tier is not None else ""
        lines.append(f"{i}. {it.title} — {it.source_name}{tier_text}")
    lines.append("")
    return "\n".join(lines)


def _parse_selection(text: str, candidate_count: int) -> list[int]:
    """Extract the model's stage-1 picks, in order, from a free-text reply.

    The model is asked for bare numbers, one per line; this tolerates bullets,
    commas, and light prose on each line, keeps the numbers in order and
    de-duplicated, and drops out-of-range values. A parsed-empty result means
    the model decided nothing earns a place (an empty section), not an error.
    """
    picked: list[int] = []
    seen: set[int] = set()
    for line in text.splitlines():
        for tok in re.findall(r"\d+", line):
            n = int(tok)
            if 1 <= n <= candidate_count and n not in seen:
                seen.add(n)
                picked.append(n)
    return picked


def _build_prompt(
    prompt_text: str,
    items: list[Item],
    today: str,
    enrichments: dict[str, str] | None = None,
    tier_by_source: dict[str, int] | None = None,
    section_by_source: dict[str, str] | None = None,
) -> tuple[str, int]:
    """Assemble the single user message the model receives, bounded to the budget.

    The category's curation prompt file provides the driving instructions; the
    day's items (with any pre-fetched full text attached) are appended as the
    material to summarize. Prompt text only — no tool definition, no network
    role for the model.

    Input is bounded so one curation call stays inside a model's context
    window and a sane daily budget. The budget is measured in UTF-8 bytes:
      - the item count is capped to CURATION_MAX_ITEMS (most-relevant first —
        the caller orders items via _order_by_relevance);
      - when the assembled prompt would exceed CURATION_PROMPT_MAX_BYTES, the
        cheapest material is dropped first — an item's full-text enrichment
        goes before the item itself, and the lowest-priority (trailing) items
        go last.

    Returns ``(prompt, items_sent)`` where ``items_sent`` is the number of
    items actually fed to the model (≤ CURATION_MAX_ITEMS), so the caller can
    report the true fed count in the run log.
    """
    enrichments = enrichments or {}
    tier_by_source = tier_by_source or {}
    section_by_source = section_by_source or {}
    items = items[:CURATION_MAX_ITEMS]

    budget = CURATION_PROMPT_MAX_BYTES - _prompt_bytes(prompt_text) - _BANNER_OVERHEAD
    chosen: list[tuple[Item, str | None]] = []   # (item, enrichment or None)
    used = 0
    index = 1
    for it in items:
        enr = _enrichment_for(it, enrichments)
        tier = tier_by_source.get(it.source_name)
        section = section_by_source.get(it.source_name)
        with_len = _prompt_bytes("\n".join(_item_lines(it, index, enr, tier, section)))
        bare_len = _prompt_bytes("\n".join(_item_lines(it, index, None, tier, section)))
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


def _section_blurb(sec: Section) -> str:
    """Render one section's definition for the curation prompt.

    Section name and description are injected from the category config rather
    than hardcoded in the prompt file, so the JSON stays the single source of
    truth for what belongs in each section.
    """
    if sec.description:
        return f"- **{sec.name}** \u2014 {sec.description}"
    return f"- **{sec.name}**"


def _sections_blurb(sections: tuple[Section, ...]) -> str:
    """Render the category's section definitions for the curation prompt.

    Section names, descriptions, and order are injected here from the category
    config rather than hardcoded in the prompt file, so the JSON stays the
    single source of truth for what sections exist, what belongs in each, and
    their digest order. This renders every section at once; the curator uses
    ``_section_blurb`` to pass only the current section to each per-section
    call. Kept as the single entry on the assumption a future caller may want
    the full set (e.g. an overview section); do not reintroduce it into the
    per-section path.
    """
    lines = ["# Sections", ""]
    for sec in sections:
        lines.append(_section_blurb(sec))
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

    Section placement is a pipeline guarantee, not a model choice: the model's
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
    # Normalized-title index: the model is told to copy titles verbatim, but
    # in practice it often normalizes whitespace or casing — and arXiv titles
    # in particular are long enough to drift. Fall back to a whitespace-
    # collapsed, lowercased key so a mildly-drifted title still reconciles
    # (the digest still renders the canonical input title/URL, never the
    # model's).
    def _norm_title(t: str) -> str:
        return " ".join(t.split()).lower()
    by_title_norm = {_norm_title(it.title): it for it in items}
    # Section names double as the model's `## <Section>` headers, which are not
    # items — an unreconciled heading that matches one is ignored, not treated
    # as a dropped item.
    section_names = {name.strip().lower() for name in section_order}

    def _strip_lead_sep(t: str) -> str:
        return re.sub(r"^[\u2014\u2013-]\s*", "", t).strip()

    # Resolve a model-emitted (title, url) to an input item. `url` is empty for
    # link-less headings. Matching falls back exact -> URL -> whitespace/case-
    # normalized, then to "the title part before the first colon/em/en dash"
    # (the common `### Title: summary` drift). Rendering is always the canonical
    # input title/URL — a fuzzy match can never leak model text into the digest.
    def _resolve_title(title: str, url: str) -> Item | None:
        it = by_title.get(title) or by_url.get(url) or by_title_norm.get(_norm_title(title))
        if it is not None:
            return it
        head = re.split(r"\s*[:\u2014\u2013]\s*", title, maxsplit=1)[0].strip()
        if head and head != title:
            return by_title_norm.get(_norm_title(head))
        return None

    # Reconcile a link-less heading (the model dropped the markdown link and
    # wrote `### Title summary…` instead of `### [Title](url) summary…`). Only a
    # heading that names one input item becomes an item: exact/normalized title,
    # a `Title` prefix before one colon/dash, or — as a last resort — a heading
    # whose text prepends a summary to a verbatim input title. The remainder
    # glued onto the title is returned as the leading summary. Anything else (a
    # `##` sub-header the model invented) is ignored.
    def _resolve_bare_heading(text: str) -> tuple[Item, str] | None:
        text = text.strip()
        if len(text) >= 2 and text[0] == "[" and text[-1] == "]":
            text = text[1:-1].strip()
        it = _resolve_title(text, "")
        if it is not None:
            return it, ""
        ntext = _norm_title(text)
        if not ntext:
            return None
        # Last resort: the model glued a summary onto a verbatim title without
        # any separator. Prefer the longest (most specific) input title that
        # prefixes the heading — "GLM-5.3-Flash" beats "GLM-5.3" — so a
        # summary-glued heading still lands on the right item.
        matches = [
            it for it in items
            if ntext.startswith(_norm_title(it.title))
            and _norm_title(it.title) != ntext
        ]
        if not matches:
            return None
        it = max(matches, key=lambda it: len(it.title))
        return it, _strip_lead_sep(text[len(it.title):].strip())

    # Captures an optional tail after the link: the model often glues the first
    # summary sentence onto the heading line ("### [T](url) — summary").
    link_heading = re.compile(r"^#{1,4}\s+\[(.+)\]\(([^)]*)\)\s*(.*)$")
    # Any other heading (the model's `##` sub-headers, or a link-dropped item
    # heading) — captured for item reconciliation, then either matched or ignored.
    bare_heading = re.compile(r"^#{1,4}\s+(.*?)\s*$")
    source_line = re.compile(r"^\s*\*Source:")

    # (input item, model-written summary) pairs, in the model's output order.
    parsed: list[tuple[Item, str]] = []
    current: Item | None = None
    body: list[str] = []
    for line in markdown.splitlines():
        m = link_heading.match(line)
        if m:
            if current is not None:
                parsed.append((current, _trim_summary_lines(body)))
            current = _resolve_title(m.group(1), m.group(2))
            body = []
            trailing = _strip_lead_sep(m.group(3) or "")
            if trailing:
                body.append(trailing)
            continue

        b = bare_heading.match(line)
        if b:
            heading_text = b.group(1)
            resolved = _resolve_bare_heading(heading_text)
            if resolved is not None:
                if current is not None:
                    parsed.append((current, _trim_summary_lines(body)))
                current, trailing = resolved
                body = []
                if trailing:
                    body.append(trailing)
            elif heading_text.strip().lower() not in section_names:
                # An item heading whose title doesn't reconcile (non-verbatim or
                # invented): close the item above so the orphan body doesn't
                # bleed into it, and drop it rather than fabricating a title.
                if current is not None:
                    parsed.append((current, _trim_summary_lines(body)))
                current = None
                body = []
            # else: a section header — ignored, the open item stays open.
            continue

        if current is not None and not source_line.match(line):
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
            out.append("")
            if summary:
                out.append(summary)
                out.append("")
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


def _snippet(text: str, limit: int = 500) -> str:
    """Collapse whitespace and trim an error body for the run log."""
    text = " ".join((text or "").split())
    return text[:limit] + ("…" if len(text) > limit else "")


def _run_model(prompt: str, model: str) -> tuple[str, dict]:
    """Run one OpenRouter chat completion per section; return (content, usage).

    Raises OpenRouterError on a missing key, a timeout, a non-200 status, or a
    malformed response, isolating this one section call's failure to the
    caller. The response `usage` block is returned alongside the content so
    the caller can sum token accounting across sections.
    """
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise OpenRouterError("OPENROUTER_API_KEY is not set")

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        resp = requests.post(
            f"{OPENROUTER_BASE_URL}/chat/completions",
            headers=_request_headers(api_key),
            json=payload,
            timeout=OPENROUTER_TIMEOUT_SECONDS,
        )
    except requests.exceptions.Timeout:
        raise OpenRouterError(
            f"OpenRouter timed out after {OPENROUTER_TIMEOUT_SECONDS}s"
        ) from None
    except requests.RequestException as e:
        raise OpenRouterError(f"OpenRouter request failed: {e}") from None

    if resp.status_code != 200:
        body = _snippet(resp.text)
        hint = {
            401: "check OPENROUTER_API_KEY",
            402: "add credits to the OpenRouter account",
            404: "unknown model id",
            429: "rate-limited or out of provider credits",
        }.get(resp.status_code)
        msg = f"OpenRouter HTTP {resp.status_code}"
        if hint:
            msg += f" ({hint})"
        if body:
            msg += f": {body}"
        raise OpenRouterError(msg)

    try:
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage") or {}
    except (ValueError, KeyError, IndexError, TypeError):
        raise OpenRouterError("OpenRouter returned a malformed response") from None
    return content or "", usage


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
    """Run curation for one category via a two-stage, per-section OpenRouter pass.

    One pinned model (config.OPENROUTER_MODEL, overridden by OPENROUTER_MODEL)
    is used for every call. Section coverage is an orchestration guarantee: an
    item is offered as a candidate to every Section its source is mapped to,
    and each section's candidates are curated in two stages — stage 1 selects
    the items that earn a place from titles alone (so every candidate is seen
    and no source is starved), the selected subset is then deep-read
    (pre-fetch), and stage 2 summarizes and formats those enriched items. A
    deterministic no-double-pick guard keeps one URL in exactly one Section of
    the digest: a URL picked in an earlier Section is excluded from every
    later Section's candidate set. The
    per-section outputs are post-processed deterministically (verbatim titles,
    canonical source/tier lines, canonical section order) and concatenated.
    Raises OpenRouterError if any call fails; main.py turns that into the
    broken-agent email.
    """
    if today is None:
        today = datetime.now(timezone.utc).date().isoformat()

    if not items:
        return _empty_result()

    # Items are relevance-ordered (tier, then recency) for a stable numbered
    # list, but that ordering never drops anything — stage 1 sees every item,
    # so a busy feed cannot starve another source. The pre-fetch (deep-read)
    # runs between stage 1 and stage 2, over only the selected subset: stage 1
    # selects from titles and needs no enrichment, so deep-reading every
    # candidate would be wasted fetches on items that never reach stage 2.
    ordered = _order_by_relevance(items, category)
    prompt_text = category.prompt_path.read_text(encoding="utf-8")
    # No global section blurb here: each per-section call appends only its own
    # section's definition (see _section_prompt) since a call curates exactly
    # one section — other sections' descriptions would be noise.
    tier_by_source = _tier_by_source(category)
    section_order = tuple(sec.name for sec in category.sections)
    groups = _group_by_section(
        ordered, _sections_by_source(category), section_order,
    )
    # The no-double-pick guard's state: URLs stage-1 has already selected in
    # the Sections processed so far (declared order). Once picked, an item is
    # excluded from every later Section's candidate set. The companion map
    # records *where* each URL was picked — the map and the set always agree.
    picked_urls: set[str] = set()
    picked_section_by_url: dict[str, str] = {}

    # One pinned model for every call so the digest reflects a single model's
    # selection, not a rotating cast.
    model = _pick_model()
    print(f"[{category.id}] curating with {model} (two-stage)", file=sys.stderr, flush=True)

    digest_parts: list[str] = []
    total_candidates = 0
    total_chars = 0
    total_prompt_tokens = 0
    total_completion_tokens = 0
    for section in category.sections:
        section_items = _candidates_for_section(
            groups[section.name], picked_urls,
        )
        if not section_items:
            continue

        # Stage 1 — select from titles alone. Every candidate is seen; the
        # model's picks are mapped back to items by ordinal position and
        # hard-clipped to the section's ceiling (section.max_items, or the
        # global CURATION_SELECT_MAX_ITEMS fallback).
        max_items = section.max_items or CURATION_SELECT_MAX_ITEMS
        total_candidates += len(section_items)
        print(
            f"[{category.id}]   {section.name}: [stage 1] selecting from "
            f"{len(section_items)} items (max {max_items})…",
            file=sys.stderr, flush=True,
        )
        select_prompt = _selection_prompt(
            section, section_items, tier_by_source, today, max_items,
        )
        select_raw, select_usage = _run_model(select_prompt, model)
        total_prompt_tokens += int(select_usage.get("prompt_tokens") or 0)
        total_completion_tokens += int(select_usage.get("completion_tokens") or 0)
        total_chars += len(select_prompt)
        picks = _parse_selection(select_raw, len(section_items))[:max_items]
        selected = [section_items[i - 1] for i in picks]
        picked_urls.update(it.url for it in selected)
        picked_section_by_url.update((it.url, section.name) for it in selected)
        print(
            f"[{category.id}]   {section.name}: [stage 1] done — selected "
            f"{len(selected)}/{len(section_items)}",
            file=sys.stderr, flush=True,
        )
        if not selected:
            print(
                f"warn: [{category.id}] section {section!r}: stage 1 selected "
                f"nothing from {len(section_items)} items; section skipped.",
                file=sys.stderr,
            )
            continue

        # Enrichment — deep-read full text for the selected subset only,
        # between stage 1 and stage 2. Stage 1 picks from titles and needs no
        # enrichment; deep-reading candidates stage 1 would drop is wasted.
        section_prefetch = prefetch(selected, category.sources)

        # Stage 2 — summarize + format only the selected subset.
        print(
            f"[{category.id}]   {section.name}: [stage 2] summarizing "
            f"{len(selected)} items…",
            file=sys.stderr, flush=True,
        )
        # This call curates exactly one Section, so every candidate it sees is
        # rendered into *that* Section — a multi-section source's item lands
        # under the Section that picked it, never its source's first mapping.
        call_section_by_source = {
            s.name: section.name
            for s in category.sources if section.name in s.sections
        }
        prompt, _ = _build_prompt(
            _section_prompt(prompt_text, section),
            selected, today, section_prefetch.enrichments,
            tier_by_source, call_section_by_source,
        )
        raw_md, usage = _run_model(prompt, model)
        total_prompt_tokens += int(usage.get("prompt_tokens") or 0)
        total_completion_tokens += int(usage.get("completion_tokens") or 0)
        total_chars += len(prompt)
        section_md = _postprocess(
            raw_md, selected, tier_by_source, call_section_by_source,
            section_order,
        )
        if section_md:
            digest_parts.append(section_md)
        elif raw_md.strip():
            sample = " ".join(raw_md.split())[:400]
            print(
                f"warn: [{category.id}] section {section!r}: the model returned "
                f"{len(raw_md)} chars but none matched an input item; dropped. "
                f"sample head: {sample!r}",
                file=sys.stderr,
            )
        else:
            print(
                f"warn: [{category.id}] section {section!r}: the model returned "
                f"nothing for {len(selected)} selected items; section skipped.",
                file=sys.stderr,
            )
        print(
            f"[{category.id}]   {section.name}: [stage 2] done — "
            f"{_count_items_in_digest(section_md)} items in digest",
            file=sys.stderr, flush=True,
        )

    digest_markdown = "\n\n".join(digest_parts)
    return CurateResult(
        digest_markdown=digest_markdown,
        items_input=total_candidates,
        items_output=_count_items_in_digest(digest_markdown),
        prompt_size=total_chars,
        model=model,
        prompt_tokens=total_prompt_tokens,
        completion_tokens=total_completion_tokens,
        picked_section_by_url=picked_section_by_url,
    )
