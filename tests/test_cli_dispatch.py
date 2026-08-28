"""Ticket 09 — per-category CLI dispatch: ``--category <id>`` / ``--all``.

Seam: the per-category run seam (spec's confirmed primary seam), raised one
level to the dispatch that decides *which categories* a given invocation runs.
All tests are fully offline: the dispatch runs the real ``run_category``
composition with the injectable fetcher/curator/emailer/state fakes (the
conftest fakes), so no network, no OpenRouter, and no real data/ writes happen.

Behavior under test:
  - no argument (or ``--all``) runs every discovered category — today's
    run-everything behavior is preserved;
  - ``--category <id>`` runs only that category;
  - per-category isolate-and-continue holds: one category failing (either a
    hard run failure or a captured curate/email error) never halts the
    others' runs;
  - each category's state stays namespaced per category across a dispatch;
  - unknown category ids are rejected with the available ids listed.
"""

from __future__ import annotations

import pytest
from conftest import HOST, FakeCurator, FakeState, make_category, make_fetch_results, make_item

import main as main_mod
from main import CategoryRunOutcome, main, parse_args, run_categories, select_categories


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
def curator_failing_for(fail_id: str):
    """A curator that raises only for ``fail_id``; records calls per category
    so a test can see which categories actually reached curation."""
    inner = FakeCurator(digest="## Digest\n\nitem A")
    calls = inner.calls

    def curate(items, category, **kw):
        if category.id == fail_id:
            raise RuntimeError("curation exploded for " + category.id)
        return inner(items, category, **kw)

    curate.calls = calls
    return curate


def outcome_stub(category):
    """A minimal successful CategoryRunOutcome for a run_one fake."""
    return CategoryRunOutcome(
        category_id=category.id,
        items_input=0,
        items_output=0,
        results=(),
        digest_md="",
        subject="s",
        recipient="r@example.com",
        curate_error=None,
        email_error=None,
        email_sent=True,
    )


@pytest.fixture
def three_categories():
    # Distinct names so the digest subject carries the category id (the
    # flaky-emailer fake routes on it); a recipient so no env lookup happens.
    return [
        make_category("cat-a", name="cat-a", recipient="reader@example.com"),
        make_category("cat-b", name="cat-b", recipient="reader@example.com"),
        make_category("cat-c", name="cat-c", recipient="reader@example.com"),
    ]


@pytest.fixture
def wire_main(monkeypatch):
    """Fixture returning wire(cats): points main's discovery/dispatch at
    fakes and returns the dict the fake dispatch fills with the categories
    main handed it."""
    captured = {}

    def fake_run_categories(selected, **kw):
        captured["selected"] = list(selected)
        return [], 0

    def wire(cats):
        monkeypatch.setattr(main_mod, "discover_categories", lambda: cats)
        monkeypatch.setattr(main_mod, "run_categories", fake_run_categories)
        return captured

    return wire


@pytest.fixture
def state_for():
    """A per-category state factory: one namespaced FakeState per category."""
    built = {}

    def _state_for(category):
        state = FakeState(category_id=category.id)
        built[category.id] = state
        return state

    _state_for.built = built
    return _state_for


# ---------------------------------------------------------------------------
# 1. Argument contract
# ---------------------------------------------------------------------------
class TestArgumentContract:
    def test_no_argument_defaults_to_running_everything(self):
        args = parse_args([])
        assert args.category is None
        assert args.all is False

    def test_all_flag_is_explicit_run_everything(self):
        args = parse_args(["--all"])
        assert args.category is None
        assert args.all is True

    def test_category_flag_selects_one_id(self):
        args = parse_args(["--category", "tech"])
        assert args.category == "tech"
        assert args.all is False

    def test_category_and_all_are_mutually_exclusive(self):
        with pytest.raises(SystemExit) as e:
            parse_args(["--all", "--category", "tech"])
        assert e.value.code == 2

    def test_category_flag_requires_a_value(self):
        with pytest.raises(SystemExit) as e:
            parse_args(["--category"])
        assert e.value.code == 2


# ---------------------------------------------------------------------------
# 2. Selection: which categories does an invocation run?
# ---------------------------------------------------------------------------
class TestSelection:
    def test_no_selector_selects_every_discovered_category(self, three_categories):
        assert select_categories(three_categories) == three_categories

    def test_category_id_selects_only_that_category(self, three_categories):
        selected = select_categories(three_categories, category_id="cat-b")
        assert [c.id for c in selected] == ["cat-b"]

    def test_unknown_category_id_lists_the_available_ids(self, three_categories):
        with pytest.raises(ValueError) as e:
            select_categories(three_categories, category_id="nope")
        msg = str(e.value)
        assert "nope" in msg
        for cat in three_categories:
            assert cat.id in msg


# ---------------------------------------------------------------------------
# 3. The dispatch seam: which categories a given invocation runs,
#    verified end to end offline through the real run_category composition.
# ---------------------------------------------------------------------------
class TestDispatchWhichCategoriesRun:
    def test_no_selector_runs_every_category_end_to_end(
        self, three_categories, state_for, emailer_factory,
    ):
        """No argument: every category runs its full pipeline — each reaches
        curation and gets its own email sent (today's behavior preserved)."""
        curate = FakeCurator()
        emailer, sent = emailer_factory()
        outcomes, exit_code = run_categories(
            three_categories,
            fetcher_registry=make_fetch_results({"S1": [make_item(HOST + "x")], "S2": []}),
            curate_fn=curate,
            emailer=emailer,
            state_for=state_for,
        )
        assert exit_code == 0
        ran = [category.id for _items, category in curate.calls]
        assert ran == ["cat-a", "cat-b", "cat-c"]
        assert len(sent) == 3
        assert [o.category_id for o in outcomes] == ["cat-a", "cat-b", "cat-c"]

    def test_category_selector_runs_only_that_category_end_to_end(
        self, three_categories, state_for, emailer_factory,
    ):
        """--category cat-b: only cat-b's pipeline runs — the others never
        reach curation and never get an email."""
        selected = select_categories(three_categories, category_id="cat-b")
        curate = FakeCurator()
        emailer, sent = emailer_factory()
        outcomes, exit_code = run_categories(
            selected,
            fetcher_registry=make_fetch_results({"S1": [make_item(HOST + "x")], "S2": []}),
            curate_fn=curate,
            emailer=emailer,
            state_for=state_for,
        )
        assert exit_code == 0
        assert [category.id for _items, category in curate.calls] == ["cat-b"]
        assert len(sent) == 1
        assert [o.category_id for o in outcomes] == ["cat-b"]

    def test_main_wires_the_argv_selector_into_the_dispatch(self, wire_main):
        """main(--category cat-b) selects cat-b out of the discovered
        categories and dispatches exactly that one."""
        cats = [make_category("cat-a"), make_category("cat-b")]
        captured = wire_main(cats)
        assert main(["--category", "cat-b"]) == 0
        assert [c.id for c in captured["selected"]] == ["cat-b"]

    def test_main_without_a_selector_dispatches_every_discovered_category(
        self, wire_main,
    ):
        cats = [make_category("cat-a"), make_category("cat-b")]
        captured = wire_main(cats)
        assert main([]) == 0
        assert [c.id for c in captured["selected"]] == ["cat-a", "cat-b"]

    def test_main_rejects_an_unknown_category_id(self, monkeypatch, three_categories):
        monkeypatch.setattr(main_mod, "discover_categories", lambda: three_categories)
        assert main(["--category", "nope"]) == 2


# ---------------------------------------------------------------------------
# 4. Isolate-and-continue across categories
# ---------------------------------------------------------------------------
class TestIsolateAndContinue:
    def test_a_hard_run_failure_never_halts_the_other_categories(
        self, three_categories, state_for, emailer_factory,
    ):
        """cat-b's run raises an unexpected exception; cat-a and cat-c still
        run their full pipelines and the invocation reports failure."""
        runs = []
        curate = FakeCurator()
        emailer, sent = emailer_factory()

        def flaky_run_one(category, **kw):
            """Records every attempted category id, then fails hard for cat-b."""
            runs.append(category.id)
            if category.id == "cat-b":
                raise RuntimeError("hard failure")
            return outcome_stub(category)

        outcomes, exit_code = run_categories(
            three_categories,
            run_one=flaky_run_one,
            curate_fn=curate,
            emailer=emailer,
            state_for=state_for,
        )
        assert runs == ["cat-a", "cat-b", "cat-c"]
        assert exit_code == 1
        assert [o.category_id for o in outcomes] == ["cat-a", "cat-c"]

    def test_a_curation_failure_never_halts_the_other_categories(
        self, three_categories, state_for, emailer_factory,
    ):
        """cat-b's curation explodes (captured into its outcome as a
        broken-agent path); cat-a and cat-c still run, get emails, and the
        invocation reports failure."""
        curate = curator_failing_for("cat-b")
        emailer, sent = emailer_factory()
        outcomes, exit_code = run_categories(
            three_categories,
            fetcher_registry=make_fetch_results({"S1": [make_item(HOST + "x")], "S2": []}),
            curate_fn=curate,
            emailer=emailer,
            state_for=state_for,
        )
        assert exit_code == 1
        assert len(sent) == 3                       # every category still emailed
        assert [o.category_id for o in outcomes] == ["cat-a", "cat-b", "cat-c"]
        by_id = {o.category_id: o for o in outcomes}
        assert by_id["cat-b"].curate_error is not None
        assert by_id["cat-a"].curate_error is None
        assert by_id["cat-c"].curate_error is None

    def test_an_email_failure_never_halts_the_other_categories(
        self, three_categories, state_for, emailer_factory,
    ):
        """cat-b's email send fails; the other categories' sends are
        unaffected and the invocation reports failure."""
        emailer, sent = emailer_factory()

        def flaky_emailer(markdown, subject, recipient):
            if "cat-b" in subject:
                raise RuntimeError("resend down")
            return emailer(markdown, subject, recipient)

        outcomes, exit_code = run_categories(
            three_categories,
            fetcher_registry=make_fetch_results({"S1": [make_item(HOST + "x")], "S2": []}),
            emailer=flaky_emailer,
            state_for=state_for,
        )
        assert exit_code == 1
        by_id = {o.category_id: o for o in outcomes}
        assert by_id["cat-b"].email_error is not None
        assert not by_id["cat-b"].email_sent
        assert by_id["cat-a"].email_sent and by_id["cat-c"].email_sent

    def test_all_ok_reports_exit_code_zero(
        self, three_categories, state_for, emailer_factory,
    ):
        outcomes, exit_code = run_categories(
            three_categories,
            emailer=emailer_factory()[0],
            state_for=state_for,
        )
        assert exit_code == 0
        assert all(o.ok for o in outcomes)


# ---------------------------------------------------------------------------
# 5. Per-category state namespacing under one dispatch
# ---------------------------------------------------------------------------
class TestPerCategoryStateNamespacing:
    def test_each_category_runs_against_its_own_state_namespace(
        self, three_categories, state_for, emailer_factory,
    ):
        """The dispatch hands each category's run a state bound to that
        category's id — never another category's namespace — and each
        namespace records its own seen items."""
        registry = make_fetch_results({
            "S1": [make_item(HOST + "x")],
            "S2": [],
        })
        outcomes, _ = run_categories(
            three_categories,
            fetcher_registry=registry,
            curate_fn=FakeCurator(),
            emailer=emailer_factory()[0],
            state_for=state_for,
        )
        assert set(state_for.built) == {"cat-a", "cat-b", "cat-c"}
        for outcome in outcomes:
            assert outcome.marked_seen == (HOST + "x",)

    def test_a_url_seen_in_one_category_still_runs_in_the_others(
        self, three_categories, state_for, emailer_factory,
    ):
        """The same fetched URL flows through every category's curator: dedup
        is strictly per category even under a single dispatch, and each
        namespace records its own seen item."""
        registry = make_fetch_results({"S1": [make_item(HOST + "x")], "S2": []})
        curate = FakeCurator()
        run_categories(
            three_categories,
            fetcher_registry=registry,
            curate_fn=curate,
            emailer=emailer_factory()[0],
            state_for=state_for,
        )
        for _items, _category in curate.calls:
            assert [it.url for it in _items] == [HOST + "x"]
        for cat_id in ("cat-a", "cat-b", "cat-c"):
            assert HOST + "x" in state_for.built[cat_id].saved_seen[0]

    def test_one_categorys_state_does_not_leak_into_another(
        self, three_categories, state_for, emailer_factory,
    ):
        """Only cat-b fetches an item; the other namespaces never record it —
        what one category saved never appears in another's seen map."""
        registry_a = make_fetch_results({"S1": [], "S2": []})
        registry_b = make_fetch_results({"S1": [make_item(HOST + "only-b")], "S2": []})
        curate = FakeCurator()

        run_categories(
            three_categories[:1],
            fetcher_registry=registry_a,
            curate_fn=curate,
            emailer=emailer_factory()[0],
            state_for=state_for,
        )
        run_categories(
            three_categories[1:2],
            fetcher_registry=registry_b,
            curate_fn=curate,
            emailer=emailer_factory()[0],
            state_for=state_for,
        )
        assert state_for.built["cat-a"].saved_seen[0] == {}
        assert HOST + "only-b" in state_for.built["cat-b"].saved_seen[0]
