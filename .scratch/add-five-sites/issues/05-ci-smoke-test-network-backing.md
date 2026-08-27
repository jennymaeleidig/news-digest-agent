# 05: CI smoke-test the new network-backed fetchers from a datacenter IP

**Status:** claimed

**Blocked by:** 02 (ai-ml category and native RSS sources), 03 (Hugging Face Daily Papers), 04 (AI Release Tracker)

**What to build:** Before the effort is claimed done, continuous integration smoke-tests the three new network-backed fetchers — Hugging Face Daily Papers, AI Release Tracker, and radarai.top — to confirm each returns a full body from a GitHub Actions datacenter IP. The daily scheduled run executes from exactly that environment, so a fetch that works on a local residential IP is not sufficient evidence; the smoke test proves the no-RSS ingestion path and the native feed hold up where the digest actually runs.

- [ ] The CI job runs a smoke-test that invokes all three network-backed fetchers (Hugging Face Daily Papers, AI Release Tracker, radarai.top) unauthenticated.
- [ ] Each fetcher returns a full body and maps to non-empty items from the datacenter IP, not a partial or bot-blocked response.
- [ ] A fetch that a local residential IP accepts but the datacenter IP blocks is surfaced as a failure, so a scrape that only works locally cannot silently pass.
- [ ] The smoke-test runs within the existing workflow's constraints (10-minute timeout, same dependency set) and does not hammer any single host with retries.
- [ ] A passing smoke-test is a stated entry condition for claiming the effort complete.
