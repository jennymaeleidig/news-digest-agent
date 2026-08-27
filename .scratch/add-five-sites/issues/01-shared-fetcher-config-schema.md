# 01: Shared fetcher-config schema for bespoke feedless source kinds

**Status:** claimed

**Blocked by:** None (can start immediately)

**What to build:** A shared fetcher-config schema is introduced as a small prefactor so future feedless sites slot in as config. Each bespoke feedless source kind's configuration takes the same shape — a `url` plus `item`/`title`/`link`/`date` field paths or selectors — so the two bespoke fetcher kinds (Hugging Face Daily Papers and AI Release Tracker) consume one config contract rather than each inventing its own. The schema pins the config shape only; it deliberately leaves parsing mechanics to each consuming kind. It must not force the two kinds into a single fetch mechanism, introduce an HTML-selector `webpage` kind, or bring in a headless browser.

- [ ] A shared fetcher-config schema exists that covers a `url` plus `item`/`title`/`link`/`date` field paths or selectors, so future feedless sites configure through it.
- [ ] The bespoke feedless source kinds consume this shared config schema as the shape of each kind's configuration (the same `url` + item/title/link/date contract), rather than each defining its own ad-hoc config.
- [ ] The schema only pins the config-shape contract and leaves parsing mechanics to each consuming kind — it does not dictate a single implementation of how fields are mapped or selectors matched.
- [ ] The shared schema introduces no single-shared-kind fetch framework: the bespoke kinds stay distinct (JSON-API field mapping vs HTML-selector scraping), no HTML-selector `webpage` kind is built, and no headless browser or external federation is added.
