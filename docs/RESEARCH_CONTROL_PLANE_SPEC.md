# Research Control Plane

## Authoritative Implementation Contract

Status: Phase 0 contract freeze
Protocol version: 0.1
Target release: v0.1 Core
Last updated: 2026-08-03

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
| ReviewDecision and Report revision | same submission PR before merge | manager | Git clone |
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
    research/impact/OPERATION_ID

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
allowed paths before creating a snapshot commit.

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

researchctl submit creates a branch from the default branch and adds:

- finalized RunSpec and RunResult records;
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
- bootstrap proposal and acceptance;
- manager-owned Linear projection policy control; and
- ordinary source changes with no protocol path, reported as
  `not_applicable`.

Unknown or mixed protocol changes fail closed. Standalone post-bootstrap
policy, schema, Project, Report, Decision, and other control mutations are
unsupported until a protected-base validator and tests are added for that type.
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
merge gate.

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
reader, a one-shot shadow host, enqueue/delivery core, and fake-port transport
tests. A GitHub-authenticated artifact ingress and real Linear publisher adapter
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
