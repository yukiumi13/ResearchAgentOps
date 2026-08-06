# CODEOWNERS and CI Contract

This repository installs an initial single-maintainer `.github/CODEOWNERS`
baseline for `@yukiumi13`. `.github/CODEOWNERS.template` remains an inert
deployment template for repositories that need different principals. The
installed file must still be reviewed on the protected default branch; its
presence does not prove that branch rules or reviewer policy are enabled.

This single-maintainer baseline cannot by itself satisfy a required approving
review on a PR authored by `@yukiumi13`: GitHub does not count a PR author's own
approval. The intended Agent-proposal deployment uses a distinct GitHub App or
bot principal to create the PR and keeps the human manager as CODEOWNER. A
second human CODEOWNER is an alternative. Until one of those identities exists,
required checks can be enabled, but requiring one CODEOWNER approval would
deadlock maintainer-authored PRs.

A configured Linear app or credential-origin label, such as
`researchctl-app`, is not a GitHub CODEOWNER and must not be substituted into
this template unless GitHub independently has a real principal with that exact
name and the administrator deliberately selects it.

## Required Repository Setup

The checked-in workflows expose two independent checks:
`researchctl/source-tests` and `researchctl/exact-head`. The exact-head check's
protected-base dispatcher recognizes Submission proposals/acceptance, generated
Task control changes, generated bootstrap proposal/acceptance, and supported
manager-owned policy control. A PR with no `.research/**` or
`.researchctl.toml` change passes exact-head as `not_applicable`; that result
means only that the research protocol has no accepted-state change to validate.
The separate source check executes the exact PR source in a credential-free,
read-only `pull_request` job.

The workflows run from their configured pull-request events whether or not
branch protection marks them required. A required-status-check rule does not
start CI; it prevents merge when the named run is absent, pending, or failing.
Conversely, a green non-required check is advisory. Agent-created PR automation
should use a GitHub App installation identity or another external principal;
it must not assume that a PR created recursively with a workflow's default
`GITHUB_TOKEN` will emit another workflow run.

Standalone post-bootstrap policy, schema, Project, Report, Decision, or other
control mutations are intentionally unsupported at R0. Unknown protocol paths,
multiple protocol PR types, and a supported type with extra protected paths all
fail closed. Add a protected-base validator and its tests before enabling a new
control mutation; branch naming alone never makes a change valid.

Before enabling the complete protected merge gate, the administrator must:

1. Review the installed `.github/CODEOWNERS` and confirm that `@yukiumi13`, or a
   deliberately approved replacement, is the intended repository principal.
   The exact-head workflow fails closed while that file is absent or empty.
2. Establish a distinct PR-author and reviewer principal: preferably the Agent
   proposal GitHub App as author and the human manager as CODEOWNER, or two human
   maintainers. Do not grant the Agent approval authority.
3. Review both workflows and the multi-type protected-base dispatcher, then
   protect the default branch and require checks named
   `researchctl/source-tests` and `researchctl/exact-head`.
4. Require at least one CODEOWNER approval and dismiss stale approvals when the
   PR head changes.
5. Require approval of the latest reviewable push and require the branch to be
   current with the protected base before merge.
6. Deny force pushes and branch deletion, and restrict administrator or service
   bypass to an explicitly audited break-glass procedure.
7. Keep Actions permissions read-only for PR validation. Do not add Linear,
   SSH, cloud, package-publish, or manager credentials to either workflow.

The administrative change that confirms CODEOWNERS and enables branch rules
must be complete before these checks become mandatory. It is an onboarding
operation, not an exception that Submission agents can exercise.

`researchctl github doctor --repository OWNER/REPOSITORY --json` is the
read-only preflight and verification command for this checklist. It observes
classic branch protection and active rulesets applicable to the selected branch
and fails when a required gate is absent. It deliberately does not apply
settings. `--project .` additionally binds the audit to accepted
`ProjectPolicy.github`; repository-only mode warns that it cannot prove the
configured Agent and Manager principals.

After the field-specific GitHub policy proposal is reviewed and protected-merged,
a human Manager can inspect the exact rule delta without mutation:

```bash
researchctl github apply-governance --project . --json
```

Applying that reviewed preview is a separate explicit operation requiring both
reported digests and a live `gh` login matching an accepted Manager user or
active team member:

```bash
researchctl github apply-governance --project . --apply \
  --expected-policy-digest sha256:... \
  --expected-observation-digest sha256:... --json
```

The bounded writer supports classic protection only and refuses applicable
rulesets or configured bypass actors rather than creating overlapping rule
authorities. It re-reads and audits the result, and a Session capability or
Agent App cannot invoke the mutation. This repository's real settings were not
changed while implementing the command.

## Trust Boundary

`.github/workflows/research-validate-pr.yml` uses `pull_request_target` only to
obtain the workflow and validator from the protected base. It does not checkout
the PR head, import Python from it, invoke its scripts, run its tests, or install
its package. The workflow fetches `refs/pull/<number>/head` into the Git object
database, verifies that it equals the event head SHA, and passes that SHA to the
protected `researchctl ci dispatch` command as data. Classification uses the
exact `base..head` tree diff, canonical record content, commit parent/marker,
and then the source branch as a corroborating identity.

This use of `pull_request_target` is valid only while every executable action
and script remains protected-base code. A future step that needs to execute PR
content must move to an unprivileged `pull_request` job with no credentials and
must not share caches, artifacts, environments, or writable state with this
job.

`.github/workflows/research-source-tests.yml` intentionally uses `pull_request`
and checks out the event's exact head. It may execute untrusted PR code only in
that read-only, credential-free job. The source and exact-head jobs must not
share caches, writable environments, or artifacts as trusted inputs.

The workflow pins third-party actions by full commit SHA. Updating those pins is
a CODEOWNER-reviewed control-plane change.

The source workflow also runs portable project-document lint from the exact PR
head and compares baseline-frozen documents with a detached checkout of the
exact base SHA. In a repository that has not run `researchctl init`,
`.researchctl-docs.yaml` is the complete document taxonomy and therefore must be
CODEOWNERS-protected: changing it can add or remap an accepted classification,
directory, schema contract, generated index, machine artifact root, or Agent
guide target. Configured Agent guide managed blocks are deterministic policy
projections; source lint rejects missing or stale blocks but does not authorize
the underlying policy change. After
initialization, the same policy is inside protected ProjectPolicy and changes
through the manager-only, protected-base-validated `doc.configure-layout` path.

## Attestation Contract

Successful validation writes one canonical YAML artifact outside the repository
worktree. Its outer dispatch envelope binds:

- repository and pull request number;
- exact base commit, subject head/tree, and base/head refs;
- protected dispatcher, workflow, check identity, PR type, and applicability;
- sorted named checks and their evidence digests; and
- for a Submission only, the typed exact-head attestation with schema manifest,
  generated outputs, Submission/Decision/Report identities, renderer identity,
  and credential-free Linear projection preview.

The artifact is uploaded by the workflow and is never committed to the PR. A
new PR commit produces a different required-check execution; an attestation for
an earlier head cannot satisfy branch protection for the new head. The outer
dispatch document is a strict, canonical workflow envelope, not a `.research`
protocol record and therefore is not registered in the repository schema
manifest. For a Submission it embeds the schema-registered
`CIValidationAttestation`; trusted consumers must load the canonical envelope
with `load_ci_dispatch_artifact()` (or extract with
`submission_attestation_from_dispatch_artifact()`), never select fields from
unvalidated YAML.

Local human invocation:

```bash
researchctl ci dispatch -C REPOSITORY \
  --artifact /tmp/researchctl-ci-attestation.yaml \
  --repository OWNER/REPOSITORY \
  --pull-request-number 17 \
  --subject-head FULL_HEAD_SHA \
  --base-commit FULL_BASE_SHA \
  --head-ref SOURCE_BRANCH \
  --base-ref DEFAULT_BRANCH
```

Strict JSON callers send one `CIPRDispatchRequest` on stdin and keep the output
path as a transport option:

```bash
researchctl ci dispatch -C REPOSITORY --json \
  --artifact /tmp/researchctl-ci-attestation.yaml < request.json
```

`researchctl ci validate --submission-id ...` remains the lower-level
Submission-only validator used by the dispatcher and is useful for focused
local diagnostics.

The request cannot select `validator_id`, `validator_version`, `workflow_id`,
`check_identity`, renderer identity, Linear credentials, or Linear payload.
Those values come from protected code and protected-base records.

CI never publishes to Linear. Only trusted post-merge delivery may perform a
remote read or mutation after revalidating an accepted merge and matching this
attestation preview.
