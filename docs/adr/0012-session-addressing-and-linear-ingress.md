# ADR 0012: Session Addressing and Linear Ingress

Status: Accepted
Date: 2026-08-03

## Context

The manager needs to ask one running Agent Session to inspect an exact commit
and receive the answer in the originating Linear thread. A shared Linear app
identity is operationally simpler than creating an email address or Linear user
for every Session, but the shared identity must not erase attribution or allow a
comment to be routed by a forged display name or pasted hidden marker.

RCP also cannot safely inject arbitrary text into a live Codex or Claude
transcript. Doing so would bypass the Session capability boundary, race with
ongoing work, and make delivery indistinguishable from model input. Linear may
be unavailable, comments may be retried, and a Session may stop between ingress
and receipt.

## Decision

RCP uses one trusted Linear application identity for transport and a durable
Session inbox for addressing. A deployment identifies the application in every
visible comment, for example `researchctl-app`. It does not create a mailbox,
email address, Linear account, or background process per Session.

The visible supported comment commands are deliberately fixed:

```text
@researchctl-app notify session:<full-session-id> commit:<full-git-object-id>
<message>

@researchctl-app reply commit:<full-git-object-id>
<message>
```

There is no per-Session mention. The trusted adapter verifies and strips only
the configured shared-app mention before passing the remaining bytes to the
strict parser. The explicit form names a Session. The contextual form is
allowed only when the current Linear thread is bound by a previously delivered
RCP receipt. It derives the Task and Session from that receipt; it never trusts
a marker copied into the new comment body. Short Session IDs, short commit IDs,
title search, display names, shell-like suffixes, and fallback destinations are
rejected.

A trusted webhook or poller adapter authenticates the Linear event and observes
the exact workspace, issue, thread, comment, author, app mention, and command
bytes. Before Task resolution or any receipt write, the ingress facade requires
the configured workspace, authenticated and mentioned app IDs, credential
identity, and a comment-author UUID from the manager-owned allowlist. It stores
a content-addressed ingress receipt before calling the shared
`ApplicationService`. Stable operation, notification, and idempotency IDs are
derived from the authenticated Linear event/comment identity, so retrying the
same event cannot create another notification. Reusing one workspace/comment
identity succeeds only when every canonical receipt field still matches,
including app, author, credential, issue/thread/event, Task, source marker,
command digest, and observed payload digest; changed content is a conflict. The
application then verifies:

1. the issue UUID is the manager-owned binding of the canonical Task;
2. the Session belongs to that Task and Project;
3. the Session has a recorded branch;
4. the full commit exists and is reachable from that exact Session branch; and
5. a contextual source receipt binds the same issue, Task, and Session.

The notification and its route are written to host-local SQLite. Active,
preparing, idle, or stopping Sessions use the Session route. A stopped or lost
Session, including one that becomes terminal while a message is pending, routes
transactionally to the manager exception inbox with a new revision and an
explicit reason. No message is silently discarded.

Agents and humans read the same inbox through the shared service API. The human
CLI renders a control-safe view; the Agent CLI uses strict JSON. The read-only
`session list`, `session show`, and `session address` commands discover a
target and produce a full-SHA-verified first-line header without contacting
Linear. A manager may address visible Project Sessions; a Session actor can
address, list, acknowledge, or reply only for its bound Session. Reading is
polling at Agent safe checkpoints, not terminal keystroke injection. `ack`
proves the Session application consumed a revision; durable persistence alone
does not claim that the model saw it.

A reply and `linear.session-reply.v1` outbox event are committed atomically.
Only the trusted Linear worker may claim and deliver that event. The outbound
comment contains a transport marker that attributes the shared app action to
`agent_id`, `session_id`, `task_id`, optional `report_id`, notification ID,
reply ID, and payload digest. The worker observes that marker before create and
requires the observed comment author to equal the configured app identity.
Wrong-author markers cannot satisfy delivery. The worker stores a receipt after
observation, so a crash after a successful API call is recovered without a
duplicate visible comment.

The ingress adapter and outbound publisher may run as short-lived scheduled
jobs. RCP does not require an HTTP service, daemon, message broker, or inbound
listener on an SSH-only research host. A deployment-specific webhook receiver
may forward authenticated events to the same strict ingress command, but it may
not introduce another state machine or business API.

## Authority and Attribution

- Git remains authoritative for Project, Task, Decision, Report, and accepted
  Linear bindings.
- SQLite owns durable Session notification, ingress receipt, outbox,
  delivery-attempt, and delivery-receipt state. Pending state is not assumed to
  be reconstructible without an external observation adapter and replay source.
- Linear is a transport and disposable projection, not a command authority for
  Task state, acceptance, scheduling, or resource allocation.
- The visible Linear author is the trusted app identity. The transport marker
  and local receipt preserve the originating Agent, Session, Task, operation,
  report, and payload identities separately.

## Consequences

The manager gets practical `@app`-style Session addressing without managing one
external identity per worker. Delivery remains attributable and retryable, and
a missing Session has a visible fallback. The tradeoff is that RCP cannot claim
instant transcript delivery: end-to-end receipt is observable only after the
Session polls and acknowledges the inbox. A real Linear `@app` interaction also
requires one deployed trusted webhook or poller adapter and a real publisher.
The repository facade, worker, durable store, and fake-port tests are not by
themselves a live integration.
