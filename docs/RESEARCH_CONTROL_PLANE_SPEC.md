# Research Control Plane

## Authoritative Implementation Contract

Status: Phase 0 contract freeze
Protocol version: 0.1
Target release: v0.1 Core
Last updated: 2026-08-04

This document is the implementation authority for Research Control Plane (RCP).
Historical design notes are inputs, not executable requirements. A change to an
invariant or authority boundary requires an accepted ADR before code changes.

`MUTATION_CONTRACTS.md` is the normative command and recovery addendum.
`USER_SCENARIOS.md` and `TRACEABILITY_MATRIX.md` preserve the real conversation
cases as required acceptance inputs; a feature may be phased later but may not
silently discard one of those cases.

RCP is a research-agent harness with a lightweight control plane. It wraps agent
runtimes and research tools; it does not replace their reasoning loop.

## 1. Product outcome

RCP lets one human manager govern multiple coding agents and research runs
without treating chat transcripts as project state.

Human CLI, agent JSON mode, trusted automation, and Python callers invoke the
same typed `ApplicationService`. Presentation and transport adapters cannot
fork domain rules or grant authority. Core requires no general HTTP API, web UI,
message queue, second project database, or workflow DSL.

The manager must be able to answer, from one exception-oriented inbox:

1. What work is active?
2. What is blocked or deviating from plan?
3. What decision or review needs human attention next?

RCP must also ensure that:

- an agent proposes, but never accepts, durable research truth;
- every accepted result is tied to immutable evidence;
- code review and research-result review remain separate decisions;
- an existing repository can opt in through researchctl init;
- on-prem and cloud hosts use outbound SSH and tmux workflows;
- optional dashboards never become hidden sources of truth.

## 2. Supported operating envelope

The first production target is intentionally bounded:

- one trusted human manager;
- up to 10 managed repositories;
- up to 16 SSH hosts and 64 GPUs;
- up to 50 active tasks or sessions;
- up to 32 active runs and 200 queued resource requests;
- up to 5,000 finalized run records and 1,000 reports per repository;
- one GitHub repository per managed research project;
- agents are treated as error-prone, not hostile same-user adversaries.

The following are not v0.1 goals:

- a web UI;
- a multi-tenant cluster scheduler;
- hostile-process isolation under one Unix account;
- automatic environment provisioning;
- automatic semantic impact analysis;
- automatic acceptance, merge, rerun, or GPU preemption;
- DVC, jj, Dagster, Prefect, Slurm, Slack, or Agent Canvas integration.

## 3. Authority boundaries

No field may have two writers. A projection may be rebuilt from its authority.

| Object | Authority | Allowed writer | Recovery source |
|---|---|---|---|
| Project protocol and policy | Git default branch | manager-reviewed PR | Git clone |
| Task intent and declared status | Git default branch | manager | Git clone |
| Session code | session branch | assigned session | Git remote |
| Immutable RunSpec | immutable Git run tag/ref | researchctl run | Git fetch |
| Live RunAttempt and operation journal | host-local SQLite | researchctl/runner | reconciliation |
| Final RunResult | submission branch, then default branch | deterministic collector | Git clone |
| ResearchSubmission | submission branch | agent through renderer | Git remote |
| ReviewDecision/ImpactDecision and Report revision | reviewed decision PR before merge | manager | Git clone |
| Pull request and review state | GitHub | GitHub users/workflows | GitHub API |
| CIValidationAttestation | GitHub exact-head check and artifact | trusted CI | deterministic rerun |
| Live GPU allocation and lease | Resource Controller | controller transaction | DB and host reconcile |
| Linear issue/comment/labels | projection only | trusted projection worker | outbox replay |
| ProjectionReceipt | projection outbox journal | trusted projection worker | remote marker reconcile |

Git is authoritative for accepted project intent and research state. It is not
authoritative for a live process or a live GPU lease. Local SQLite is not a
second project database; it is durable runtime state and must be backed up.

Linear is an outbound projection plus a narrowly authenticated Session-message
transport in the MVP. A Linear edit may create only a non-authoritative
`SessionNotification`; it must not start a Task, accept a result, change
priority, allocate a GPU, or mutate accepted Git state.

## 4. Actors and trust model

### 4.1 Manager

The manager may create or close tasks, prepare acceptance records, merge, waive,
invalidate, supersede, rerun, reprioritize, preempt, and force release. Manager
identity comes from an authenticated manager command and GitHub review or merge
metadata. A YAML reviewer string is not proof of identity.

### 4.2 Execution agent

An agent may modify its session worktree, run approved commands, publish
structured status events, create code PRs, and create ResearchSubmissions. It
may not write accepted decisions, canonical reports, policies, project state,
or another session branch.

### 4.3 Trusted automation

Trusted CI validates deterministic rules. It must run a pinned validator from
the base repository or an immutable published action, never a validator modified
by the untrusted PR under test. Workflows handling untrusted PRs receive no
Linear, SSH, cloud, or manager credentials. CI emits an exact-head attestation;
an agent-supplied check result or status message is never accepted as CI proof.

### 4.4 Host security boundary

A worktree and an agent sandbox reduce accidental damage but do not isolate
malicious processes running as the same Unix user. Sensitive or unattended work
requires a separate Unix user, clone, container, or another OS boundary. The
hard v0.1 boundary is accepted-state protection through scope validation,
CODEOWNERS, required checks, and human merge.

## 5. Object model and cardinality

    Project 1:N Task
    Task 1:N Session
    Task 1:N RunSpec
    RunSpec 1:N RunAttempt
    RunSpec 0:1 RunResult
    Task 1:N ResearchSubmission
    ResearchSubmission 1:N RunResult
    ResearchSubmission 0:N CIValidationAttestation
    Report 1:N ReportRevision
    ReportRevision 0:N ProjectionReceipt
    ResourceRequest 0:N Allocation
    Allocation 1:N GPU

A task is not a process. A session is not a run. A failed attempt is not an
invalid research claim. A stale report is not evidence that its original run
did not happen.

Canonical IDs are globally unique, generated locally, and never depend on
Linear or a central database. Human-readable keys such as MAR-17 are aliases.
The canonical form is kind_YYYYMMDDTHHMMSSZ_96-bit-lowercase-hex.

All persisted timestamps are RFC 3339 UTC with a Z suffix. Repository paths are
normalized, relative POSIX paths. IDs, paths, and user input must never be
interpolated into a shell command.

## 6. State machines

### 6.1 Project

    uninitialized -> bootstrapping -> managed -> suspended -> managed
    managed|suspended -> archived

Only researchctl bootstrap accept may prepare the bootstrapping-to-managed
change. Merging an inventory-only PR cannot implicitly change project state.

### 6.2 Task

    planned -> ready -> active -> blocked -> active
    active|blocked -> needs_review -> active|done
    planned|ready|active|blocked|needs_review -> canceled

Only the manager changes declared Task state. Observed session or run state is
rendered separately and cannot silently mark a task Done.

### 6.3 Session

    preparing -> active -> idle -> active
    preparing|active|idle -> stopping -> stopped
    stopped -> active only for a positively observed same-host native resume
    preparing|active|idle|stopping -> lost
    lost is terminal

Every session has its own branch and worktree. The branch format is
research/task/TASK_KEY/SESSION_ID. Two hosts never write the same session branch
concurrently. A lost Session, uncertain ownership, or cross-host continuation
creates a new Session ID, branch, and worktree linked by `continued_from`. It
never revives or takes over the old Session.

### 6.4 RunAttempt

    preparing -> snapshotted -> preflighted -> allocated -> launching -> running
    running -> collecting -> succeeded
    preparing|snapshotted|preflighted|allocated|launching|running|collecting
      -> failed|canceled
    launching|running|collecting -> lost

RunSpec is immutable after snapshotted. RunAttempt is an append-only operation
journal. A retry creates a new attempt with retry_of and never rewrites the old
attempt. Succeeded, failed, canceled, and lost are terminal.

### 6.5 ResearchSubmission

    draft -> open -> changes_requested -> open
    open -> acceptance_prepared -> accepted
    draft|open|changes_requested -> withdrawn|rejected

Accepted means the submission, manager ReviewDecision, and generated Report
revision are present in the same merged commit.

### 6.6 Report

Report evidence validity and applicability are separate fields:

    evidence_status: verified | invalid
    applicability: snapshot_only | current | impact_pending | stale | superseded

claim_scope snapshot describes a historical result at an exact tree and is not
invalidated merely because main changes. claim_scope baseline asserts
applicability to a baseline and records validation_basis.main_tree_sha.

Stored applicability is not by itself the current read model. `report status`
evaluates a Report at an exact target commit/tree. Snapshot, stale, and
superseded stored states remain authoritative. For a stored current or
impact_pending baseline, the read compares its basis tree with the target and
excludes `.research/**` from governed source changes. This is required because
accepting a Report, Impact, or Decision changes the protocol tree and must not
make its own result immediately pending. Any governed source change yields
effective `impact_pending`; an unavailable basis tree also fails closed as
`impact_pending`. The result exposes both stored and effective applicability,
the exact target identity, comparison reason, and changed governed paths.

An `ImpactDecision` is distinct from `ReportImpact`. Impact records analyzer
facts; Decision records a manager disposition:

    rerun | waive | keep_stale | invalidate | dependency_fix

Every Decision binds its ID, accepted Impact ID/digest and target commit/tree,
current expected Report revision, exact decision base commit/tree,
authenticated reviewer actor, reason, decision time, disposition-specific
inputs, and a canonical digest. `rerun` requires an accepted planned/ready Task
ID but starts no Run. `waive` advances the validation basis and keeps verified
evidence current. `keep_stale` and `rerun` remain stale. `invalidate` makes
evidence invalid and applicability stale. `dependency_fix` requires a changed
dependency declaration and remains stale until new analysis resolves it.

### 6.7 Allocation

    queued -> offered -> launching -> running -> releasing -> released
    offered|launching|running -> suspect -> quarantined
    queued|offered -> canceled
    launching|running -> failed

Heartbeat loss never makes a GPU available. Quarantine ends only after fresh
host observation proves the process is gone and the host-local lock is free, or
after an explicit dangerous manager override.

## 7. Structured records

### 7.1 Task

A Task contains a human goal, done conditions, deliverables, constraints,
priority, one execution-domain key, execution preferences, a non-empty set of
allowed write-path prefixes, and typed required inputs. Optional parent Task and
milestone fields support hierarchy and views without creating another authority.
Free-text constraints are explanatory and cannot power deterministic CI checks.

Typed inputs include expected logical ID, immutable digest or version, resolver
policy, and whether a manager waiver is permitted.

Project policy maps each execution-domain key to one or more host-pool keys.
"Team" is only a view over that mapping, not an independent domain object.

Allowed write paths are normalized repository-relative POSIX prefixes matched
on path-segment boundaries, not a glob language. `.` covers ordinary repository
content, but `.research` and every descendant are globally protected and always
override a Task allowlist. Submission preparation checks the frozen source
change before creating proposal side effects, and exact-head Submission CI
repeats the check from protected-base Task truth. Local Run start and retry
validate the frozen source change before creating Run records or launching a
process. These checks cover renames, symlinks, submodules, traversal, file modes,
and protected paths. Session creation itself cannot predict or confine every
future write by a same-user process.

### 7.2 RunSpec

RunSpec is produced before any remote launch and contains:

- task, session, run, and operation IDs;
- source code commit and source tree SHA;
- source baseline commit;
- argv and working directory;
- sanitized environment fingerprint or image digest;
- exact config snapshot and digest;
- dataset and checkpoint logical IDs, resolvers, versions, and digests;
- requested host and resource constraints;
- artifact output declarations;
- canonical record digest.

Secrets and secret environment values are never persisted.

#### ExperimentPlan and independent plan review

`PLAN.yaml` is the strict `ExperimentPlan` CLI contract. The protocol provides
the versioned Pydantic model, generated `experiment-plan.schema.json`, canonical
Plan digest, typed `PlanReview`, generated `plan-review.schema.json`, and a
deterministic compiler from `ExperimentPlan` to `RunSpec`. The schema forbids
unknown fields and represents the
hypothesis, comparison or baseline, argv, configuration, immutable inputs,
metrics and directions, repetitions or seeds, resource request, stop/failure
conditions, and artifact declarations. Prose may explain a choice but cannot
replace a typed decision-bearing field.

Every resolved value that could change experiment semantics records its source:
an exact value in the accepted Task's `plan_choices` or the accepted Project
policy's `plan_choices`, together with the referenced record digest and field
path. An authenticated user decision therefore becomes executable only after a
manager-owned Task or policy proposal is reviewed and merged. A drafting Agent
cannot prove user intent by writing `source: user` into its own Plan.
`agent_inference`, provider defaults, environment-dependent defaults, and
omitted critical values are invalid sources. A policy default is legal only
when the accepted policy explicitly defines that exact value; it is never
inferred merely because a CLI or library has a default.

`researchctl plan lint PLAN.yaml` performs schema validation,
canonicalization, cross-field checks, Task and input binding, default-source
validation, and compilation precondition checks without creating a Run ref,
allocating resources, or launching a process. A separate `plan compile` reruns
those gates and deterministically emits the RunSpec. Missing or ambiguous semantic
choices return `needs_input` with stable field-level findings. The Agent must
ask the user or manager; it may not repair the Plan by guessing.

Deterministic lint is followed by an independent semantic review against the
accepted Task and explicit decision receipts. The reviewer emits a
schema-registered `PlanReview` bound to the exact Plan digest, Task digest,
review policy version, reviewer execution identity, and typed findings. Its
outcome is `passed`, `needs_input`, or `invalid`; `passed` is evidence that the
review ran, not human approval or authority to accept a Report.

The reviewer may be a short-lived background subagent under the parent RCP
Session and does not need another long-lived worktree, tmux process, or
user-addressable Session. The implemented adapter starts a distinct invocation:
Codex is ephemeral and read-only; Claude is non-persistent, uses plan permission
mode, and uses bare mode with tools, slash commands, and ambient MCP
configuration disabled. Both require an explicit manager-owned provider,
model, policy version, and timeout. GitHub, Linear, Session-capability, and SSH
agent credentials are removed from the reviewer environment. If the provider
cannot supply a distinct attributed result, review fails closed; it never
silently falls back to self-review by the drafting Agent. The reviewer cannot
edit the Plan, create a RunSpec, execute, submit, accept, approve, or merge.

The manager configures that provider/model contract with
`researchctl plan configure-review`. The command derives one fixed
`research/control/<operation-id>` proposal and may change only `plan_review` in
`.research/policies/default.yaml`. Protected-base CI reloads the previous and
replacement ProjectPolicy and rejects changes to any other policy field. The
command prepares a reviewed proposal; it does not push, approve, merge, or grant
manager authority to an Agent.

The compiler binds the accepted Plan and PlanReview digests into the resulting
RunSpec. Execution cannot begin until lint and required review pass. Submission
CI later repeats deterministic lint and verifies that the frozen RunSpec and
observed RunResult preserve the reviewed values and their sources. Local Run
start additionally requires the journaled `plan.review` completion receipt for
the exact Review digest. Plan-backed Submission evidence stores Plan and Review
as separate files. Live provider invocation remains a deployment canary rather
than a claimed production integration.

### 7.3 RunAttempt

Each journal event contains operation ID, monotonically increasing sequence,
step, observed state, host, time, idempotency key, external identifiers, and an
error or compensation record. Events are append-only.

### 7.4 RunResult

RunResult is created once a terminal state is observed. It contains the frozen
RunSpec digest, attempt lineage, actual host and GPU UUIDs, start/finish time,
exit status, failure classification, metrics, artifact attestations, and log
summary. A failed run remains evidence but cannot be rendered as a candidate
result unless policy explicitly permits a failure-study claim.

### 7.5 ResearchSubmission

The implementation type is ResearchSubmission. Agent Commit is UI language only
and must not be confused with a Git commit. A submission contains a claim,
linked RunResults, metrics, limitations, reviewed dependencies, a decision
request, and a deterministic review bundle no larger than 10 MiB.

### 7.6 ReviewDecision

The agent never creates accepted identity fields. researchctl review accept
records the authenticated actor, timestamp, conditions, claim scope, target
Report, expected Report revision, and accepted submission digest.

### 7.7 StatusUpdate

StatusUpdate is an operational event with running, blocked, needs_input, or
ready_for_review. It contains a concise summary, evidence links, blocker,
decision question, options, and suggested next action. It never changes Task
state. A local outbox persists it until configured projections acknowledge its
stable event ID.

### 7.8 CIValidationAttestation and ProjectionReceipt

Every `ci dispatch` execution emits a strict canonical workflow envelope for
one exact PR head. The envelope binds repository, PR, base commit, subject head
and tree, base/head refs, protected dispatcher/workflow/check identities, PR
type, applicability, sorted named checks and evidence digests, generation time,
and overall result. This outer envelope is not a `.research` protocol record
and is not registered in the schema manifest.

Only a Submission dispatch embeds a schema-registered
`CIValidationAttestation`. The nested record binds the same exact identities
as the outer envelope plus Project, Task, Submission, Decision, Report,
schema-manifest, generated-output, and renderer evidence. When Linear is
configured, it also contains a credential-free preview with target issue ID,
renderer version, and payload digest. It is validation evidence, not an accepted
research Report. Task, bootstrap, and ordinary-source dispatches do not invent
empty Submission/Report/projection fields.

A `ProjectionReceipt` records observation of one external delivery. It contains
the stable outbox event ID, accepted main commit, Report ID and revision, target
issue ID, external comment ID, renderer version, payload digest, delivery time,
and observed remote marker. It is operational evidence and never changes the
accepted Report.

### 7.9 SessionNotification and verified transport receipts

A `SessionNotification` is a non-authoritative, revisioned runtime message. It
binds Project, Task, Session, full commit object ID, message, workspace, issue,
thread, source comment, route, state, and timestamps. It never grants authority
to change a Task, Decision, Report, policy, or accepted Git path.

A `VerifiedLinearIngressReceipt` is written only by the dedicated authenticated
adapter boundary. It binds the configured app and non-secret credential
identity, exact workspace/issue/thread/comment/event identities, Task, command
and observed-payload digests, verification time, and any source marker resolved
from a local outbound receipt. A `LinearDeliveryReceipt` binds an accepted
result or Session reply to its exact target, comment, payload/transport digests,
stable marker, Task, Session, Agent, and optional Report attribution. These
SQLite records are durable operational state, not Git authority. Git-derived
observations may be reconciled, but operation history, capabilities, undelivered
notifications/outbox events, and inbox actions require backup and restore.

## 8. Canonical serialization and schema evolution

Pydantic v2 models define the protocol. JSON Schema is generated from the
models. Human-readable Git records use a deterministic YAML renderer; hashes
use canonical JSON with sorted keys, UTF-8, no NaN or infinity, and compact
separators.

YAML parsing must:

- use a safe loader;
- reject duplicate keys and unknown fields;
- reject aliases that exceed configured limits;
- reject non-finite numbers;
- normalize times to UTC and paths to repository-relative POSIX form.

Every object has schema_version. Unknown major versions fail closed. Minor
upgrades require researchctl upgrade --check and an explicit manager-applied
migration. Accepted records are never silently rewritten.

The managed repository pins protocol, CLI, schema, and CI action versions.
GitHub Actions are pinned to immutable commit SHAs, not floating tags.

## 9. Git protocol

Standard refs are preferred:

    research/bootstrap/BOOTSTRAP_ID
    research/control/OPERATION_ID
    research/task/TASK_KEY/SESSION_ID
    research/run/RUN_ID
    research/submission/SUBMISSION_ID
    research/impact/IMPACT_ID

An immutable research-run/RUN_ID tag points to the commit containing the frozen
RunSpec and retains its parent code commit. The run executes the recorded code
commit, not the metadata commit. The tag namespace should be protected by a
GitHub ruleset when supported. CI rejects tag reuse or a digest mismatch.

The run branch may append the final RunResult. The Submission PR copies RunSpec
and RunResult into accepted repository paths. Run branches may be removed only
after the accepted record is reachable from the default branch or after an
explicit rejection or retention decision.

## 10. Initialization and bootstrap acceptance

researchctl init is local, idempotent, and safe on a dirty existing repository.
It creates protocol configuration, empty managed directories, deterministic
schemas, and default policies. It does not classify existing documents, commit,
push, create a PR, or change existing Markdown.

researchctl bootstrap creates an isolated proposal branch and worktree. The
agent may read the repository and write only its bootstrap proposal directory.
Imported reports default to unverified; merging an inventory does not prove an
old result correct.

After review, the manager runs:

    researchctl bootstrap accept BOOTSTRAP_ID

The command adds the explicit Project transition and selected classifications
to the same PR. CI verifies deterministic rendering. Only then may the manager
merge and enter managed.

### 10.1 Portable project document contracts

Document linting is independently adoptable and does not require
`researchctl init`. An uninitialized Git repository may define one protected
`.researchctl-docs.yaml` containing a strict `DocumentLayoutPolicy` and run:

```text
researchctl doc contracts
researchctl doc schema --contract markdown-frontmatter
researchctl doc scaffold --type runbook --title TITLE
researchctl doc check DOCUMENT --json
researchctl doc render DOCUMENT.yaml --output-file DOCUMENT.md
researchctl doc policy-template --output-file .researchctl-docs.yaml
researchctl doc policy-lint .researchctl-docs.yaml
researchctl doc index --output-file docs/INDEX.md
researchctl doc agent-guide --output-file CLAUDE.md
researchctl doc tree --project . --json
```

These commands do not open `.research`, SQLite, a Session, or a manager context.
The same policy can be used by an editor adapter, pre-commit hook, external Agent,
or arbitrary CI. Stable JSON findings are the machine integration contract.
If no standalone, managed, or explicitly selected policy exists, the commands
fail with `document_policy_missing`; they do not apply a guessed hierarchy.
`doc policy-template` renders a complete structural candidate with all policy
sections explicit. Every route carries a `TEMPLATE:` rationale placeholder;
`doc policy-lint` rejects an uninvestigated template until each placeholder is
replaced with project-specific evidence. The template preserves a human-written
`docs/README.md` and maps its generated index to `docs/INDEX.md`. The customized
policy requires ordinary manager/CODEOWNER review before adoption.

Managed projects store the same policy at
`.research/policies/default.yaml.document_layout`. `doc.configure-layout` is
manager-only and prepares one fixed control proposal. Protected-base CI compares
the old and new ProjectPolicy field by field; no Agent can introduce or remap a
classification, contract, directory, generated index, or machine artifact root
inside an ordinary document proposal.

Each document route binds exactly one canonical `a/b:c` classification, short
document type, schema contract, directory, and non-empty rationale. Directory overlap, unknown paths,
type/path disagreement, unknown fields, missing required relations, unsafe
links, excessive nesting, orphan renders, and renderer byte drift fail closed.
Existing files may bypass conversion only through finite per-file legacy entries
with declared migration targets.

`DocumentLabel` enforces lowercase slash-separated namespaces and one `:`
category. `classification_depth` independently bounds the number of namespace
segments before `:`; defaults are two through four. Filesystem `max_depth`
bounds nesting below a mapped route, is constrained to `1..8`, and defaults to
four. Thus `a/b:c` is the shortest default label shape, not advisory prose, but
a reviewed project policy may explicitly tighten or widen the finite bounds.

Manual Markdown begins with strict frontmatter. `validity` is `valid`, `invalid`,
or `frozen`; only `invalid` has `invalid_reason`. When CI supplies a trusted
baseline checkout, a baseline-frozen document cannot change bytes or disappear.
On the first standalone-policy adoption, an otherwise valid baseline may have no
policy or old document root; only then does frozen scanning apply the subject
route shape to any old tree that exists. Any present-but-invalid, shadowed, or
unsafe baseline policy fails closed, and later policy-owned roots cannot vanish.
Structured design, status, and brief YAML remains canonical while its Markdown
is renderer-owned and visibly identifies its renderer version. A hidden
provenance marker binds the canonical source digest and generated body digest.
The renderer may atomically refresh an owned file only while its old body digest
still matches; manual edits continue to fail closed.

Standalone correctness also requires Agent discovery. `DocumentLayoutPolicy`
may declare `agent_guides`, each binding a repository-relative Markdown path to
the `claude` or `agents` format. `doc agent-guide` inserts or replaces only its
versioned managed block, preserves unrelated instructions in the file, and may
write only a target explicitly declared by policy. The block identifies both
policy locations, forbids hierarchy fallback, lists accepted routes, explains
manual versus structured authoring, and requires `doc tree`. It points Agents to
`doc contracts`, `doc schema`, `doc scaffold`, route-aware `doc check`, and
route-aware `doc render`, including the AnalysisBrief workflow. Tree lint rejects
a missing, malformed, symlinked, or byte-stale configured guide. The guide is a
discovery projection of policy, not another authority.

Top-level `doctor` detects standalone document mode when no managed markers are
present. It reports managed records and generated schemas as not applicable and
runs the effective policy/tree checks. If managed markers exist but required
schemas are missing, the original managed-project errors remain mandatory.

A shared Skill may describe the generic workflow and an MCP server may proxy
diagnostics for remote clients, but neither is required for standalone use and
neither may own project taxonomy or implement different validation semantics.
The repository policy, generated guide block, and CI form the portable baseline.

Projects may declare `machine_artifact_roots`, for example `data/` with an
extension allowlist of `.csv`, `.json`, `.jsonl`, `.xlsx`, and `.py`. Markdown
can never be allowed in such a root. This enforces the human-readable `docs/`
versus machine-consumed `data/` boundary without moving stable script inputs.
See ADR 0014 for the complete authority and integration decision.

## 11. Session and attention workflow

researchctl start creates or observes a unique session branch, worktree, tmux
session, and agent process. Every multi-step mutation writes an operation journal
before its first side effect. Repeating the same operation ID observes and
continues the original operation instead of creating duplicates.

researchctl inbox is a core command, not a Linear-only feature. It renders:

1. Needs Decision
2. Blocked
3. Needs Review
4. Stale or Needs Rerun
5. Failed or Lost
6. Running, including age and last observation
7. Waiting

StatusUpdates remain append-only. The inbox coalesces repeated observations by
a stable key derived from project, Task, Session, attention kind, and normalized
decision or blocker identity; coalescing never deletes the underlying events.
New evidence or a more severe state updates the visible item immediately.

`inbox ack` records that one manager saw the current item. `inbox snooze` hides
that item until an explicit time or until materially new evidence arrives.
`inbox resolve` closes only the attention item and records actor, reason, and
source update; it never changes Task state or rewrites a StatusUpdate. New
evidence after resolution creates or reopens the appropriate item.

The default reminder budget permits at most one unchanged reminder per dedupe
key every four hours and three repeats without new evidence. Projects may lower
that budget. Urgency affects ordering, not permission to spam another channel.

The fast path is cache-first and labels stale observations. --refresh performs
bounded parallel host queries. One unreachable host returns a partial result
and never blocks the entire inbox indefinitely.

### 11.1 Addressing one Session from Linear

One trusted Linear application identity, visibly identified by its configured
deployment name such as `researchctl-app`, transports messages for all Sessions.
RCP does not
create a Linear user, email address, daemon, or mailbox per Session. There is no
per-Session mention: a manager always mentions the shared app and uses exactly
one fixed first line followed by a non-empty message:

```text
@researchctl-app notify session:<full-session-id> commit:<full-git-object-id>
<message>

@researchctl-app reply commit:<full-git-object-id>
<message>
```

The explicit form addresses a full Session ID. The contextual form is valid
only in a thread already bound by a local RCP delivery receipt and derives the
Session from that receipt, never from pasted comment text or a display name. A
trusted adapter authenticates the event and app mention, records a verified
receipt, resolves the manager-owned Task issue binding, and invokes the same
`ApplicationService.notification_send` used by supported Python automation.
Ordinary CLI or Agent JSON cannot claim the trusted identity.

`researchctl session list`, `session show`, and `session address` use the
same read-only application queries. A manager may address any visible Session;
an Agent may address only its capability-bound Session. `session address`
verifies the full commit against the exact recorded branch and emits only the
first-line header plus `message_required=true`. It performs no durable write
and does not contact Linear.

Before persistence, RCP verifies the exact workspace and issue, Session/Task
relationship, recorded Session branch, full commit identity, and reachability
from that branch. Active, preparing, idle, and stopping Sessions receive a
durable Session route. A stopped or lost Session, including one that becomes
terminal while a message is open, is transactionally rerouted to the manager
exception inbox with a new revision.

The bound Agent and human manager inspect the same inbox through
`notification list`; the Agent can list, acknowledge, or reply only within its
Session capability. Polling occurs at safe checkpoints rather than by injecting
keystrokes into a live model transcript. Persistence proves the message was not
lost; only `ack` or `reply` proves that the Session application consumed it.

A reply, closed notification revision, and `linear.session-reply.v1` event are
one SQLite transaction. The trusted worker posts to the exact verified source
issue/thread, observes a stable marker before create, and records a receipt.
Visible comment attribution and the hidden marker identify the shared app,
Agent, Session, Task, optional Report, event, and payload; retries never turn
Linear into an authority for research acceptance.

## 12. Run workflow and distributed operations

`researchctl run start` does not implicitly commit arbitrary worktree changes.
The default requires a clean committed HEAD. Explicit snapshot mode previews
allowed paths before creating a snapshot commit. For a Plan-backed Run, Plan
schema/lint and the required independent review complete before
the first Run operation side effect. `needs_input` creates no Run ref, process,
or resource allocation.

For local `run start` and `run retry`, the launch boundary first resolves one
exact local protected-head object ID, loads the canonical Task bytes from that
commit, and validates the RunSpec baseline/source lineage and complete changed
path set against that protected Task. These checks precede construction of the
Run repository, ref/tag/worktree creation, local preflight, and process launch.
The same-user host limitation in Section 4.4 still applies: this is strong
mistake containment, not an OS sandbox against a malicious process that can
rewrite local Git refs or objects.

The operation order is:

1. allocate an operation ID and persist the local journal;
2. validate task policy, source state, and typed inputs;
3. create and hash RunSpec;
4. create and push the immutable run record and tag;
5. select an explicit target or request a non-binding controller offer;
6. preflight that target's protocol, environment, inputs, disk, and executable;
7. only after successful preflight, transactionally claim the offered resources
   or validate the explicit manual/static assignment;
8. stage a JSON RunSpec to a fixed remote-runner entry point;
9. acquire host-local locks, validate fencing, and physically check the GPU;
10. start or observe the deterministic tmux/process identity;
11. record startup acknowledgement and continue heartbeats;
12. collect terminal state and generate RunResult;
13. release or quarantine resources and reconcile external state.

Each step defines observe, retry, and compensate. An ambiguous SSH response is
observed before retry. Blind rollback and duplicate launch are forbidden.

Before the global controller exists, manual and static ResourceAllocator
backends require an explicit host and GPU selection. They do not claim global
queueing or protection against external users. --host auto is disabled.

With the controller, auto-host selection uses this fixed order:

    offer -> target preflight -> transactional claim

An offer grants no launch right. Failed preflight discards the offer before
another candidate is considered.

## 13. Remote execution and artifacts

SSH transports bytes, not trusted shell fragments. The controller sends
canonical JSON to a fixed researchctl remote-runner command. The remote runner
validates protocol compatibility and invokes the experiment with an argv array
using local process APIs.

An ArtifactRef contains scheme, immutable digest, size, media type, producer
host, retention class, and verification state. Initial adapters are git, file
scoped to a host profile, ssh, and optional gs.

Critical accepted evidence cannot rely only on an unqualified local path. Every
submission includes a small review bundle with metrics, config, plots or tables,
and a bounded log tail. Large artifacts remain external but have a digest and
retention declaration.

The deterministic review-bundle renderer targets at most 2 MiB by default and
enforces a 10 MiB hard limit over the sum of referenced bundle bytes. A managed
repository has a configurable reachable-bundle budget, default 1 GiB. Content
that would exceed either budget stays in an external artifact store and is
represented by its digest, size, media type, retention, and verification state.

Collection records the verification known at RunResult finalization. Any later
background recheck creates a separate append-only
`ArtifactVerificationAttestation` containing ArtifactRef digest, verifier,
observation time, observed destination digest, and outcome. It never edits a
terminal RunResult in place. A submission or Report may reference the newer
attestation explicitly.

## 14. Submission and atomic human acceptance

`researchctl submit` is invoked by the assigned Agent after collection. It
validates evidence, creates the fixed Submission branch and deterministic
proposal commit from the protected default branch, pushes that exact commit,
and creates or observes the one GitHub PR for the derived repository, head, and
base. The caller cannot choose an arbitrary branch, base, repository, title, or
body. The title and body are deterministic renderings of structured records.
The operation reaches `proposal_open` only after the exact remote branch and PR
are observed; a local proposal commit alone is not an open proposal.

The Submission branch adds:

- finalized RunSpec and RunResult records;
- for a Plan-backed Run, the finalized ExperimentPlan and PlanReview records;
- ResearchSubmission;
- deterministic review bundle;
- a proposed Report diff stored under the proposal, not accepted paths.

The agent cannot write ReviewDecision or accepted Report fields. After review,
the manager runs:

    researchctl review accept SUBMISSION_ID [--condition TEXT]

This command verifies expected PR head and Report revision, writes the
ReviewDecision, materializes the accepted Report revision on the same branch,
and reruns validation. The command prepares acceptance; its caller and the
recorded reviewer string do not certify manager identity. Acceptance is valid
only when the expected head `H`, trusted required checks for `H`, a current
authorized CODEOWNER approval for `H`, branch protection, and the resulting
merge into the protected default branch all agree. Any commit after approval
invalidates the checks and approval for the previous head.

The merged commit atomically contains submission, evidence, decision, and report.
A post-merge job may project this fact but may not invent or change research
semantics. Rejection closes the proposal; a durable rejection record is a
separate manager control change when required.

## 15. Impact and baseline propagation

Impact analysis is conservative. A non-overlap means only that declared
dependencies did not overlap; it is not proof of semantic validity.

Each baseline-scoped Report records a validation basis. When main changes, the
effective status is impact_pending until analysis against the Report basis
completes. The first release creates proposals and never automatically advances
validity.

Impact changes use optimistic concurrency against expected Report revision and
latest main tree. A stale impact PR must be regenerated before merge.

The first implemented code-path slice is:

    researchctl impact REPORT_ID \
      --expected-report-revision REVISION \
      --target-commit FULL_MAIN_COMMIT

Only a manager or trusted automation may invoke it. The command resolves the
exact protected default head, loads the latest accepted Report from that Git
object, and diffs the Report's own `validation_basis.main_tree` against the
target tree with rename detection disabled. Protocol records under `.research`
are excluded from code-path impact so an Impact revision cannot recursively
trigger itself. Path dependencies are either exact repository paths or a
single trailing `/**` segment-prefix pattern; other glob syntax and protected
control paths are invalid.

Changed-path collection, dependency evaluation, and Report governance are
separate boundaries. The built-in `DeclaredDependencyImpactEvaluator` consumes
canonical Git changed paths plus optional typed resource/environment receipts
and may return only declared dependencies. The Report builder rejects evidence
mutation, undeclared matches, omitted receipt uncertainty, or evaluator identity
drift. Optional dependency frameworks still require a trusted adapter and
protected-base provider replay; they cannot be injected through an untrusted PR
or selected by an Agent.

Each `DependencyChangeReceipt` binds provider ID/version, a provider-query
digest, Report basis tree, exact target commit/tree, known or unknown
observations, external identities, evidence digests, observation time, and a
canonical receipt digest. `changed` and `unchanged` must agree with the recorded
basis/target identities; `unknown` requires a reason. Each `ReportImpact`
persists the complete receipts it consumed plus `change_provider_id` and
`dependency_evaluator_id`. Its canonical digest and rendered review artifact
bind these values, and a batch source digest binds each child source digest.

A path overlap can conservatively propose `stale` even when an external
dependency remains unresolved. A no-overlap validity advance requires a known
observation for every declared resource and environment. With no trusted live
provider configured, the Git batch records those Reports as unresolved and
does not propose a Report revision for them.

The generated branch is `research/impact/<impact-id>` with the exact target
commit as its single parent. It adds exactly:

- `.research/impacts/<impact-id>/impact.yaml`;
- `.research/reports/<report-id>/<next-revision>.yaml`; and
- `.research/reports/<report-id>/<next-revision>.md`.

An overlap proposes `stale` while preserving the prior validation basis. A
no-overlap proposes `current` with the validation basis advanced to the exact
target tree. Neither changes evidence tree, accepted-at tree, RunResult IDs,
claim, dependency declarations, or evidence status. Both outcomes require an
Impact PR, protected-base byte regeneration, human review, and merge; neither
starts a Run. Snapshot Reports are not applicable.

After an Impact analysis and its conservative Report revision are accepted, a
manager may record the actual disposition:

    researchctl report status REPORT_ID [--target-commit FULL_COMMIT]

    researchctl review impact IMPACT_ID REPORT_ID \
      --expected-impact-digest sha256:... \
      --expected-report-revision REVISION \
      --target-commit FULL_MAIN_COMMIT \
      --disposition rerun|waive|keep_stale|invalidate|dependency_fix \
      --reason TEXT

The status command is read-only and is available to manager, Agent, and trusted
automation roles. The decision command is manager-only. Caller input cannot set
reviewer identity, decision time, repository, remote, branch, base branch, PR
title/body, or rendered Report bytes. A rerun disposition additionally names an
already accepted manager-created Task. It does not invoke `run start`, `run
retry`, or `run collect`.

The derived decision branch is
`research/impact-decision/<decision-id>` over the exact current protected main
commit. It adds exactly one canonical `ImpactDecision` under
`.research/decisions/` and the next Report YAML/Markdown revision. The PR is
manager-authored through the shared ApplicationService. Protected-base CI loads
the accepted single or batch Impact and current Report from the exact base,
checks optimistic concurrency and any rerun Task, rebuilds the Decision bundle,
and compares the complete path set and bytes. A reviewer string in YAML or a
commit author is not manager authentication; accepted authority still requires
current CODEOWNER approval, required exact-head checks, branch protection, and
merge into protected main.

This slice analyzes Git code-path events only. It does not interpret the absence
of an external resource signal as proof that datasets, checkpoints, or runtime
environments did not change.

The merge-triggered code-path workflow is also implemented:

    researchctl ci impact \
      --before PREVIOUS_MAIN_COMMIT \
      --after CURRENT_MAIN_COMMIT \
      --generated-at MERGE_TIMESTAMP

The trusted main-push entry point scans every accepted Report at the exact
target commit. Snapshot Reports are recorded but skipped; stale or superseded
Reports are ineligible; Reports already based on the target tree are up to
date. Every other eligible Report is compared from its own validation basis,
not from `--before`. If proposals exist, one `ReportImpactBatch` and one fixed
Impact branch/PR contain the next YAML and Markdown revision for every affected
Report. Reports lacking complete external evidence are recorded as
`unresolved_report_ids` and never receive a no-overlap validity revision. If no
proposals exist, the operation returns `impact_unresolved` when such Reports
exist, otherwise `no_change`, before creating a worktree, pushing, or contacting
GitHub.

The batch ID, operation ID, rendered bytes, Git timestamps, commit SHA, branch,
and PR are deterministic for the push event. Protected-base CI parses the batch
record and independently repeats the all-Report scan from the exact PR base,
then requires the exact generated path set and bytes. The batch is limited to
256 Report proposals. It never launches or retries a Run.

Trusted live resource/environment provider adapters, protected-base provider
replay, and safe Session baseline synchronization remain required extensions.

Dependency engines are evidence providers, not Report authorities. Repositories
that already use DVC, Bazel/Pants/Nx, dbt, Dagster, or OpenLineage may later
enable a typed adapter that emits immutable path/resource/environment changes
and a source receipt. Missing, ambiguous, or unstructured provider output fails
closed. No adapter may advance applicability, create a waiver, start a Run, or
accept a PR. Git path comparison remains the dependency-free built-in provider;
ADR 0013 defines the integration boundary.

A live session, dirty worktree, or unknown state receives update_pending. The
MVP never tries to infer an agent safe point. Only a stopped session or explicit
manager or session action applies a baseline merge.

## 16. Resource Controller safety contract

The controller is a cooperative allocator for RCP-managed workloads. It does
not supersede an institutional scheduler and cannot prevent unrelated users or
manual processes from consuming a GPU.

Every allocation uses:

- global `gpu_uuid` uniqueness across every non-terminal assignment;
- an idempotency key with stored response;
- a monotonically increasing fencing generation;
- a host-local lock held for the runner lifetime;
- startup acknowledgement;
- monotonic heartbeat sequence numbers;
- fresh inventory and an immediate pre-launch physical check.

Lost contact moves GPUs to quarantine. Controller restart enters reconciliation

Claim is the allocation linearization point. Generation fences stale launch,
heartbeat, and release calls; it is not part of the uniqueness key and can never
make a second simultaneous assignment for one physical GPU valid. An offered or
expired lease is not proof that a previous process or host-local lock is gone.

mode and forbids new allocation until active leases and hosts are checked.

SQLite WAL is acceptable within the supported envelope only with one service
instance, one writer process, persistent storage, busy timeout, migrations,
unique constraints, backups, and restore tests. Postgres requires a new ADR
when load or availability exceeds the envelope.

## 17. CI and path policy

The workflow entrypoint is `researchctl ci dispatch`. It classifies the exact
base-to-head Git-object diff by protected paths, canonical content, commit
marker and corroborating source ref, never by branch name alone. It supports:

- Submission proposal and acceptance;
- generated Task create, update, and cancel control changes;
- generated Report Impact proposals;
- bootstrap proposal and acceptance;
- manager-owned Linear projection policy control; and
- ordinary source changes with no protocol path, reported as
  `not_applicable`.

Unknown or mixed protocol changes fail closed. Standalone post-bootstrap
policy, schema, Project, Report, Decision, and other control mutations are
unsupported until a protected-base validator and tests are added for that type;
the only supported Report-only revision is the closed generated revision inside
a validated Impact proposal.
An ordinary-source `not_applicable` result proves only protocol-path absence;
it does not run tests, validate semantics, or approve source. The workflow never
checks out, imports, installs, or executes PR source.

Every dispatch writes one canonical outer envelope outside the PR worktree. It
binds repository, PR, exact base/head/tree and refs, protected dispatcher,
workflow and check identities, PR type, applicability, sorted named checks and
their evidence digests, and the overall result. Only a Submission envelope
embeds the typed `CIValidationAttestation`.

Submission validation checks the closed changed-path set, actor/scope contract,
schema and canonical serialization, source and Run evidence linkage, typed Task
inputs and waivers, expected Report revision and acceptance identity, and
symlink, submodule, traversal, rename, file-mode, and `.research` policy. Only
this path regenerates and byte-compares canonical Report YAML, accepted Report
Markdown, and, when configured, the credential-free
`linear.accepted-result.v1` preview. It does not query or mutate Linear. A
missing integration is recorded as `projection: disabled`.

For Plan-backed Runs, Submission validation also reruns ExperimentPlan lint,
validates the independent PlanReview receipt and reviewer/drafter separation,
and compares every decision-bearing Plan value and source with the frozen
RunSpec and collected RunResult. An omitted source, an Agent-invented default,
a provider-dependent fallback, review of a different Plan digest, or execution
that drifted from the reviewed Plan fails the exact-head check. A review-model
opinion never overrides a deterministic failure.

The outer artifact is not committed back to the PR, because doing so would
create a new head that invalidates its own evidence. Repository unit and
protocol conformance tests protect releases of the dispatcher and validators.
The protected-base workflow does not execute the PR's test suite; a separate
credential-free `pull_request` workflow checks out the exact PR head and runs
source tests. Both checks are required because exact-head `not_applicable` does
not validate source behavior.

CI validates only. It never accepts research or publishes a result. Acceptance
is the protected merge whose exact head has current required checks and
CODEOWNER approval. The checked-in single-maintainer CODEOWNERS baseline does
not install branch protection or prove reviewer policy; an administrator must
configure and verify those controls before the checks become an operational
merge gate. Because GitHub does not count self-approval, the intended deployment
uses a distinct Agent GitHub App/bot identity to author proposals and the human
manager identity to review them. A manager-authored PR needs a second human
reviewer or cannot be placed behind a required one-approval rule.

A bounded read-only governance adapter and `researchctl github doctor` normalize
classic protection plus active rulesets applicable to the target branch. The
audit checks required PR/CODEOWNER/latest-push review, both fixed status checks,
strict base currency, force-push denial, deletion denial, and visible bypasses.
It exits nonzero on a missing required gate and never installs rules. With a
managed project it binds the observation to accepted `ProjectPolicy.github`;
without one it cannot certify proposal principals.

The protected field-specific control proposal changes only that GitHub policy.
`researchctl github apply-governance` is a separate deployment operation: by
default it only previews and emits accepted-policy and observed-state digests.
Explicit apply requires both digests, rejects a Session environment, verifies
the live `gh` user against accepted human Manager users or active teams, refuses
active ruleset or bypass-policy ambiguity, writes one bounded classic protection
payload, and audits the effective result. Every uncertain result requires a new
observation before retry. This command is implemented and locally tested, but
has not applied rules to this repository. ADR 0015 retains the proposal broker,
pre-create App credential proof, and authenticated post-merge identity checks
as deployment work.

Trusted post-merge automation revalidates an accepted merge and prepares one
stable projection event. Credential-free `shadow` mode emits only a canonical
observation and writes neither a live outbox row nor a remote comment. Live
enqueue requires authenticated GitHub workflow/check/artifact provenance. The
projection worker, never the Agent, then:

1. reads the issue UUID bound to the canonical Task;
2. verifies remotely that the issue belongs to the allowlisted workspace and
   team or project and is not archived;
3. re-renders the accepted Report and matches its renderer and payload digest;
4. observes the stable event marker before creating a comment; and
5. records the returned issue/comment IDs and payload digest in a receipt.

There is no title search, guessed target, default-issue fallback, or Agent
credential. A target or digest mismatch performs no Linear mutation and enters
the exception inbox as `dead_letter`. A remote outage remains `retryable` and
never changes the accepted Git state.

The exact-head PR workflow runs from the protected base and uses no untrusted secrets.
`pull_request_target` supplies only protected executable code; the PR head is
read as Git objects. Only the trusted post-merge projection worker may load
Linear credentials. The repository contains a real local Git accepted-merge
reader, a one-shot shadow host, an authenticated `gh api` post-merge ingress,
enqueue/delivery core, and fake-port transport tests. The authenticated ingress
binds the merged PR, protected workflow identity, exact successful check run,
and exact non-expired artifact before enqueue. Installing its GitHub credential,
scheduling it from a trusted environment, and providing a real Linear publisher
remain deployment-specific and are not claimed to be live.

## 18. Idempotency, reconciliation, and observability

Every external mutation has an operation ID, stable idempotency key, durable
step journal, precondition, observation method, compensation, and absorbing
terminal state.

researchctl doctor --reconcile compares Git records, local DB, worktrees, tmux,
processes, remote hosts, and the controller. It defaults to dry-run and prints a
repair plan. It never assumes an ambiguous side effect failed.

Structured logs carry operation, project, task, session, run, request,
allocation, and host IDs. Secrets and credential paths are redacted. Stable JSON
output includes schema version, command, success, data, warnings, errors, and
observation time.

## 19. Performance and recovery objectives

| Operation | Objective |
|---|---|
| local status or inbox | p95 <= 500 ms, p99 <= 1 s |
| fleet status across 16 hosts | p95 <= 5 s, hard deadline 8 s, partial results |
| warm start to tmux ready | p95 <= 30 s, p99 <= 60 s |
| run request acknowledgement | p95 <= 3 s; queued request returns immediately |
| controller allocation API | p95 <= 300 ms with fresh inventory |
| local submission validation | p95 <= 3 s |
| GitHub CI result | p95 <= 4 min, queue and execution measured separately |
| local attention event visibility | <= 5 s |
| Linear projection | p95 <= 2 min, p99 <= 15 min |
| controller restart | RTO <= 5 min; no allocation before reconcile |
| accepted Git state | RPO 0 after merge |

Inventory age up to 30 seconds is fresh. Between 30 and 60 seconds the controller
must complete a target refresh before it may offer or claim that GPU; cached
data alone is insufficient. At more than 60 seconds, or when refresh fails, the
GPU is unallocatable. Heartbeat period is 15 seconds. After 45 seconds an active
allocation becomes suspect, never available solely because of elapsed time.

Every SLO report records release/commit, fixture digest, host profile, cache
state, observation window, sample count, queue time, execution time, failures,
and partial results. Local p95/p99 uses at least 200 samples over at least 30
minutes. Fleet, CI, and projection reports use at least 20 end-to-end samples
from the supported-envelope fixture; small samples are labeled preliminary and
cannot pass a production gate.

For `warm start to tmux ready`, the CLI and adapter are installed, the repository
and required Git objects are present, the target environment and inputs passed
preflight, and the host is reachable. Measurement begins when the accepted
start request enters `ApplicationService` and ends only after tmux/process and
native agent-session identity are durably observed. It excludes environment
provisioning and large input transfer, which are outside v0.1. A cold result
uses a new researchctl process with empty in-process caches and is reported
separately; it cannot be substituted for the warm objective.

CI latency is measured from GitHub event creation to the exact-head required
check completion, with queue and execution percentiles separate. Linear latency
is measured from durable accepted-merge outbox event to matching receipt; retry
and outage-delayed samples remain visible rather than being discarded.

## 20. Implementation gates

### Phase 0: contract freeze

Complete this specification, ADRs, state machines, threat model, SLOs, and a
traceability matrix.

### Phase 1: portable core

Implement package skeleton, domain models, schema generation and migrations,
canonical serialization, Git protocol helpers, init, doctor, and upgrade check.

### Phase 2: task, session, and inbox

Implement explicit bootstrap acceptance, tasks, unique sessions, worktrees,
tmux, agent adapters, status events, operation journal, and inbox.

### Phase 3: immutable local runs

Implement RunSpec, RunAttempt, RunResult, manual/static allocation, local runner,
preflight, artifact review bundles, collection, retry, and reconciliation.
ExperimentPlan, deterministic Plan-to-RunSpec compilation, strict plan lint,
independent read-only PlanReview, manager-owned reviewer configuration, Run
gating, Submission evidence, and protected-CI replay are implemented as one
gated slice. A live explicitly selected Codex/Claude reviewer remains a pilot
gate.

### Phase 4: trusted GitHub review

Implement ResearchSubmission, manager acceptance, Report materialization,
GitHub PR adapter, deterministic render validation, exact-head CI attestation,
and CODEOWNERS checks. Passing this phase is v0.1 Core.

### Phase 5: SSH fleet

Implement versioned remote runner, host profiles, bounded fleet queries,
cross-host inputs and artifacts, and on-prem-to-cloud recovery.

### Phase 6: conservative impact

Implement baseline validation basis, impact proposals, optimistic concurrency,
and explicit synchronization.

### Phase 7: GPU controller

Implement the frozen API and safety state machine. Enable --host auto only after
concurrency, crash, physical-race, and network-partition tests pass.

### Phase 8: Linear projection and pilot

Implement manager-owned Linear target binding, secretless CI preview, trusted
post-merge enqueue and publishing, delivery receipts, idempotent one-way replay,
dead-letter inbox visibility, verified `@app` Session ingress, durable
list/ack/reply, same-thread reply projection, and reconciliation. One shared app
identity retains Agent/Session/Task/Report attribution; no Session mailbox is
created. Run a shadow pilot on one real repository, two hosts, and at least 20
runs before declaring the complete MVP production-ready.

### Phase 9: optional interaction integrations

Implement Slack/chat milestone projection and a read-only grouped mobile view
as adapters over the shared application queries and event outbox. Additional
chat transports may reuse Phase 8's explicit Session addressing, but cannot
become Task, Report, Session, or allocation authorities and are not required for
Core or the complete MVP.

## 21. Definition of done

v0.1 Core is done when a real repository can complete:

    init -> bootstrap proposal -> manager acceptance -> managed
    task -> isolated session -> immutable local run -> RunResult
    ResearchSubmission -> CI -> manager acceptance -> atomic merged Report
    inbox -> blocked/decision/review visibility

The complete MVP additionally requires explicit-host SSH execution, conservative
impact analysis, controller partition safety, and optional Linear projection.

No phase is complete merely because commands return success. Acceptance tests,
crash tests, security checks, response objectives, and recovery must pass.
