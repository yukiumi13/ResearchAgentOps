# Research Control Plane

Research Control Plane (RCP) is a local-first Agent harness and lightweight
research control plane for Codex and Claude. It coordinates isolated Agent
Sessions, immutable experiment evidence, human acceptance, exact-head CI, and
optional Linear projection around an existing Git repository.

RCP is not an Agent model, an experiment framework, a distributed scheduler, or
a replacement for Git, GitHub, SSH, tmux, and repository-native launchers. The
core deliberately uses one Python application service, one host-local SQLite
database, Git worktrees, and short-lived integration jobs. It has no required
HTTP service, broker, custom web UI, or always-on Agent daemon.

> **Status: pre-release local implementation.** The local harness, immutable
> Runs, deterministic Reports, two-check CI contract, durable Session
> notifications, credential-free post-merge shadow, and authenticated GitHub
> post-merge observation/enqueue are implemented and tested. Real repository
> protection, trusted scheduling and credentials, production Linear transport,
> and the measured shadow/canary pilot remain deployment work. No production
> latency or production-readiness claim is made.

## Workflow

```text
Agent work in a Session branch/worktree
  -> strict ExperimentPlan with accepted Task/policy value provenance
  -> deterministic no-fallback lint
  -> independent ephemeral read-only PlanReview
  -> deterministic reviewed RunSpec
  -> immutable research-run/<run_id> evidence
  -> ResearchSubmission
  -> manager review accept creates a candidate Decision + Report
  -> researchctl/source-tests executes the exact PR source
  -> researchctl/exact-head validates protocol and generated Report bytes
  -> current CODEOWNER approval + protected merge
  -> Decision + accepted Report become authoritative in Git
  -> trusted post-merge validation and optional outbox enqueue
  -> trusted Linear app projects the same Report and stores a receipt
```

Humans, Agents using strict JSON, and Python automation call the same
`ApplicationService`, request models, authorization checks, and state
transitions. JSON is a presentation contract, not a second business API.

## Authority

| Data | Authority | Purpose |
|---|---|---|
| Project, Task, policy, Decision, accepted Report | Protected default branch | Reviewable research truth |
| Source proposal | Session branch and isolated worktree | One Agent's unaccepted changes |
| Frozen RunSpec and collected RunResult | Immutable `research-run/<run_id>` ref | Reproducible provenance |
| Session lifecycle, operation journal, notifications, outbox, claims, receipts | Host-local SQLite | Durable live operational state |
| PR, review, and required checks | GitHub | Review and merge transport |
| Comments and addressed requests | Linear | Non-authoritative projection and transport |

SQLite does not own Reports or scientific dependency truth. It stores durable
runtime facts such as operation idempotency, Session observations, ingress
receipts, notification revisions, delivery attempts, leases, dead letters, and
projection receipts. Some undelivered inbox/outbox state cannot be reconstructed
from Git alone. Production use therefore requires persistent storage, one
writer, consistent backups, and a tested restore procedure.

## Reports And CI

A final Report is not authored by CI or invented after merge:

1. The Agent proposes a canonical Submission and evidence.
   `researchctl submit` pushes the one derived Submission branch and creates or
   observes its deterministic GitHub PR; it does not ask the human to open it.
2. `researchctl review accept` is manager-only and materializes a candidate
   Decision, Report YAML, Report Markdown, and credential-free Linear preview
   on the exact proposal head.
3. `researchctl/exact-head` regenerates those outputs from canonical records and
   compares them byte for byte while binding the result to the exact head/tree.
4. The protected merge makes that already-validated Report accepted Git truth.
5. Post-merge automation revalidates the accepted merge and projects the same
   deterministic content to Linear. It does not rewrite or summarize it again.

Two independent required checks cover different risks:

| Check | Trigger and trust | What it proves |
|---|---|---|
| `researchctl/source-tests` | `pull_request`; exact PR head; read-only and credential-free | The source being proposed passes the repository test suite |
| `researchctl/exact-head` | `pull_request_target`; validator installed from protected base; PR head read only as Git objects | Supported RCP protocol changes and generated outputs match the exact proposed head |

For an ordinary source-only PR, exact-head returns `not_applicable`. That means
no governed RCP protocol path changed. It is not a source test, and cannot
replace `researchctl/source-tests`.

The protected-base dispatcher currently recognizes Submission, generated Task
control, manager-owned Plan reviewer policy control, Report Impact, explicit
ImpactDecision, bootstrap proposal/acceptance, and manager-owned Linear policy
control changes. Unknown or mixed protocol mutations fail closed. Review the installed
single-maintainer CODEOWNERS baseline, require both checks, dismiss stale
approvals, and restrict protected-branch updates before treating merge as an
acceptance boundary.

## Plans Before Runs

`PLAN.yaml` is a strict generated-schema protocol record. Every field that can
change experiment semantics must cite the digest of an accepted Task or Project
policy containing that exact value. A drafting Agent cannot make a choice
authoritative by labeling its own YAML `user`; missing accepted intent becomes
`needs_input` rather than a provider, CLI, library, or environment fallback.

The manager first proposes an explicit reviewer provider/model policy. The
command changes only `plan_review` in the protected Project policy on a fixed
control branch; exact-head CI replays that field-level scope before merge:

```bash
researchctl plan configure-review \
  --provider codex \
  --model EXPLICIT_MODEL_ID \
  --policy-version plan-review-v1 \
  --timeout-seconds 60 \
  --expected-default-head FULL_DEFAULT_BRANCH_SHA
```

After the relevant Task choices and reviewer policy are accepted, the Agent
runs the deterministic and semantic gates before any Run side effect:

```bash
researchctl plan lint PLAN.yaml
researchctl plan review PLAN.yaml --output-file plan-review.yaml
researchctl plan compile PLAN.yaml \
  --review-file plan-review.yaml --output-file run-spec.yaml
researchctl run start --spec-file run-spec.yaml
```

The reviewer is a fresh attributed invocation with read-only access and no
second persistent RCP Session. Codex runs ephemerally; Claude runs in
bare non-persistent print/plan mode with tools, slash commands, and ambient MCP
configuration disabled. Reviewer credentials are
deployment inputs, not Plan fields. The environment strips GitHub, Linear,
Session-capability, and SSH-agent credentials. Self-review, reviewer-policy
drift, missing local review receipt, Plan/Task/policy digest drift, or a
non-passing opinion blocks compilation or execution. Plan-backed Submissions
carry separate `plan.yaml` and `plan-review.yaml` evidence, and protected CI
reruns deterministic validation from the protected Task and Project policy.

## Concise Research Writing

`AnalysisBrief` and `ResearchUpdate` are presentation contracts, not accepted
research authority. They keep Agent-authored analysis and operational updates
short before those texts enter a review document or Linear comment.

An `AnalysisBrief` contains one question, one conclusion, one protocol, up to
five metrics, up to eight `setting` rows, at most three interpretation points,
at most three material limitations, and explicit source references. Every
setting supplies the same metrics and references declared sources. The linter
also enforces sentence, English-word, and CJK-character budgets.

```bash
researchctl brief lint analysis-brief.yaml
researchctl brief render analysis-brief.yaml --output-file analysis-brief.md
```

The Markdown renderer fixes the order to Answer, Evidence, Interpretation,
Limits, and Sources. Generated output carries a renderer marker and should not
be edited by hand.

`ResearchUpdate` represents exactly one operational delta: started, completed,
failed, or conclusion changed. It allows no more than two evidence values and
renders a compact heading/bullet update with a visible renderer marker:

```bash
researchctl update lint research-update.yaml
researchctl update render research-update.yaml
```

The update renderer produces a deterministic Linear-sized preview. Delivery
policy, issue binding, credentials, and accepted Report projection remain
separate concerns; passing writing lint does not authorize publication or make
the update canonical research truth.

## Portable Project Documents

Document contracts can be adopted without `researchctl init`. Put a strict
`DocumentLayoutPolicy` in `.researchctl-docs.yaml`, protect that file with
CODEOWNERS, and run the same static checks locally or in any CI:

```bash
researchctl doc tree --project .
researchctl doc tree --project . --json
```

The policy is required in standalone mode. If neither it nor a managed Project
policy exists, commands fail with `document_policy_missing`; `researchctl` does
not silently choose a repository hierarchy. A one-off caller may instead pass
an explicit `--policy-file`.

The command does not open `.research`, SQLite, a Session, or manager state. It
checks canonical `a/b:c` classification routes, frontmatter schemas, type/path
agreement, required relations, directory depth, links, structured YAML/Markdown
pairs, renderer bytes, optional generated index freshness, and finite legacy
exceptions. An optional baseline checkout also enforces byte immutability for
documents already marked `validity: frozen`:

```bash
researchctl doc tree --project . --baseline-project /path/to/base-checkout
```

Projects can keep stable machine inputs under paths such as `data/` by declaring
`machine_artifact_roots` with explicit extension allowlists. Those roots can
never allow Markdown, so prose moves to `docs/` without forcing scripts to change
their data paths. `researchctl doc index` deterministically renders the configured
type/classification/contract/directory table.

After initialization, the identical policy lives under
`.research/policies/default.yaml.document_layout`. Changing a label, directory,
contract, index, artifact root, or route mapping then uses manager-only
`researchctl doc configure-layout`; protected-base CI verifies that no other
Project policy field changed. Ordinary tags never affect routing or authority.

Generated schemas include `document-layout-policy`, `markdown-frontmatter`,
`design-document`, `project-status-summary`, and `analysis-brief`. Editor and
Agent integrations should consume those schemas or `doc tree --json` rather than
implementing a second validator. The exact decision is recorded in ADR 0014.

## Session Addressing

Sessions do not need email addresses, Linear accounts, or one external user per
Agent. One trusted app identity, configured per deployment and shown here as
`researchctl-app`, owns transport. The comment body names the exact local
Session and commit:

```text
@researchctl-app notify session:<full-session-id> commit:<full-commit-sha>
Please inspect this exact commit and reply with the evidence path.
```

In a thread already bound by a verified RCP delivery receipt, the manager can
ask the originating Session to follow up without repeating its ID:

```text
@researchctl-app reply commit:<full-commit-sha>
Please re-check this exact commit.
```

The trusted ingress verifies the workspace, issue, thread, stable app and author
IDs, Task binding, full Session ID, exact Session branch, and commit reachability.
It never trusts a display name or a copied hidden marker. The request becomes a
durable, non-authoritative `SessionNotification`. An active Session lists and
acknowledges the inbox at safe checkpoints; stopped or lost Sessions route to a
manager exception inbox rather than disappearing.

```bash
researchctl session list
researchctl session show SESSION_ID
researchctl session address SESSION_ID --commit FULL_COMMIT_SHA \
  --app researchctl-app
researchctl notification list
researchctl notification ack NOTIFICATION_ID --expected-revision REVISION
researchctl notification reply NOTIFICATION_ID \
  --expected-revision REVISION --body 'Reviewed; evidence is consistent.'
```

RCP does not inject keystrokes or arbitrary text into a model while it is
generating or running an experiment. Persistence proves delivery to the inbox;
only `ack` or `reply` proves the Session application consumed the revision.

## Linear Delivery

The repository includes deterministic accepted-result and Session-reply events,
stable markers, target preflight contracts, durable claims, retry/dead-letter
state, crash-after-create recovery, and lineage-rich receipts. Every projected
comment records Agent, Session, Task, optional Report, accepted merge, exact-head
attestation, renderer, and payload identities while the visible author remains
the trusted app.

A manager prepares the complete non-secret Linear target and notification
allowlist as a reviewed control proposal. The command does not contact Linear,
merge its own proposal, or accept credentials:

```bash
researchctl linear configure \
  --policy-file linear-policy.yaml \
  --expected-default-head FULL_DEFAULT_BRANCH_SHA
```

`researchctl-linear-host shadow` is credential-free. It validates a local
dispatch artifact against an accepted Git merge and emits a canonical
observation. It performs no network call and writes no live outbox row. Enqueue
requires authenticated GitHub provenance with workflow, check, and artifact
identities; only a later trusted delivery worker may use the Linear credential.

`researchctl-github-post-merge` is the one-shot authenticated enqueue adapter.
It uses an authenticated `gh api` environment to bind one merged PR to the
fixed workflow, exact check run, and exact attestation artifact before it may
write the durable outbox event. Artifact-derived local requests remain
shadow-only and cannot self-assert authenticated provenance.

The repository does not yet ship a production Linear API/MCP adapter or a
deployed webhook/poller installation. The GitHub adapter is also not scheduled
or credentialed merely because its code is installed. Until those deployment
adapters can verify stable internal `author_app_id` values, fake-port tests and
local shadow runs are not a claim of live Linear integration.

## Implemented Boundary

Implemented and tested locally:

- safe existing-repository initialization, doctor, protocol compatibility, and
  isolated bootstrap acceptance;
- manager-owned Task proposals, guided Task creation, and Agent capability
  denials;
- Codex/Claude Session worktrees, tmux identity, attach/pause/continue, status,
  grouped exception inbox, notification inbox, and terminal fallback;
- frozen local Runs, preflight, attempts/retries, immutable provenance,
  collection, and read-only reconciliation;
- strict ExperimentPlan and PlanReview schemas, accepted-value provenance,
  no-fallback lint, explicit manager-owned reviewer policy control, independent
  ephemeral review, deterministic RunSpec compilation, Run gating, separate
  Submission evidence, and protected-CI replay;
- portable standalone/managed project-document schemas, classification routes,
  frontmatter/tree lint, deterministic render/index checks, frozen-baseline
  enforcement, machine-artifact boundaries, and manager-owned layout control;
- Submission, Decision, deterministic Report rendering, manager acceptance,
  constrained Agent-authored GitHub PR delivery, protected-base dispatch,
  exact-head validation, and source tests;
- manager/trusted-automation Report Impact proposals with strict code-path
  dependencies, optimistic concurrency, merge-triggered all-Report batching,
  fixed GitHub delivery, clean-runner replay, exact-head regeneration,
  effective `report status` reads, and manager-only rerun/waive/keep-stale/
  invalidate/dependency-fix Decision PRs; no Impact or decision path launches
  an experiment;
- accepted-merge Git-object validation, credential-free post-merge shadow,
  authenticated GitHub observation/enqueue, Linear outbox/worker state
  machines, ingress grammar, and receipt lineage.

Still gated deployment or later-phase work:

- branch rules and reviewer-policy verification for the installed CODEOWNERS
  baseline, GitHub Submission/post-merge credential installation, a live PR
  pilot, and trusted scheduling;
- live Codex/Claude Plan-review invocation and credential/configuration canary
  against the explicitly selected deployment models;
- real Linear transport under the deployment credential and a measured shadow
  then allowlisted canary pilot;
- SSH fleet execution and cross-host artifact staging;
- trusted live resource/environment Impact providers, protected provider replay,
  and safe batch worktree synchronization;
- mobile/chat views and a shared GPU controller, only if operational evidence
  justifies their additional complexity.

See [DESIGN_ASSESSMENT.md](docs/DESIGN_ASSESSMENT.md) for maintainability,
extensibility, response-time, usability, failure simulations, and rollout gates.
The [scenario catalog](docs/USER_SCENARIOS.md) maps all 77 private historical
requirements to 33 public acceptance scenarios without publishing the source
conversation. Static traceability is requirement coverage, not a claim that all
later deployment scenarios already pass.

## Install And Develop

Python 3.12 is required:

```bash
python -m pip install -e '.[dev]'
python -m pytest
researchctl --help
researchctl-linear-host --help
researchctl-github-post-merge --help
```

The authoritative contracts are
[RESEARCH_CONTROL_PLANE_SPEC.md](docs/RESEARCH_CONTROL_PLANE_SPEC.md), the
accepted [ADRs](docs/adr), [MUTATION_CONTRACTS.md](docs/MUTATION_CONTRACTS.md),
the audited [WORKFLOW_COVERAGE.md](docs/WORKFLOW_COVERAGE.md), and
[TRACEABILITY_MATRIX.md](docs/TRACEABILITY_MATRIX.md). Development and tests
must write only inside the checked-out workspace or explicit temporary
directories; external reference repositories are read-only inputs.

Contributions follow [CONTRIBUTING.md](CONTRIBUTING.md). Report vulnerabilities
through [SECURITY.md](SECURITY.md); participation is governed by
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). The project is licensed under the
[MIT License](LICENSE).
