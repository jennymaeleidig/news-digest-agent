# Spec — Add four sites (and solve the no-RSS ingestion path)

## Problem Statement

The reader of the daily AI/ML digest wants stronger coverage of the **Chinese
model and ecosystem** (DeepSeek, Qwen, GLM and peers) and of the broader AI
ecosystem beyond the LLM/coding-agent focus that the single `ai-ml` category
currently serves. Four sources would fill that gap — `huggingface.co/papers`,
`aireleasetracker.com/latest`, `radarai.top/en/` and `reddit.com/r/LocalLLaMA`
— but three of them publish **no RSS**, and the digest's fetch pipeline only
ships a single `kind: rss` fetcher. Adding these sites would otherwise be a
string of one-off scraping hacks, each handled the moment it ships and
forgotten. The no-RSS ingestion path needs to be a **repeatable mechanism**
(a new source kind is a fetcher module plus a registration, not a pipeline
edit), so that each new feedless site ships as a real, working source in the
daily run.

## Solution

Add the four sources to the existing `ai-ml` category and broaden that
category's curation prompt to welcome AI ecosystem and regional news with
substance — especially Chinese models and ecosystem — keeping the existing cut
list. Two sources are plain `kind: rss` adds that ride the existing RSS
fetcher unchanged. The two feedless sites get new **bespoke in-repo fetcher
kinds** registered on the existing `kind → fetcher` registry seam, so the
no-RSS path is a repeatable mechanism rather than a one-off hack. The category
display name is renamed "LLM" → "AI" (the `id` stays `ai-ml` so seen/health
state survives). Trust tiers order the new items for curation, and a topics
allow-list keeps the one broad feed (HF papers) on-focus.

The four in-scope sources, their exact extraction targets, and their tiers:

| Source                      | kind                                                  | URL / target                                                                   | Tier |
| --------------------------- | ----------------------------------------------------- | ------------------------------------------------------------------------------ | ---- |
| radarai.top/en/             | `rss`                                                 | `https://radarai.top/en/feed.xml` (native RSS 2.0, verified 200 full body)     | 3    |
| reddit.com/r/LocalLLaMA     | `rss`                                                 | `https://www.reddit.com/r/LocalLLaMA.rss` (native Atom, ~25 entries, verified) | 4    |
| huggingface.co/papers       | `huggingface_papers`                                  | `https://huggingface.co/api/daily_papers` (JSON, ~50 entries)                  | 3    |
| aireleasetracker.com/latest | `requests` + `BeautifulSoup` scrape of `/latest` HTML | server-rendered list of 27 model releases                                      | 4    |

## User Stories

1. As a working AI engineer reader, I want HF Daily Papers in my digest, so that I see the latest notable papers without browsing HF myself.
2. As a working AI engineer reader, I want AI model releases (Qwen, Z.ai, DeepSeek, OpenAI, xAI, Google, Meta, Moonshot, …) surfaced from AI Release Tracker, so that I stay current on which models just shipped.
3. As a working AI engineer reader who specifically follows the Chinese model ecosystem, I want radarai.top's aggregated AI coverage, so that Chinese model and ecosystem news (DeepSeek, Qwen, GLM and peers) reaches me.
4. As a working AI engineer reader, I want r/LocalLLaMA community highlights in my digest, so that I catch substantive community discussion around LLM engineering.
5. As a user, I want the `ai-ml` category display name to read "AI" rather than "LLM", so that the email subject reflects the broadened coverage.
6. As a user, I want the category `id` to stay `ai-ml`, so that my existing seen/health state survives the rename.
7. As a user, I want the HF papers source scoped by LLM/agent topics, so that only the on-focus papers flow in and the digest stays tight.
8. As a user, I want radarai.top and AI Release Tracker left unfiltered, so that curation, not a keyword filter, ranks what matters.
9. As a user, I want the two native feeds (radarai, Reddit) handled by the existing RSS fetcher, so that no bespoke code is needed for them.
10. As a developer, I want a no-RSS source added to the digest as a fetcher kind registered on the existing `kind → fetcher` registry, so that no pipeline edit is ever needed to add one.
11. As a developer, I want the HF papers fetcher to hit the JSON API and map title/summary/date without any DOM scraping, so that the implementation is small and robust.
12. As a developer, I want the HF papers fetcher to use `paper.submittedOnDailyAt` as the item's published date, so that the time-window filter and dedup reflect the Daily Papers feature day rather than the arXiv date.
13. As a developer, I want the AI Release Tracker fetcher to scrape the server-rendered `/latest` HTML with requests + BeautifulSoup and no headless browser, so that the extraction is JS-free and dependency-light.
14. As a developer, I want each new fetcher to return HTTP errors rather than raise them and to isolate-and-continue per source, so that one broken source never stops the run.
15. As a developer, I want the bespoke fetchers to send a browser-like User-Agent and full header set, so that bot-sensitive hosts respond with a full body.
16. As a user, I want every new source to set a `homepage` so the deep-read allowlist permits its own article hosts; radarai's external links then stay snippet-only, so that the no-RSS sources enrich properly within the existing pre-fetch policy.
17. As a developer, I want the two bespoke fetcher kinds (huggingface_papers, AI Release Tracker) to stay **distinct** — JSON-API field mapping and HTML-selector scraping are different mechanisms — while sharing one config-schema contract, so that future feedless sites slot in as config without a sprawling framework.
18. As a developer, I want HF papers and AI Release Tracker each configured through a shared fetcher-config schema (url, item/title/link/date field paths or selectors), so future feedless sites slot in as config.
19. As a developer, I want the CI to smoke-test the three new network-backed fetchers before the effort is claimed done, so that the GitHub Actions datacenter-IP environment can't silently break a scrape that works locally.
20. As a user, I want a broken new source to still show up in my digest's source-health footer rather than hide, so that I'm aware when one feed is down.
21. As a developer, I want the new fetchers tested against external, deterministic behavior (correct field mapping into Items), so that tests don't couple to implementation details.
22. As a user, I want the broader curation prompt to welcome ecosystem and regional news with substance while keeping the existing cut list, so that breadth doesn't become noise.

## Implementation Decisions

### Strategy — bespoke in-repo fetcher kinds on the existing registry seam

The no-RSS strategy is **bespoke in-repo fetcher kinds**, added to the existing
`kind → fetcher` registry — the seam the registry already reserves for
`newsletter` and site-specific kinds. The hosted ai-rss-feeds pattern
(someone else's scraper harness generating static RSS/Atom) is **not adopted**;
no external federation or proxy dependency. Adding a new source kind is exactly
one fetcher module plus one registration — no pipeline edit. Source lifecycles,
filtering, curation, enrichment, state, and delivery all keep working unchanged
because the new kinds are dispatched through the same `fetch_one` seam the
pipeline already calls.

The registry dispatch contract (unchanged from today):

```python
# kind -> fetcher; a fetcher is Callable[[Source], FetchResult].
FETCHERS: dict[str, Callable[[Source], FetchResult]] = {}

def register(kind: str, fetcher: Callable[[Source], FetchResult]) -> None: ...
def fetch_one(source: Source) -> FetchResult:
    """Dispatch to the registered fetcher; unknown kind -> isolated failure."""
```

All new fetchers reuse the shared types and helpers (`Item`, `FetchResult`,
`strip_html`) and the standard hygiene the RSS fetcher already models:
browser-like User-Agent + full header set, HTTP errors returned (not raised),
isolate-and-continue per source.

### The four sources

**radarai.top/en/** — plain `kind: rss`, url `https://radarai.top/en/feed.xml`.
Native RSS 2.0, verified HTTP 200 with a full 45,479-byte body through the
repo's exact header set; the `IncompleteRead` did not reproduce with
the full header set, so no retry loop is needed. Its item links point to
external articles (it is an aggregator) — the RSS fetcher already copies each
entry's link, so no bespoke handling. Tier 3, prioritized.

**reddit.com/r/LocalLLaMA** — plain `kind: rss`, url
`https://www.reddit.com/r/LocalLLaMA.rss`. Native Atom, verified HTTP 200 and
25 entries parsed cleanly by feedparser through the RSS fetcher. Caveat: Reddit
returns **429 on rapid repeats** of the same endpoint — the once-daily run is
fine, but **do not add retry loops that hammer it**; rely on the single daily
scheduled run. Tier 4.

**huggingface.co/papers** — a new bespoke `huggingface_papers` JSON fetcher
kind hitting `https://huggingface.co/api/daily_papers` (HTTP 200,
`application/json`, ~50 entries). **No scraping, no headless browser, no
`__NEXT_DATA__`.** Field map:

```python
# GET https://huggingface.co/api/daily_papers  -> list of {title, summary, paper{id, ...}}
item.title     <- entry["title"]
item.url       <- f"https://huggingface.co/papers/{entry['paper']['id']}"
item.snippet   <- entry["summary"]                      # the abstract
item.published <- entry["paper"]["submittedOnDailyAt"]  # Daily Papers feature day —
                                                        # use THIS for the window/dedup,
                                                        # NOT paper.publishedAt (arXiv date)
# also available (unused by the digest): paper.authors, paper.upvotes,
#   numComments, paper.ai_summary, paper.ai_keywords
```

The published date drives the time-window filter and seen-dedup — it must be
the Daily Papers feature day, not the arXiv publish date. Distinct from the
already-configured HF blog feed (`/blog/feed.xml`) and from the raw arXiv RSS
sources. Tier 3.

**aireleasetracker.com/latest** — a bespoke fetcher kind using `requests` +
`BeautifulSoup` to scrape the **server-rendered** `/latest` HTML
(unauthenticated, HTTP 200, ~144 KB, Next.js App Router, 27 model-release
items embedded in the initial DOM). **No headless browser, no JSON API** (the
API routes all 404). Item shape:

- container: `<a href="/model/{provider}/{slug}">` (27 present)
- title: `span` with classes `text-white` + `truncate`
- provider: `span` with classes `text-gray-500` + `truncate`
- date: right-aligned `div` in the anchor, text like `"Wed, Aug 26 2026"` — **not
  an ISO timestamp, must be parsed**
- link: `https://aireleasetracker.com/model/{provider}/{slug}`

The `published` date is parsed from the display string into a timezone-aware
value (parsing mechanics left to the code), and it is treated **normally** by
the filter: an item whose parsed date falls outside the age window is an
ordinary out-of-window drop, identical to any other source — releases are not
special-cased into the digest regardless of date.

Tier 4, left unfiltered.

### Category placement

All four sources land in the existing `ai-ml` category. The category's display
`name` is renamed **"LLM" → "AI"** (used in the email subject); the `id` stays
`ai-ml` so seen/health state survives. The curation prompt is broadened to
explicitly welcome AI **ecosystem / regional** news with substance —
especially Chinese models and ecosystem (DeepSeek, Qwen, GLM and peers) —
while keeping the existing cut list (off-focus pure ML, linguistics, hype /
press listicles). **No AI-infrastructure/energy bucket.**

### Trust tiers and topics

Tiers (Kagi ladder, lower = more trusted) order items for curation:
- radarai.top/en/ — **3**
- huggingface.co/papers — **3**
- reddit r/LocalLLaMA — **4**
- aireleasetracker.com/latest — **4**

radarai.top is prioritized over AI Release Tracker because the reader actively
wants Chinese model/ecosystem coverage.

Topics allow-list **only on huggingface.co/papers** — LLM/agent terms: `llm`,
`language model`, `agent`, `reasoning`, `fine-tun`, `align`, `rlhf`,
`pretrain`, `multimodal`, `code generation`, `coding`, `retrieval`, `rag`,
`in-context`, `chain-of-thought`, `synthetic data`, `benchmark`, `scaling`.
The other new feeds are niche and stay unfiltered; curation ranks quality.

### Spec scope — light shared seam

Build the bespoke fetchers now; **do not pre-commit to a general framework.**
The two bespoke kinds stay **distinct**: `huggingface_papers` is JSON-API field
mapping and the AI Release Tracker kind is HTML-selector scraping — different
mechanisms, so the single-shared-kind condition does **not** apply.
What they share is the **config-schema contract**: each kind's config is the
same shape (`url`, plus `item`/`title`/`link`/`date` field paths or
selectors), so future feedless sites slot in as config. The spec pins that
config schema but leaves parsing mechanics to the code. **No proactive
HTML-selector `webpage` kind** — its only potential consumer (NeuralWatt) is
out of scope.

### Enrichment

Every new source rides the existing pre-fetch policy unchanged: an item is
deep-read only if its snippet is below `DEEP_READ_SNIPPET_CHARS` (500) or it
is HN-linked. Each new source sets its `homepage` so the deep-read allowlist
permits its own article hosts. radarai's items link to external articles,
which will fail the allowlist and stay **snippet-only** (accepted, fine).

## Testing Decisions

A good test asserts **external, deterministic behavior** — that a fetcher,
given a source and a captured response, produces the correct `Item`s with the
right title, url, published date, and snippet (including the AI Release
Tracker date-string parsing and the HF `submittedOnDailyAt` mapping). Tests
must not couple to implementation details like which internal function parses
a JSON path or which selector expression matches; they assert the resulting
items, not the parsing mechanics.

The modules under test are the new bespoke fetcher kinds and, where one shared
config-driven kind is extracted, that shared kind and its config schema. The
plain `kind: rss` sources (radarai, Reddit) need no new fetcher tests — they
exercise the existing RSS fetcher path that is already covered; the spec-level
verification for them is a live 200-once check rather than unit tests.

Prior art in this repo: the suite runs **fully offline** — the offline test
bootstrap stubs third-party deps (`requests`, `feedparser`, `bs4`, …) in
`sys.modules` before any app import, and a run-seam test drives the pipeline
through injected fakes (fake fetcher registry, fake curator, fake emailer,
in-memory state) with zero network. New fetcher tests follow the same shape:
feed each
fetcher a captured/stubbed response (JSON for HF papers, an HTML string for AI
Release Tracker) and assert the mapped `FetchResult`/`Item`s, plus the shared
isolate-and-continue behavior (HTTP error → failure result, not a raise).

**CI / geo caveat:** GitHub Actions runners use
datacenter IPs that bot protection may block even when a scrape works locally.
Before the effort is claimed done, the CI must run a **smoke-test** of the
three new network-backed fetchers (huggingface_papers, AI Release Tracker,
radarai) to confirm they return full bodies from a datacenter IP — a scrape
that works on a local residential IP is not sufficient evidence of a working
source in the scheduled run.

**Entry condition for claiming the effort complete:** a **passing**
smoke-test of the three new network-backed fetchers (ticket 05) is a stated
entry condition for claiming ticket 05 done and the effort complete. The
smoke-test must run unauthenticated from the GitHub Actions datacenter IP,
assert each fetcher returns a full body mapping to non-empty items, honor the
workflow's 10-minute timeout and dependency set, and never retry a host. The
job lives in `.github/workflows/daily-digest.yml` (see
`scripts/smoke_test_fetchers.py`).

## Out of Scope

- **NeuralWatt / `neuralwatt.com/news` is OUT of scope.** The user dropped it;
  AI energy/cleantech sits off the digest's focus. It must **not** appear as a
  planned source, and there is no AI-infrastructure/energy bucket. It is
  dropped and out of scope. (Consequence: no HTML-selector
  `webpage` kind is built, because NeuralWatt was its only consumer.)
- **No general no-RSS framework / API** is built in this effort — bespoke
  fetchers on the existing registry seam; the two new kinds remain **distinct**
  (JSON-API vs HTML-selector mechanisms) and share only a config-schema
  contract.
- **No headless browser** is introduced (Playwright or otherwise) for any
  source.
- **No adoption of the hosted ai-rss-feeds pattern** (no federation, no static
  RSS proxy dependency).
- **No retry loops** for the Reddit feed (it 429s on rapid repeats).
- No change to the deep-read / pre-fetch policy itself beyond setting each new
  source's `homepage`.

## Further Notes

- The `ai-ml` category currently has three RSS sources (arXiv LLM, arXiv Code,
  Hugging Face blog). The four new sources bring it to seven.
- Hidden/unused HF fields (`authors`, `upvotes`, `numComments`, `ai_summary`,
  `ai_keywords`) are available for future use but are not consumed by the
  digest.
- The AI Release Tracker page also embeds a Next.js RSC flight payload
  (`self.__next_f.push`), but it is unnecessary — the server-rendered list is
  sufficient.
- The effort was originally scoped as "add five sites"; it is now four after
  NeuralWatt was dropped. The remainder of this spec reflects that.
- The daily schedule (`17 16 * * *`) and the workflow's 10-minute timeout are
  unchanged; the added fetchers run within the existing `run_category` fetch
  stage alongside the RSS sources.
- The category display-`name` rename "LLM" → "AI" changes the email subject
  line from an "LLM digest" subject to an "AI digest" subject. This is an
  intentional, user-visible consequence of the broadened coverage,
  not an accidental side effect.
