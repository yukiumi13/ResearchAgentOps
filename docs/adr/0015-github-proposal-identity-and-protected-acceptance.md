---
type: adr
title: GitHub proposal identity and protected acceptance
owner: person:yl2708
last_updated: 2026-08-05
validity: valid
tags: [github, identity, review, governance]
references:
  - kind: repository_path
    location: src/researchctl/services/github_governance.py
  - kind: repository_path
    location: src/researchctl/adapters/github_governance.py
  - kind: repository_path
    location: src/researchctl/adapters/github_protection.py
  - kind: repository_path
    location: docs/CODEOWNERS_CONTRACT.md
relations:
  supersedes: []
  derived_from: []
  see_also:
    - docs/adr/0003-atomic-human-acceptance.md
    - docs/adr/0007-agent-threat-model.md
    - docs/adr/0010-ci-attestation-and-linear-delivery.md
---
# ADR 0015: GitHub Proposal Identity and Protected Acceptance

Status: proposed deployment contract; audit, policy proposal, and rule apply implemented locally

## Context

The research workflow requires the Agent to create the proposal PR and a human
manager to accept or reject it. GitHub does not count a PR author's own approval,
so a Session using the manager's `@yukiumi13` credential cannot satisfy the
intended one-approval gate. A checked-in CODEOWNERS file and green Actions runs
are also insufficient: checks run independently of whether branch protection
requires them, and repository prose cannot prevent an Agent credential from
approving, merging, force-pushing, or bypassing a rule.

Local RCP actor detection is not a production identity boundary. In the current
same-user development mode, absence of Session environment variables produces a
local manager actor based on the Unix UID. A process sharing that UID can alter
its environment. Protected GitHub acceptance therefore needs independently
authenticated principals and repository-enforced rules in addition to local
capability checks.

## Proposed Decision

The human Manager is an allowlisted GitHub user or team that owns review and
merge authority. For this repository the initial Manager is `@yukiumi13`. A
review Agent is advisory only: it may add findings or request changes through a
non-accepting channel, but it is not an allowlisted approving Manager and cannot
merge.

Agent proposal PRs use a distinct GitHub App installation principal. The App is
not a CODEOWNER, has no approval or merge authority, cannot push the protected
default branch, and receives no ruleset bypass. A trusted proposal broker mints
short-lived installation tokens and exposes only the bounded operations needed
to push a derived proposal branch and create or observe its one deterministic
PR. Session processes do not receive the App private key or a reusable Manager
credential.

Accepted protected project policy will bind, without secrets:

- the canonical GitHub repository and protected default branch;
- the expected Agent App installation/account identity;
- the allowlisted human Manager users or teams;
- `researchctl/source-tests` and `researchctl/exact-head` as fixed checks;
- at least one CODEOWNER approval, stale-review dismissal, and latest-push
  approval; and
- strict base currency, force-push/deletion denial, and an explicit audited
  break-glass policy.

An ordinary Agent content commit remains a proposal. This applies to design,
brief, runbook, decision, experiment, benchmark, and source changes, not only to
taxonomy changes. Taxonomy, schema routing, identity policy, CODEOWNERS, and
repository-rule changes additionally require a distinct manager-owned control
proposal because they redefine what later proposals may pass.

The proposal unit is one reviewable change set, not one commit or one file type.
Follow-up commits for that change set stay on the same proposal branch and PR.
Implementation and the documentation required to review or operate it may land
atomically; unrelated documentation is separate. Control-policy changes remain
isolated from ordinary content except for a minimal, explicitly justified
compatibility migration that cannot pass in independently mergeable halves.

## Enforcement Workflow

```text
local deterministic lint and review-agent findings
-> trusted broker authenticates the configured Agent App
-> Agent App authors the exact proposal PR
-> source-tests + exact-head run for the latest head
-> GitHub requires current checks and CODEOWNER approval
-> allowlisted human Manager reviews and merges
-> post-merge host re-reads author, reviews, head, checks, merge, and Git tree
-> accepted result may enter the projection outbox
```

`researchctl github doctor` performs bounded,
read-only `gh api` observations of repository metadata, classic branch
protection, and active rulesets applicable to the target branch. It fails when
required merge gates are absent and warns about administrator/ruleset bypass.
It does not mutate GitHub. Repository-only audit warns that no accepted
principal policy was supplied; `--project` binds the audit to the accepted
repository, branch, Agent App, human Managers, required gates, and bypass policy.

The strict `ProjectPolicy.github` schema and project-bound audit are also
implemented. They bind repository/default branch, App and installation IDs, App
bot login, sorted human Manager principals, the exact two checks, non-disableable
review/status/branch gates, and explicit bypass actors. Submission, Impact, and
ImpactDecision delivery now requires this accepted policy before any push and
rejects an observed PR whose `user.login` is not the configured App bot. This is
receipt verification, not pre-create credential proof: without the broker, a
wrong credential can create an unauthorized open PR before observation rejects
it, although that PR cannot produce a valid RCP delivery receipt.

Repository-rule application is implemented locally as the separate
`researchctl github apply-governance` command. Its default mode is read-only and
emits canonical policy and observation digests. Mutation requires explicit
`--apply`, both reviewed digests, no Session capability environment, and a live
`gh` identity matching an accepted human Manager user or active team member.
The bounded writer currently supports classic branch protection only, rejects
applicable rulesets and configured bypass actors before mutation, sends one
canonical update through stdin, and audits the result by read-back. A timeout or
unreadable/non-compliant final state is uncertain or incomplete, never assumed
successful; replay observes before retrying. It is never an implicit side
effect of `init`, `submit`, `doctor`, document lint, or an Agent command. No
repository rule has been applied as part of this implementation.

The PR adapter and post-merge ingress will then enforce the identity relation,
not merely document it: proposal author equals the configured App principal;
approving reviewer and merger are allowlisted Managers; author and accepting
reviewer are distinct; approval covers the latest head; and all configured
checks cover that same head. Any mismatch fails closed before acceptance or
projection.

Creating the GitHub App, granting repository installation consent, selecting
Manager identities, and placing the private key in an external secret manager
remain GitHub-owner operations. RCP may generate least-privilege setup guidance
and verify the result, but it cannot manufacture owner consent.

## Delivery Roadmap

1. Implement bounded read-only governance observation and `github doctor`. Done.
2. Add the strict project policy model for Agent App, Managers, checks, and
   branch, including a field-specific protected proposal. Done locally.
3. Make proposal adapters verify the distinct configured App author. Receipt
   verification done; pre-create proof depends on item 5.
4. Add manager-only idempotent repository-rule apply and read-back verification.
   Done locally for conflict-free classic branch protection; deployment pending.
5. Add the trusted short-lived-token proposal broker.
6. Revalidate author, reviewer, merger, latest head, checks, and Git after merge.
7. Add App consent/bootstrap guidance and run a live protected-repository canary.

Items 1 through 4 are locally implemented within the bounded scope above. The
real repository is not yet protected by this command. Pre-create App proof in
item 3 and items 5 through 7 remain proposed until their credential boundary,
tests, owner consent, and deployment evidence exist.

## Consequences

- A green check, Agent-authored commit, review-Agent opinion, or generated
  CLAUDE.md statement cannot become acceptance by itself.
- Single-maintainer repositories can retain one human Manager because the
  proposal author is a distinct App principal.
- The App credential becomes a small trusted deployment component and requires
  rotation, audit, recovery, and least-privilege installation.
- Local same-UID capability checks remain useful mistake containment but are not
  presented as hostile-process isolation.
- Standalone document lint remains usable without GitHub or RCP initialization;
  protected acceptance is an additional repository deployment layer.
- Agent code changes correctly use proposal branches, but a new branch and PR
  for every follow-up commit is unintended churn.

## Verification

Unit tests cover healthy and incomplete classic protection, applicable and
excluded rulesets, required-check gaps, bypass visibility, malformed and
oversized responses, bounded timeout/failure behavior, environment filtering,
stable CLI JSON/exit semantics, strict identity-policy schema, and mismatched PR
authors. Apply tests cover read-only preview, stale digest rejection, direct and
team Manager authorization, Agent App denial, classic payload scope,
ruleset/bypass conflicts, secret filtering, timeout/nonzero/read-back outcomes,
and no-change replay. Future items require reviewer/merger adversarial cases,
broker credential-boundary tests, authenticated post-merge replay, and one live
App-authored PR canary before this ADR can be marked accepted and deployed.
