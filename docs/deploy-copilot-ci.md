# Deployment plan: Copilot CLI in GitHub Actions

Status: **implemented** in `.github/workflows/daily-digest.yml` — auth via a
fine-grained **personal access token** (route B), billed to the owner's own
Copilot seat.

Curation in this repo runs through GitHub Copilot CLI, invoked non-interactively
from `curator.py` as a pure summarizer:

```
copilot -p "<assembled prompt>" -s --no-ask-user --allow-tool=
```

The tool allow-list is empty by design (network access lives in the
deterministic Python pre-fetch stage, never in Copilot). This document is the
deployment record for wiring that CLI into the GitHub Actions workflow.

## 1. What changed

GitHub-hosted runners do **not** ship `@github/copilot` preinstalled (confirmed
against the `ubuntu-24.04` runner manifest). The workflow previously set Copilot
auth but never installed the CLI, so every curation subprocess failed with
`copilot CLI not found on PATH` and only broken-agent emails could be produced.

Three edits landed in `run-digest`:

1. **Install the CLI** — `actions/setup-node@v7` + `npm install -g @github/copilot`.
2. **Authenticate via PAT** — `COPILOT_GITHUB_TOKEN` env, a fine-grained PAT
   with the **Copilot Requests** permission. This bills the token owner's own
   Copilot seat, which suits a single-owner repo; no org policy toggle needed.
3. **Permissions** — left as `contents: write` only (the PAT route does not
   require the `copilot-requests: write` permission).

`smoke-test-fetchers` is unchanged; it does not invoke Copilot.

## 2. Auth: the two routes

| | Route B — fine-grained PAT (**implemented**) | Route A — `GITHUB_TOKEN` (fallback) |
|---|---|---|
| Secret needed | `COPILOT_GITHUB_TOKEN` (PAT + "Copilot Requests" scope) | none |
| Billing | the token owner's Copilot seat | account/organization with Copilot CLI enabled |
| Prerequisite | create the PAT + store the secret | org policy "Allow use of Copilot CLI billed to the organization" enabled |
| Workflow env | `COPILOT_GITHUB_TOKEN: ${{ secrets.COPILOT_GITHUB_TOKEN }}` | `GITHUB_TOKEN: ${{ github.token }}` |
| Permission | not required | `copilot-requests: write` required |

**Route B** is the current implementation (chosen because this is a
single-owner repo — "it's just me"). To switch to **Route A**, replace the
`COPILOT_GITHUB_TOKEN` env line with `GITHUB_TOKEN: ${{ github.token }}`, add
`copilot-requests: write` to the `permissions` block, and enable the org policy.

## 3. Human prerequisites

Do these before the first real run (they cannot be automated):

1. **Create the PAT**:
   1. Go to <https://github.com/settings/personal-access-tokens/new>.
   2. Create a **fine-grained** PAT with the **Copilot Requests** permission.
   3. Repository access: select `news-digest-agent`.
   4. Copy the token value.
2. **Store it**: repo → **Settings → Secrets and variables → Actions → New
   repository secret**, name it `COPILOT_GITHUB_TOKEN`, paste the token.
3. **Secrets**: `RESEND_API_KEY` and `RECIPIENT_EMAIL` must already exist.
4. **Pin the CLI version** once a known-good release is verified
   (`npm install -g @github/copilot@<version>`). `@latest` tracks releases that
   can change the `-p` / `--no-ask-user` / `--yolo` flag surface underneath us.

## 4. Cutover

1. Changes are on `main`.
2. Run the workflow once manually: **Actions → Daily digest → Run workflow**.
3. Verify (below). Do not wait for the next 16:17 UTC scheduled run.
4. Confirm the schedule is still registered and not disabled.

## 5. Verification checklist

- [ ] `data/run_log.jsonl` has today's row with `curate_error: null` and
      `items_output > 0` (or a clean empty digest).
- [ ] The delivered email is a real digest, not "Agent error during curation".
- [ ] No `copilot CLI not found on PATH` in the job log.
- [ ] The "Commit state changes" step still pushes `data/` files (`contents:
      write` retained).

## 6. Rollback

- **CLI install regression** → revert the install step diff.
- **PAT auth fails** (expired/revoked token, wrong scope) → rotate the
  `COPILOT_GITHUB_TOKEN` secret, or switch to Route A (GITHUB_TOKEN) as above.
- No code rollback is needed; `curator.py` is unchanged by this plan.

## 7. Risks to know

- **`--no-ask-user` vs `--yolo`.** Current docs' direct-invocation example uses
  `copilot --yolo -p …`. `curator.py` uses `-s --no-ask-user`. With an empty
  tool allow-list there is nothing to auto-approve, so `--no-ask-user` should
  suffice — but treat this as an explicit check on the first manual run. If the
  step hangs prompt-help, switch `_copilot_command` in `curator.py` to `--yolo`.
- **Broad access on direct invocation.** Copilot run directly in a step gets
  broad access to the job's environment. This workflow triggers only on
  `schedule` + `workflow_dispatch` (no `pull_request`), so fork-triggered abuse
  is not a realistic vector. Keep it that way — don't add a `pull_request`
  trigger later without reconsidering this warning.
- **PAT secret hygiene.** The token grants Copilot request authority and is not
  printed by the workflow, but rotate it if it ever leaks or the account's
  Copilot plan changes.
- **Prompt size.** Copilot's argv handling degrades on very large prompts and
  can crash (V8 boot error). Already bounded in `config.py`
  (`CURATION_MAX_ITEMS`, `CURATION_PROMPT_MAX_CHARS`); do not raise those caps
  without re-testing against the installed CLI version.

## 8. Sources

- [Automate Copilot CLI with Actions](https://docs.github.com/en/copilot/how-tos/copilot-cli/automate-copilot-cli/automate-with-actions)
- [Use Copilot CLI in Actions with GITHUB_TOKEN](https://docs.github.com/en/copilot/how-tos/copilot-cli/use-copilot-cli-in-actions)
- [Ubuntu 24.04 runner image manifest](https://github.com/actions/runner-images/blob/main/images/ubuntu/Ubuntu2404-Readme.md)
