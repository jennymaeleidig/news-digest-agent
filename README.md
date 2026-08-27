# News digest agent

A category-driven daily news digest: fetch curated sources, have an LLM pick
and summarize the day's most relevant items, email the result. Ships configured
for AI/ML news (LLMs and AI coding agents), but the engine is topic-agnostic —
any subject is just another category. Runs on GitHub Actions at 16:17 UTC.

> Fork of [al-strunova/ai-news-digest-agent](https://github.com/al-strunova/ai-news-digest-agent).

## How it works

`fetch → filter → curate → email`, one email per category.

- **Fetch** — one module per source kind, dispatched through a `kind → fetcher`
  registry (`rss`, `huggingface_papers`, `reddit_rss_api`,
  `airelease_tracker`). Source failures are isolated, never fatal.
- **Filter** — drop items outside the age window, off-topic items (per-source
  allow-list), and already-seen items (14-day dedup).
- **Curate** — GitHub Copilot CLI as a pure summarizer (no network tools), one
  call per non-empty section, re-stitched in the category's declared order. A
  pre-fetch stage deep-reads thin-snippet items first.
- **Email** — markdown → HTML via Resend.

## Categories

Every `categories/*.json` is one category, discovered and run independently —
each with its own recipient, state, and digest email. A category is two files:

- **`categories/<id>.json`** — the single source of truth for structure:
  `id`, `name`, an ordered `sections` list (name + what belongs there), and
  `sources` (tier, kind, url, section, optional topics/age-window).
- **`categories/prompts/<id>.md`** — the curation prompt. Section-agnostic:
  section names and descriptions are injected from the JSON at run time.

Adding a category is "drop one JSON + one prompt"; no stage in the pipeline
knows a section or source by name.

## State

Three committed files in `data/`, keyed by category `id`:

- `seen_items.json` — dedup set (14-day expiry, written on successful send).
- `source_health.json` — per-source success/failure, shown as a digest footer.
- `run_log.jsonl` — per-run duration, item counts, prompt size, errors.

## Setup

```
git clone <repo>
cd news-digest-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # RESEND_API_KEY, RECIPIENT_EMAIL
python main.py
```

Curation runs through Copilot CLI (a flat seat — no provider keys). One-time
interactive login: `copilot`, then `/login` (or `gh auth login`).

## Example digest

![Example digest](assets/example_digest_screenshot.png)

## Operational notes

- A failed email send does not write `seen_items.json`, so the next run retries
  the same items — fix send failures promptly.
- `source_health.json` and `run_log.jsonl` are written even when the run fails.
