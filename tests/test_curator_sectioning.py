"""Ticket 07 — multi-section candidate offering + no-double-pick guard.

Seam: the curator's deterministic per-section grouping and selection unit
(``curator._group_by_section``, ``curator._candidates_for_section``, and the
per-section loop inside ``curator.curate``). All tests are offline: synthetic
items and section maps drive the pure functions directly, and the end-to-end
tests monkeypatch ``curator._run_model`` and ``curator.prefetch`` with in-memory
fakes — the model is never consulted for placement or de-duplication, which is
exactly the behavior under test.

Behavior under test:
  - an item from a multi-section source is offered as a candidate to every
    Section that source is mapped to (each per-section stage-1 pass sees it);
  - the no-double-pick guard excludes a URL picked in an earlier Section from
    the candidate set of every later Section, so one URL lands in exactly one
    Section of the digest;
  - an item renders under the Section that actually picked it, not its
    source's first declared section.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

from conftest import REPO_ROOT

import curator
from categories import Category, Section, Source
from curator import (
    _candidates_for_section,
    _group_by_section,
    _sections_by_source,
)
from fetchers.common import Item


# ---------------------------------------------------------------------------
# Synthetic category / item builders (no config files, no network)
# ---------------------------------------------------------------------------
SECTION_ORDER = ("Alpha", "Beta", "Gamma")

SECTIONS = (
    Section("Alpha", "alpha scope"),
    Section("Beta", "beta scope"),
    Section("Gamma", "gamma scope"),
)

SINGLE = Source(
    name="single", tier=2, kind="rss", url="https://example.com/single",
    sections=("Beta",),
)
MULTI = Source(
    name="multi", tier=2, kind="rss", url="https://example.com/multi",
    sections=("Alpha", "Beta"),
)
STRAY = Source(
    name="stray", tier=3, kind="rss", url="https://example.com/stray",
    sections=("Gamma",),
)


def make_category(sources) -> Category:
    return Category(
        id="test-cat",
        name="Test",
        schedule="",
        recipient=None,
        prompt="prompts/test.md",
        prompt_path=REPO_ROOT / "categories" / "prompts" / "ai-ml.md",
        sources=tuple(sources),
        sections=SECTIONS,
    )


def make_item(title: str, url: str, source_name: str) -> Item:
    return Item(
        title=title,
        source_name=source_name,
        url=url,
        published="2099-01-01T00:00:00+00:00",
        content_snippet="snippet",
    )


def items_a1_a2_b1():
    """a1/a2 from the multi-section source (Alpha+Beta), b1 from a
    Beta-only source — so after Alpha picks a1/a2, Beta still has b1."""
    return [
        make_item("Alpha story", "https://example.com/a1", "multi"),
        make_item("Second story", "https://example.com/a2", "multi"),
        make_item("Solo Beta story", "https://example.com/b1", "single"),
    ]


# ---------------------------------------------------------------------------
# 1. Multi-section offering: each per-section pass sees the item
# ---------------------------------------------------------------------------
class TestGroupBySection:
    def test_multi_section_item_offered_to_every_mapped_section(self):
        groups = _group_by_section(
            items_a1_a2_b1(), _sections_by_source(make_category([MULTI, SINGLE])),
            SECTION_ORDER,
        )
        assert [it.url for it in groups["Alpha"]] == [
            "https://example.com/a1", "https://example.com/a2",
        ]
        assert [it.url for it in groups["Beta"]] == [
            "https://example.com/a1", "https://example.com/a2",
            "https://example.com/b1",
        ]

    def test_single_section_item_appears_in_exactly_one_group(self):
        groups = _group_by_section(
            items_a1_a2_b1(), _sections_by_source(make_category([MULTI, SINGLE])),
            SECTION_ORDER,
        )
        appearances = [
            (s, it.url) for s in SECTION_ORDER for it in groups[s]
            if it.url == "https://example.com/b1"
        ]
        assert appearances == [("Beta", "https://example.com/b1")]

    def test_unmapped_source_falls_back_to_last_section(self):
        it = make_item("Stray story", "https://example.com/s1", "stray")
        groups = _group_by_section(
            [it], _sections_by_source(make_category([STRAY])), SECTION_ORDER,
        )
        assert [it.url for it in groups["Gamma"]] == ["https://example.com/s1"]
        assert all(not groups[s] for s in ("Alpha", "Beta"))

    def test_sections_by_source_maps_every_declared_section(self):
        mapping = _sections_by_source(make_category([SINGLE, MULTI]))
        assert mapping["single"] == ("Beta",)
        assert mapping["multi"] == ("Alpha", "Beta")


# ---------------------------------------------------------------------------
# 2. The no-double-pick guard (pure, deterministic)
# ---------------------------------------------------------------------------
class TestNoDoublePickGuard:
    def test_url_picked_in_earlier_section_excluded_from_later_candidates(self):
        candidates = items_a1_a2_b1()
        picked = {"https://example.com/a1"}
        kept = _candidates_for_section(candidates, picked)
        assert [it.url for it in kept] == [
            "https://example.com/a2", "https://example.com/b1",
        ]

    def test_no_picks_yet_returns_full_candidate_set(self):
        candidates = items_a1_a2_b1()
        assert _candidates_for_section(candidates, set()) == candidates

    def test_guard_is_deterministic(self):
        candidates = items_a1_a2_b1()
        picked = {"https://example.com/a2", "https://example.com/b1"}
        first = _candidates_for_section(candidates, picked)
        second = _candidates_for_section(candidates, picked)
        assert [it.url for it in first] == [it.url for it in second]
        assert [it.url for it in first] == ["https://example.com/a1"]


# ---------------------------------------------------------------------------
# 3. End-to-end through curate() with a scripted model (still offline)
# ---------------------------------------------------------------------------
class FakeModel:
    """Stage-1 replies come from ``picks_by_section`` (section name -> 1-based
    pick numbers); stage-2 echoes every candidate back as a digest entry, so
    anything selected is guaranteed to reach the digest. Stage-1 prompts are
    recorded so tests can assert exactly which candidates each pass saw."""

    def __init__(self, picks_by_section=None):
        self.picks_by_section = picks_by_section or {}
        self.select_prompts: list[str] = []

    def __call__(self, prompt: str, model: str):
        m = re.search(r"You are selecting the \*\*(.+?)\*\* section", prompt)
        if m:
            section = m.group(1)
            self.select_prompts.append((section, prompt))
            picks = self.picks_by_section.get(section, [])
            return "\n".join(str(n) for n in picks), {}
        # Stage 2: echo every Title/URL pair back as a digest entry.
        titles = re.findall(r"^Title: (.+)$", prompt, re.M)
        urls = re.findall(r"^URL: (.+)$", prompt, re.M)
        return "\n".join(
            f"### [{t}]({u})\n\nSummary." for t, u in zip(titles, urls)
        ), {}


def patch_pipeline(monkeypatch, fake_model):
    monkeypatch.setattr(curator, "_run_model", fake_model)
    monkeypatch.setattr(
        curator, "prefetch",
        lambda selected, sources: SimpleNamespace(enrichments={}),
    )


class TestCurateOfferingAndGuard:
    def test_offered_to_every_mapped_section_when_not_picked_early(self, monkeypatch):
        """The multi-section item is a candidate in both Alpha and Beta when
        the earlier pass doesn't pick it."""
        fake = FakeModel(picks_by_section={})
        patch_pipeline(monkeypatch, fake)
        result = curator.curate(
            items_a1_a2_b1(), make_category([MULTI, SINGLE]), today="2099-01-01",
        )
        by_section = dict(fake.select_prompts)
        assert "Alpha story" in by_section["Alpha"]
        assert "Alpha story" in by_section["Beta"]
        assert result.digest_markdown == ""

    def test_picked_url_excluded_from_later_section_and_lands_once(
        self, monkeypatch,
    ):
        """a1 picked into Alpha never appears in Beta's candidate set and
        appears exactly once in the digest."""
        fake = FakeModel(picks_by_section={"Alpha": [1, 2], "Beta": [1]})
        patch_pipeline(monkeypatch, fake)
        result = curator.curate(
            items_a1_a2_b1(), make_category([MULTI, SINGLE]), today="2099-01-01",
        )
        by_section = dict(fake.select_prompts)
        assert "Alpha story" in by_section["Alpha"]
        assert "Alpha story" not in by_section["Beta"]
        # Beta still saw its own surviving candidate (b1) and picked it.
        assert "Solo Beta story" in by_section["Beta"]
        # No double-pick: a1 exactly once, in Alpha; b1 in Beta.
        assert result.digest_markdown.count("https://example.com/a1") == 1
        alpha_block = result.digest_markdown.split("## Beta")[0]
        beta_block = "## Beta" + result.digest_markdown.split("## Beta", 1)[1]
        assert "### [Alpha story]" in alpha_block
        assert "### [Alpha story]" not in beta_block
        assert "### [Solo Beta story]" in beta_block
        assert "### [Solo Beta story]" not in alpha_block

    def test_lands_in_second_section_when_only_there_it_earns_a_place(
        self, monkeypatch,
    ):
        """When Alpha picks nothing, a1 is still unpicked and offered to
        Beta; picked there, it must render under Beta — the section that
        picked it — not under its source's first declared section."""
        fake = FakeModel(picks_by_section={"Alpha": [], "Beta": [1]})
        patch_pipeline(monkeypatch, fake)
        result = curator.curate(
            items_a1_a2_b1(), make_category([MULTI, SINGLE]), today="2099-01-01",
        )
        alpha_block = result.digest_markdown.split("## Beta")[0]
        beta_block = "## Beta" + result.digest_markdown.split("## Beta", 1)[1]
        assert "### [" not in alpha_block
        assert "### [Alpha story]" in beta_block
        assert "### [Alpha story]" not in alpha_block

    def test_deterministic_across_runs(self, monkeypatch):
        fake = FakeModel(picks_by_section={"Alpha": [1, 2], "Beta": [1]})
        patch_pipeline(monkeypatch, fake)
        first = curator.curate(
            items_a1_a2_b1(), make_category([MULTI, SINGLE]), today="2099-01-01",
        ).digest_markdown
        fake2 = FakeModel(picks_by_section={"Alpha": [1, 2], "Beta": [1]})
        patch_pipeline(monkeypatch, fake2)
        second = curator.curate(
            items_a1_a2_b1(), make_category([MULTI, SINGLE]), today="2099-01-01",
        ).digest_markdown
        assert first == second
