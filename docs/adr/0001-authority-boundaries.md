# ADR 0001: Authority Boundaries

Status: Accepted
Date: 2026-08-02

## Context

RCP observes and projects state through Git, local SQLite, GitHub, a resource
controller, and optionally Linear. Treating all of them as equivalent stores
would create dual writes, stale overwrites, and irrecoverable conflict rules.

## Decision

Authority is assigned per object:

- Git default branch owns project policy, Task intent, finalized evidence,
  ReviewDecision, and accepted Report revisions.
- Session branches own task-local code before review.
- Immutable run refs own pre-launch RunSpec.
- Host-local SQLite owns durable live operation journals, Session state,
  notifications, outbox/claims/receipts, and observations only.
- Resource Controller owns live RCP GPU requests, allocations, and leases.
- GitHub owns PR and review transport, not accepted research semantics.
- Linear is a one-way, disposable projection.

A projection never writes back to its authority in the MVP. Git-derived facts
can be reconciled into SQLite, but undelivered notifications/outbox rows,
operation history, acknowledgements, and other live facts are not generally
reconstructible from Git. SQLite therefore requires persistent storage, one
writer, consistent backups, and restore testing. Git cannot claim that a live
process is running, and the controller cannot change accepted research truth.

## Consequences

Every schema field has one owner and one mutation path. Integration failures are
repaired by replay or reconciliation. Mobile edits in Linear cannot start work
or accept results until a future ADR defines an explicit import-proposal model.
