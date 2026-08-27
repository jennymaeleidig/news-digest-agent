# Deployment plan: Copilot CLI in GitHub Actions

Status: **implemented** in `.github/workflows/daily-digest.yml` (commit on `main`).

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

Three edits landed in `run-digest` (and the top-level `permissions` block):

1. **Install the CLI** — `actions/setup-node@v7` + `npm install -g @github/copilot`.
2. **Permission** — added `copilot-requests: write` (required for Copilot
   requests over `GITHUB_TOKEN`).
3. **Auth** — authenticate with the workflow's `GITHUB_TOKEN`
   (`${{ github.token }}`), replacing the `COPILOT_GITHUB_TOKEN` PAT.

`smoke-test-fetchers` is unchanged; it does not invoke Copilot.

## 2. Auth: the two routes

| | Route A — `GITHUB_TOKEN` (implemented) | Route B — fine-grained PAT (fallback) |
|---|---|---|
| Secret needed | none | `COPILOT_GITHUB_TOKEN` (PAT + "Copilot Requests" scope) |
| Billing | account/organization with Copilot CLI enabled | a specific user's Copilot seat |
| Prerequisite | org policy "Allow use of Copilot CLI billed to the organization" enabled | none beyond the PAT |
| Workflow env | `GITHUB_TOKEN: ${{ github.token }}` | `COPILOT_GITHUB_TOKEN: ${{ secrets.COPILOT_GITHUB_TOKEN }}` |
| Permission | `copilot-requests: write` required | not required |

**Route A** is the current implementation and matches current GitHub guidance.
To fall back to **Route B**, replace the `GITHUB_TOKEN` env line in
`run-digest` with the commented `COPILOT_GITHUB_TOKEN` line and create the
secret (a fine-grained PAT with the **Copilot Requests** permission).

## 3. Human prerequisites

Do these before the first real run (they cannot be automated):

1. **Confirm the policy** for Route A: in the account/org Copilot policy
   settings, under "Copilot CLI", confirm **Allow use of Copilot CLI billed to
   the organization** is selected.
2. **Secrets**: `RESEND_API_KEY` and `RECIPIENT_EMAIL` must exist. Route A
   needs no Copilot secret; Route B needs `COPILOT_GITHUB_TOKEN`.
3. **Pin the CLI version** once a known-good release is verified
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
- **GITHUB_TOKEN auth fails** (policy off, billing error) → switch to Route B
  (uncomment the `COPILOT_GITHUB_TOKEN` line, create the PAT, remove
  `copilot-requests: write` if desired).
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
- **Prompt size.** Copilot's argv handling degrades on very large prompts and
  can crash (V8 boot error). Already bounded in `config.py`
  (`CURATION_MAX_ITEMS`, `CURATION_PROMPT_MAX_CHARS`); do not raise those caps
  without re-testing against the installed CLI version.

## 8. Sources

- [Automate Copilot CLI with Actions](https://docs.github.com/en/copilot/how-tos/copilot-cli/automate-copilot-cli/automate-with-actions)
- [Use Copilot CLI in Actions with GITHUB_TOKEN](https://docs.github.com/en/copilot/how-tos/copilot-cli/use-copilot-cli-in-actions)
- [Ubuntu 24.04 runner image manifest](https://github.com/actions/runner-images/blob/main/images/ubuntu/Ubuntu2404-Readme.md)
