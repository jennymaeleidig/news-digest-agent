"""Ticket 08 — seen_items records the actually-picked Section, keyed per category.

Seam: the per-category run composition (``main.run_category``) — the spec's
confirmed primary seam. All tests are fully offline: an injectable fetcher
registry, curator, emailer, and state operator (the conftest fakes) drive the
run, so no network, no OpenRouter, and no real data/ writes happen.

Behavior under test:
  - ``seen_items`` records the Section an item was **actually picked into**
    (a single string) — never the source's first/mapped Section;
  - an item the curator offered but never picked records no Section (None);
  - seen-items state stays keyed per category: a URL seen in one category
    still reaches another category's curator (no cross-category suppression),
    while a category's own seen map still dedupes its own repeats.
"""

from __future__ import annotations

from conftest import HOST, REPO_ROOT, FakeCurator, make_fetch_results

from categories import Category, Section, Source
from fetchers.common import Item
from main import run_category


# ---------------------------------------------------------------------------
# Synthetic category / item builders (no config files, no network).
# conftest's factories pin a single-section fallback category, so the
# multi-section shapes this ticket is about are built locally.
# ---------------------------------------------------------------------------
def make_source(name, sections):
    return Source(
        name=name,
        tier=2,
        kind="rss",
        url=f"{HOST}feed-{name}",
        sections=tuple(sections),
    )


def make_category(cat_id, sources):
    return Category(
        id=cat_id,
        name=cat_id,
        schedule="17 16 * * *",
        recipient="reader@example.com",
        prompt=f"prompts/{cat_id}.md",
        prompt_path=REPO_ROOT / "categories" / "prompts" / "ai-ml.md",
        sources=tuple(sources),
        sections=(
            Section("Alpha", "alpha scope"),
            Section("Beta", "beta scope"),
        ),
    )


def make_item(url, source_name):
    return Item(
        title=f"T {url}",
        source_name=source_name,
        url=url,
        published="2099-01-01T00:00:00+00:00",
        content_snippet="snippet",
    )


def curator_picked_into(picks):
    """A FakeCurator whose CurateResult's picked-Section map is ``picks``
    (url -> section name); ``.calls`` captures each run's input items."""
    return FakeCurator(
        digest="## Digest\n\nsomething",
        picked_section_by_url=picks,
    )


def sent_emailer(sent):
    def emailer(markdown, subject, recipient):
        sent.append((markdown, subject, recipient))
        return "msg_%d" % len(sent)

    return emailer


MULTI = make_source("multi", ("Alpha", "Beta"))
URL_X = HOST + "x"
URL_Y = HOST + "y"


# ---------------------------------------------------------------------------
# 1. The picked Section, not the source's first mapped Section
# ---------------------------------------------------------------------------
class TestPickedSectionTagging:
    def test_seen_records_the_picked_section(self, state_factory, emailer_factory):
        """A multi-section source's item picked into its second mapped
        Section (Beta) is recorded under Beta — not Alpha, the source's
        first declared Section."""
        category = make_category("cat-a", [MULTI])
        state = state_factory(category_id=category.id)
        emailer, _sent = emailer_factory()
        outcome = run_category(
            category,
            fetcher_registry=make_fetch_results(
                {"multi": [make_item(URL_X, "multi")]},
            ),
            curate_fn=curator_picked_into({URL_X: "Beta"}),
            emailer=emailer,
            state=state,
        )
        assert outcome.ok
        record = state.saved_seen[0][URL_X]
        assert record["section"] == "Beta"

    def test_seen_records_first_section_when_it_did_the_picking(
        self, state_factory, emailer_factory,
    ):
        category = make_category("cat-a", [MULTI])
        state = state_factory(category_id=category.id)
        emailer, _sent = emailer_factory()
        run_category(
            category,
            fetcher_registry=make_fetch_results(
                {"multi": [make_item(URL_X, "multi")]},
            ),
            curate_fn=curator_picked_into({URL_X: "Alpha"}),
            emailer=emailer,
            state=state,
        )
        assert state.saved_seen[0][URL_X]["section"] == "Alpha"

    def test_unpicked_item_records_no_section(self, state_factory, emailer_factory):
        """The curator was offered both items but picked only X; Y was never
        picked into any Section, so its seen record carries no Section."""
        category = make_category("cat-a", [MULTI])
        state = state_factory(category_id=category.id)
        emailer, _sent = emailer_factory()
        run_category(
            category,
            fetcher_registry=make_fetch_results({
                "multi": [make_item(URL_X, "multi"), make_item(URL_Y, "multi")],
            }),
            curate_fn=curator_picked_into({URL_X: "Beta"}),
            emailer=emailer,
            state=state,
        )
        seen = state.saved_seen[0]
        assert seen[URL_X]["section"] == "Beta"
        assert seen[URL_Y]["section"] is None

    def test_section_is_a_single_string_not_a_list(
        self, state_factory, emailer_factory,
    ):
        """Within a category a picked item lands in exactly one Section (the
        no-double-pick guard), so the recorded value stays one string."""
        category = make_category("cat-a", [MULTI])
        state = state_factory(category_id=category.id)
        emailer, _sent = emailer_factory()
        run_category(
            category,
            fetcher_registry=make_fetch_results(
                {"multi": [make_item(URL_X, "multi")]},
            ),
            curate_fn=curator_picked_into({URL_X: "Beta"}),
            emailer=emailer,
            state=state,
        )
        assert isinstance(state.saved_seen[0][URL_X]["section"], str)


# ---------------------------------------------------------------------------
# 2. Per-category namespacing: no cross-category suppression
# ---------------------------------------------------------------------------
class TestPerCategoryNamespacing:
    def test_url_seen_in_one_category_still_reaches_another(
        self, state_factory, emailer_factory,
    ):
        """The same URL is seen in cat-a's run; cat-b's run has its own empty
        seen map, so the URL resurfaces in cat-b's curator input — dedup is
        strictly per category, with no cross-category suppression."""
        cat_a = make_category("cat-a", [MULTI])
        cat_b = make_category("cat-b", [MULTI])
        state_a = state_factory(category_id=cat_a.id)
        state_b = state_factory(category_id=cat_b.id)
        emailer, _sent = emailer_factory()
        curate = curator_picked_into({URL_X: "Beta"})
        registry = make_fetch_results({"multi": [make_item(URL_X, "multi")]})

        run_category(cat_a, fetcher_registry=registry, curate_fn=curate,
                     emailer=emailer, state=state_a)
        run_category(cat_b, fetcher_registry=registry, curate_fn=curate,
                     emailer=emailer, state=state_b)

        # cat-a marked it seen, under its own namespace...
        assert URL_X in state_a.saved_seen[0]
        # ...and cat-b's curator still received it (no cross-category suppression).
        assert [it.url for it in curate.calls[1][0]] == [URL_X]

    def test_each_categorys_state_stays_in_its_own_namespace(
        self, state_factory, emailer_factory,
    ):
        """What cat-a saved never leaks into cat-b's seen map and vice versa:
        each run's save carries only its own category's URLs."""
        cat_a = make_category("cat-a", [MULTI])
        cat_b = make_category(
            "cat-b", [make_source("other", ("Alpha",))],
        )
        state_a = state_factory(category_id=cat_a.id)
        state_b = state_factory(category_id=cat_b.id)
        emailer, _sent = emailer_factory()
        curate = curator_picked_into({URL_X: "Beta"})
        url_z = HOST + "z"

        run_category(
            cat_a,
            fetcher_registry=make_fetch_results(
                {"multi": [make_item(URL_X, "multi")]},
            ),
            curate_fn=curate, emailer=emailer, state=state_a,
        )
        run_category(
            cat_b,
            fetcher_registry=make_fetch_results(
                {"other": [make_item(url_z, "other")]},
            ),
            curate_fn=curate, emailer=emailer, state=state_b,
        )

        assert set(state_a.saved_seen[0]) == {URL_X}
        assert set(state_b.saved_seen[0]) == {url_z}

    def test_own_category_seen_still_suppresses_its_own_repeat(
        self, state_factory, emailer_factory,
    ):
        """The namespacing flip side: a URL in a category's own seen map is
        deduped out of that category's curator input on the next run."""
        category = make_category("cat-a", [MULTI])
        seen_record = {"date": "2099-01-01T00:00:00+00:00", "section": "Beta"}
        state = state_factory(category_id=category.id, seen={URL_X: seen_record})
        emailer, _sent = emailer_factory()
        curate = curator_picked_into({})

        outcome = run_category(
            category,
            fetcher_registry=make_fetch_results(
                {"multi": [make_item(URL_X, "multi")]},
            ),
            curate_fn=curate, emailer=emailer, state=state,
        )
        assert outcome.marked_seen == ()
        assert curate.calls == []     # X never reached this category's curator
