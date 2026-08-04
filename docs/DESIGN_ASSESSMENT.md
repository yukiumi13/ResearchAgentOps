# Research Control Plane Design Assessment

Status: implementation review
Updated: 2026-08-04

## Conclusion

RCP is an Agent harness and a lightweight research control plane. It wraps
existing Codex and Claude runtimes with identity, worktree isolation, narrow
capabilities, immutable execution evidence, human acceptance, exact-head CI,
and optional projections. It is not an Agent model, workflow engine, experiment
framework, scheduler, or replacement for Git, SSH, tmux, GitHub, or Linear.

The architecture can meet the stated local research-management goal without a
daemon or message broker. The implemented local core is already stronger than
the original design draft in authority separation and crash recovery. It is not
yet a production-complete multi-host system: a real Linear connector, protected
repository configuration, SSH fleet execution, live resource Impact providers,
worktree sync, and the optional GPU controller still require
deployment or later phases. Main-push code-path Impact batching is implemented
locally but still needs a live repository pilot.

## Quality Assessment

| Quality | Current assessment | Evidence and remaining risk |
|---|---|---|
| Correctness and governance | Strong for the local core | Strict records, actor capabilities, Git path closure, immutable Run refs, manager-only acceptance, exact-head CI, and crash/idempotency tests prevent an Agent status or self-authored Report from becoming accepted truth. Real GitHub rules still must be installed by an administrator. |
| Maintainability | Acceptable for R0, with required internal cleanup after it | Domain models, deterministic renderers, stable errors, and thin CLI presentations are clear. `ApplicationService` and `RuntimeStore` are already large, and Linear composition currently uses a concrete worker plus a private bind hook. Split internal command/store modules behind small protocols after R0 while retaining one public facade and one SQLite database. |
| Extensibility | Strong at declared boundaries | Git, GitHub Submission delivery, Agent CLI, tmux, CI, run execution, and Linear use narrow ports. Optional Linear project scope is supported. New transports should consume the same requests/outbox rather than add another Task or Session state machine. |
| Local response speed | Appropriate for the target scale | Indexed SQLite reads and local Git object checks avoid network calls in interactive commands. The 50-item inbox benchmark exists, but it is explicitly preliminary; no production p95/p99 claim is valid until the 200-sample, 30-minute end-to-end run is recorded. |
| CI response | Bounded and reproducible | Protected-base exact-head CI reads PR content only as Git objects and has a ten-minute timeout. A separate unprivileged, credential-free workflow intentionally executes the exact PR source with a fifteen-minute timeout. Large evidence bytes remain external; GitHub queue time must be reported separately. |
| Ease of use | Usable for technical researchers, not yet polished for a pilot | Human flags and strict Agent JSON call the same service, guided Task creation covers the compact PM fields, and the inbox is grouped by management question. Long canonical IDs, CODEOWNERS review, and branch-rule configuration are deliberate safety costs. Session notifications still need one deployed Linear adapter before `@app` works remotely. |
| Operational simplicity | Strong local design | The core needs Python, Git, SQLite, tmux, and repository-native Agent CLIs. It has no broker, web UI, inbound research-host listener, or general scheduler. The Linear adapter and future SSH controller can run as short-lived jobs. |
| Scale | Suitable for one researcher or a small lab project | One local SQLite writer and Git-native records are a good fit for tens of active Sessions and moderate Run history. They are not a distributed control database. A separate fenced controller is justified only for a genuinely shared GPU pool. |

The codebase is operationally simple but no longer small: it contains roughly
30,000 lines of Python implementation and 20,000 lines of tests. In particular,
`RuntimeStore`, `ApplicationService`, and the CI validators are already large
change surfaces. R1 should freeze new core mechanisms, record actual change
hotspots during the pilot, and then split only those internals behind the
existing public facade. Adding another service, database, or plugin framework
would make maintenance worse at the current scale.

The end-to-end workflow and implementation-status audit is maintained in
`WORKFLOW_COVERAGE.md`. It maps all 33 stable scenarios to 15 workflows and
keeps `verified_local`, `partial`, `deployment_pending`, and `designed`
separate. This prevents static prompt traceability from being presented as an
executed acceptance result.

## Goal Simulations

### Exact accepted research result

```text
Agent Session proposal
  -> immutable RunSpec/RunResult evidence
  -> deterministic Submission + proposed Report preview
  -> manager acceptance candidate
  -> protected-base dispatcher validates the exact head
  -> Submission path regenerates and compares Report/projection bytes
  -> current CODEOWNER approval + protected merge
  -> accepted Submission + Decision + Report YAML + Report Markdown in Git
  -> [deployment boundary] trusted post-merge trigger and outbox
  -> real Linear target preflight + marker observation/create
  -> durable delivery receipt
```

Expected failures are closed: a new PR commit invalidates the attestation and
approval; an Agent cannot accept; CI has no Linear credential; a target or
payload mismatch writes nothing; a Linear outage leaves accepted Git state
usable and the outbox retryable. The repository implements and tests the
post-merge delivery core plus a one-shot authenticated GitHub observation and
enqueue adapter; trusted scheduling, deployment credentials, and the real
Linear adapter remain R1 deployment work.

### Address one Session from Linear

```text
authenticated @app comment
  -> durable ingress receipt for workspace/issue/thread/comment/event
  -> fixed command parser
  -> canonical Task issue + Session/Task + exact branch/commit checks
  -> Session inbox revision
  -> Agent safe-checkpoint list -> ack or reply
  -> linear.session-reply.v1 outbox
  -> trusted app publishes to the original verified thread
  -> marker + delivery receipt preserve agent/session/task/report attribution
```

Retries converge by authenticated comment/event identity. A stopped or lost
Session routes to the manager exception inbox. A copied hidden marker, short
commit, wrong issue, cross-Session actor, or unreachable commit is rejected.
This design guarantees durable addressing, not immediate injection into a live
model transcript.

### Wrong, stale, or defaulted experiment intent

Before compilation, every decision-bearing ExperimentPlan field must match an
exact value in the digest-bound accepted Task or Project policy. Missing values
produce `needs_input`; provider/CLI/library defaults and Agent-authored source
labels cannot establish intent. A distinct ephemeral read-only reviewer binds
its opinion and invocation identity to the exact Plan, Task, policy, and review
policy. Self-review or any digest drift fails closed.

Before `run start` or `run retry` creates a Run ref, worktree, or process, it
resolves the exact local protected head, loads the canonical Task directly from
that commit, validates the Plan/Review again when present, requires the local
review operation receipt, and validates baseline/source lineage and write scope.
A Session-side Task edit cannot widen its allowlist. Typed environment, config,
dataset, checkpoint, source tree, and artifact declarations are then checked
before launch where possible. A rejected source leaves no Run ref, Run
worktree, Attempt event, or process. A mistake found only after launch remains a
failed or partial RunResult with attempt lineage; it does not become a normal
claim. A later retry uses a new Attempt identity under the same frozen RunSpec,
while changed experiment intent uses a new Run ID.

### Multiple Sessions editing the same code

Each Session has a unique branch, worktree, tmux identity, capability, and
native Agent session ID. Submission checks use the Task's write scope and always
deny `.research`. `lost` is terminal; uncertain or cross-host continuation uses
a new Session identity. This prevents hidden dual writers but does not claim
hostile-process isolation between processes running as the same OS account.

## Coverage Reality

The 77 source prompts are cataloged as 33 stable scenarios and every scenario
has a traceability row. That is requirements coverage, not proof that all 33
acceptance scenarios execute today.

The current scenario-level status is intentionally conservative:

| Status | Scenarios | Main qualification |
|---|---|---|
| Local behavior substantially implemented and tested | `US-002`-`US-003`, `US-008`, `US-014`-`US-016`, `US-023`, `US-025` | Supported local paths pass focused tests; write scope is mistake containment, not hostile same-user process isolation. |
| Partial local implementation | `US-001`, `US-011`-`US-013`, `US-017`-`US-022`, `US-026`-`US-028`, `US-031` | Missing pieces include formal inbox staleness/SLO verification, cross-domain staging, a live GitHub/Plan-reviewer pilot and protected-repository configuration, cleanup/retention, live resource Impact providers/replay, glossary tests, the fixed SSH remote runner/fleet, and upgrade apply. |
| Local core plus fake external ports; deployment pending | `US-006`, `US-009`, `US-010`, `US-030`, `US-033` | Authenticated ingress, accepted-result/reply delivery, receipts, replay, fallback, and same-thread follow-up execute with a fake Linear port; no real `@<app-handle>` deployment is claimed. |
| Not implemented | `US-004`, `US-005`, `US-007`, `US-024`, `US-029`, `US-032` | Mobile/chat views, SSH fleet and worktree sync, on-prem-to-cloud execution, and shared GPU control remain gated work. |

The exact-head workflow now uses a protected-base dispatcher for Submission,
generated Task control, Report Impact, explicit ImpactDecision, bootstrap
proposal/acceptance, manager-owned Plan reviewer policy, and manager-owned
Linear policy PRs. Ordinary source is
explicitly `not_applicable` to that dispatcher
and is tested by the separate exact-PR-source workflow; unknown or mixed
protected changes fail closed. Standalone post-bootstrap schema, Project,
Report, Decision, and unsupported policy mutations remain closed.
Submission preparation and exact-head Submission CI both enforce Task write
scope, and local Run start/retry validates its frozen source before creating Run
records or launching. A single-maintainer CODEOWNERS baseline is installed;
branch protection and reviewer policy still require repository configuration
and verification.

The assigned Agent's `researchctl submit` path now derives one fixed branch,
pushes only its exact generated commit, and creates or observes one exact
same-repository GitHub PR through bounded `git` and `gh api` adapters. Offline
tests cover remote-head conflicts, duplicates, closed or ambiguous PRs, secret
sanitization, and timeout recovery. No claim is made that a credential or live
repository rule has been installed.

## Final Delivery Plan

### Gate R0 - Close the repository build

Repository implementation is complete for the previously open R0 items:

1. Trusted Linear ingress, inbox/outbox, receipts, marker recovery, fallback,
   and same-thread reply/follow-up are covered through fake external ports.
2. Task write scope is wired into Submission preparation, exact-head Submission
   CI, and frozen-source Run start/retry; `.research` is always denied.
3. The protected-base multi-type dispatcher fails unknown and mixed protocol
   changes closed without executing PR code.
4. ExperimentPlan schema/provenance/lint, independent PlanReview, compilation,
   Run gating, Submission evidence, and CI replay form one local gated slice;
   reviewer selection is a manager-only field-scoped policy proposal.
5. Protocol fingerprints, the complete test suite, focused dispatcher/run/Linear
   suites, Python compilation, workflow syntax, and diff checks are the R0
   verification boundary.
6. The authoritative spec, assessment, rollout plan, mutation contract, and
   CI/Linear/Run ADRs describe the implemented boundary consistently.

A release operator must review a tracked baseline, rerun the verification
checklist from that exact commit, review the installed CODEOWNERS baseline,
configure branch rules, and then observe both workflows on the protected
repository. Checked-in files alone are not an operational protected-branch gate.

Exit: a fresh local repository can execute the complete local Agent-to-accepted
Report flow, and all external effects are either disabled or represented by a
tested port and durable outbox. This does not install GitHub protection or make
the fake Linear port a live integration.

### Gate R1 - One-repository shadow pilot

1. Verify the installed CODEOWNERS baseline, configure protected-branch rules,
   require both `researchctl/source-tests` and `researchctl/exact-head`, and do
   not use template placeholders as principals.
2. Deploy one short-lived trusted Linear poller/webhook adapter and publisher
   under the visible app identity. Keep its credential outside Git and Agent
   environments.
3. Run at least 20 representative local Runs, deliberately inject duplicate
   webhooks, API timeout, worker crash-after-create, stale PR head, lost Session,
   and Linear outage, then reconcile every receipt.
4. Exercise both configured Codex and Claude Plan reviewer adapters with
   explicit model IDs, including timeout, malformed output, and identity
   separation failures; record invocation receipts without exposing secrets.
5. Record end-to-end inbox and CI latency separately from queue/execution time.

Exit: no duplicate projection, no lost Session notification, no Agent authority
escalation, and observed latency meets the declared supported envelope.

### Gate R2 - SSH fleet

Add a typed SSH transport, fixed remote runner, host profiles, exact source/input
preflight, tmux observation, artifact digest verification, and bounded fleet
status. Research hosts need no inbound listener. An ambiguous SSH result is
observed before retry and never blindly launches a duplicate.

Exit: one on-prem-created frozen Run executes on one cloud host with verified
inputs and returned artifact digests, including outage and partial-transfer
recovery tests.

### Gate R3 - Change impact and worktree sync

The code-path proposal loop is implemented: exact/trailing-recursive
dependencies, optimistic Report/main checks, immutable evidence identity,
merge-triggered all-Report batching, deterministic clean-runner replay, fixed
GitHub delivery, protected-base batch regeneration, effective applicability
reads, and explicit rerun/waive/keep-stale/invalidate/dependency-fix Decision
PRs. Trusted live resource providers, protected replay, and preview-first
Session baseline sync remain. Live, dirty, lost, or unknown Sessions are never
batch-mutated.

Exit: stale optimistic revisions fail, no-overlap never auto-proves validity,
and one conflict does not block or corrupt other Session updates.

### Gate R4 - Shared GPU controller only when required

Keep manual/static GPU assignment until two or more hosts genuinely contend for
the same pool. Then build the separately bounded controller with a frozen API,
single writer, database uniqueness, host locks, fencing generations, startup
acknowledgement, quarantine, reconcile-before-allocate, backup, and restore.

Exit: 50 concurrent requests cannot double-allocate a GPU; lease expiry or
network partition never frees hardware without positive process/lock evidence.

Mobile and additional chat surfaces remain projections after R1. They may read
the same inbox and outbox but cannot introduce a second authority or be required
for local research work.
