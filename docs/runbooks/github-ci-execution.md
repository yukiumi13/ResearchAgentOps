---
type: runbook
title: GitHub CI execution and runner recovery
owner: person:yl2708
last_updated: 2026-08-06
validity: valid
tags: [github, ci, runner, ruleset, recovery]
references:
  - kind: repository_path
    location: docs/adr/0016-ci-execution-and-merge-gate-availability.md
  - kind: repository_path
    location: .github/workflows/research-validate-pr.yml
relations:
  supersedes: []
  derived_from: []
  see_also:
    - docs/CODEOWNERS_CONTRACT.md
---
# GitHub CI Execution and Runner Recovery

## Diagnose Before Changing Rules

Observe repository governance and the exact PR head:

```bash
researchctl github doctor --repository OWNER/REPOSITORY --json
researchctl github pr-status \
  --repository OWNER/REPOSITORY \
  --pull-request NUMBER \
  --json
```

Interpret the terminal result:

| Result | Meaning | Manager action |
|---|---|---|
| `ci_capacity_pending` | A required job never acquired a runner | Restore billing/capacity or attach an authorized runner |
| `checks_pending` | Dispatch or execution has not reached a terminal result | Inspect the named workflow and wait or rerun |
| `validation_failed` | The validator ran and rejected the exact head | Fix the named findings on the same proposal branch |
| `review_pending` | Checks passed but review or another PR gate remains | Review the current head; do not approve an older commit |
| `governance_misconfigured` | No applicable protection or required check exists | Repair the Manager-owned ruleset/policy |
| `ready` | Observed checks and review count satisfy the gate | Merge through the protected PR path |

Do not disable a ruleset merely because `ci_capacity_pending` is reported. That
removes validation instead of restoring it.

## Manager Configuration

In GitHub, keep these concepts separate:

```text
Actions workflow/job -> runner executes -> GitHub App publishes status check
PR + status checks + reviews -> ruleset evaluates -> main accepts or rejects
```

For a private repository, inspect `Settings > Billing and licensing` for Actions
spending or minute limits. Inspect `Settings > Actions > Runners` for registered,
online runners and labels. Inspect `Settings > Rules > Rulesets` for the default
branch target and required check names.

The recommended control-verifier runner is repository-scoped and uses the label
`researchctl-control`. Create it through
`Settings > Actions > Runners > New self-hosted runner`; GitHub displays the
current architecture-specific download, registration token, and service
commands. Run it under a dedicated OS user or container. Do not reuse a Session
shell, Agent token, mutable research environment, or GPU experiment runner.

After the runner is online, a Manager-owned protected workflow change selects:

```yaml
runs-on: [self-hosted, linux, x64, researchctl-control]
```

The protected-base workflow may install and execute only the accepted validator
and treat the PR head as Git objects. Keep its permissions read-only and give it
no repository, SSH, Linear, cloud, or research credentials. Keep any workflow
that checks out and executes PR source on GitHub-hosted infrastructure or a
separate disposable runner label.

## Verify The Pilot

Open or update one harmless proposal. Then check:

```bash
researchctl github pr-status \
  --repository OWNER/REPOSITORY \
  --pull-request NUMBER \
  --json
```

Verify that the required check is bound to the proposal's current 40-character
head SHA, the job reports the intended self-hosted runner and labels, and the
ruleset still requires the same check. Stop the runner once and confirm the PR
remains blocked. Restart it and confirm the queued job completes without any
ruleset change.

## Proposal Branches

Reuse one branch and PR for follow-up commits that belong to the same reviewable
change set. Put implementation and its required documentation in that PR. Split
unrelated documentation. Always isolate changes to document taxonomy, schemas,
CODEOWNERS, workflows, validator pins, rulesets, or runner trust policy into a
Manager-owned control proposal, except for an explicitly justified minimal
compatibility migration that cannot pass in independent halves.
