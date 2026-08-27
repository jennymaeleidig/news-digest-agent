# Deployment plan: OpenRouter API in GitHub Actions

Status: **implemented** in `.github/workflows/daily-digest.yml` — auth via a
single **`OPENROUTER_API_KEY`** bearer secret, billed per token to the key
owner's OpenRouter account.

Curation previously ran through GitHub Copilot CLI; the seat hit its monthly
request quota (`copilot CLI exited 1: You have exceeded your monthly quota`),
so curation now POSTs to OpenRouter's OpenAI-compatible
`/api/v1/chat/completions` endpoint instead. This document is the deployment
record for wiring that API into the GitHub Actions workflow. Curation stays a
pure summarizer that sends plain text and no tools; beyond transport and auth,
the run shape later became two-stage (select by title, then summarize).

## 1. What changed

Four edits landed in `run-digest`:

1. **Dropped the Node.js + Copilot CLI install steps** — nothing to install
   anymore; OpenRouter is plain HTTPS over `requests`.
2. **One secret** — `OPENROUTER_API_KEY`, a bearer key from
   <https://openrouter.ai/settings/keys>. Billed per token to the key owner's
   OpenRouter balance; no org policy, no PAT scope, no monthly seat.
3. **Pinned model** — the curation model is fixed in `config.py`
   (`OPENROUTER_MODEL`, default `z-ai/glm-5.3-flash`), overridable per run by
   the `OPENROUTER_MODEL` env var / repo variable (model ids aren't secret).
   Dynamic selection was removed:
   the discount-ranked catalog had no stable public API and drifted between
   models, and a sloppy sale-priced pick regressed digest quality. The model
   id is recorded in `run_log.jsonl`.
4. **Two-stage curation** — each section is curated in two calls, both on the
   pinned model: stage 1 asks the model to pick which items earn a place from
   titles alone (every candidate is seen; the deterministic per-source cut was
   removed so one busy feed can't starve the others), stage 2 summarizes and
   formats only the selected subset. `CURATION_SELECT_MAX_ITEMS` bounds one
   section's stage-2 prompt.

`smoke-test-fetchers` is unchanged; it does not invoke OpenRouter.

## 2. Auth

| Thing | Value |
|---|---|
| Secret needed | `OPENROUTER_API_KEY` (bearer key from openrouter.ai) |
| Billing | per-token, to the key owner's OpenRouter balance |
| Prerequisite | an OpenRouter account with a credit balance |
| Workflow env | `OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}` |
| Model override | optional `OPENROUTER_MODEL` repo variable (`vars.OPENROUTER_MODEL`) |
| Local env | same key in `.env` (`OPENROUTER_API_KEY=…`) |
| GitHub permission | not required (no PAT, no `github.token`) |

## 3. Human prerequisites

Do these before the first real run:

1. **Create the key**: <https://openrouter.ai/settings/keys> → **Create Key**,
   copy the value (starts `sk-or-v1-`).
2. **Fund the account**: <https://openrouter.ai/settings/credits> → add credits.
   The pinned model (`z-ai/glm-5.3-flash`) costs almost nothing, but a zero balance returns HTTP 402
   and turns every run into a broken-agent email.
3. **Store it**: repo → **Settings → Secrets and variables → Actions → New
   repository secret**, name it `OPENROUTER_API_KEY`, paste the key.
4. **Secrets**: `RESEND_API_KEY` and `RECIPIENT_EMAIL` must already exist.
5. **(Optional) override the model**: the default is `z-ai/glm-5.3-flash`;
   set an `OPENROUTER_MODEL` **repository variable** (model ids aren't secret)
   to pin a different id — run `setup-deploy.sh` and it will offer this.

## 4. Cutover

1. Changes are on `main`.
2. Run the workflow once manually: **Actions → Daily digest → Run workflow**.
3. Verify (below). Do not wait for the next 16:17 UTC scheduled run.
4. Confirm the schedule is still registered and not disabled.

## 5. Verification checklist

- [ ] `data/run_log.jsonl` has today's row with `curate_error: null`,
      `items_output > 0` (or a clean empty digest), and a `model` field equal
      to the pinned id (`z-ai/glm-5.3-flash` unless overridden).
- [ ] The delivered email is a real digest, not "Agent error during curation".
- [ ] `prompt_tokens` / `completion_tokens` are populated (OpenRouter reports
      usage; token accounting is back).
- [ ] No `OPENROUTER_API_KEY is not set` or `OpenRouter HTTP 402` in the job log.
- [ ] The "Commit state changes" step still pushes `data/` files (`contents:
      write` retained).

## 6. Rollback

- **Key fails** (revoked, zero balance) → rotate the `OPENROUTER_API_KEY`
  secret / re-fund the account; no code change needed.
- **Pinned model produces weak digests** → set `OPENROUTER_MODEL` to a
  known-good model id; that is an operator change, not a rollback.
- To restore Copilot CLI, revert this commit and re-run the
  `setup-deploy.sh` wizard's Copilot stage; `curator.py` carried the
  subprocess wrapper in git history.

## 7. Risks to know

- **Model quality variance.** The model is pinned to `z-ai/glm-5.3-flash`; if
  its summaries drift or it paraphrases titles, swap `OPENROUTER_MODEL` to a
  known-good id rather than reintroducing dynamic selection. Watch the `model`
  field in `run_log.jsonl`.
- **Prompt size.** The HTTP body has no argv limit, but a model
  context window does, and so does the daily budget. Already bounded in
  `config.py` (`CURATION_MAX_ITEMS`, `CURATION_PROMPT_MAX_BYTES`); keep those
  in step with whatever model is pinned.
- **Key hygiene.** The bearer key is a spendable credential and is not printed
  by the workflow, but rotate it if it ever leaks. It is redacted from error
  messages by `main.sanitize_error` before anything touches disk.
- **No `pull_request` trigger.** The workflow fires only on `schedule` +
  `workflow_dispatch`, so the key is not exposed to fork-triggered runs. Keep
  it that way.

## 8. Sources

- [OpenRouter API reference](https://openrouter.ai/docs/api_reference/overview)
- [OpenRouter quickstart](https://openrouter.ai/docs/quickstart)
- [OpenRouter API keys](https://openrouter.ai/settings/keys)
