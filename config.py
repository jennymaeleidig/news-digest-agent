"""Tuning constants for the news digest agent.

Centralized so things like the User-Agent string or the OpenRouter
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

# OpenRouter orchestrator (thin wrapper driving the OpenRouter
# chat-completions API; supersedes the Copilot CLI, whose seat hit its monthly
# request quota). The API key is read from the environment at the call site
# (like RESEND_API_KEY), not here.
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_TIMEOUT_SECONDS = 300    # per-HTTP-call cap; the workflow also times out

# The curation model is pinned, not selected per run. Dynamic selection was
# removed: the discount-ranking had no stable public API and drifted between
# models, and a sloppy sale-priced pick regressed digest quality. An
# OPENROUTER_MODEL environment variable overrides this per run (the .env /
# repo secret carries the override); otherwise this default runs.
OPENROUTER_MODEL = "z-ai/glm-5.3-flash"

# Curation input budget. The assembled prompt is sent to OpenRouter as one
# chat user message; a day's items plus full-text enrichments must fit (a) a
# model context window and (b) the owner's daily budget, so curation is
# bounded in bytes to stay well inside both:
#   CURATION_MAX_ITEMS         hard cap on items fed to curate() (most-relevant
#                              first; see curator._order_by_relevance)
#   CURATION_PROMPT_MAX_BYTES  hard cap on the assembled prompt, measured in
#                              UTF-8 bytes; when over, full-text enrichments
#                              are dropped first, then the lowest-priority
#                              (trailing) items, until it fits.
# A normal day (~8-25 items, snippet-only) never approaches these bounds; they
# exist to make a pathological first-run (hundreds of unseen items + pre-fetch
# enrichments) fit in a single model call.
CURATION_MAX_ITEMS = 200
CURATION_PROMPT_MAX_BYTES = 110_000

# Two-stage curation. Stage 1 asks the model to pick which items earn a place
# from titles alone (cheap — every candidate is seen, so no source is starved
# by a busy feed and the deterministic relevance ranking no longer cuts
# anything). Stage 2 summarizes only the selected subset. This is the fallback
# per-section selection ceiling when a section omits ``max_items``; a section's
# ``max_items`` in its category JSON overrides it.
CURATION_SELECT_MAX_ITEMS = 15

# Pre-fetch budget constants — preserved for the later pre-fetch stage
# (ticket 04). The curation model itself is a pure summarizer and never
# fetches; these cap the deterministic Python pre-fetch that deep-reads items
# before the prompt is assembled.
TOOL_CALL_CAP = 20        # max fetches per pre-fetch call (runs per section, after stage-1 selection)
MAX_BYTES = 1_000_000     # 1MB response cap per fetch
MAX_RETURN_CHARS = 50_000 # 50k chars returned per fetch
MAX_REDIRECTS = 5         # redirect recheck bound for the allowlist gate

# Pre-fetch deep-read policy: items whose snippet is shorter than this many
# characters are deep-read (in addition to HN-style linked items, which are
# always deep-read). Snippets at or above the threshold are judged on the
# snippet alone — long-but-vague snippets are an accepted loss.
DEEP_READ_SNIPPET_CHARS = 500

# Transcript deep-read cap: a fetched YouTube transcript is reduced
# deterministically (evenly-spaced excerpt windows, no model pass) to at most
# this many characters before it is attached as enrichment. Well under
# MAX_RETURN_CHARS and far under the CURATION_PROMPT_MAX_BYTES budget.
TRANSCRIPT_MAX_CHARS = 12_000

# State management
SEEN_TTL_DAYS = 14
HEALTH_RUNS_KEPT = 14

# main.py
ITEM_AGE_LIMIT_DAYS = 7
