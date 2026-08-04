# Workflow and Use-Case Coverage

Status: audited implementation checklist
Updated: 2026-08-04

The private chat export is design research, not an executable contract. Its 77
prompt anchors are normalized into the 33 stable scenarios in
`USER_SCENARIOS.md`; this document adds the missing workflow and implementation
status layer. `RESEARCH_CONTROL_PLANE_SPEC.md`, accepted ADRs, and
`MUTATION_CONTRACTS.md` remain authoritative when historical proposals conflict.

Checklist `[x]` means the scenario has exactly one reviewed row and a named
primary workflow. It does not mean the acceptance scenario is implemented.
Status has these meanings:

- `verified_local`: the supported local path has focused executable coverage;
- `partial`: a useful path exists, but one or more acceptance conditions remain;
- `deployment_pending`: repository logic/fake-port coverage exists, but a real
  authenticated installation or pilot is absent;
- `designed`: the contract is recorded but the end-to-end workflow is absent.

## Workflow Catalog

### WF-00 - Governed Mutation and Recovery

```text
typed request -> OperationStarted -> actor/capability check -> derived mutation
-> observe external identity -> terminal journal result -> idempotent replay
```

Git, GitHub, tmux, SSH, and projection timeouts remain unknown until observed.
Human CLI, Agent JSON, and Python callers enter the same `ApplicationService`.
Unknown delivery-stage events fail closed; runtime SQLite is operational state,
not accepted project authority.

### WF-01 - Repository Adoption and Protocol Upgrade

```text
discover Git root -> conflict inventory -> init missing managed files
-> isolated bootstrap proposal -> manager preparation -> exact-head CI/review
-> protected merge -> doctor/upgrade compatibility checks
```

Existing files, branches, commits, and remotes are never silently rewritten.
Upgrade apply and the complete novice glossary remain open.

### WF-02 - Task Intent and Experiment Plan

```text
manager creates/updates Task -> accepted policy resolves domain/write scope
-> Agent drafts ExperimentPlan -> no-fallback lint -> independent PlanReview
-> deterministic RunSpec compilation or needs_input
```

This workflow is implemented locally. Every decision-bearing value must match
an exact accepted Task or Project-policy `plan_choices` value; lint returns
`needs_input` rather than accepting an Agent/provider default. A manager-only
control proposal configures the explicit reviewer provider/model, a distinct
ephemeral read-only invocation emits the digest-bound PlanReview, compilation is
deterministic, and Run/Submission/protected CI repeat the gates. A live
provider/model canary remains deployment pending.

### WF-03 - Session and Scoped Worktree

```text
accepted Task -> Session capability -> unique branch/worktree/tmux
-> Agent work/status -> attach or pause -> same-host continuation
-> stopped/lost or new identity for uncertain cross-host continuation
```

Submission and Run source checks enforce `allowed_write_paths`; this is mistake
containment, not hostile isolation between processes sharing one OS account.

### WF-04 - Attention and Addressed Notification

```text
Session status -> durable observation -> grouped exception inbox
Linear authenticated mention -> exact Session/commit receipt -> Session inbox
-> ack or same-thread reply -> terminal-Session manager fallback
```

Core inbox and notification state exist. Formal stale/SLO verification and a
real authenticated Linear ingress/publisher installation remain open.

### WF-05 - Frozen Run and Evidence

```text
RunSpec -> source/write-scope and input preflight -> immutable ref/worktree
-> attempt journal -> local/authorized runner -> collect artifacts/digests
-> one immutable RunResult -> retry uses a new Attempt or intent uses a new Run
```

Local execution is covered. Cross-domain staging and the fixed SSH remote runner
remain open; a Run never executes inside PR CI.

### WF-06 - Agent Submission and Human Acceptance

```text
collected Runs -> assigned Agent researchctl submit -> fixed Submission commit
-> push/open-or-observe PR -> source tests plus protected exact-head validation
-> manager review preparation -> CODEOWNER approval -> protected merge
-> accepted Decision and Report
```

The Agent proposes; it cannot populate accepted fields, approve, or merge. Live
branch-rule/reviewer-policy verification and cleanup dispositions remain open.

### WF-07 - Main Change Impact and Decision

```text
accepted main push -> trusted researchctl ci impact -> scan all Reports from
their own validation bases -> effective report status is current, stale, or
impact_pending -> no_change or one deterministic Impact batch PR
-> protected-base full regeneration -> human review/merge of analysis fact
-> manager review impact -> explicit Decision/Report PR -> protected merge
```

Git path batching, typed resource/environment receipts, fail-closed receipt
evaluation, unresolved Report classification, effective `impact_pending`
queries, and explicit rerun/waive/keep-stale/invalidate/dependency-fix records
are covered locally. Decision PRs bind the accepted Impact digest, current
Report revision, exact main base, and manager actor, then undergo protected-base
byte regeneration. Trusted live provider adapters and protected provider replay
remain open. No Impact or decision path launches a Run.

### WF-08 - Baseline Sync, Disposition, and Retention

```text
accepted baseline -> preview Session targets -> safe-point/dirty observation
-> independent update or update_pending/conflict -> merge/abandon/retain code
-> reachability-aware worktree/ref/artifact cleanup
```

Disposition fields exist in review records, but batch-safe sync, cleanup, and
retention enforcement are not implemented end to end.

### WF-09 - Protected CI and Source Validation

```text
PR event -> unprivileged exact-head source tests
plus protected-base dispatcher reading PR head as Git objects
-> strict PR-type regeneration -> exact-head attestation -> branch protection
```

The workflows and validators are locally tested. Repository rules and a live
pilot must still prove that required checks and approvals cannot be bypassed.

### WF-10 - Accepted-Merge Projection

```text
certified accepted merge -> authenticated GitHub check/artifact observation
-> stable outbox event -> configured target preflight -> observe marker/create
-> durable receipt -> retry/dead-letter/reconcile
```

Git remains authoritative and Linear disposable. Fake-port and authenticated
GitHub adapter tests exist; the real Linear transport and scheduled pilot do not.

### WF-11 - SSH and Cross-Domain Execution

```text
HostProfile -> outbound SSH handshake -> resolve exact tree/environment/inputs
-> fixed remote runner and tmux identity -> bounded observation -> digest-checked
artifact return -> ambiguity observed before retry
```

Only the bounded SSH transport primitive exists. Host onboarding, fixed runner,
fleet view, staging, and on-prem-to-cloud execution remain designed work.

### WF-12 - Shared GPU Allocation

```text
resource request -> candidate offer -> target preflight -> transactional claim
-> physical GPU check and host lock -> startup ack -> fenced heartbeat/release
-> quarantine and reconcile before reuse
```

The safety contract is frozen. No shared controller is implemented; manual
static assignment remains the supported path until real contention justifies it.

### WF-13 - Read-Only Mobile and Chat Views

```text
core inbox/outbox queries -> grouped mobile view or coalesced milestone event
-> disposable message/drill-through -> deletion never mutates accepted truth
```

These Phase 9 adapters are designed only. Addressed Session notification belongs
to WF-04 and is not a general chat command channel.

### WF-14 - Design, Traceability, and Performance Governance

```text
historical prompt anchors -> stable scenarios -> workflow/status checklist
-> acceptance IDs and tests -> measured supported envelope -> release gate review
```

Static completeness and preliminary local benchmarks exist. Static mapping is
not runtime acceptance, and production p95/p99 or external pilot claims require
recorded measurements from an exact release commit.

## Scenario Checklist

| Check | Scenario | Status | Primary workflow | Supporting workflows | Current proof | Open acceptance gap |
|---|---|---|---|---|---|---|
| [x] | `US-001` | `partial` | `WF-04` | `WF-14` | grouped 50-item inbox and preliminary benchmark | formal staleness and production SLO run |
| [x] | `US-002` | `verified_local` | `WF-02` | `WF-00` | manager Task/reviewer-policy authority, Agent denial, accepted-value Plan provenance, and CI field-scope tests | protected-repository and live reviewer pilot |
| [x] | `US-003` | `verified_local` | `WF-02` | `WF-00` | guided/non-interactive Task parity | novice pilot evidence |
| [x] | `US-004` | `designed` | `WF-13` | `WF-04` | read-only projection contract | mobile adapter and drill-through |
| [x] | `US-005` | `designed` | `WF-13` | `WF-10` | milestone projection contract | coalescing chat adapter |
| [x] | `US-006` | `deployment_pending` | `WF-04` | `WF-10` | exact Session receipt, ack/reply, fallback tests | real authenticated Linear ingress/publisher |
| [x] | `US-007` | `designed` | `WF-11` | `WF-03` | bounded outbound SSH primitive | HostProfile doctor and remote tmux onboarding |
| [x] | `US-008` | `verified_local` | `WF-00` | `WF-02`, `WF-03` | actor/capability deny matrix and audit | live credential boundary pilot |
| [x] | `US-009` | `deployment_pending` | `WF-10` | `WF-00` | adapter-only credential contracts and redaction tests | real credential installation and rotation |
| [x] | `US-010` | `deployment_pending` | `WF-03` | `WF-00`, `WF-10` | Session-scoped attribution and spoof rejection | real shared-app attribution pilot |
| [x] | `US-011` | `partial` | `WF-02` | `WF-11` | execution-domain policy records | HostPool selection and grouped fleet view |
| [x] | `US-012` | `partial` | `WF-05` | `WF-11` | typed local preflight and immutable inputs | cross-domain staging and destination verification |
| [x] | `US-013` | `partial` | `WF-03` | `WF-04` | local tmux attach/pause/continue and one-writer rules | provider-complete remote continuation |
| [x] | `US-014` | `verified_local` | `WF-00` | `WF-14` | Git/tmux/SQLite composition and CLI/JSON parity | observed pilot maintenance budget |
| [x] | `US-015` | `verified_local` | `WF-05` | `WF-06` | protected paths and renderer-owned Markdown tests | cleanup pilot across long-lived repository |
| [x] | `US-016` | `verified_local` | `WF-05` | `WF-06` | mismatch/failure evidence and submission checks | explicit failure-study policy workflow |
| [x] | `US-017` | `partial` | `WF-06` | `WF-09`, `WF-10` | canonical Submission/renderers plus separate Plan/PlanReview evidence and deterministic CI replay | live PR and installed-rule pilot |
| [x] | `US-018` | `partial` | `WF-06` | `WF-09` | fixed GitHub PR plus CLI/Git review artifacts | request-changes/reject/abandon end-to-end pilot |
| [x] | `US-019` | `partial` | `WF-08` | `WF-01`, `WF-06` | Git-owned accepted records and immutable refs | reachability-aware cleanup and retention apply |
| [x] | `US-020` | `partial` | `WF-07` | `WF-05` | batch, typed receipts, fail-closed unresolved classification, effective status and explicit decisions | live providers/replay and protected-repository pilot |
| [x] | `US-021` | `partial` | `WF-09` | `WF-05` | secretless exact-head CI separated from Runs | installed branch rules and authorized runner pilot |
| [x] | `US-022` | `partial` | `WF-01` | `WF-14` | init/doctor/help and core terminology | glossary golden tests and novice usability run |
| [x] | `US-023` | `verified_local` | `WF-03` | `WF-06`, `WF-09` | traversal/symlink/rename/protected path tests | hostile same-user isolation remains out of scope |
| [x] | `US-024` | `designed` | `WF-08` | `WF-03`, `WF-07` | safe-point and conflict contract | preview/apply batch sync implementation |
| [x] | `US-025` | `verified_local` | `WF-14` | `WF-00` | 77-prompt/33-scenario static traceability tests | executed acceptance results must stay separate |
| [x] | `US-026` | `partial` | `WF-08` | `WF-06` | review disposition is typed | cleanup enforcement for all three dispositions |
| [x] | `US-027` | `partial` | `WF-14` | `WF-00`, `WF-09` | repeatable preliminary inbox benchmark | production sample/window and external queue metrics |
| [x] | `US-028` | `partial` | `WF-03` | `WF-11` | deterministic local tmux and no daemon dependency | fixed SSH remote lifecycle |
| [x] | `US-029` | `designed` | `WF-11` | `WF-05` | exact-input and ambiguity contracts | on-prem-to-cloud run and artifact return |
| [x] | `US-030` | `deployment_pending` | `WF-10` | `WF-09` | stable outbox/receipt and crash-recovery fake-port tests | real Linear comment canary |
| [x] | `US-031` | `partial` | `WF-01` | `WF-00` | dirty/idempotent init and bootstrap tests | explicit upgrade apply and realistic repo pilot |
| [x] | `US-032` | `designed` | `WF-12` | `WF-11` | allocation safety ADR and state contract | controller, contention, quarantine, restore tests |
| [x] | `US-033` | `deployment_pending` | `WF-10` | `WF-13` | Git-authoritative replay and ignored-mutation tests | outage/canary against real Linear transport |

## Design Review

1. `High`: the typed receipt and fail-closed evaluator now prevent uncovered
   resources/environments from advancing validity, but no trusted live provider
   adapter or protected-base provider replay exists. Reports with those
   dependencies therefore remain unresolved under the default Git workflow.
2. `Medium`: WF-02 now closes the local no-fallback gap with accepted-value
   provenance, strict schemas, independent attributed review, deterministic
   compilation, Run receipts, Submission evidence, and CI replay. The remaining
   risk is operational: no live selected Codex/Claude reviewer canary or
   protected-repository policy PR has been observed.
3. `Medium`: WF-07 now has effective `impact_pending` reads and explicit
   manager Decision PRs, but live CODEOWNER/branch-protection certification has
   not been observed. The recorded reviewer string alone is not identity proof.
4. `Medium`: static traceability previously proved only that every prompt had a
   scenario, not that every scenario named an end-to-end workflow or honest
   implementation status. This checklist and its parser test close that audit
   gap; they do not manufacture acceptance evidence.
5. `Medium`: `ApplicationService`, `RuntimeStore`, and the protected dispatcher
   are large change surfaces. Delivery-stage event recording has been unified
   behind one allowlisted helper; further splits should follow observed hotspots,
   with the public service and one SQLite authority retained.
6. `Medium`: external GitHub/Linear/SSH behavior is mostly adapter or fake-port
   tested. Documentation must keep `deployment_pending` separate from local
   completion until branch rules, credentials, and a shadow/canary pilot are
   observed from an exact release commit.

## Dependency Framework Decision

No mature framework covers the whole Report governance problem. DVC, build
graphs, dbt, Dagster, and OpenLineage can be optional change-evidence providers;
they must not become a second Report state machine or acceptance authority. The
accepted integration boundary and tool-by-tool comparison are in ADR 0013.

The next implementation order derived from this review is:

1. implement a trusted provider port and protected-base receipt replay;
2. implement preview-first Session baseline sync;
3. run the protected GitHub, explicit Plan reviewer, and real Linear shadow
   pilot before adding another
   service, scheduler, or UI.
