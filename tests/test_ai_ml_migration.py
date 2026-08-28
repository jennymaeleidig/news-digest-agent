"""Ticket 10 — AI category migration onto the per-category scheme.

Seams: the category load/validate unit module (``categories.load_category``)
over the real shipped ``categories/ai-ml.json``, and the per-category run seam
(spec's confirmed primary seam) raised to the dispatch that decides *which*
categories run (``main.select_categories`` + ``main.run_categories``).

All tests are deterministic and offline: the dispatch tests run the real
``run_category`` composition with the conftest fakes, so no network, no
OpenRouter, and no real data/ writes happen.

Behavior under test:
  - the ``ai-ml`` config carries every source in the ``sections`` list form
    (the legacy singular ``section`` key is gone from the JSON);
  - the migrated config loads cleanly against the multi-section schema with
    identical behavior to before (sections, per-source mappings, topics,
    fetcher configs);
  - ``ai-ml`` runs through the per-category dispatch selector like the new
    categories — selectable by id, runnable end to end offline;
  - its digest subject/recipient/delivery shape are unchanged;
  - its state (seen items / source health / run log) stays namespaced under
    the ``ai-ml`` category id.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import FakeCurator, FakeState, make_category, make_fetch_results, make_item

from categories import load_category
from main import run_categories, select_categories


@pytest.fixture(autouse=True)
def default_recipient(monkeypatch):
    """ai-ml's config ships ``recipient: null`` (the default RECIPIENT_EMAIL
    secret applies — part of the unchanged delivery contract), so the offline
    dispatch tests pin a test default instead of requiring a real secret."""
    monkeypatch.setenv("RECIPIENT_EMAIL", "reader@example.com")


AI_ML_CONFIG = Path("categories/ai-ml.json")


# The shipped ai-ml digest sections (names, order, scope lines, ceilings) —
# the migration must not change any of them.
EXPECTED_SECTIONS = [
    {
        "name": "Releases",
        "description": (
            "Model releases, capability milestones, new tools and frameworks."
        ),
        "max_items": 5,
    },
    {
        "name": "Research",
        "description": (
            "Papers and technical deep-dives that change practice."
        ),
        "max_items": 8,
    },
    {
        "name": "General news",
        "description": (
            "Broad LLM and tech ecosystem happenings — focus on "
            "engineering-relevant news, not business dealings."
        ),
        "max_items": 10,
    },
]

# Per-source section mapping pinned by the migration contract: identical to
# the pre-migration singular `section` values.
EXPECTED_SOURCE_SECTIONS = {
    "arXiv (LLM)": ["Research"],
    "arXiv (Code)": ["Research"],
    "Hugging Face": ["General news"],
    "Hugging Face Daily Papers": ["Research"],
    "radarai.top (AI)": ["General news"],
    "Reddit r/LocalLLaMA": ["General news"],
    "AI Release Tracker": ["Releases"],
}


@pytest.fixture(scope="module")
def ai_ml():
    return load_category(AI_ML_CONFIG)


@pytest.fixture(scope="module")
def ai_ml_raw():
    return json.loads(AI_ML_CONFIG.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 1. The config is migrated to the `sections` list form
# ---------------------------------------------------------------------------
class TestConfigMigratedToSectionsListForm:
    def test_no_source_carries_the_legacy_singular_section_key(self, ai_ml_raw):
        for source in ai_ml_raw["sources"]:
            assert "section" not in source, source["name"]

    def test_every_source_uses_the_sections_list_form(self, ai_ml_raw):
        for source in ai_ml_raw["sources"]:
            assert isinstance(source.get("sections"), list), source["name"]
            assert source["sections"], source["name"]

    def test_every_source_maps_to_its_pinned_section_list(self, ai_ml_raw):
        for source in ai_ml_raw["sources"]:
            assert list(source["sections"]) == EXPECTED_SOURCE_SECTIONS[source["name"]], (
                source["name"]
            )


# ---------------------------------------------------------------------------
# 2. The migrated config loads cleanly with identical behavior
# ---------------------------------------------------------------------------
class TestMigratedConfigLoadsIdentically:
    def test_category_identity_unchanged(self, ai_ml):
        assert ai_ml.id == "ai-ml"
        assert ai_ml.name == "AI"

    def test_sections_unchanged_names_order_scope_ceilings(self, ai_ml):
        assert [
            {"name": s.name, "description": s.description, "max_items": s.max_items}
            for s in ai_ml.sections
        ] == EXPECTED_SECTIONS

    def test_source_section_mappings_resolve_identically(self, ai_ml):
        by_name = {s.name: s for s in ai_ml.sources}
        assert set(by_name) == set(EXPECTED_SOURCE_SECTIONS)
        for name, expected in EXPECTED_SOURCE_SECTIONS.items():
            assert list(by_name[name].sections) == expected, name
            # Loader normalization still yields the legacy singular accessor.
            assert by_name[name].section == expected[0], name

    def test_source_metadata_untouched(self, ai_ml):
        by_name = {s.name: s for s in ai_ml.sources}
        tracker = by_name["AI Release Tracker"]
        assert tracker.tier == 4
        assert tracker.kind == "airelease_tracker"
        assert tracker.age_limit_days == 30
        assert tracker.fetcher_config is not None
        assert by_name["arXiv (LLM)"].topics  # topic allow-list survives
        assert by_name["Hugging Face"].topics == ()  # unscoped source survives


# ---------------------------------------------------------------------------
# 3. ai-ml runs through the per-category dispatch selector
# ---------------------------------------------------------------------------
class TestDispatchThroughPerCategorySelector:
    def test_the_selector_selects_ai_ml_by_id(self, ai_ml):
        # A sibling category only so the selector has something to filter
        # out — a conftest-built stand-in, no coupling to another real config.
        sibling = make_category("tech", name="tech", recipient="reader@example.com")
        selected = select_categories([sibling, ai_ml], category_id="ai-ml")
        assert [c.id for c in selected] == ["ai-ml"]

    def test_ai_ml_runs_end_to_end_through_the_dispatch_offline(
        self, ai_ml, emailer_factory,
    ):
        """The real migrated ai-ml config flows through run_categories with
        the conftest fakes: its item reaches curation, the digest email is
        sent with the unchanged subject shape, and the outcome reports the
        ai-ml category id."""
        state = FakeState(category_id="ai-ml")
        # Feed one item through a real ai-ml source (Hugging Face carries no
        # topics allow-list, so the item survives the relevance filter).
        registry = make_fetch_results({"Hugging Face": [make_item()]})
        curate = FakeCurator()
        emailer, sent = emailer_factory()

        outcomes, exit_code = run_categories(
            [ai_ml],
            fetcher_registry=registry,
            curate_fn=curate,
            emailer=emailer,
            state_for=lambda _category: state,
            today="2099-01-01",
        )

        assert exit_code == 0
        assert [o.category_id for o in outcomes] == ["ai-ml"]
        # Curation ran on ai-ml's items; the digest subject/delivery shape is
        # unchanged: `<name> digest — <date>` to the resolved recipient.
        assert [c.id for _items, c in curate.calls] == ["ai-ml"]
        assert len(sent) == 1
        markdown, subject, recipient = sent[0]
        # Subject/delivery shape unchanged: `<name> digest — <date>`, routed
        # to the default recipient secret (ai-ml ships `recipient: null`).
        assert subject == "AI digest — 2099-01-01"
        assert ai_ml.recipient is None
        assert recipient == "reader@example.com"
        # The digest renders from the shortlist: the fake curator's digest
        # body reaches the email verbatim (plus the health footer it appends).
        assert markdown.startswith("## Digest")


# ---------------------------------------------------------------------------
# 4. ai-ml state stays namespaced per category
# ---------------------------------------------------------------------------
class TestAiMlStateStaysNamespaced:
    def test_seen_health_and_run_log_land_under_the_ai_ml_namespace(
        self, ai_ml, emailer_factory,
    ):
        state = FakeState(category_id="ai-ml")
        item = make_item(source_name="Hugging Face")
        registry = make_fetch_results({"Hugging Face": [item]})
        emailer, _sent = emailer_factory()

        outcomes, _ = run_categories(
            [ai_ml],
            fetcher_registry=registry,
            curate_fn=FakeCurator(),
            emailer=emailer,
            state_for=lambda _category: state,
            today="2099-01-01",
        )

        # Seen items recorded under the ai-ml namespace, carrying the picked
        # section (None from the fake curator) and the source name.
        saved = state.saved_seen[-1]
        assert set(outcomes[0].marked_seen) == {item.url}
        assert saved[item.url]["source"] == "Hugging Face"
        # Source health recorded for every fetched ai-ml source, keyed by name.
        assert set(state.saved_health[-1]["sources"]) == {
            s.name for s in ai_ml.sources
        }
        # Run-log row attributes itself to the ai-ml category.
        assert state.log_rows[-1]["category"] == "ai-ml"

    def test_ai_ml_state_never_leaks_into_another_category(
        self, ai_ml, emailer_factory,
    ):
        """ai-ml's run writes only to its own namespace: a sibling category's
        state stays untouched across the same invocation."""
        ai_ml_state = FakeState(category_id="ai-ml")
        tech_state = FakeState(category_id="tech")
        registry = make_fetch_results({"Hugging Face": [make_item()]})

        run_categories(
            [ai_ml],
            fetcher_registry=registry,
            curate_fn=FakeCurator(),
            emailer=emailer_factory()[0],
            state_for=lambda category: (
                ai_ml_state if category.id == "ai-ml" else tech_state
            ),
            today="2099-01-01",
        )

        assert tech_state.saved_seen == []
        assert tech_state.saved_health == []
        assert tech_state.log_rows == []
