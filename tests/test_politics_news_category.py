"""Ticket 06 — Politics & News category drop-in config and prompt.

Seams:
  - the category load/validate unit module (``categories.load_category``)
    over the real shipped ``categories/politics-news.json`` +
    ``categories/prompts/politics-news.md``, mirroring the tech (04) and
    video-games (05) category suites;
  - ``fetchers.rss.fetch`` — the shared full-header RSS path, for the
    democracynow http→https permalink normalization the Substack-hosted
    feeds also ride on.

All tests are deterministic and offline: no network is touched (verifying
the nine pinned feed URLs / channel ids resolve is a CI/operator live-smoke
concern, per the spec's Testing Decisions — they were verified live at spec
time).

The spec pins the exact source table (tier, kind, URL, homepage, section
mapping) and the section scope-line prose (spec.md, "Prompt scope-line
prose" — Politics & News table; "Sources → category → sections"). The four
sections carve along two independent axes — geography (US vs Global) and
subject (News vs Politics) — so an item routes to exactly one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from categories import CategoryError, load_category


PN_CONFIG = Path("categories/politics-news.json")
TECH_CONFIG = Path("categories/tech.json")

# Spec-verified YouTube channel ids (ticket 02 decision 4).
MAJORITY_REPORT_CHANNEL_ID = "UC-3jIAlnQmbbVMV6gR7K8aQ"
HASANABI_CHANNEL_ID = "UCtoaZpBnrd0lhycxYJ4MNOQ"

# The spec's settled scope-line prose (spec.md → Prompt scope-line prose →
# Politics & News). These strings are the single source of truth for section
# scope; each carves along the two axes so an item routes to exactly one.
SCOPE_LINES = {
    "Global News": (
        "Events and affairs outside the United States — wars, disasters, "
        "courts, institutions, and world news as it happens; the center is "
        "what occurred beyond the US, not the politics around it."
    ),
    "Global Politics": (
        "Governments, elections, and power outside the United States — "
        "foreign leaders, parties, elections, diplomacy, and geopolitics; "
        "the center is who holds or contests power abroad."
    ),
    "US News": (
        "Domestic events and affairs in the United States — courts, "
        "disasters, investigations, institutions, and national news as it "
        "happens; the center is what occurred at home, not the politics "
        "around it."
    ),
    "US Politics": (
        "The contest for power and policy in the United States — elections, "
        "Congress, the administration, campaigns, policy fights, and "
        "political movements; the center is the political fight itself."
    ),
}

# The spec's pinned source table (spec.md → Sources → category → sections).
EXPECTED_SOURCES = {
    "democracynow": {
        "tier": 2, "kind": "rss",
        "url": "https://www.democracynow.org/democracynow.rss",
        "homepage": "https://www.democracynow.org/",
        "sections": ["Global News", "Global Politics", "US News", "US Politics"],
    },
    "dropsite": {
        "tier": 2, "kind": "rss",
        "url": "https://www.dropsitenews.com/feed",
        "homepage": "https://www.dropsitenews.com/",
        "sections": ["Global News", "Global Politics", "US News"],
    },
    "propublica": {
        "tier": 2, "kind": "rss",
        "url": "https://www.propublica.org/feeds/propublica/main",
        "homepage": "https://www.propublica.org/",
        "sections": ["US News", "US Politics"],
    },
    "defector": {
        "tier": 3, "kind": "rss",
        "url": "https://defector.com/feed",
        "homepage": "https://defector.com/",
        "sections": ["US News", "US Politics"],
    },
    "kenklippenstein": {
        "tier": 3, "kind": "rss",
        "url": "https://www.kenklippenstein.com/feed",
        "homepage": "https://www.kenklippenstein.com/",
        "sections": ["US News", "US Politics"],
    },
    "usermag": {
        "tier": 2, "kind": "rss",
        "url": "https://www.usermag.co/feed",
        "homepage": "https://www.usermag.co/",
        "sections": ["US News", "US Politics"],
    },
    "majority-report": {
        "tier": 4, "kind": "youtube",
        "url": MAJORITY_REPORT_CHANNEL_ID,
        "homepage": None,
        "sections": ["US Politics"],
    },
    "hasanabi": {
        "tier": 4, "kind": "youtube",
        "url": HASANABI_CHANNEL_ID,
        "homepage": None,
        "sections": ["US Politics"],
    },
    "true-anon": {
        "tier": 4, "kind": "rss",
        "url": "https://www.patreon.com/public-rss/2963533?show=875184",
        "homepage": "https://podcast.trueanon.com",
        "sections": ["US Politics"],
    },
}

# Substack-hosted feeds: CDN-fronted, served through the shared full-header
# RSS fetch path (no 403).
SUBSTACK_HOSTED = ["usermag", "dropsite", "kenklippenstein"]


@pytest.fixture(scope="module")
def pn():
    return load_category(PN_CONFIG)


class TestConfigLoads:
    def test_politics_news_config_loads_against_the_multi_section_schema(
            self, pn):
        assert pn.id == "politics-news"
        assert pn.name

    def test_sections_non_empty_and_ordered(self, pn):
        assert [s.name for s in pn.sections] == [
            "Global News", "Global Politics", "US News", "US Politics",
        ]

    def test_schedule_carries_the_staggered_slot(self, pn):
        # Spec: Politics & News runs at 09:30 UTC in the staggered base
        # (kept-but-ignored here; ticket 12's workflows own the cron).
        assert pn.schedule == "30 9 * * *"


class TestSectionScopeLines:
    def test_each_section_carries_its_scope_line(self, pn):
        for section in pn.sections:
            assert section.description == SCOPE_LINES[section.name]

    def test_scope_lines_express_both_routing_axes(self, pn):
        by_name = {s.name: s.description for s in pn.sections}
        # News-vs-Politics: the story's center — an event is News, a contest
        # over power or policy is Politics.
        assert "not the politics around it" in by_name["US News"]
        assert "not the politics around it" in by_name["Global News"]
        assert "the political fight itself" in by_name["US Politics"]
        assert "who holds or contests power" in by_name["Global Politics"]
        # US-vs-Global: the subject's location, not the publication.
        assert "United States" in by_name["US News"]
        assert "outside the United States" in by_name["Global News"]
        assert "outside the United States" in by_name["Global Politics"]


class TestSources:
    def test_every_spec_source_is_present(self, pn):
        assert {s.name for s in pn.sources} == set(EXPECTED_SOURCES)

    def test_every_source_carries_its_spec_metadata(self, pn):
        for source in pn.sources:
            expected = EXPECTED_SOURCES[source.name]
            assert source.tier == expected["tier"], source.name
            assert source.kind == expected["kind"], source.name
            assert source.url == expected["url"], source.name
            assert source.homepage == expected["homepage"], source.name
            assert list(source.sections) == expected["sections"], source.name

    def test_sources_validate_only_against_politics_news_sections(self, pn):
        declared = {s.name for s in pn.sections}
        for source in pn.sources:
            assert set(source.sections) <= declared

    def test_tiers_follow_adr_0001(self, pn):
        # Tier 2 = disinterested institutional reporting; tier 3 =
        # interested-party primary; tier 4 = commentary (attributed, never
        # asserted). The politics mix spans 2–4.
        for source in pn.sources:
            assert source.tier in (2, 3, 4), source.name


class TestMultiSectionSource:
    def test_democracynow_feeds_all_four_sections(self, pn):
        dnow = next(s for s in pn.sources if s.name == "democracynow")
        assert dnow.sections == (
            "Global News", "Global Politics", "US News", "US Politics")
        assert dnow.section == "Global News"  # first of the list


class TestCrossCategoryCoexistence:
    def test_usermag_declares_a_different_list_per_category(self):
        tech = load_category(TECH_CONFIG)
        politics = load_category(PN_CONFIG)
        tech_usermag = next(s for s in tech.sources if s.name == "usermag")
        pn_usermag = next(
            s for s in politics.sources if s.name == "usermag")
        # One section list per category: Tech maps usermag to Industry news
        # only; Politics & News maps it to the two US sections.
        assert tech_usermag.sections == ("Industry news",)
        assert pn_usermag.sections == ("US News", "US Politics")

    def test_each_config_validates_only_against_its_own_sections(self):
        tech = load_category(TECH_CONFIG)
        politics = load_category(PN_CONFIG)
        tech_sections = {s.name for s in tech.sections}
        pn_sections = {s.name for s in politics.sections}
        for source in tech.sources:
            assert set(source.sections) <= tech_sections
        for source in politics.sources:
            assert set(source.sections) <= pn_sections


class TestYouTubeSources:
    def test_channels_are_listed_keyless(self, pn):
        for name, channel_id in [
            ("majority-report", MAJORITY_REPORT_CHANNEL_ID),
            ("hasanabi", HASANABI_CHANNEL_ID),
        ]:
            source = next(s for s in pn.sources if s.name == name)
            # Keyless: the bare channel id (not a feed URL or API endpoint),
            # no homepage, and no API key material anywhere in the source.
            assert source.url == channel_id, name
            assert not source.url.startswith("http"), name
            assert source.homepage is None, name

    def test_channels_are_tier_4_commentary(self, pn):
        for name in ("majority-report", "hasanabi"):
            source = next(s for s in pn.sources if s.name == name)
            assert source.tier == 4, name
            assert source.kind == "youtube", name


class TestSubstackAndTrueAnon:
    def test_substack_hosted_sources_ride_the_full_header_rss_path(self, pn):
        # kind: rss routes through the shared RSS fetcher, which sends the
        # full header set (Accept, Accept-Language, Accept-Encoding) that
        # Substack/Cloudflare-fronted feeds require — no 403.
        for name in SUBSTACK_HOSTED:
            source = next(s for s in pn.sources if s.name == name)
            assert source.kind == "rss", name
            assert source.url.startswith("https://"), name

    def test_true_anon_uses_its_show_style_rss_feed(self, pn):
        true_anon = next(s for s in pn.sources if s.name == "true-anon")
        # The Patreon show-style RSS feed (title + show notes) — no
        # transcript dependency.
        assert true_anon.kind == "rss"
        assert true_anon.url == (
            "https://www.patreon.com/public-rss/2963533?show=875184")


class TestPromptFile:
    def test_prompt_is_a_category_level_scope_with_no_per_section_block(
            self, pn):
        prompt_text = pn.prompt_path.read_text(encoding="utf-8")
        # Category-level scope present...
        assert "# Scope" in prompt_text
        # ...but no per-section block: the pipeline injects each section's
        # name + description.
        assert "# Sections" not in prompt_text
        for section in pn.sections:
            headings = [
                line for line in prompt_text.splitlines()
                if line.lstrip().startswith("#") and section.name in line
            ]
            assert not headings, (
                f"prompt carries a per-section block for {section.name!r}")


class TestUnlistedSectionRejected:
    def test_politics_news_unlisted_section_name_is_rejected_path_qualified(
            self, pn, tmp_path):
        data = json.loads(PN_CONFIG.read_text(encoding="utf-8"))
        data["sources"][0]["sections"] = ["Industry news"]  # Tech's, not ours
        # Real sibling prompt so the failure is the section validation, not
        # a missing prompt file.
        (tmp_path / "prompts").mkdir()
        (tmp_path / "prompts" / "politics-news.md").write_text(
            pn.prompt_path.read_text(encoding="utf-8"), encoding="utf-8")
        config_path = tmp_path / "politics-news.json"
        config_path.write_text(json.dumps(data), encoding="utf-8")

        with pytest.raises(CategoryError) as excinfo:
            load_category(config_path)
        msg = str(excinfo.value)
        assert "politics-news.json" in msg          # path-qualified
        assert "Industry news" in msg               # names the bad value
        assert "Global News" in msg                 # names the valid choices
