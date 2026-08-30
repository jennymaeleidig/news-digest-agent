# Source trust tiers

> **Partially superseded (2026-08-30):** tiers are no longer a selection
> weight. Curation order is recency-only (`_order_by_relevance`), the
> prompts carry no tier information, and items are judged solely on
> alignment to the section's scope. The tier still exists in category
> config and renders in each digest entry's source line as attribution —
> the framing caveat below stands. Kept unrewritten as the record of the
> original decision.

Every source carries a 1–4 trust tier (borrowed from the Kagi trust ladder)
that both orders items for curation (lower number = stronger primary signal,
tied by recency) and sets how claims from that tier may be framed. Tiers are
a *selection weight and a framing caveat*, never a strict inclusion gate: a
low tier is never grounds to include a weak item, but a sharp tier-4 piece
can still earn a shortlist slot. Unknown/misconfigured sources default to
tier 4, so their items are dropped before real ones when the budget caps.

- **Tier 1** — Primary, independent of interested parties: filings, raw data,
  peer-reviewed studies, audit reports, original transcripts.
- **Tier 2** — Disinterested institutional reporting relaying Tier 1 material.
- **Tier 3** — Interested-party primary: corporate reports, press releases,
  think-tank white papers, someone's own account. Never the terminal source
  for a consequential claim.
- **Tier 4** — Commentary, aggregation, AI summaries: find and frame, never
  settle.

Tier 3 and tier 4 claims are attributed to their source rather than stated as
settled fact (e.g. "per OpenAI", "the lab reports X") so a reader can weigh an
unverified or self-interested claim correctly.
