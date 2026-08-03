# User Scenario Catalog

Status: implementation input
Updated: 2026-08-03
Source: private historical design research (intentionally not published)

This catalog turns all 77 historical user prompts into 33 independently
testable scenarios. Public `P-NNN` anchors preserve complete requirement
coverage without publishing the private conversation, its metadata, or its
content. Several prompts appear in more than one scenario when one real
incident exposed more than one system boundary.

RCP is an agent harness and research control plane: it constrains and observes
agent work, sessions, evidence, review, and resources. It is not an agent model,
an experiment framework, or a replacement for Git, tmux, SSH, or GitHub.

## Simplicity and interface rule

There is one shared application service API. Human-readable CLI output and
agent-facing `--json` output are two thin presentations of the same command and
result; they must not contain separate business rules. Agents may invoke the
same CLI with `--json`, and automation may call the same in-process service
API. A web service, message broker, custom UI, or second project database is not
required by the core harness.

`Core` means behavior owned by the harness. `Extension` means a replaceable
transport, projection, or presentation adapter over the same service API; it
does not mean the user need is discarded. Phase 9 is reserved for post-MVP
Integrations. Mobile and general Slack/chat milestone views remain Phase 9.
The later 2026-08-03 requirement promoted addressed Session notification into
the current harness: its inbox/outbox is Core, while Linear ingress and publish
remain a replaceable Phase 8 transport.

The test ID `AT-US-NNN` is stable. Implementations may add narrower test cases,
but this ID remains the acceptance-suite and reporting key.

## Scenario catalog

### US-001 - Exception inbox answers management questions quickly

- Sources: historical P-001, P-002.
- Real expectation: without reopening transcripts, the manager can see what is
  active, what deviated or is blocked, and what needs a decision or review.
- Scope / earliest phase / test: Core / Phase 2 / `AT-US-001`.
- Acceptance: with 50 mixed tasks and sessions, `researchctl inbox` returns the
  three answers in explicit groups, marks stale observations, and meets local
  p95 <= 500 ms and p99 <= 1 s on the supported benchmark host.

### US-002 - The human owns plan and declared task state

- Sources: historical P-001, P-003, P-007, P-012, P-032.
- Real expectation: agents report observations and proposals, while only the
  manager changes goals, priority, declared task state, or accepted plan.
- Scope / earliest phase / test: Core / Phase 2 / `AT-US-002`.
- Acceptance: an agent-authenticated plan or Task-state mutation fails closed
  and is audited; the equivalent manager operation succeeds, and agent status
  events cannot implicitly mark a Task done.

### US-003 - A novice can create a well-formed task

- Sources: historical P-004, P-005, P-008.
- Real expectation: a researcher with no PM experience gets a small, guided
  Task template rather than having to design a Jira/Notion process first.
- Scope / earliest phase / test: Core / Phase 2 / `AT-US-003`.
- Acceptance: the guided and non-interactive paths produce the same Task model
  with goal, done conditions, constraints, typed inputs, owner/team choice, and
  next human decision; validation identifies a missing field and how to fix it.

### US-004 - Mobile status is grouped by project and task

- Sources: historical P-008, P-018, P-019, P-023.
- Real expectation: away from the workstation, the manager can scan grouped
  work and exceptions instead of navigating an ungrouped list of chats.
- Scope / earliest phase / test: Extension / Phase 9 Integrations /
  `AT-US-004`.
- Acceptance: the mobile adapter renders the same inbox groups and stable
  project/Task/Session identities as the core query, supports drill-through to
  evidence, and cannot mutate accepted truth.

### US-005 - Chat receives milestones, not execution noise

- Sources: historical P-009, P-010, P-011, P-012, P-013, P-017, P-018,
  P-020, P-023.
- Real expectation: Slack or another chat client is a convenient notification
  surface for blocked, decision, review, and completion events, not the board or
  canonical report store.
- Scope / earliest phase / test: Extension / Phase 9 Integrations /
  `AT-US-005`.
- Acceptance: repeated progress events are coalesced, each material event is
  projected once by stable event ID to its configured destination, and deleting
  or editing the chat message does not change RCP state.

### US-006 - Linear can address one governed Session and receive its reply

- Sources: historical P-014, P-016, P-020, P-033.
- Real expectation: messages in one configured thread reach exactly the
  intended existing Claude/Codex session, while reports from that session return
  to the same thread.
- Scope / earliest phase / test: Extension / Phase 8 / `AT-US-006`.
- Acceptance: an authenticated fixed-format
  `notify session:<full-id> commit:<full-sha>` command records a verified
  workspace/issue/thread/comment receipt and reaches only the bound Session
  inbox. A contextual `reply commit:<full-sha>` derives its Session from a
  trusted prior delivery receipt,
  never pasted text. Restart and retry preserve one notification and one reply;
  active Sessions list/ack/reply at safe checkpoints, a lost/stopped Session
  falls back visibly to the manager, and messages never cross into another
  Session. One shared app identity retains agent/Task/Session/report attribution
  without per-Session email or Linear accounts.

### US-007 - SSH-only hosts need no inbound RCP listener

- Sources: historical P-015, P-031, P-065, P-073.
- Real expectation: an on-prem research host reachable only by SSH can join the
  workflow without exposing a web console, opening a port, or installing a root daemon.
- Scope / earliest phase / test: Core / Phase 5 / `AT-US-007`.
- Acceptance: a host profile passes `doctor`, starts and observes tmux work over
  outbound/controller-initiated SSH, and completes onboarding with no inbound
  RCP socket, Slurm service, or privileged installation.

### US-008 - Agent capabilities are fixed and narrow

- Sources: historical P-012, P-022, P-024, P-032, P-035, P-036.
- Real expectation: a session receives only explicit report/status/submission
  capabilities; channel privacy, prompt instructions, tags, or project naming
  are not treated as authorization.
- Scope / earliest phase / test: Core / Phase 2 / `AT-US-008`.
- Acceptance: the agent identity can publish allowed status and proposal types
  but cannot change Plan/Task authority, create a Decision, reach raw Linear
  mutation APIs, or broaden its capability set; every denial names the violated
  capability without disclosing credentials.

### US-009 - Linear credentials belong to one adapter boundary

- Sources: historical P-006, P-010, P-025, P-026, P-027, P-028, P-034,
  P-035.
- Real expectation: the system has a comprehensible service credential design,
  with secrets outside Git and without handing a personal full-access key to
  every agent session.
- Scope / earliest phase / test: Extension / Phase 8 / `AT-US-009`.
- Acceptance: only the projection worker can load the configured Linear
  credential, agents and untrusted CI cannot access it, logs and JSON redact it,
  and rotating it requires no canonical record rewrite or per-session account.

### US-010 - Sessions are attributable without one external user each

- Sources: historical P-026, P-027, P-028, P-036.
- Real expectation: one service identity may perform projections, but every
  action remains attributable to the originating project, Task, Session, actor,
  and operation without creating an email/app user per session.
- Scope / earliest phase / test: Core / Phase 2 / `AT-US-010`.
- Acceptance: two sessions using one adapter identity produce distinguishable,
  queryable audit events and projected attribution; spoofing another Session ID
  is rejected and no per-session external account is required.

### US-011 - Team and HostPool express on-prem and cloud placement

- Sources: historical P-032.
- Real expectation: Tasks can be grouped for people as on-prem or cloud and select
  an allowed host pool without conflating that label with a host, Session, or
  agent identity.
- Scope / earliest phase / test: Core / Phase 2 / `AT-US-011`.
- Acceptance: a manager assigns a Task execution domain and accepted Project
  policy maps that domain to allowed HostPools. Session creation resolves only
  eligible hosts, the inbox may render the domain/pools as a Team, and an agent
  cannot change placement policy; no independent Team authority is created.

### US-012 - ExecutionDomain makes data visibility explicit

- Sources: historical P-032, P-061, P-066, P-070.
- Real expectation: code portability must not imply that datasets, checkpoints,
  environments, and artifacts are visible in another execution domain.
- Scope / earliest phase / test: Core / Phase 3 / `AT-US-012`.
- Acceptance: preflight rejects an unresolved logical input in the selected
  ExecutionDomain; a cross-domain run stages or resolves immutable references,
  verifies destination digests, and never accepts an unqualified source-host
  path as portable evidence.

### US-013 - Locate, pause, attach, and resume preserve one writer

- Sources: historical P-014, P-016, P-031, P-032, P-033.
- Real expectation: the manager can find and enter the real native tmux/agent
  session for inspection or intervention, then resume it without forking hidden
  workers or losing context.
- Scope / earliest phase / test: Core / Phase 2 / `AT-US-013`.
- Acceptance: status resolves host, tmux identity, native session ID, and last
  positive observation; attach reaches the existing process and pause/resume is
  idempotent only on the owning host. Concurrent attach is read-only. Uncertain
  ownership, lost state, or cross-host continuation creates a new Session ID,
  branch, and worktree while the old Session remains terminal.

### US-014 - Use mature tools and enforce a complexity budget

- Sources: historical P-005, P-021, P-029, P-030, P-031, P-036, P-037,
  P-050, P-051, P-052, P-060, P-061, P-062, P-063, P-064.
- Real expectation: the harness should compose Git, GitHub, SSH, tmux, SQLite,
  and repository-native launchers, with minimal new machinery and one obvious
  operational path.
- Scope / earliest phase / test: Core / Phase 1 / `AT-US-014`.
- Acceptance: core operation requires no web service, broker, DVC, Dagster, jj,
  Slurm, or custom experiment framework; human CLI and agent `--json` call the
  same application service and return equivalent state, errors, and operation
  IDs in contract tests.

### US-015 - Runs never pollute old or orphan Markdown

- Sources: historical P-039.
- Real expectation: an agent cannot append results to an obsolete document or
  create a new isolated Markdown report merely because an experiment ended.
- Scope / earliest phase / test: Core / Phase 3 / `AT-US-015`.
- Acceptance: snapshots of all pre-existing Markdown remain byte-identical
  through init, run, failure, and submission proposal; only managed structured
  paths may be created, and canonical Markdown is renderer-owned after review.

### US-016 - Wrong-input executions are retained but are not claims

- Sources: historical P-039.
- Real expectation: a mistaken experiment remains diagnosable evidence without
  becoming a successful result, report, or basis for a scientific claim.
- Scope / earliest phase / test: Core / Phase 3 / `AT-US-016`.
- Acceptance: typed mismatch fails before launch when detectable; otherwise the
  terminal RunResult retains attempt lineage, logs, and `failure_classification`,
  is excluded from candidate results, and cannot enter a normal Submission
  unless an explicit failure-study policy permits it.

### US-017 - One canonical Submission supports multiple renderers

- Sources: historical P-038, P-040, P-047, P-071.
- Real expectation: Outcome, Evidence, Metrics, Diff/limitations, and requested
  decision are one typed Agent Commit/ResearchSubmission, not independently
  edited local, GitHub, and Linear documents.
- Scope / earliest phase / test: Core / Phase 4 / `AT-US-017`.
- Acceptance: one canonical fixture deterministically renders YAML, CLI text,
  Markdown review, GitHub content, and projection payloads with the same digest
  and semantics; trusted CI regenerates every declared output and compares it
  byte for byte. Wrong paths, orphaned free-form reports, manual generated-file
  edits, and schema or renderer-version drift fail the named check; changing a
  renderer cannot mutate the source record.

### US-018 - Review works in GitHub, VS Code, and CLI

- Sources: historical P-037, P-038, P-041, P-042, P-043, P-044, P-045,
  P-046.
- Real expectation: the manager can inspect code diff and research evidence in
  familiar tools and explicitly accept, request changes, reject, or abandon,
  without adopting a new VCS or bespoke UI.
- Scope / earliest phase / test: Extension / Phase 4 / `AT-US-018`.
- Acceptance: the same proposal and digest are visible as a standard GitHub PR,
  standard Git/VS Code diff, and CLI summary; only the authenticated manager
  acceptance path materializes Decision and Report.

### US-019 - One repository owns truth and retention

- Sources: historical P-039, P-040, P-074.
- Real expectation: plan, accepted reports, evidence metadata, and lifecycle
  rules live with the code repository; large bytes may remain in declared,
  digest-addressed external storage.
- Scope / earliest phase / test: Core / Phase 4 / `AT-US-019`.
- Acceptance: a fresh clone validates all accepted records and artifact refs
  without Linear or local SQLite; cleanup removes a run ref only after accepted
  reachability or an explicit rejection/retention disposition.

### US-020 - Impact decisions offer rerun, waive, or stale

- Sources: historical P-048, P-049, P-054, P-070.
- Real expectation: when code or a declared dependency changes, the manager sees
  which reports may be affected and chooses to rerun, explicitly waive, or mark
  stale instead of silently changing a dependency commit.
- Scope / earliest phase / test: Core / Phase 6 / `AT-US-020`.
- Acceptance: a relevant baseline change creates an `impact_pending` proposal
  with those three decisions; no-overlap never auto-validates; an outdated
  expected Report revision or main tree is rejected by optimistic concurrency.

### US-021 - Trusted CI validates; SSH runners experiment

- Sources: historical P-057, P-063, P-065, P-068.
- Real expectation: GitHub Actions runs deterministic, secretless policy checks,
  while long or GPU experiments run explicitly through SSH/tmux rather than
  untrusted PR workflows.
- Scope / earliest phase / test: Core / Phase 4 / `AT-US-021`.
- Acceptance: PR CI cannot access SSH/cloud/Linear credentials or invoke remote
  experiments, uses the protected validator, and gates merge. It emits a
  machine-readable attestation bound to the exact PR head/tree, validator,
  schema manifest and generated-output digests; any new commit invalidates it,
  and an Agent status cannot satisfy it. A separately authorized runner can
  consume the already-frozen RunSpec.

### US-022 - Onboarding and help teach the object model

- Sources: historical P-004, P-051, P-061, P-068, P-075.
- Real expectation: a researcher should understand Task, Session, Run,
  Submission, Report, CI, projection, and next actions without prior PM/Ops
  vocabulary or a separate product deployment.
- Scope / earliest phase / test: Core / Phase 1 / `AT-US-022`.
- Acceptance: `init`, `doctor`, command help, and the glossary use the same terms,
  expand an acronym at first use, show the next safe command, and pass novice
  task-oriented documentation checks with no mandatory external service.

### US-023 - `allowed_write_paths` bounds an agent worktree

- Sources: historical P-024, P-053, P-067.
- Real expectation: a session is launched in its own worktree and its declared
  write scope is mechanically checked, while documentation is honest that this
  is mistake containment rather than same-user hostile-process isolation.
- Scope / earliest phase / test: Core / Phase 2 / `AT-US-023`.
- Acceptance: allowed files can be proposed; out-of-scope changes, traversal,
  symlink escape, rename escape, protected paths, and wrong worktree root fail
  before submission, and the failure reports the normalized path policy.

### US-024 - Worktree baseline sync is explicit and batch-safe

- Sources: historical P-054, P-055, P-058.
- Real expectation: a bug fix can be offered to many session worktrees without
  blindly merging into agents that are running, dirty, or at an unknown point.
- Scope / earliest phase / test: Core / Phase 6 / `AT-US-024`.
- Acceptance: batch sync previews each target and expected baseline; stopped or
  explicitly consenting sessions update independently; live/dirty/unknown
  sessions become `update_pending`; one conflict cannot corrupt or block all
  other targets.

### US-025 - Delegation receives a complete, traceable plan

- Sources: historical P-056, P-057, P-064, P-072, P-077.
- Real expectation: a coding agent receives the whole architecture, the real
  workflows, complexity and performance constraints, gates, and testable user
  cases rather than an attractive but incomplete component list.
- Scope / earliest phase / test: Core / Phase 0 / `AT-US-025`.
- Acceptance: a static traceability check finds exactly `US-001` through
  `US-033`, one stable acceptance test ID and earliest phase for each, coverage
  of every historical prompt anchor, and no scenario absent from the matrix.

### US-026 - Every isolated change gets an explicit disposition

- Sources: historical P-045, P-046, P-058, P-059.
- Real expectation: session code may be merged, abandoned, or retained in
  isolation; cleanup must not accidentally merge it or erase evidence.
- Scope / earliest phase / test: Core / Phase 4 / `AT-US-026`.
- Acceptance: manager actions record `merge`, `abandon`, or `retain_isolated`;
  branch/worktree cleanup requires that disposition and run-record reachability;
  an abandoned code change can still support a snapshot-scoped accepted result.

### US-027 - Local validation and status stay responsive

- Sources: historical P-060, P-063, P-064.
- Real expectation: Python implementation does not imply a slow interactive
  loop; cheap deterministic checks run locally and remote/CI waits are reported
  separately.
- Scope / earliest phase / test: Core / Phase 1 / `AT-US-027`.
- Acceptance: repeatable supported-envelope benchmarks enforce the spec's local
  status/inbox, validation, fleet deadline, run acknowledgement, and CI
  objectives, and report queue time separately from execution time.

### US-028 - The execution substrate is SSH plus tmux, not Slurm

- Sources: historical P-014, P-065, P-073.
- Real expectation: existing interactive SSH/tmux habits remain usable; RCP does
  not require a scheduler or always-on agent daemon to manage a session.
- Scope / earliest phase / test: Core / Phase 2 / `AT-US-028`.
- Acceptance: local sessions use deterministic tmux identities and Phase 5 adds
  the same lifecycle over SSH; restart observes or attaches to the existing
  process, and the default installation contains no Slurm or inbound-daemon
  dependency.

### US-029 - On-prem-to-cloud execution preserves exact inputs and artifacts

- Sources: historical P-066.
- Real expectation: code prepared on-prem runs as the exact approved tree on a
  cloud host with its environment, dataset/checkpoint identity, and result artifacts
  verified rather than inferred from matching paths.
- Scope / earliest phase / test: Core / Phase 5 / `AT-US-029`.
- Acceptance: an immutable on-prem-created RunSpec executes the recorded commit
  and tree on a cloud profile; destination preflight checks protocol, environment,
  inputs, executable, disk, and digests; artifact collection verifies digests;
  an ambiguous SSH response never launches a duplicate.

### US-030 - One accepted merge creates exactly one Linear comment

- Sources: historical P-038, P-069.
- Real expectation: Linear is updated automatically only after accepted truth is
  merged, with no duplicate comments from webhook retry or worker restart.
- Scope / earliest phase / test: Extension / Phase 8 / `AT-US-030`.
- Acceptance: no draft, rejected, or merely CI-passing proposal emits the
  accepted comment. One accepted merge creates one stable event whose
  manager-owned issue UUID and workspace/team/project scope are remotely
  preflighted; its deterministic renderer and payload digest match the CI
  preview and delivery receipt. A target or digest mismatch writes nothing and
  enters the inbox; repeated, reordered, outage-delayed delivery or a worker
  crash after API success has exactly one visible effect. Agents have no Linear
  credential and cannot choose the issue, template, or free-form comment body.

### US-031 - Existing repositories opt in safely

- Sources: historical P-040, P-075.
- Real expectation: adoption feels like `git init`: initialize in place, inspect
  an isolated bootstrap inventory, explicitly accept it, and migrate protocol
  versions without rewriting the existing project.
- Scope / earliest phase / test: Core / Phase 1 / `AT-US-031`.
- Acceptance: init on a dirty realistic repository changes no existing file,
  commit, branch, or remote and a second init has no diff; imported history is
  unverified until manager bootstrap acceptance; future versions fail closed
  and migration requires explicit preview and application.

### US-032 - The global GPU queue fails safely

- Sources: historical P-076.
- Real expectation: finite GPUs across hosts are queued and visible globally,
  with no double allocation, premature reuse, or dependence on Linear as the
  allocator database.
- Scope / earliest phase / test: Core / Phase 7 / `AT-US-032`.
- Acceptance: 50 concurrent requests cannot allocate one GPU twice; heartbeat
  loss quarantines rather than frees; external use is surfaced; restart blocks
  allocations until reconcile; idempotent retry returns the original outcome.

### US-033 - Linear is a disposable one-way projection

- Sources: historical P-006, P-007, P-024, P-039, P-040, P-069, P-074.
- Real expectation: Linear may make Tasks and results easier to scan, but it is
  neither required for local reports nor allowed to become a second source of
  plan, research, run, or resource truth.
- Scope / earliest phase / test: Extension / Phase 8 / `AT-US-033`.
- Acceptance: with Linear unavailable or its projected objects deleted, all core
  workflows continue and replay reconstructs the projection from Git/controller
  events. The only accepted inbound action is a non-authoritative, explicitly
  addressed Session notification under US-006; Linear edits never start a Task,
  accept results, reprioritize, change policy, or allocate resources.

## Integration boundary

Phase 8 implements the narrow Linear transport for `US-006`, `US-009`,
`US-030`, and `US-033` only after the shared service API, stable event IDs,
Session scope, and outbox replay exist. Phase 9 may add `US-004` and `US-005`.
Every integration consumes core queries and commands; none may introduce a new
Task state machine, acceptance path, Session owner, or canonical store.
