# News digest agent

A topic-agnostic daily digest engine: fetch curated sources, have an LLM pick
and summarize the day's most relevant items, email the result. Each output is
one self-contained **digest** per category. It currently ships with an AI/ML
news category, but the engine is not AI/ML-specific — future categories will
add more general-subject digests.

## Language

### The unit of content

**Category**:
The top-level unit of the product — one digest per run for its own audience,
with its own state and recipient. A category declares its own **Sections** (the
single source of truth for how its digest is divided).
_Avoid_: feed, newsletter, publication

**Source**:
A curated feed whose items flow into a category's digest, scoped by trust tier
and an optional topic allow-list. Every source feeds one or more declared
**Sections**; the no-double-pick guard places each picked item in exactly one
of them per digest.
_Avoid_: channel, stream

**Section**:
A named division of a digest with a defined scope — what belongs in it. A
category's declared sections are authoritative: their names, order, and scope;
the digest renders one heading per non-empty section.
_Avoid_: subsection, topic, bucket

**Schedule**:
A category's own declared run time, independent of every other category's;
its workflow mirrors the declared schedule.
_Avoid_: cadence, cron

**Item**:
A single raw entry from a source — title, link, publish date, a content snippet,
and an optional linked URL (a link post's external target).
_Avoid_: article, post, story

**Trust tier**:
A source's 1–4 standing, borrowed from Kagi — a selection weight and a framing
caveat. Lower tiers are higher-priority primary signal; higher tiers are
attributed rather than asserted.
_Avoid_: priority, score

**Topics**:
A source's optional relevance allow-list narrowing a broad feed to a domain — an
item is kept only if a listed term appears in its title or snippet.
_Avoid_: keywords

### The pipeline

**Fetch**:
The stage that pulls items from a category's sources; one source's failure never
stops the run.

**Filter**:
The stage that narrows fetched items before curation — dropping items outside the
age window, topic misses, and already-**seen** items.

**Curation**:
The stage that selects the day's best unseen items and writes their **shortlist**.
A pure summarizer: works only from provided text, never the network.
_Avoid_: summarization, selection

**Shortlist**:
The curated selection Curation produces; the digest renders from it.

**Pre-fetch**:
The stage before Curation that obtains full text for **deep-read** items.

**Deep-read**:
Selecting and fetching an item's substance so it can be judged on more than
its snippet. Articles are gated by an allowlist; videos are gated by their
video id (a transcript fetch, no allowlist needed).
_Avoid_: fetch full article

**Enrichment**:
An item's obtained full text, attached so Curation can judge it.

### The deliverable

**Digest**:
The curated deliverable — one per category per run, rendered from the shortlist.
Distinct from the **email** that carries it.
_Avoid_: report

**Email**:
Delivery of a digest. One-to-one with a digest, but they fail independently.

**Digest entry**:
One item's summarized line inside a digest. Prose-only, not a first-class
concept.

**Empty digest**:
A healthy digest that found nothing notable today. Distinct from a
**broken-agent email**.
_Avoid_: quiet day

**Broken-agent email**:
What is sent when Curation fails. An empty digest is a success; this is not.
_Avoid_: error digest, failure email

### State

**Seen**:
An **item** already delivered, so excluded from future runs by dedup. Recorded
only after a successful send, expiring after a window.
_Avoid_: covered, consumed, read

**Run**:
One category's execution of the full pipeline — fetch → filter → curate → email.
_Avoid_: pass, execution

**Source health**:
The per-source success/failure record surfaced with each digest.

**Run log**:
The per-run record left behind so post-mortem data survives a failure.
