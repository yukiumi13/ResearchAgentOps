---
type: adr
title: CI execution and merge-gate availability
owner: person:yl2708
last_updated: 2026-08-06
validity: valid
tags: [ci, github, runners, availability, proposals]
references:
  - kind: repository_path
    location: src/researchctl/services/github_pr_gate.py
  - kind: repository_path
    location: src/researchctl/adapters/github_pr_gate.py
  - kind: repository_path
    location: .github/workflows/research-validate-pr.yml
  - kind: repository_path
    location: docs/runbooks/github-ci-execution.md
relations:
  supersedes: []
  derived_from: []
  see_also:
    - docs/adr/0007-agent-threat-model.md
    - docs/adr/0010-ci-attestation-and-linear-delivery.md
    - docs/adr/0015-github-proposal-identity-and-protected-acceptance.md
---
# ADR 0016: CI Execution and Merge-Gate Availability

Status: accepted architecture; typed diagnosis implemented, self-hosted deployment pending

## Context

The term CI had been used for three separate responsibilities:

1. an executor acquires a runner and invokes the validator;
2. an authenticated GitHub App publishes a check for an exact commit; and
3. a ruleset consumes that check, reviews, and branch conditions to decide
   whether the protected branch may change.

GitHub Actions supplies the first two responsibilities, while GitHub rulesets
supply the third. A workflow existing in the repository does not make it a
required merge gate, and a ruleset cannot execute a validator by itself.

The initial deployment chose GitHub-hosted Actions because the repository,
proposal PR, exact head identity, workflow event, check publication, and merge
gate already live in GitHub. It avoids operating a webhook receiver, queue,
runner registry, check-publishing credential, and upgrade path. This is a good
default for short, secretless validation, but it is not an architectural
requirement of researchctl.

On 2026-08-06, latent-agents PR 6 provided a concrete availability failure. Its
required `doc-tree` workflow requested `ubuntu-latest`. Two jobs remained with
`runner_id: 0`, executed no steps, and were cancelled after 15 minutes with the
GitHub annotation `The job was not acquired by Runner of type hosted even after
multiple attempts`. The RCP validator never ran. Disabling the ruleset would
have converted an executor outage into an unvalidated merge.

## Decision

GitHub remains the proposal, authenticated check, review, and merge authority.
Verifier execution is a replaceable deployment component. The supported order
of preference for a single-manager research repository is:

1. a dedicated self-hosted GitHub Actions runner for protected-base governance
   and document checks;
2. GitHub-hosted Actions when its private-repository capacity is healthy; or
3. an external CI controller that publishes an authenticated check for the
   exact PR head.

A local shell command, pre-commit hook, `act` invocation, Agent statement, or
uploaded text file is useful preflight evidence but cannot satisfy the protected
gate. The proposer can omit or forge those voluntary results. Jenkins,
Buildkite, Woodpecker, Tekton, and similar systems are acceptable executors only
when their trusted GitHub integration publishes the fixed required check for
the exact SHA. They are not the default because they add control-plane and
credential operations without changing RCP's deterministic checker.

The self-hosted deployment uses separate trust domains:

- A control verifier may run only protected-base workflows that treat the PR
  tree as data. It has no research, SSH, Linear, cloud, or Manager secrets.
- A source-test runner executes PR code only in a disposable, credential-free
  container or VM. It shares no writable cache, workspace, or artifacts trusted
  by the control verifier.
- A research runner executes an already-frozen RunSpec outside PR CI. It is not
  registered as a general PR runner.

`researchctl github pr-status` observes the current ruleset or classic
protection, exact PR head, required checks, Actions runs/jobs, and check
annotations. It distinguishes `ci_capacity_pending`, `checks_pending`,
`validation_failed`, `review_pending`, `governance_misconfigured`, and `ready`.
It never disables a ruleset or recommends bypass as a substitute for validation.

## Proposal Boundary

An Agent proposal branch represents one reviewable change set, not one commit.
Follow-up fixes for the same proposal are pushed to the same branch and PR; the
new head invalidates old checks and approvals as configured. Creating a new PR
for every commit is unintended churn.

Code and documentation are not mechanically separated. Implementation code and
the design, runbook, benchmark, or generated report required to understand and
operate that implementation may be one atomic PR. Unrelated documentation is a
separate content proposal. A taxonomy, schema route, CODEOWNERS rule, workflow,
validator pin, or repository-governance change alters what future proposals can
pass, so it is isolated as a Manager-owned control proposal. When a compatibility
migration cannot be split without making either half fail, one explicitly
reviewed control migration may carry the minimum coupled content and must state
why the pieces cannot land independently.

## Manager Deployment Contract

The human Manager owns runner registration, host selection, ruleset required
checks, billing limits, and break-glass policy. RCP may audit and diagnose these
settings. It does not silently install a daemon on a research host, mint a
registration token, weaken a ruleset, or use Manager credentials from a Session.

An executor outage is recovered by restoring account capacity or an authorized
runner. A validator defect is fixed through a compatibility-preserving control
proposal. A documented break-glass bypass is reserved for a separately accepted
emergency policy; it is not the ordinary response to either condition.

## Consequences

- GitHub-hosted minutes and hosted-pool availability no longer define the
  architecture, although GitHub still anchors acceptance and audit.
- A self-hosted runner removes metered hosted capacity but adds patching,
  liveness, disk cleanup, isolation, and service ownership.
- A local CI product does not remove the need for an authenticated GitHub check
  publisher and exact-head binding.
- Runner acquisition failures become typed observations instead of generic CI
  failures, so Agents can continue local work while the Manager repairs capacity.
- Proposal isolation follows review and authority boundaries, not file extension
  or commit count.

## Verification

Unit tests cover exact-head check normalization, hosted-runner acquisition
annotations, capacity-versus-validation classification, pending review, ready
state, governance gaps, environment filtering, and stable CLI JSON. A live
self-hosted pilot remains required before the deployment part of this ADR is
complete.
