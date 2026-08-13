---
type: runbook
title: GitHub App proposal identity deployment
owner: person:yukiumi13
last_updated: 2026-08-13
validity: valid
tags: [github, identity, deployment, canary]
references:
  - kind: repository_path
    location: docs/adr/0015-github-proposal-identity-and-protected-acceptance.md
  - kind: repository_path
    location: docs/design/github-app-proposal-broker.yaml
sources: []
provenance: []
relations:
  supersedes: []
  derived_from: [docs/adr/0015-github-proposal-identity-and-protected-acceptance.md]
  see_also: [docs/runbooks/github-ci-execution.md, docs/design/github-app-proposal-broker.yaml]
---
# GitHub App proposal identity deployment

Use this procedure to create the proposal-author identity. It does not create
manager acceptance by itself: branch protection, current checks, and a distinct
human CODEOWNER review remain separate requirements.

## Create The App

1. Sign in to GitHub as the repository owner and open
   `Settings -> Developer settings -> GitHub Apps -> New GitHub App`, or visit
   `https://github.com/settings/apps/new`.
2. Set a unique name such as `researchctl-agent-<owner>`. Use the
   ResearchAgentOps repository URL as the Homepage URL.
3. Disable the webhook. Leave callback, setup, device-flow, and OAuth features
   unset; proposal delivery uses installation authentication only.
4. Set repository permissions exactly as follows:

   | Permission | Access |
   | --- | --- |
   | Metadata | Read-only, automatically required |
   | Contents | Read and write |
   | Pull requests | Read and write |

5. Leave Administration, Actions, Checks, Workflows, Deployments, Environments,
   Members, Secrets, and every organization/account permission at `No access`.
6. Choose `Only on this account` unless an organization installation is
   intentionally required. Create the App.

## Install And Record Non-Secrets

1. Open the App's `Install App` page and install it on `yukiumi13`.
2. Select `Only select repositories` and choose only `ResearchAgentOps` for the
   first canary.
3. Record the numeric App ID from the App settings page.
4. Record the installation ID from the installation settings URL, whose final
   path component is the numeric ID.
5. Record the canonical bot login shown by GitHub, normally the App slug with
   `[bot]` appended. Verify it through the GitHub UI before placing it in policy.

These three values are not secrets and belong in the reviewed governance
policy:

```yaml
agent_app:
  app_id: APP_ID
  installation_id: INSTALLATION_ID
  login: APP_SLUG[bot]
```

Do not grant the App manager, CODEOWNER, review, merge, protected-main, or
ruleset-bypass authority.

## Store The Private Key

1. Generate a private key from the App settings page only after installation.
2. Move the downloaded PEM to a repository-external deployment path owned by
   the trusted broker service account. Do not place it under the workspace,
   home directories shared with Agents, `/tmp`, or GitHub Actions artifacts.
3. Set the containing directory to owner-only access and the PEM to mode `0400`.
4. Configure the trusted host with the path through its service manager or
   secret manager. Never pass PEM content on argv or standard input and never
   export it to an Agent Session.
5. Verify that Git, shell history, process listings, logs, operation journals,
   receipts, and crash reports contain neither PEM bytes nor installation
   tokens.

If a private key enters Git or an Agent-visible environment, revoke it from the
App settings immediately, stop the broker, generate a new key, and audit open
proposal branches and PRs before resuming.

## Accept Governance Policy

After ResearchAgentOps has managed ProjectPolicy state, prepare the complete
policy using the observed values:

```yaml
repository: yukiumi13/ResearchAgentOps
default_branch: main
agent_app:
  app_id: APP_ID
  installation_id: INSTALLATION_ID
  login: APP_SLUG[bot]
managers:
  - kind: user
    login: yukiumi13
required_status_checks:
  - researchctl/exact-head
  - researchctl/source-tests
required_approvals: 1
require_code_owner_review: true
dismiss_stale_reviews: true
require_last_push_approval: true
strict_status_checks: true
block_force_pushes: true
block_deletions: true
bypass_actors: []
```

Run the manager-owned configuration command from a clean accepted base. Review
and merge that control proposal before enabling App-backed delivery. Do not put
the private key in the policy file.

## Run The Canary

1. Keep protected-main mutation disabled for the App and confirm that the human
   manager remains authenticated separately.
2. Start the trusted proposal host under its isolated deployment identity. The
   Agent sends only the existing typed Submission request and Session
   capability; it receives no GitHub credential.
3. Submit one documentation-only proposal whose canonical branch and head are
   derived by researchctl.
4. Confirm the remote branch has the exact expected commit and the PR author is
   the configured App bot.
5. Confirm the App cannot approve, merge, push `main`, change workflows, change
   repository settings, publish checks, or bypass a rule.
6. Let both exact-head checks finish. Review the latest head as `yukiumi13` and
   merge only after the broker receipt, GitHub UI, and check SHAs agree.
7. Push a follow-up canary commit and verify that stale approval is dismissed
   and latest-push approval is required again.
8. Run post-merge identity verification and record the App author, human
   reviewer/merger, head, check SHAs, policy digest, and installation ID without
   recording credentials.

## Apply And Audit Protection

After the canary proves identity separation, preview repository governance:

```bash
researchctl github apply-governance --project . --json
```

Review the emitted policy and observation digests, then apply the exact preview
as the authenticated human manager. Run the project-bound doctor afterward:

```bash
researchctl github doctor --project . --json
```

Do not disable the rule to recover runner capacity. Restore the runner or use an
authorized exact-head check publisher. Break-glass access must be explicitly
reviewed and auditable; the initial policy has no bypass actor.

## Revoke Or Roll Back

1. Stop the trusted host so no new tokens can be minted.
2. Revoke the affected App key or uninstall the App from ResearchAgentOps.
3. Close any App-authored proposal whose head or receipt cannot be verified.
4. Keep protected `main` and accepted records intact; uninstalling a projection
   identity must not rewrite accepted truth.
5. Update the manager-owned policy through a separate reviewed control proposal
   before installing a replacement identity.
