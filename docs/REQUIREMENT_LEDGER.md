# Post-export Requirement Ledger

Status: normative implementation input
Updated: 2026-08-03

`USER_SCENARIOS.md` preserves anonymous anchors for all 77 prompts in the
private historical research. This ledger gives stable identities to later
requirements so traceability does not silently stop at that boundary.

## REQ-20260803-001 - Workspace and reference-repository boundary

- Source: current implementation review, 2026-08-03.
- Maps to: `US-014`, `US-023`, `US-031`.
- Acceptance: implementation writes only inside its checked-out workspace;
  external compatibility repositories remain read-only references and receive
  no test, cache, formatting, initialization, or generated files.

## REQ-20260803-002 - One simple service for humans, Agents, and Python

- Source: current implementation review, 2026-08-03.
- Maps to: `US-003`, `US-008`, `US-014`, `US-022`.
- Acceptance: human CLI, strict Agent JSON, and supported Python automation use
  the same `ApplicationService`, request models, actor checks, state transitions,
  and error codes; no general HTTP API, broker, daemon, or second business state
  machine is introduced.

## REQ-20260803-003 - Historical questions are real acceptance cases

- Source: current implementation review, 2026-08-03.
- Maps to: `US-001` through `US-033`.
- Acceptance: all 77 historical prompt anchors remain covered by the scenario
  catalog and traceability test; a scenario is not marked implemented merely
  because its requirement is cataloged.

## REQ-20260803-004 - Product is an Agent harness

- Source: current implementation review, 2026-08-03.
- Maps to: `US-008`, `US-013`, `US-014`, `US-021`, `US-023`, `US-028`.
- Acceptance: RCP is described and implemented as an Agent harness plus a
  lightweight research control plane around Codex/Claude, Git, tmux, SSH,
  GitHub, and Linear; it is not represented as an Agent model, experiment
  framework, scheduler, or replacement for those systems.

## REQ-20260803-005 - CI validates Report bytes; post-merge worker publishes

- Source: current implementation review, 2026-08-03.
- Maps to: `US-017`, `US-018`, `US-021`, `US-030`, `US-033`.
- Acceptance: exact-head CI reconstructs and byte-compares accepted Report YAML,
  Markdown, and credential-free Linear preview and emits an external attestation.
  CI neither accepts nor publishes. Only trusted post-merge automation may
  revalidate the accepted merge, enqueue, publish, and store a delivery receipt;
  an Agent never selects the accepted destination, body, renderer, or credential.

## REQ-20260803-006 - Shared app addresses and hears one Session

- Source: current implementation review, 2026-08-03.
- Maps to: `US-006`, `US-009`, `US-010`, `US-030`, `US-033`.
- Acceptance: one visible app identity such as `researchctl-app` accepts
  authenticated `notify session:<full-id> commit:<full-sha>` and receipt-bound contextual
  `reply commit:<full-sha>` commands. The exact Session gets a durable inbox
  revision and can list, acknowledge, or queue a same-thread reply; stopped or
  lost Sessions reroute visibly to the manager. App comments and receipts retain
  Agent, Session, Task, optional Report, event, and payload attribution without
  per-Session email accounts or external users.

## REQ-20260803-007 - The assigned Agent proposes the Submission PR

- Source: post-export workflow correction, 2026-08-03.
- Maps to: `US-017`, `US-018`, `US-021`, `US-026`.
- Acceptance: `researchctl submit` validates collected evidence, creates and
  pushes the one derived Submission branch and commit, and creates or observes
  the exact GitHub PR with a deterministic title/body. The operation is not
  `proposal_open` after only a local commit, and the caller cannot select an
  arbitrary repository, head, base, or PR body. The Agent proposes; only the
  manager can prepare acceptance, and only protected exact-head review and merge
  create accepted truth.

## REQ-20260803-008 - Plans cannot hide Agent fallback decisions

- Source: post-export Plan/schema/reviewer correction, 2026-08-03.
- Maps to: `US-008`, `US-013`, `US-021`, `US-023`, `US-027`.
- Acceptance: any supported `PLAN.yaml` is a strict versioned ExperimentPlan
  with generated schema and deterministic RunSpec compilation. Local lint and
  exact-head Submission CI reject missing semantic choices, unknown fields,
  implicit provider/CLI/library defaults, and values without an authenticated
  user, accepted Task, or explicit Project-policy source. An independent typed
  PlanReview is bound to the exact Plan/Task digests; it may run as a read-only
  background subagent without another persistent Session, but it has a distinct
  invocation identity and can only pass, request input, or report invalidity.

## REQ-20260803-009 - Main merges create at most one reviewed Impact batch

- Source: post-export Report-code dependency continuation, 2026-08-03.
- Maps to: `US-019`, `US-020`, `US-021`, `US-026`.
- Acceptance: every accepted main push may invoke `researchctl ci impact` under
  trusted automation. It scans every accepted baseline Report from that
  Report's own validation basis, emits no mutation when no proposals exist,
  otherwise creates one digest-bound batch commit/branch/PR, and never launches
  a Run. Stable event-derived identity and commit timestamps make clean-runner
  retries reproduce the same commit SHA. Protected-base CI rescans all Reports
  and requires the exact generated path set and bytes before merge.

## REQ-20260803-010 - Every use case names its workflow and honest status

- Source: post-export workflow coverage and dependency-framework review,
  2026-08-03.
- Maps to: `US-014`, `US-020`, `US-025`, `US-027`.
- Acceptance: every stable `US-001` through `US-033` appears exactly once in a
  workflow checklist with one existing primary workflow, zero or more existing
  supporting workflows, current proof, and an open acceptance gap. Status must
  distinguish locally verified, partial, deployment-pending, and design-only
  work. Dependency tools may provide typed change evidence, but cannot become a
  second Report authority or silently turn missing lineage into no change.

## REQ-20260803-011 - External dependency evidence fails closed

- Source: post-export typed dependency-receipt continuation, 2026-08-03.
- Maps to: `US-016`, `US-020`, `US-021`.
- Acceptance: every resource/environment receipt binds provider identity and
  version, provider-query digest, Report basis, exact target, sorted
  observations, external basis/target identities, evidence digests,
  observation time, and a canonical receipt digest. `unknown`, absent,
  duplicate, undeclared, or target-mismatched evidence cannot produce a
  no-overlap validity advance. Batch scans expose unresolved Report IDs without
  blocking conservative stale proposals or automatically starting a Run. Live
  third-party adapters and protected provider replay remain a separate gate.

## REQ-20260803-012 - Effective Report state and Impact decisions are explicit

- Source: post-export Impact decision and manager-role continuation,
  2026-08-03.
- Maps to: `US-002`, `US-018`, `US-020`, `US-021`.
- Acceptance: `report status` derives applicability at an exact commit from the
  accepted Report and immutable Git objects. A changed full tree caused only by
  `.research/**` records does not make a newly accepted Report immediately
  pending; a governed source change or unavailable validation basis fails closed
  as `impact_pending`. Rerun, waiver, keep-stale, invalidation, and dependency
  correction are canonical digest-bound `ImpactDecision` records prepared only
  by an authenticated manager. Each decision binds the accepted Impact digest,
  Impact target, current Report revision, exact decision base, reviewer, reason,
  and disposition-specific inputs. Protected-base CI regenerates the exact
  Decision/Report bytes. A rerun decision references a manager-created Task but
  never starts, retries, or collects a Run.
