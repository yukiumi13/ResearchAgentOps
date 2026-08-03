# ADR 0009: Operation Journal and Reconciliation

Status: Accepted
Date: 2026-08-02

## Context

Starting a session or run mutates Git, SSH hosts, tmux, processes, artifacts,
and eventually a resource controller. Any response may be lost after the side
effect succeeds. Simple retries can create duplicate processes, PRs, or leases.

## Decision

Every mutating command allocates or accepts an operation ID before its first
side effect. Each external step defines:

- preconditions;
- stable idempotency key;
- durable step-start and observation events;
- how to observe ambiguous state;
- retry behavior;
- compensation when safe;
- an absorbing terminal state.

Retries with the same key return or continue the original operation. They never
assume a timeout means failure.

researchctl doctor --reconcile compares authoritative records and observed Git,
worktree, tmux, process, remote, artifact, and controller state. It defaults to
dry-run and emits a repair plan. Repair requires explicit application.

## Consequences

Crash recovery is a first-class workflow instead of cleanup documentation.
Adapters return typed errors and observation data. Tests inject failure after
every external step and prove retry does not duplicate effects.
