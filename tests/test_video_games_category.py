"""Ticket 05 — Video games category drop-in config and prompt.

Seam: the category load/validate unit module (``categories.load_category``)
over the real shipped ``categories/video-games.json`` +
``categories/prompts/video-games.md``, mirroring the tech-category suite
(ticket 04). All tests are deterministic and offline; no network is touched
(verify the aftermath feed and the keyless per-channel YouTube Atom feed
resolve is a CI/operator live-smoke concern, per the spec's Testing
Decisions — the channel id and feed URL were verified live at spec time).

The spec pins the exact source table and the section scope-line prose
(spec.md, "Prompt scope-line prose" — Video games table; "Sources →
category → sections").
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from categories import CategoryError, load_category


VG_CONFIG = Path("categories/video-games.json")
TECH_CONFIG = Path("categories/tech.json")

# Spec-verified Jason Schreier channel id (ticket 02 decision 4).
SCHREIER_CHANNEL_ID = "UCQoOmu6mKZkXTnwZcpD8Ciw"

# The spec's settled scope-line prose (spec.md → Prompt scope-line prose →
# Video games). These strings are the single source of truth for section
# scope.
SCOPE_LINES = {
    "video game news": (
        "The games industry and medium as a beat — studios, publishers, "
        "platforms, labor, regulation, and game culture — industry news, "
        "excluding individual release announcements."
    ),
    "video game releases": (
        "Releases as a beat — new-game and expansion announcements, launch "
        "dates, previews, hands-on impressions, and major updates or "
        "patches: what is coming out and when it lands."
    ),
}

# The spec's pinned source table (spec.md → Sources → category → sections).
EXPECTED_SOURCES = {
    "aftermath": {
        "tier": 3, "kind": "rss", "url": "https://aftermath.site/rss/",
        "homepage": "https://aftermath.site/",
        "sections": ["video game news", "video game releases"],
    },
    "jason-schreier": {
        "tier": 4, "kind": "youtube", "url": SCHREIER_CHANNEL_ID,
        "homepage": None,
        "sections": ["video game news"],
    },
}


@pytest.fixture(scope="module")
def vg():
    return load_category(VG_CONFIG)


class TestConfigLoads:
    def test_video_games_config_loads_against_the_multi_section_schema(
            self, vg):
        assert vg.id == "video-games"
        assert vg.name

    def test_sections_non_empty_and_ordered(self, vg):
        assert [s.name for s in vg.sections] == [
            "video game news", "video game releases",
        ]

    def test_schedule_carries_the_staggered_slot(self, vg):
        # Spec: Video games runs at 09:00 UTC in the staggered base
        # (kept-but-ignored here; ticket 12's workflows own the cron).
        assert vg.schedule == "0 9 * * *"


class TestSectionScopeLines:
    def test_each_section_carries_its_scope_line(self, vg):
        for section in vg.sections:
            assert section.description == SCOPE_LINES[section.name]

    def test_scope_lines_distinguish_news_from_releases(self, vg):
        by_name = {s.name: s.description for s in vg.sections}
        assert "excluding individual release announcements" in (
            by_name["video game news"])
        assert "what is coming out and when it lands" in (
            by_name["video game releases"])


class TestSources:
    def test_every_spec_source_is_present(self, vg):
        assert {s.name for s in vg.sources} == set(EXPECTED_SOURCES)

    def test_every_source_carries_its_spec_metadata(self, vg):
        for source in vg.sources:
            expected = EXPECTED_SOURCES[source.name]
            assert source.tier == expected["tier"], source.name
            assert source.kind == expected["kind"], source.name
            assert source.url == expected["url"], source.name
            assert source.homepage == expected["homepage"], source.name
            assert list(source.sections) == expected["sections"], source.name

    def test_sources_validate_only_against_video_games_sections(self, vg):
        declared = {s.name for s in vg.sections}
        for source in vg.sources:
            assert set(source.sections) <= declared

    def test_youtube_source_is_listed_keyless(self, vg):
        schreier = next(
            s for s in vg.sources if s.name == "jason-schreier")
        # Keyless: the bare channel id (not a feed URL or API endpoint), no
        # homepage, and no API key material anywhere in the source.
        assert schreier.url == SCHREIER_CHANNEL_ID
        assert not schreier.url.startswith("http")
        assert schreier.homepage is None


class TestMultiSectionSource:
    def test_aftermath_feeds_both_sections(self, vg):
        aftermath = next(s for s in vg.sources if s.name == "aftermath")
        assert aftermath.sections == ("video game news", "video game releases")
        assert aftermath.section == "video game news"  # first of the list


class TestCrossCategoryCoexistence:
    def test_aftermath_declares_a_different_list_per_category(self):
        tech = load_category(TECH_CONFIG)
        games = load_category(VG_CONFIG)
        tech_aftermath = next(s for s in tech.sources if s.name == "aftermath")
        games_aftermath = next(
            s for s in games.sources if s.name == "aftermath")
        # One section list per category: Tech maps aftermath to Industry
        # news only; Video games maps it to both games sections.
        assert tech_aftermath.sections == ("Industry news",)
        assert games_aftermath.sections == (
            "video game news", "video game releases")

    def test_each_config_validates_only_against_its_own_sections(self):
        tech = load_category(TECH_CONFIG)
        games = load_category(VG_CONFIG)
        tech_sections = {s.name for s in tech.sections}
        games_sections = {s.name for s in games.sections}
        for source in tech.sources:
            assert set(source.sections) <= tech_sections
        for source in games.sources:
            assert set(source.sections) <= games_sections


class TestPromptFile:
    def test_prompt_is_a_category_level_scope_with_no_per_section_block(
            self, vg):
        prompt_text = vg.prompt_path.read_text(encoding="utf-8")
        # Category-level scope present...
        assert "# Scope" in prompt_text
        # ...but no per-section block: the pipeline injects each section's
        # name + description.
        assert "# Sections" not in prompt_text
        for section in vg.sections:
            headings = [
                line for line in prompt_text.splitlines()
                if line.lstrip().startswith("#") and section.name in line
            ]
            assert not headings, (
                f"prompt carries a per-section block for {section.name!r}"
            )


class TestUnlistedSectionRejected:
    def test_video_games_unlisted_section_name_is_rejected_path_qualified(
            self, vg, tmp_path):
        data = json.loads(VG_CONFIG.read_text(encoding="utf-8"))
        aftermath = next(
            s for s in data["sources"] if s["name"] == "aftermath")
        aftermath["sections"] = ["Industry news"]   # Tech's, not ours
        # Real sibling prompt so the failure is the section validation, not
        # a missing prompt file.
        (tmp_path / "prompts").mkdir()
        (tmp_path / "prompts" / "video-games.md").write_text(
            vg.prompt_path.read_text(encoding="utf-8"), encoding="utf-8")
        config_path = tmp_path / "video-games.json"
        config_path.write_text(json.dumps(data), encoding="utf-8")

        with pytest.raises(CategoryError) as excinfo:
            load_category(config_path)
        msg = str(excinfo.value)
        assert "video-games.json" in msg            # path-qualified
        assert "Industry news" in msg               # names the bad value
        assert "video game news" in msg             # names the valid choices
