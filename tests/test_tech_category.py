"""Ticket 04 — Tech category drop-in config and prompt.

Seam: the category load/validate unit module (``categories.load_category``)
over the real shipped ``categories/tech.json`` + ``categories/prompts/tech.md``,
mirroring how ticket 01's suite exercises the real ai-ml config. All tests are
deterministic and offline; no network is touched (verifying the three pinned
feed URLs resolve is a CI/operator live-smoke concern, per the spec's Testing
Decisions).

The spec pins the exact source table (tier, kind, URL, homepage, section
mapping) and the section scope-line prose (spec.md, "Prompt scope-line
prose" — the descriptions are the single source of truth for what belongs in
each section).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from categories import CategoryError, load_category


TECH_CONFIG = Path("categories/tech.json")


@pytest.fixture(scope="module")
def tech():
    return load_category(TECH_CONFIG)


# The spec's settled scope-line prose (spec.md → Prompt scope-line prose →
# Tech). These strings are the single source of truth for section scope.
SCOPE_LINES = {
    "Industry news": (
        "The tech industry as it moves — companies, products, platforms, "
        "regulation, labor, and the people running them; industry news and "
        "events, more than individual product releases or pure business "
        "dealings."
    ),
    "Events": (
        "Time-bound tech happenings — conferences, summits, product events, "
        "hearings, and deadlines — and the notable moments that come out of "
        "them, distinct from ongoing industry coverage."
    ),
}

# The spec's pinned source table (spec.md → Sources → category → sections).
EXPECTED_SOURCES = {
    "404media": {
        "tier": 2, "kind": "rss", "url": "https://www.404media.co/rss/",
        "homepage": "https://www.404media.co/",
        "sections": ["Industry news"],
    },
    "usermag": {
        "tier": 2, "kind": "rss", "url": "https://www.usermag.co/feed",
        "homepage": "https://www.usermag.co/",
        "sections": ["Industry news"],
    },
    "aftermath": {
        "tier": 3, "kind": "rss", "url": "https://aftermath.site/rss/",
        "homepage": "https://aftermath.site/",
        "sections": ["Industry news"],
    },
}


class TestConfigLoads:
    def test_tech_config_loads_against_the_multi_section_schema(self, tech):
        assert tech.id == "tech"
        assert tech.name

    def test_sections_non_empty_and_ordered(self, tech):
        assert [s.name for s in tech.sections] == ["Industry news", "Events"]


class TestSectionScopeLines:
    def test_each_section_carries_its_scope_line(self, tech):
        for section in tech.sections:
            assert section.description == SCOPE_LINES[section.name]

    def test_scope_lines_distinguish_the_sections(self, tech):
        by_name = {s.name: s.description for s in tech.sections}
        assert "releases or pure business" in by_name["Industry news"]
        assert "Time-bound" in by_name["Events"]


class TestSources:
    def test_every_spec_source_is_present(self, tech):
        assert {s.name for s in tech.sources} == set(EXPECTED_SOURCES)

    def test_every_source_carries_its_spec_metadata(self, tech):
        for source in tech.sources:
            expected = EXPECTED_SOURCES[source.name]
            assert source.tier == expected["tier"], source.name
            assert source.kind == expected["kind"], source.name
            assert source.url == expected["url"], source.name
            assert source.homepage == expected["homepage"], source.name
            assert list(source.sections) == expected["sections"], source.name

    def test_sources_validate_only_against_techs_sections(self, tech):
        declared = {s.name for s in tech.sections}
        for source in tech.sources:
            assert set(source.sections) <= declared


class TestCrossCategorySources:
    def test_aftermath_declares_only_its_tech_mapping(self, tech):
        aftermath = next(s for s in tech.sources if s.name == "aftermath")
        assert aftermath.sections == ("Industry news",)

    def test_usermag_declares_only_its_tech_mapping(self, tech):
        usermag = next(s for s in tech.sources if s.name == "usermag")
        assert usermag.sections == ("Industry news",)


class TestPromptFile:
    def test_prompt_is_a_category_level_scope_with_no_per_section_block(
            self, tech):
        prompt_text = tech.prompt_path.read_text(encoding="utf-8")
        # Category-level scope present...
        assert "# Scope" in prompt_text
        # ...but no per-section block: the pipeline injects each section's
        # name + description (ai-ml prompt carries no "# Sections" block
        # either — the injector renders it per call).
        assert "# Sections" not in prompt_text
        for section in tech.sections:
            headings = [
                line for line in prompt_text.splitlines()
                if line.lstrip().startswith("#") and section.name in line
            ]
            assert not headings, (
                f"prompt carries a per-section block for {section.name!r}"
            )


class TestUnlistedSectionRejected:
    def test_tech_unlisted_section_name_is_rejected_path_qualified(
            self, tech, tmp_path):
        data = json.loads(TECH_CONFIG.read_text(encoding="utf-8"))
        data["sources"][0]["sections"] = ["video game news"]  # not a Tech section
        # Real sibling prompt so the failure is the section validation, not
        # a missing prompt file.
        (tmp_path / "prompts").mkdir()
        (tmp_path / "prompts" / "tech.md").write_text(
            tech.prompt_path.read_text(encoding="utf-8"), encoding="utf-8")
        config_path = tmp_path / "tech.json"
        config_path.write_text(json.dumps(data), encoding="utf-8")

        with pytest.raises(CategoryError) as excinfo:
            load_category(config_path)
        msg = str(excinfo.value)
        assert "tech.json" in msg                    # path-qualified
        assert "video game news" in msg              # names the bad value
        assert "Industry news" in msg                # names the valid choices
