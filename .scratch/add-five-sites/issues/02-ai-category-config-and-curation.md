# 02: Broaden the ai-ml category and add the two native RSS sources

**Status:** claimed

**Blocked by:** None (can start immediately)

**What to build:** The `ai-ml` category covers the reader's expanded ask: its display name reads "AI" instead of "LLM" (the stable `ai-ml` id is unchanged so existing seen and source-health state survives), its curation prompt now welcomes AI ecosystem and regional news with substance — especially Chinese models and ecosystem (DeepSeek, Qwen, GLM and peers) — while keeping the existing cut list, and two plain `kind: rss` sources land in the category and start flowing items through the existing RSS fetch, filter, curation, pre-fetch, and digest path with no new code.

- [ ] The `ai-ml` category display name reads "AI" while its `id` stays `ai-ml`, so the digest subject changes from an LLM to an AI digest and existing seen / source-health state is preserved.
- [ ] The curation prompt explicitly welcomes AI ecosystem and regional news with substance (especially Chinese models and ecosystem such as DeepSeek, Qwen, GLM and their peers) while keeping the existing cut list (off-focus ML, linguistics, hype / press listicles); no AI-infrastructure/energy bucket is introduced.
- [ ] radarai.top is added as a `kind: rss` source at its native feed URL with trust tier 3 and its own homepage set, so it flows through the existing fetch → filter → curation → pre-fetch → digest path unchanged.
- [ ] reddit r/LocalLLaMA is added as a `kind: rss` source at its native feed URL with trust tier 4 and its own homepage set, flowing through the same unchanged path.
- [ ] Both new sources are left unfiltered (no topics allow-list), so curation ranks their quality.
- [ ] A single live HTTP-200 check against each new feed returns a full body (no retry loops added for either, since rapid repeats risk a 429 from Reddit).
- [ ] The run records source health for each new source so a down feed surfaces in the digest footer rather than hiding, and run-log rows capture the category run.

## Comments

- Claimed and implemented (ticket-implementer). Config `categories/ai-ml.json`: display name renamed "LLM"→"AI" (id stays `ai-ml`); added radarai.top (kind:rss, tier 3) and Reddit r/LocalLLaMA (kind:rss, tier 4), each with its own homepage and no topics allow-list (unfiltered). Prompt `categories/prompts/ai-ml.md` broadened to welcome AI ecosystem & regional news with substance (DeepSeek, Qwen, GLM and peers) while keeping the existing cut list: explicitly rules out any AI-infrastructure/energy bucket.
- Tests: added `tests/test_ai_ml_category_config.py` at the category-loader seam (`load_category` on the real `categories/ai-ml.json`) asserting name/id, the broadened prompt, and both new RSS sources (kind/tier/url/homepage/unfiltered).
- Live HTTP-200 checks (AC6): both native feeds fetched once via the fetch tool and returned full bodies — the radarai.top feed (RSS 2.0, 45,479-byte body, many items incl. GLM/Qwen coverage) and the Reddit r/LocalLLaMA feed (native Atom, 25 entries, published timestamps). No retry loops added (avoid hammering Reddit on rapid repeats).
- Source-health + run-log (AC7): covered by the existing generic run-seam (`tests/test_run_seam.py`); the new sources ride the same fetch→health→run-log path, confirmed present by the config test.
- Environment caveat: this sandbox blocks PyPI, so pytest could not be installed here; the config-seam assertions were verified by direct `load_category` invocation (green) and the unit test file compiles cleanly. The suite should be run once in an environment with pytest installed before closing.
