# 12: Per-category workflows on a staggered schedule

**What to build:** Each category runs as its own workflow, each invoked through the per-category dispatch selector, on a staggered UTC schedule — AI 08:00, Tech 08:30, Video games 09:00, Politics & News 09:30 (base 08:00 +30m each). All four categories deliver into the same inbox as separate messages, each scoped to its own subject, with per-category isolate-and-continue preserved so one category failing never halts the others.

**Blocked by:** 09 (Per-category CLI dispatch), 02 (YouTube listing fetcher), 03 (YouTube-transcript deep-read)

**Status:** resolved

- [x] Each category is wired as its own workflow that invokes only that category via the dispatch selector.
- [x] The staggered schedule lands at AI 08:00, Tech 08:30, Video games 09:00, Politics & News 09:30 UTC (base 08:00 UTC, +30m each).
- [x] All four categories deliver into the same inbox as separate messages, each scoped to its own subject.
- [x] Per-category isolate-and-continue holds across the schedule: one category failing never halts the others.
- [x] The schedule wiring is verified (static/config check) and the per-category dispatch selector is exercised for each scheduled category.

## Comments

**Resolved (ticket 12).** Schedule wiring is one workflow per category, statically
verified by a new test module:

- `.github/workflows/digest-<id>.yml` (four new files, one per category): each
  schedules its category's own cron — AI `0 8 * * *`, Tech `30 8 * * *`,
  Video games `0 9 * * *`, Politics & News `30 9 * * *` (base 08:00 UTC, +30m
  each) — and runs exactly `python main.py --category <id>` (the per-category
  dispatch selector from ticket 09). Each keeps the previous run job shape:
  Python 3.13, pip cache, the RESEND_API_KEY / RECIPIENT_EMAIL /
  OPENROUTER_API_KEY / OPENROUTER_MODEL env wiring, and the always-commit
  state step (seen/health/run-log are shared files keyed by category, so the
  whole file is committed).
- All four workflows share one `concurrency: group: digest` with
  `cancel-in-progress: false`: they read-modify-write the same data/ state
  files and push to the same branch, so an overlapping run (manual dispatch
  during a scheduled slot) queues instead of clobbering another workflow's
  state commit. The 30-minute stagger plus the 10-minute timeout means
  scheduled runs never overlap anyway.
- Isolate-and-continue across the schedule is structural: each category is its
  own workflow and its own process, so a category failing fails only its own
  workflow and cannot halt the others (in-run per-source/per-stage isolation
  was already `run_category`'s job).
- `.github/workflows/daily-digest.yml` removed (the run-everything workflow);
  its `smoke-test-fetchers` job moved to its own
  `.github/workflows/smoke-test-fetchers.yml`, scheduled 10:17 UTC (after the
  digest block, non-round minute) with `workflow_dispatch`.
- `categories/ai-ml.json` schedule updated `"17 16 * * *"` → `"0 8 * * *"`; the
  category config's `schedule` field is now the workflow-sync source of truth
  (docstrings in `categories.py` updated). `main.py` header comments updated
  from "one shared schedule fires this single job" to the per-category workflow
  model. README updated (schedule line + an Operational-notes bullet).
- New `tests/test_workflow_schedule.py` (26 tests, offline; pyyaml added to
  requirements-dev as a test-only dep): every category has its own workflow;
  the four crons land at the pinned staggered slots and agree with each
  category config's `schedule` field (no drift); each workflow runs
  `python main.py --category <id>` and only that; no workflow invokes main.py
  without `--category` and daily-digest.yml is gone; every workflow wires the
  same `RECIPIENT_EMAIL` secret and no category config overrides the recipient
  (same inbox, separate messages, own subject); `workflow_dispatch`,
  `contents: write`, and the shared concurrency group are pinned per workflow;
  and the dispatch selector is exercised for each scheduled category both
  ways — `--category <id>` selects exactly that discovered category, and each
  scheduled category runs the full fetch → filter → curate → email pipeline
  end to end offline through `run_categories` with the conftest fakes.
- Test evidence: `tests/test_workflow_schedule.py` 26 passed; full suite
  `.venv/bin/python -m pytest` → 171 passed, fully offline.
