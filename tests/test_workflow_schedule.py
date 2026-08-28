"""Ticket 12 — per-category workflows on a staggered schedule.

The schedule wiring is verified statically (a config check, no CI required):
one GitHub Actions workflow per category — ``.github/workflows/digest-<id>.yml``
— each invoking the per-category dispatch selector (``python main.py
--category <id>``) on its category's own staggered UTC cron: AI 08:00,
Tech 08:30, Video games 09:00, Politics & News 09:30 (base 08:00, +30m each).

All four categories deliver into the same inbox (the workflow wires the
RECIPIENT_EMAIL secret; no category config overrides the recipient), each as
its own message scoped to its own subject — the per-category digest/subject
routing already lives in the pipeline.

Isolate-and-continue holds across the schedule structurally: each category is
its own workflow and therefore its own process, so a category failing (curation,
email, or a hard error) fails only its own workflow run and can never halt the
others'.

The per-category dispatch selector is also exercised for each scheduled
category against the real discovered category configs, fully offline (the
conftest fakes — no network, no OpenRouter, no data/ writes).

The workflows are parsed as YAML with pyyaml, a test-only dependency
(requirements-dev.txt) — the app itself never touches it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml
from conftest import FakeCurator, FakeState, make_fetch_results, make_item

import main as main_mod
from main import discover_categories, parse_args, run_categories, select_categories

WORKFLOWS_DIR = Path(__file__).resolve().parent.parent / ".github" / "workflows"

# The pinned staggered schedule (spec: base 08:00 UTC, +30m each), keyed by
# category id. Cron is always UTC.
STAGGERED_CRONS = {
    "ai-ml": "0 8 * * *",          # 08:00 UTC
    "tech": "30 8 * * *",          # 08:30 UTC
    "video-games": "0 9 * * *",    # 09:00 UTC
    "politics-news": "30 9 * * *", # 09:30 UTC
}


def _load_workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS_DIR / name).read_text())


def _workflow_for(category_id: str) -> dict:
    return _load_workflow(f"digest-{category_id}.yml")


def _run_command(workflow: dict) -> str:
    """Extract the `python main.py ...` command from the run job's steps."""
    steps = workflow["jobs"]["run-digest"]["steps"]
    for step in steps:
        run = step.get("run", "")
        for line in run.splitlines():
            stripped = line.strip()
            if stripped.startswith("python main.py"):
                return stripped
    raise AssertionError(
        f"no `python main.py ...` run step found in workflow "
        f"{workflow.get('name')!r}"
    )


def _schedule_crons(workflow: dict) -> list[str]:
    on = workflow[True] if True in workflow else workflow["on"]
    return [t["cron"] for t in on.get("schedule", [])]


# ---------------------------------------------------------------------------
# Workflow-per-category structure and the staggered schedule
# ---------------------------------------------------------------------------
def test_every_category_has_its_own_workflow():
    categories = discover_categories()
    for category in categories:
        path = WORKFLOWS_DIR / f"digest-{category.id}.yml"
        assert path.exists(), (
            f"category {category.id!r} has no per-category workflow at "
            f"{path} — each category must run as its own workflow"
        )


def test_staggered_schedule_lands_at_the_pinned_utc_slots():
    """The workflows carry the staggered crons: base 08:00 UTC, +30m each —
    AI 08:00, Tech 08:30, Video games 09:00, Politics & News 09:30."""
    for category_id, cron in STAGGERED_CRONS.items():
        workflow = _workflow_for(category_id)
        crons = _schedule_crons(workflow)
        assert cron in crons, (
            f"workflow digest-{category_id}.yml must be scheduled at "
            f"{cron!r} ({category_id}); found {crons}"
        )


def test_workflow_cron_agrees_with_the_category_config_schedule():
    """The category JSON's `schedule` field and its workflow's cron are the
    same schedule — the static check pins them to each other so they cannot
    drift apart."""
    categories = discover_categories()
    for category in categories:
        workflow = _workflow_for(category.id)
        assert category.schedule, (
            f"category {category.id!r} must declare its schedule"
        )
        assert category.schedule in _schedule_crons(workflow), (
            f"category {category.id!r} config schedule {category.schedule!r} "
            f"does not match its workflow's schedule "
            f"{_schedule_crons(workflow)}"
        )


def test_each_workflow_dispatches_only_its_own_category():
    """Each scheduled workflow invokes the per-category dispatch selector for
    exactly its own category (`python main.py --category <id>`)."""
    for category_id in STAGGERED_CRONS:
        command = _run_command(_workflow_for(category_id))
        assert command == f"python main.py --category {category_id}", (
            f"workflow digest-{category_id}.yml must run the per-category "
            f"dispatch selector for {category_id!r} only; found {command!r}"
        )


def test_no_run_everything_workflow_remains():
    """The old shared-schedule run-everything workflow is gone: every workflow
    that invokes main.py goes through the per-category dispatch selector."""
    assert (WORKFLOWS_DIR / "daily-digest.yml").exists() is False, (
        "the run-everything daily-digest.yml must be removed once every "
        "category runs as its own workflow"
    )
    for path in sorted(WORKFLOWS_DIR.glob("*.yml")):
        workflow = yaml.safe_load(path.read_text())
        for job in workflow.get("jobs", {}).values():
            for step in job.get("steps", []):
                for line in step.get("run", "").splitlines():
                    stripped = line.strip()
                    if stripped.startswith("python main.py"):
                        assert "--category" in stripped, (
                            f"{path.name} invokes main.py without a "
                            f"--category selector: {stripped!r}"
                        )


# ---------------------------------------------------------------------------
# Same inbox, separate messages
# ---------------------------------------------------------------------------
def test_all_scheduled_workflows_deliver_into_the_same_inbox():
    """Every category config leaves the recipient at null, and every workflow
    wires the RECIPIENT_EMAIL secret — so all four digests land in the same
    inbox, each as its own message scoped to its own subject."""
    categories = discover_categories()
    assert {c.id for c in categories} == set(STAGGERED_CRONS)
    for category in categories:
        assert category.recipient in (None, ""), (
            f"category {category.id!r} overrides the recipient "
            f"({category.recipient!r}); the four digests must share the one "
            f"default inbox"
        )
        env = _workflow_for(category.id)["jobs"]["run-digest"]["steps"]
        run_step = next(s for s in env if "main.py" in s.get("run", ""))
        assert run_step["env"]["RECIPIENT_EMAIL"] == "${{ secrets.RECIPIENT_EMAIL }}"


# ---------------------------------------------------------------------------
# Workflow hygiene: manual dispatch, state commit-back, overlap safety
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("category_id", sorted(STAGGERED_CRONS))
def test_workflow_supports_manual_dispatch(category_id):
    workflow = _workflow_for(category_id)
    on = workflow[True] if True in workflow else workflow["on"]
    assert "workflow_dispatch" in on, (
        f"digest-{category_id}.yml must be manually dispatchable"
    )


@pytest.mark.parametrize("category_id", sorted(STAGGERED_CRONS))
def test_workflow_can_commit_state_back(category_id):
    """The run commits per-category state (seen/health/run-log) back to the
    repo, so it needs contents: write."""
    workflow = _workflow_for(category_id)
    assert workflow["permissions"]["contents"] == "write"


@pytest.mark.parametrize("category_id", sorted(STAGGERED_CRONS))
def test_workflows_serialize_on_the_shared_state_files(category_id):
    """All four workflows read-modify-write the shared data/ state files and
    push to the same branch, so they share one concurrency group with
    cancel-in-progress disabled — an overlapping run queues rather than
    clobbering another's state commit."""
    workflow = _workflow_for(category_id)
    concurrency = workflow["concurrency"]
    assert concurrency["group"] == "digest"
    assert concurrency["cancel-in-progress"] is False


# ---------------------------------------------------------------------------
# The dispatch selector exercised for each scheduled category (offline)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("category_id", sorted(STAGGERED_CRONS))
def test_selector_selects_exactly_the_scheduled_category(category_id):
    """The CLI contract each scheduled workflow relies on: `--category <id>`
    selects exactly that one discovered category."""
    categories = discover_categories()
    args = parse_args(["--category", category_id])
    selected = select_categories(categories, category_id=args.category)
    assert [c.id for c in selected] == [category_id]


@pytest.mark.parametrize("category_id", sorted(STAGGERED_CRONS))
def test_scheduled_category_runs_end_to_end_offline(category_id, monkeypatch):
    """Each scheduled category runs the full fetch → filter → curate → email
    pipeline through the dispatch selector with the conftest fakes — the run
    its workflow triggers, proven offline."""
    # The real category configs leave the recipient at null (same inbox via
    # the RECIPIENT_EMAIL default), so set the env the emailer resolves from.
    monkeypatch.setenv("RECIPIENT_EMAIL", "reader@example.com")
    categories = discover_categories()
    selected = select_categories(categories, category_id=category_id)

    items = {
        source.name: [make_item(url=f"https://t.example/{category_id}/{source.name}")]
        for source in selected[0].sources
    }
    curator = FakeCurator(digest="## Digest\n\nitem A")
    emails = []

    outcomes, exit_code = run_categories(
        selected,
        today="2025-01-01",
        fetcher_registry=make_fetch_results(items),
        curate_fn=curator,
        emailer=lambda md, subject, recipient: emails.append(subject) or "m-1",
        state_for=lambda category: FakeState(category_id=category.id),
    )

    assert exit_code == 0
    assert [o.category_id for o in outcomes] == [category_id]
    assert len(curator.calls) == 1                 # reached curation
    assert len(emails) == 1                        # got its own message
    assert outcomes[0].email_sent is True
    assert outcomes[0].subject                     # scoped to its own subject
