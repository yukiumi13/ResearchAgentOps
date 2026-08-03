# RCP Implementation Plan

Status: Active
Updated: 2026-08-03

The authoritative behavior is in RESEARCH_CONTROL_PLANE_SPEC.md and accepted
ADRs. This plan controls sequencing, not semantics.

## Current delivery status

- Phase 0 contracts, accepted ADRs, the anonymous 77-prompt/33-scenario catalog, and static
  traceability are present. Static mapping is not treated as executed acceptance.
- Phase 1 is implemented: strict protocol/schema lock, canonical serialization,
  safe existing-repository `init`, `doctor`, and `upgrade --check`.
- Phase 2 is substantially implemented locally: one shared
  `ApplicationService`, manager and Session actors, Task proposals, isolated
  worktree/tmux Agent Sessions, durable operation journal, status, exception
  inbox, read-only `session list/show/address`, and read-only reconcile. Guided
  Task creation, HostPool selection, and primary-inbox grouping remain gaps.
  Task write scope is enforced during Submission preparation, exact-head
  Submission CI, and frozen-source Run start/retry; it is mistake containment,
  not hostile same-user process isolation.
- Phase 3 is implemented for local execution: immutable Run refs, preflight,
  attempt/retry lineage, local process execution, collection, evidence reads,
  and crash/uncertainty handling. SSH transport remains Phase 5.
- Phase 4 is implemented in repository code: deterministic Submission,
  Decision, Report YAML and accepted Report Markdown; manager acceptance
  preparation; protected-base multi-type `ci dispatch`; conditional nested
  Submission attestation; protected-base workflow; a separate credential-free
  exact-PR-source test workflow; an installed single-maintainer CODEOWNERS
  baseline; and a reusable inert template. Supported
  protocol PRs validate, unknown or mixed control changes fail closed, and an
  ordinary source PR is `not_applicable` only to exact-head protocol validation
  while the source workflow tests its exact head. Branch rules, reviewer-policy
  verification, and a GitHub PR adapter remain deployment/product work.
- Phase 8's durable repository core is implemented and fake-port tested:
  credential-free preview, accepted-result and Session-reply delivery, Session
  inbox/outbox, fixed ingress grammar, verified receipts, claim/lease/retry/dead
  letter, stable-marker observation, fallback, and recovery. A real
  authenticated Linear webhook/poller and real publisher adapter remain
  deployment work. A credential-free post-merge shadow host and real Git-object
  accepted-merge reader are implemented; authenticated GitHub artifact ingress
  and a live shadow/canary pilot remain deployment work. No live Linear
  operation is implied by fake-port tests.
- Phases 5 through 7 and general Phase 9 presentation adapters are not
  implemented. They remain separate release gates so the v0.1 harness does not
  acquire a daemon, scheduler, broker, or distributed database prematurely.

The implementation order, quality assessment, goal simulations, and explicit
exit conditions are in `docs/DESIGN_ASSESSMENT.md`. Gate R0 closes the current
repository; R1 is the protected GitHub plus real Linear shadow pilot; R2 adds
SSH; R3 adds impact/sync; R4 adds a shared GPU controller only when contention
actually requires it.

Statuses above describe repository code and tests only; they do not mean GitHub
protection, either workflow, or Linear automation has been installed. A release
operator must review a tracked baseline and rerun verification from that exact
commit before enabling protected-branch enforcement.


## Reference repository policy

External repositories used for compatibility research are read-only references.
RCP development must not modify, initialize, format, test, cache, or generate
files inside them.

Their current patterns require RCP to tolerate:

- root-level Python and shell experiment entry points;
- Hydra and other repository-native configuration systems;
- dirty or script-heavy existing repositories;
- host-specific output roots and remote artifact paths;
- raw results, canonical conclusions, and lineage stored separately;
- background artifact transfer and destination-side verification;
- missing CI, lockfiles, or uniform CLI entry points.

RCP treats repository commands as opaque argv payloads. It does not rewrite a
target project's native launcher or configuration system during initialization.

## Phase 0: Contract freeze

Deliverables:

- authoritative specification;
- accepted ADRs for authority, records, acceptance, ownership, impact, resource
  safety, threat model, serialization, and operation recovery;
- requirement-to-test traceability matrix;
- explicit SLO and supported envelope.

Gate:

- no unresolved P0 contradiction from the design review;
- every mutation names owner, persistence point, idempotency, recovery, and
  terminal state;
- every later phase has observable acceptance criteria.

## Phase 1: Portable core

Deliverables:

- Python package and CLI skeleton;
- strict Project, Task, RunSpec, RunResult, Submission, Decision, Report, and
  StatusUpdate domain models;
- deterministic YAML, canonical JSON, digests, JSON Schema generation;
- protocol compatibility and explicit migration check;
- Git repository discovery and safe path handling;
- researchctl init, doctor, and upgrade --check;
- dry-run, JSON output, stable error envelope and exit codes.

Gate:

- initializing a dirty realistic repository twice creates no second diff;
- init never changes existing project files or commits;
- malformed, duplicate-key, unknown-field, and future-version records fail;
- generated schemas and config are byte-stable;
- a clone can validate managed state without local SQLite;
- unit and integration tests pass in Python 3.12.

## Phase 2: Task, session, and inbox

Deliverables:

- explicit bootstrap proposal and manager acceptance;
- Task CRUD through manager control changes;
- unique session branches and worktrees;
- tmux and Claude/Codex adapters;
- operation journal and local runtime SQLite;
- structured status outbox and exception inbox;
- read-only Session discovery/address generation plus durable notification
  list/ack/reply through the shared service.

Gate:

- two sessions can modify the same source path independently;
- an interrupted start is idempotently observed or resumed;
- restart and backup/restore preserve durable local runtime state; reconcile
  reports facts that cannot be reconstructed instead of inventing them;
- the manager can identify active, blocked, decision, and review items without
  opening transcripts.

## Phase 3: Immutable local runs

Deliverables:

- RunSpec, RunAttempt, RunResult and retry lineage;
- run branches and immutable tags;
- strict Task-required input, environment/config/input identity, explicit-host,
  worktree/path, executable, disk, fresh GPU identity, and artifact-path
  preflight;
- local runner and manual/static allocator;
- artifact record, review bundle, collection and verification.

Gate:

- a run uses its frozen commit while session code changes;
- wrong typed inputs fail before launch;
- kill or timeout after every launch step does not duplicate a process;
- deletion of local runtime DB does not lose frozen provenance;
- failed and partial attempts remain reviewable evidence.

## Phase 4: Trusted GitHub review

Deliverables:

- ResearchSubmission and deterministic canonical Report renderer;
- manager review accept command and atomic Report materialization;
- protected-base dispatcher, trusted Submission validator, outer exact-head
  workflow envelope, conditional typed Submission attestation, PR path matrix,
  and CODEOWNERS contract;
- separate unprivileged, credential-free source tests at the exact PR head;
- failure and snapshot-scope submissions.

Gate:

- one merged commit contains evidence, submission, decision, and Report;
- an agent cannot self-declare acceptance;
- every CI result and generated-output digest is bound to the exact PR head and
  tree; a new commit invalidates the prior attestation;
- required source tests execute the same exact PR head without trusted secrets;
- arbitrary Markdown and protected-path changes fail;
- isolated code can support a snapshot claim without claiming main applicability.

Passing Phase 4 is v0.1 Core.

## Phase 5: SSH fleet

Deliverables:

- typed remote transport, workspace, session, and inventory ports;
- fixed remote runner with protocol handshake;
- explicit-host cross-machine run and artifact resolution;
- bounded parallel fleet status and reconcile.

Gate:

- an on-prem-created commit runs exactly on a cloud host;
- lost SSH responses do not create duplicate tmux or runs;
- missing environment, inputs, or disk fail before GPU use;
- partial host failure returns bounded, timestamped fleet results.

## Phase 6: Conservative impact

Deliverables:

- snapshot and baseline claim scopes;
- validation basis and dependency review;
- impact proposal with optimistic concurrency;
- explicit worktree synchronization.

Gate:

- non-ancestor evidence is handled correctly;
- no-overlap never auto-proves validity;
- stale impact changes cannot overwrite a newer Report;
- live or dirty sessions are not modified automatically.

## Phase 7: GPU controller

Deliverables:

- frozen OpenAPI and allocation state machine;
- scoped identities, idempotency, audit and outbox;
- host lock, fencing generation, preflight and quarantine;
- single-writer SQLite deployment, migration, backup and restore.

Gate:

- 50 concurrent requests cannot double-allocate a GPU;
- network partition with a live old process quarantines instead of reallocating;
- controller restart reconciles before accepting new allocations;
- external GPU use is detected and surfaced.

## Phase 8: Linear projection and pilot

Repository-complete core:

- manager-owned workspace/team/project/issue bindings and remote target
  preflight;
- deterministic `linear.accepted-result.v1` rendering and secretless CI preview;
- trusted enqueue and delivery service, stable event IDs, external markers,
  delivery receipts, claim/lease/retry/dead letter, and crash recovery;
- authenticated-event ingress facade, fixed configured-`@app` command grammar,
  manager-owned author UUID allowlist, canonical request-bound receipts,
  Session inbox/list/ack/reply, terminal-Session fallback, app-author-verified
  observation, and same-thread reply delivery.

Deployment and pilot work:

- authenticated Linear webhook or poller;
- real Linear API publisher adapter under the trusted app credential;
- trusted accepted-merge trigger and operator scheduling;
- shadow pilot and measured end-to-end service objectives.

Gate:

- Linear outage never blocks Git or runs;
- CI pass without accepted merge creates no accepted-result comment;
- an exact accepted merge creates one comment whose issue ID, renderer version,
  payload digest and comment ID match its delivery receipt;
- wrong workspace, team, project, issue, renderer or digest performs zero Linear
  mutations and becomes a visible dead-letter event;
- retry, reorder, timeout, or a crash after the Linear API succeeds still has
  exactly one visible effect through marker observation;
- agents and untrusted CI cannot load or use Linear credentials;
- one real repository completes at least 20 runs on two hosts;
- attention and latency objectives are measured, not estimated.
