# ADR 0011: Execution Domains and Write Scope

Status: Accepted
Date: 2026-08-02

## Context

Research tasks may be restricted by data location, credentials, host ownership,
or GPU availability. A UI may call a group of agents and hosts a team, but a
second Team authority or general scheduling language would duplicate Task and
HostPool semantics. Free-text path constraints also cannot prevent an agent
from proposing changes outside its assignment.

## Decision

Each Task names one `execution_domain`, a non-empty set of
`allowed_write_paths`, and one or more `deliverables`. It may also name a parent
Task and milestone for presentation. Project policy defines each execution
domain as a stable key plus one or more host-pool keys. Cross-record validation
rejects a Task whose domain is absent from accepted Project policy.

An execution domain is a placement and data-boundary label, not a process,
credential, or new source of Task state. "Team" is UI language for an execution
domain and its host pools; v0.1 has no separate Team aggregate.

Allowed write paths are normalized repository-relative POSIX path prefixes.
Matching is by complete path segments, so `src/model` does not match
`src/model-old`. Glob syntax is forbidden. The root prefix `.` may allow the
ordinary repository tree, but `.research` and everything below it are globally
protected and always win over a Task allowlist. Manager-owned operations use
their own explicit changed-path contracts rather than an agent Task allowlist.

The Session adapter creates the isolated branch/worktree but does not claim to
predict or confine all future writes before launching the Agent. Submission
preparation loads the Task from the protected branch and validates the frozen
source diff before creating proposal side effects. Exact-head Submission CI
loads Task truth from its exact protected base and repeats the complete check,
including renames, symlinks, submodules, traversal, file modes, and protected
paths.

Local `run start` and `run retry` load the canonical Task from the exact local
protected head, validate the Run's frozen source scope, and perform input/host/
path/executable/disk/GPU/artifact preflight before process launch. This is
accepted-state protection and mistake containment, not hostile same-user
process isolation; a user able to rewrite local refs or another process under
the same Unix account remains outside the hard-isolation claim.

## Consequences

Placement and write authority remain simple and inspectable. A future scheduler
may interpret execution-domain and host-pool keys without changing Task truth.
Adding a distinct Team lifecycle, wildcard language, or stronger OS containment
requires a separate ADR.
