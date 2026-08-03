# ADR 0004: Task and Session Ownership

Status: Accepted
Date: 2026-08-02

## Context

One branch and one session per Task cannot represent retries, parallel agents,
handoff, or execution from two hosts. Two hosts writing task/MAR-17 would race
and eventually produce non-fast-forward failures or ambiguous ownership.

## Decision

Task is durable manager intent. Task has one or more Sessions. Every Session has:

- a globally unique session ID;
- its own branch and worktree;
- one owning host and native agent-session identity;
- one active writer at a time;
- zero or more Runs.

The branch format is research/task/TASK_KEY/SESSION_ID. A same-host native resume
may reuse the Session only while the original ownership and idle/stopped process
identity are positively observed. Lost is terminal. Uncertain ownership,
cross-host recovery, or continuation from a lost Session creates a new Session
ID, branch, and worktree linked by `continued_from`; it never takes over or
reactivates the old Session. A Run records the exact source Session and commit.

Human-readable Task keys such as MAR-17 are aliases. Canonical IDs are generated
locally and do not require Linear.

## Consequences

Parallel agents and cross-host recovery do not share mutable branches. Task
status remains a manager decision, while session and run state are observations.
The inbox aggregates multiple Sessions under one Task.
