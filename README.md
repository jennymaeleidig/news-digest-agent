# News digest agent

A category-driven daily news digest: fetch curated sources, have an LLM pick
and summarize the day's most relevant items, email the result. Ships configured
for AI/ML news (LLMs and AI coding agents), but the engine is topic-agnostic —
any subject is just another category. Runs on GitHub Actions — one workflow
per category, staggered from 08:00 UTC (AI 08:00, Tech 08:30, Video games
09:00, Politics & News 09:30).

> Fork of [al-strunova/ai-news-digest-agent](https://github.com/al-strunova/ai-news-digest-agent).

## How it works

`fetch → filter → curate → email`, one email per category.

- **Fetch** — one module per source kind, dispatched through a `kind → fetcher`
  registry (`rss`, `youtube`, `huggingface_papers`, `reddit_rss_api`,
  `airelease_tracker`). Source failures are isolated, never fatal.
- **Filter** — drop items outside the age window, off-topic items (per-source
  allow-list), and already-seen items (14-day dedup).
- **Curate** — the OpenRouter chat-completions API as a pure summarizer (no
  network tools), two stages per section: a cheap title-only pass picks which
  items earn a place, then a second pass summarizes and formats just those
  picks. Sections are re-stitched in the category's declared order. A single
  pinned model runs every call. A pre-fetch stage deep-reads thin-snippet
  items first.
- **Email** — markdown → HTML via Resend.

## Categories

Every `categories/*.json` is one category, discovered and run independently —
each with its own recipient, state, and digest email. A category is three files:

- **`categories/<id>.json`** — the single source of truth for structure:
  `id`, `name`, an ordered `sections` list (name + what belongs there),
  `sources` (tier, kind, url, sections — one or more digest sections, optional topics/age-window),
  and its own `schedule` cron.
- **`categories/prompts/<id>.md`** — the curation prompt. Section-agnostic:
  section names and descriptions are injected from the JSON at run time.
- **`.github/workflows/digest-<id>.yml`** — the per-category workflow, carrying
  the same staggered cron as the JSON's `schedule` and invoking
  `python main.py --category <id>`.

No stage in the pipeline knows a section or source by name — but the workflow
file does hard-code the category id, and its cron must mirror the JSON's
`schedule` (the JSON is the source of truth for the cron).

## Running

```
python main.py                 # run every discovered category (default)
python main.py --all           # same, explicit
python main.py --category tech # run only that category
```

Categories run one fully after the other; a category failing (curation,
email, or a hard error) never stops the rest — the process exits 1 if any
failed, 2 for a usage error (unknown category id, conflicting flags).

## State

Three committed files in `data/`, keyed by category `id`:

- `seen_items.json` — dedup set (14-day expiry, written on successful send).
- `source_health.json` — per-source success/failure, shown as a digest footer.
- `run_log.jsonl` — per-run duration, item counts, prompt size, errors.

To reset state before a debugging run (treat every recent item as unseen
again and start a clean log):

```
python -m scripts.clear_state            # seen_items + run_log
python -m scripts.clear_state --health   # + source_health
```

## Setup

```
git clone <repo>
cd news-digest-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # OPENROUTER_API_KEY, RESEND_API_KEY, RECIPIENT_EMAIL, (optional) YT_TRANSCRIPT_PROXY_URL
python main.py
```

Curation runs through OpenRouter with a bearer key (`OPENROUTER_API_KEY`).
The model is pinned in `.env` as `OPENROUTER_MODEL` (default
`z-ai/glm-5.3-flash`) — there is no per-run dynamic selection.

YouTube transcript fetches are blocked from datacenter IPs, so an optional
`YT_TRANSCRIPT_PROXY_URL` routes **only** the transcript calls through an
outbound HTTP proxy (e.g. a DataImpulse rotating residential proxy —
`http://<login>__cr.us:<password>@gw.dataimpulse.com:823`). Article fetches
stay direct to keep proxy bandwidth (the billed cost) minimal. Unset →
direct connection, and transcript failures degrade gracefully as before.
Set it locally in `.env` and as the `YT_TRANSCRIPT_PROXY_URL` repo secret
for the digest and smoke-test workflows.

## Example digest

![Example digest](assets/example_digest_screenshot.png)

## Operational notes

- Each category runs as its own GitHub Actions workflow
  (`.github/workflows/digest-<id>.yml`) on a staggered UTC schedule —
  AI 08:00, Tech 08:30, Video games 09:00, Politics & News 09:30 (base
  08:00, +30m each) — so the four digests arrive as separate messages in
  the same inbox, and one category failing fails only its own workflow.
  A workflow's cron and its category config's `schedule` field must stay
  in sync — the category JSON is the source of truth.
- A failed email send does not write `seen_items.json`, so the next run retries
  the same items — fix send failures promptly.
- `source_health.json` and `run_log.jsonl` are written even when the run fails.

## Known gaps / future work

- **Video transcripts are blocked from datacenter IPs.** YouTube serves
  transcripts to residential IPs but blocks datacenter ones — the CI smoke
  test observes `RequestBlocked` (and occasionally `VideoUnplayable` for live
  entries) from GitHub Actions runners, so shortlisted videos there are
  judged on their snippet alone. Isolate-and-continue holds: the run never
  fails on this, the item just loses its transcript deep-read. The fix is in
  place behind a proxy: setting `YT_TRANSCRIPT_PROXY_URL` (repo secret and
  `.env`) routes the `youtube-transcript-api` calls in
  `prefetch.fetch_transcript_excerpt` through an outbound HTTP proxy —
  rotating residential recommended, per
  [youtube-transcript-api · Using other proxy solutions](https://github.com/jdepoix/youtube-transcript-api#using-other-proxy-solutions)
  (verified with `python -m scripts.smoke_test_fetchers`, which reports
  `transcript proxy: configured/unset` and per-channel transcript results).
  Until the secret is set, transcript quality for the three YouTube sources
  remains a laptop-only property.
