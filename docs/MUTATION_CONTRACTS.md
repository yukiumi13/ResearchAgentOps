# Research Control Plane Mutation Contracts

Status: Normative Phase 0 addendum
Scope: v0.1 command and application-service mutations
Updated: 2026-08-03

This document makes the mutation boundary in
`RESEARCH_CONTROL_PLANE_SPEC.md` executable. Where earlier text is ambiguous,
the narrower rules here apply to v0.1. The deliberately simple choices are:

- one in-process application service owns validation, authorization,
  idempotency, journaling, and domain transitions;
- the human CLI and agent `--json` mode are adapters to the same service methods;
- no second agent business API and no general HTTP control-plane API are needed
  before a real remote boundary requires one;
- a lost Session is terminal, and cross-host continuation creates a new Session;
- mutations are retryable by observation, not by assuming that a timeout failed.

## 1. One service, two presentations

Each operation is a typed method on `ApplicationService`. Human-oriented flags
and prompts are parsed into the same request type accepted by machine mode:

```text
human:  researchctl task create --title ...
agent:  researchctl task create --json < request.json
                         |              |
                         +---- ApplicationService.task_create(request, actor)
```

`--json` means one JSON request on standard input and one stable JSON envelope on
standard output. It does not select different behavior or grant agent authority.
A Python caller may invoke the same typed service directly. Domain code does not
import Typer, tmux, SSH, GitHub, Linear, or an HTTP framework; those are ports.
The Resource Controller may later expose its own narrow remote API, backed by the
same allocation service, because allocation has a genuine process boundary.

The authenticated `ActorContext` is supplied by the invoking credential or
session binding, never trusted from request fields. The supported actors are
manager, session-scoped agent, runner, trusted automation, controller, and
projection worker. Human-readable and JSON callers receive the same error code,
terminal state, warnings, and operation ID.

## 2. Common mutation rules

Except for `init`, every mutating service request carries an `operation_id` and
stable `idempotency_key`. JSON clients must supply a key that survives their own
retry; the human CLI may generate one, persist it, and display the operation ID
before the first external side effect. An idempotency record binds:

```text
(project_id, command, idempotency_key) -> request_digest, operation_id, result
```

Repeating the key with the same canonical request observes or continues the
operation. Reusing it with a different request fails `idempotency_conflict`.
The first durable journal event precedes Git pushes, PR calls, SSH, tmux,
processes, artifact copies, controller calls, and projection calls. A timeout is
`unknown` until the adapter observes the external identity. Terminal operation
results are absorbing; a genuinely new intent uses a new key.

`init` is the exception because the journal does not exist yet. It uses atomic
repo-local file replacement and desired-content digests. Read-only commands,
including `status`, `inbox`, `session list`, `session show`,
`session address`, dry-run reconciliation, and `session attach`, do not create
operation records.

In the tables, "authority" names the durable owner of the domain fact. A local
journal is operational evidence, not a second authority.

## 3. Repository and manager intent

| Command | Actor and authority | First durable write | Idempotency key | Observe and recover | Terminal result |
|---|---|---|---|---|---|
| `researchctl init` | Repository owner; managed protocol files in the working tree | First missing managed file, written atomically after full conflict validation | Canonical root identity + desired manifest digest | Re-read every managed path and digest; rerun writes only missing identical files | `initialized`, `no_change`, or `conflict` |
| `researchctl bootstrap` | Agent or manager proposes; bootstrap branch/PR owns only proposal state | `OperationStarted`, then bootstrap branch/ref and proposal record | Bootstrap ID + source tree + requested scanner/policy digest | Observe journal, branch commit, worktree, push, and PR by stable IDs; continue the first incomplete step | `proposal_open`, `failed`, or `canceled`; never `managed` |
| `researchctl bootstrap accept BOOTSTRAP_ID` | Manager prepares; protected default branch owns accepted Project state after merge | `OperationStarted`, then acceptance-preparation commit on the expected proposal head | Bootstrap ID + expected PR head + classification digest | Re-read PR head, generated transition, CI, approval, and merge; a changed head is `stale`, not overwritten | `acceptance_prepared`, `managed` after certified merge, `stale`, or `rejected` |
| `researchctl task create` | Manager; Task record on protected default branch | `OperationStarted`, then control-branch commit and PR | Task ID + canonical create-request digest | Observe Task ID, branch, commit, and PR; duplicate key with different Task ID is a conflict | `proposal_open`, `accepted` after merge, or `canceled` |
| `researchctl task update TASK_ID` | Manager; Task record on protected default branch | `OperationStarted`, then control-branch commit based on expected Task digest | Task ID + expected revision/digest + patch digest | Compare current Task digest before continuing; regenerate on stale base, never silently rebase intent | `proposal_open`, `accepted` after merge, `stale`, or `canceled` |
| `researchctl task cancel TASK_ID` | Manager; Task state on protected default branch | `OperationStarted`, then control-branch cancellation commit | Task ID + expected revision + reason digest | Observe accepted Task and proposal PR; cancellation does not kill Sessions or Runs implicitly | `proposal_open`, `canceled` after merge, or `stale` |

Task mutations are manager control proposals. An agent may suggest a Task through
a StatusUpdate decision request, but cannot call a manager method merely by
using JSON mode.

## 4. Sessions and attention

| Command | Actor and authority | First durable write | Idempotency key | Observe and recover | Terminal result |
|---|---|---|---|---|---|
| `researchctl session start TASK_ID` | Manager or assigned agent; Session branch owns code, local journal owns observations | `OperationStarted` before branch, worktree, tmux, or agent creation | Session ID + Task ID + base commit + host + adapter | Observe branch/worktree, deterministic tmux name, process, and adapter session ID in order; continue missing steps only | `active`, `stopped`, `lost`, `failed`, or `canceled` |
| `researchctl session pause SESSION_ID` | Assigned agent or manager; local Session observation | `OperationStarted` before sending a stop/pause signal | Session ID + observed process identity + requested pause mode | Observe process/tmux before and after signal; ambiguous host makes Session `lost`, not assumed paused | `idle` or `stopped`, plus `lost`, `failed`, or `canceled` |
| `researchctl session attach SESSION_ID` | Manager or assigned agent; read-only | None | None | Resolve the owning host and exact tmux/adapter identity; report stale or unreachable state without rewriting it | `attached`, `not_active`, `unreachable`, or `not_found` |
| `researchctl session list` | Manager sees Project Sessions; Agent sees only its capability-bound Session; read-only | None | None | Query shared runtime state with optional Task/state filters and a bounded limit | Ordered visible Session summaries |
| `researchctl session show SESSION_ID` | Manager may read a visible Project Session; Agent only its bound Session; read-only | None | None | Resolve the exact canonical Session ID; cross-Session Agent access fails closed | One stable Session addressing record or `session_not_found` |
| `researchctl session address SESSION_ID --commit FULL_SHA` | Manager may address a visible Project Session; Agent only its bound Session; read-only | None | None | Verify a full object ID reachable from the Session's exact recorded branch; emit the configured `@<app-handle> notify ...` header and `message_required=true` without a Linear call | Verified header or a specific Session/branch/commit error |
| `researchctl session continue SESSION_ID --new-session` | Manager or assigned agent; a new Session branch owns continued code | `OperationStarted`, then a new Session ID/ref before worktree or process creation | Old Session ID + new Session ID + observed source commit + target host | Verify old branch and source commit, then use normal `session start` observation for the new identity | New Session `active`, `stopped`, `lost`, `failed`, or `canceled`; old Session is unchanged |
| `researchctl status publish` | Assigned agent or runner; local StatusUpdate outbox | StatusUpdate and outbox row in one local transaction | Stable update ID | Read by update ID; replay projection independently; a projection outage does not undo publication | `persisted` |
| `researchctl inbox ack UPDATE_ID` | Manager; local inbox acknowledgement only | Ack row keyed by manager/project/update | Manager identity + update ID + ack kind | Re-read ack row; newer StatusUpdates remain visible and Task state is unchanged | `acknowledged` or `no_change` |
| trusted Linear notification ingress | Dedicated configured-app automation identity; verified ingress receipt and Session inbox are local runtime facts | Authenticated workspace/issue/thread/comment receipt, then journaled notification creation | Workspace UUID + comment UUID; operation and notification IDs are derived from authenticated event content | Re-read the immutable ingress receipt and notification; a duplicate event returns the same result, while changed content for one comment is a conflict | `routed_to_session`, `routed_to_manager_exception`, or rejected before commit lookup |
| `researchctl notification list` | Bound Agent Session or manager; read-only view of the same durable inbox | None | None | Agent scope is fixed to its authenticated Session; manager may filter the fallback inbox | Current ordered notifications with route, state, and revision |
| `researchctl notification ack NOTIFICATION_ID` | Bound Agent Session, or manager for a fallback item; local receipt observation only | Journal operation, then optimistic notification revision update | Operation ID + notification ID + expected revision | Same operation replays; a changed revision is `stale_notification` and a replied item is closed | `acknowledged` or `already_acknowledged` |
| `researchctl notification reply NOTIFICATION_ID` | Bound Agent Session, or manager for a fallback item; local reply and Linear outbox | Reply, closed notification revision, and `linear.session-reply.v1` outbox in one SQLite transaction | Reply ID + notification ID + expected revision + canonical body digest | Re-read reply/outbox; trusted worker observes the stable marker before create and writes a delivery receipt after observation | `reply_queued`; delivery later becomes `delivered`, `retryable`, or `dead_letter` |

A Session has one host and one writer. `lost` is an absorbing state. v0.1 has no
Session takeover generation and no distributed takeover CAS. Same-host resume is
allowed only while the original Session is positively observed as `idle` or
`stopped`. A lost Session, any cross-host recovery, or an uncertain owner uses
`continue --new-session`, a new Session ID, and a new branch/worktree. The new
record links `continued_from`; it never changes the old record back to active.

Linear does not inject text into a live model transcript. The Session prompt
requires polling at safe checkpoints and before its final response. Durable
persistence proves addressability; only `notification ack` or
`notification reply` proves that the Session application consumed that
revision. When a Session becomes `stopped` or `lost`, every open notification
is transactionally rerouted to the manager exception inbox and its revision is
incremented, so an Agent cannot act on a stale route.

## 5. Runs, evidence, and review

| Command | Actor and authority | First durable write | Idempotency key | Observe and recover | Terminal result |
|---|---|---|---|---|---|
| `researchctl run start` | Assigned agent, manager, or runner; immutable Git RunSpec, local RunAttempt journal, controller allocation | `OperationStarted` before snapshot/ref, preflight, allocation, SSH, tmux, or process mutation | Run ID + canonical RunSpec digest | Validate frozen source write scope, then observe every named ref, target preflight, allocation, lock, tmux/process, attempt sequence, and artifact; continue only the first incomplete step | Attempt `succeeded`, `failed`, `canceled`, or `lost`; successful attempts may auto-collect |
| `researchctl run retry RUN_ID` | Assigned agent or manager; new RunAttempt under the same frozen RunSpec | New `OperationStarted` and Attempt ID linked by `retry_of` | Run ID + new Attempt ID + prior terminal Attempt ID | Revalidate frozen source scope; require prior Attempt terminal and no final RunResult; observe allocation/process by new identities | New Attempt `succeeded`, `failed`, `canceled`, or `lost` |
| `researchctl run collect RUN_ID` | Deterministic collector, runner, or manager; RunResult on the run branch | `OperationStarted` before remote reads or artifact transfer | Run ID + RunSpec digest + selected terminal Attempt IDs | Observe process terminal facts, artifact source and destination digests, then existing RunResult digest before write/push | `collected`, `already_collected`, `incomplete`, or `collection_failed` |
| `researchctl submit RUN_ID...` | Assigned agent or manager proposes; submission branch/PR owns proposal | `OperationStarted`, then submission branch commit before PR creation | Submission ID + base SHA + ordered RunResult digests + claim digest | Observe branch, deterministic bundle, push, and PR; size/digest failures occur before push | `proposal_open`, `withdrawn`, `failed`, or `canceled`; never `accepted` |
| `researchctl review accept SUBMISSION_ID` | Manager prepares; protected default branch owns Decision and Report only after merge | `OperationStarted`, then Decision and Report commit on expected PR head/revision | Submission ID + expected PR head + expected Report revision + decision digest | Re-read head, content digests, checks, exact-head approval, and merge; any head/revision drift is `stale` | `acceptance_prepared`, `accepted` after certified merge, `stale`, or `rejected` |
| `researchctl impact REPORT_ID` | Trusted automation or manager proposes; impact branch/PR owns proposal | `OperationStarted`, then impact record/branch based on expected Report revision | Report ID + expected revision + basis tree + target main tree | Recompute declared dependency comparison; re-read latest main and Report revision before push/merge | `proposal_open`, `no_change`, `stale`, `failed`, or `canceled` |
| `researchctl sync SESSION_ID --baseline COMMIT` | Assigned session owner or manager; Session branch/worktree | `OperationStarted` before fetch or merge/rebase operation | Session ID + expected Session head + target baseline commit + strategy | Observe ref and worktree first; dirty, live-unsafe, or unknown state returns `update_pending` without mutation | `synced`, `no_change`, `update_pending`, `conflict`, `failed`, or `canceled` |

For every RunAttempt state, both explicit cancel and operational failure have an
exit. The complete v0.1 rule is:

```text
preparing | snapshotted | preflighted | allocated | launching | running |
collecting -> failed | canceled

launching | running | collecting -> lost
```

`succeeded`, `failed`, `canceled`, and `lost` are terminal Attempt states. A
retry always creates a new Attempt. Terminal Attempt events remain evidence.
`collect` creates the single RunResult only after the caller chooses to finalize
the Run; until then a failed/lost Attempt may be retried. Once RunResult exists,
another experiment requires a new Run ID.

Impact never treats non-overlap as proof. It can propose `current`, `stale`,
`snapshot_only`, or `needs_rerun`, but only a protected manager control merge
changes an accepted Report. `sync` never updates a dirty or ambiguously live
worktree automatically.

## 6. Certified manager acceptance

`bootstrap accept` and `review accept` prepare acceptance; the command caller
and a YAML `reviewer_actor` field are not sufficient authentication. The command
produces candidate PR head `H`, including the complete transition or the
evidence, Submission, Decision, and Report revision. Acceptance is certified
only when all of the following refer to that exact `H`:

1. the expected head and expected record revision still match;
2. trusted required checks validate `H` from the protected base;
3. a current CODEOWNER approval by an authorized manager covers `H`;
4. branch protection/rulesets prevent agent bypass and accept the merge; and
5. the protected default branch contains the resulting merge commit.

Any commit after approval creates a new head and requires fresh checks and
approval. Accepted reviewer identity is verified against GitHub review and merge
metadata for `H`; it is never inferred from commit author text or request JSON.
This rule also applies when the manager invoked the preparation command locally.

## 7. Allocation and host selection

| Command | Actor and authority | First durable write | Idempotency key | Observe and recover | Terminal result |
|---|---|---|---|---|---|
| `researchctl allocation request` | Runner; controller DB owns request/allocation, host owns physical lock | Request and stored idempotency response in one controller transaction | Resource request ID + canonical constraints digest | Observe request, offer, target preflight, claim, assignment, host lock, and startup ack; never infer success from a lost response | `assigned`, `queued`, `canceled`, or `failed`; an unacknowledged assignment becomes `suspect` |
| `researchctl allocation heartbeat ALLOCATION_ID` | Bound runner only; controller lease observation | Monotonic heartbeat update guarded by allocation ID, generation, and sequence | Allocation ID + generation + heartbeat sequence | Read stored sequence; duplicates return prior response, stale generation/sequence is rejected | `recorded`, `duplicate`, `stale_fence`, or `quarantined` |
| `researchctl allocation release ALLOCATION_ID` | Bound runner or manager; controller allocation plus observed host lock/process | Release intent before remote observation | Allocation ID + generation + release reason digest | Observe process exit and free host lock before clearing assignment; unreachable/ambiguous becomes quarantined | `released` or `quarantined` |
| `researchctl allocation force-release ALLOCATION_ID --reason ...` | Manager only; controller allocation and audit log | Audited force intent, actor, reason, and prior observations in one transaction | Allocation ID + expected generation + reason digest | Re-read assignment and host evidence; stale generation fails. Lack of proof remains explicit in audit and forces physical preflight before reuse | `released_forced`, `stale`, or `rejected` |

Before the controller exists, manual/static allocation requires an explicit host
and GPU and `--host auto` fails closed. With the controller, auto-host is exactly:

```text
offer candidate -> target environment/input/disk preflight -> transactional claim
-> immediate physical GPU check + host lock -> launch -> startup acknowledgement
```

An offer is short-lived advice, not an assignment and grants no launch right. A
failed target preflight cancels that offer before another candidate is tried.
Claim is the linearization point.

For every GPU managed by RCP, at most one non-terminal assignment may reference
its `gpu_uuid` globally. The database enforces uniqueness on `gpu_uuid` across
non-terminal assignments, not on `(gpu_uuid, generation)`. Generation is only a
fencing token: it rejects stale heartbeat, launch, and release calls, but never
makes a second simultaneous assignment safe. Lost heartbeat moves an assignment
to `suspect` then `quarantined`; elapsed time alone never frees the GPU.

## 8. Projection and reconciliation

| Command | Actor and authority | First durable write | Idempotency key | Observe and recover | Terminal result |
|---|---|---|---|---|---|
| `researchctl linear configure` | Manager only; protected Git owns the accepted non-secret Linear policy | `OperationStarted`, then a canonical policy commit on a fixed control branch/worktree | Operation ID + exact default head + canonical policy digest | Re-read the exact default head, fixed branch/marker/path, canonical bytes, and proposal commit; replay observes the same proposal and never contacts Linear | `proposal_prepared` or `no_change`; acceptance still requires exact-head CI, CODEOWNER approval, and protected merge |
| `ApplicationService.linear_enqueue_accepted(...)` | Dedicated trusted automation; accepted Git records remain authoritative and SQLite owns the delivery event | `linear.accepted-result.v1` outbox row after protected-merge and exact-head attestation revalidation | Project + Task + Report ID + Report revision | Re-read the protected merge and existing stable event; neither Agent input nor CI selects a target, body, or credential | `queued`, `already_queued`, or `disabled` |
| `ApplicationService.linear_delivery_run_once(...)` | Dedicated configured-app worker; Linear comment is a projection and receipt is local operational evidence | Claim/lease row before any remote read or mutation | Topic + stable outbox ID; a claim ID resumes one in-flight attempt | Preflight exact UUID relationships, observe marker, create only if absent, then atomically finish status and receipt; an API timeout is retried by observation | `idle`, `delivered`, `retryable`, or `dead_letter` |
| `researchctl reconcile` | Any authorized reader; read-only observation | None | Plan digest is derived from observations | Compare Git, local DB, worktrees, tmux/processes, remotes, artifacts, controller, and projection markers under a deadline | `clean`, `plan_ready`, or `partial_observation` |
| `researchctl reconcile --apply PLAN_DIGEST` | Manager/operator, with scoped runner repair where allowed; each original authority remains unchanged | `OperationStarted` binding the exact plan digest and fresh preconditions | Plan digest + repair operation ID | Re-observe each precondition before its repair; stale or ambiguous items are skipped and reported, never guessed | `reconciled`, `partial`, `stale`, `failed`, or `canceled` |

An accepted-result event binds project, Task, Submission, Decision and Report
digests; source PR head and accepted merge; manager-owned workspace, team,
optional project and issue UUID; renderer ID/version; and payload digest. The
event ID is stable over retries. Repeated trusted worker invocations use the
same `ApplicationService` delivery method. Credential-free accepted-merge
validation is available through `researchctl-linear-host shadow`; no live
enqueue/delivery replay operator CLI is registered at R0. An Agent is not
authorized to call the mutation and never supplies the target, template,
free-form body, or credential.

The worker validates the immutable issue UUID against the configured workspace
and team or project before writing. A mismatch never falls back to title search
or a default issue; it is a visible dead letter. The comment contains a stable
event marker and payload digest. A successful observation writes a receipt with
the accepted merge, issue ID, comment ID, renderer version, payload digest,
remote marker and observation time. If the API succeeds and the worker crashes
before that write, replay observes the marker and records the receipt without
creating a second comment.

Inbound Session addressing is not a general command channel. A trusted adapter
must authenticate the Linear event and exact app mention before it constructs
the event object accepted by the dedicated ingress facade. The ordinary CLI
cannot self-assert `authenticated_app_id`, credential identity, or trusted
automation role. The visible fixed first line is either
`@researchctl-app notify session:<full-id> commit:<full-sha>` or
`@researchctl-app reply commit:<full-sha>`, followed by a non-empty message. The
trusted adapter verifies and removes only that exact mention before strict
command parsing. The contextual form
derives its Session only from a locally stored outbound delivery receipt for
the same workspace, issue, and thread; pasted hidden markers are inert text.
Both forms then verify the canonical Task issue binding, Session/Task binding,
recorded Session branch, and full commit reachability.

Projection failure never blocks Git work, local runs, acceptance, or resource
release. Reconciliation is not a new source of truth: it compares authorities,
proposes a bounded repair plan, and applies only explicitly selected repairs with
fresh preconditions.

## 9. Minimum conformance tests

An implementation is not conformant until tests prove:

- flags and `--json` invoke the same service method and produce the same domain
  transition and error code;
- actor fields in JSON cannot elevate an agent to manager authority;
- crash injection after every external step resumes by observation without a
  duplicate branch, PR, tmux process, Run, artifact, allocation, or projection;
- approval on PR head `H` becomes stale after any new commit;
- trusted CI emits an attestation whose subject head/tree and every generated
  output digest match `H`; an agent-authored status cannot satisfy that check;
- a lost or cross-host Session is continued only with a new Session ID;
- every non-terminal RunAttempt can fail or cancel without remaining stuck;
- auto-host never claims before target preflight;
- concurrent claims cannot create two non-terminal assignments for one
  `gpu_uuid`, even with different generations;
- CI pass without accepted merge produces no accepted-result comment;
- the same accepted records render byte-identical CLI, CI-preview and Linear
  payloads under golden tests;
- a wrong workspace, team, project, issue, renderer or payload digest performs
  zero Linear mutations and becomes a visible dead letter;
- duplicate, reordered and ambiguous API results, including a crash after a
  successful create, leave exactly one visible comment and a matching receipt;
- agent and untrusted-CI contexts cannot load the Linear credential or invoke
  the publisher; and
- Linear outage and partial reconcile leave authoritative state unchanged.
