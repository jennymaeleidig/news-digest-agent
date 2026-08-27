"""Tuning constants for the news digest agent.

Centralized so things like the User-Agent string or the Copilot CLI
timeout get updated in one place. No environment-variable overrides —
these are tuning constants, not deployment config.

What lives elsewhere on purpose:
  - Source URLs / kinds              category config (categories/*.json)
  - Kind -> fetcher registry          fetchers/registry.py
  - Curation prompt (driving text)   category prompt file (prompts/<id>.md)
  - Cron schedule                   .github/workflows/daily-digest.yml
"""

# Shared across all outbound HTTP fetchers
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
HTTP_TIMEOUT_SECONDS = 30
SNIPPET_CHARS = 1500

# Copilot CLI orchestrator (thin wrapper driving the external Copilot CLI)
COPILOT_TIMEOUT_SECONDS = 900   # process-level cap; the workflow also times out

# Curation input budget. Copilot's argv handling degrades sharply as the `-p`
# prompt grows and ultimately crashes with a V8 boot error (`exited -5`, an
# "Empty MaybeLocal" during its own startup) somewhere past ~1 MB. Curation is
# therefore bounded to keep the assembled prompt well under that failure region:
#   CURATION_MAX_ITEMS        hard cap on items fed to curate() (most-relevant
#                             first; see curator._order_by_relevance)
#   CURATION_PROMPT_MAX_CHARS hard cap on the assembled `-p` prompt; when over,
#                             full-text enrichments are dropped first, then the
#                             lowest-priority (trailing) items, until it fits.
# A normal day (~8-25 items, snippet-only) never approaches these bounds; they
# exist to make a pathological first-run (hundreds of unseen items + pre-fetch
# enrichments) fit in a prompt Copilot can actually ingest.
CURATION_MAX_ITEMS = 200
CURATION_PROMPT_MAX_CHARS = 150_000

# Curation input balancing: cap how many items any single source contributes
# to the input. A busy arXiv day produces far more items than every other
# source combined, and the flat most-relevant-first ordering + item cap used to
# let arXiv fill the entire prompt, starving the release and news sources.
# This per-source cap keeps every source represented (up to this many items)
# so each digest section stays fed regardless of one feed's volume.
CURATION_MAX_ITEMS_PER_SOURCE = 10

# Pre-fetch budget constants — preserved for the later pre-fetch stage
# (ticket 04). Copilot itself is a pure summarizer and never fetches; these
# cap the deterministic Python pre-fetch that deep-reads items before the
# prompt is assembled.
TOOL_CALL_CAP = 20        # max fetches per run
MAX_BYTES = 1_000_000     # 1MB response cap per fetch
MAX_RETURN_CHARS = 50_000 # 50k chars returned per fetch
MAX_REDIRECTS = 5         # redirect recheck bound for the allowlist gate

# Pre-fetch deep-read policy: items whose snippet is shorter than this many
# characters are deep-read (in addition to HN-style linked items, which are
# always deep-read). Snippets at or above the threshold are judged on the
# snippet alone — long-but-vague snippets are an accepted loss.
DEEP_READ_SNIPPET_CHARS = 500

# State management
SEEN_TTL_DAYS = 14
HEALTH_RUNS_KEPT = 14

# main.py
ITEM_AGE_LIMIT_DAYS = 7
