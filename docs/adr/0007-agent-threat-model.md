# ADR 0007: Agent Threat Model

Status: Accepted
Date: 2026-08-02

## Context

Git worktrees isolate working copies, not processes running under the same Unix
identity. An agent with arbitrary same-user command execution may access sibling
paths or credentials even when normal tooling discourages it.

## Decision

The default product protects against mistakes, scope drift, and unreviewed state
changes. It does not claim to contain a malicious same-user process.

Defense in depth includes fixed worktree cwd, agent workspace sandbox, no
dangerous skip-permission modes, credential denial where supported, controlled
researchctl entry points, changed-path validation, trusted CI, CODEOWNERS,
required human review, and protected branches.

Agents do not receive Linear or manager credentials. Untrusted PR workflows do
not receive SSH, cloud, or integration secrets. High-risk unattended work uses
a separate user, clone, container, or stronger OS isolation.

## Consequences

Security documentation must distinguish host compromise from accepted-state
protection. A successful merge gate does not prove that the host was isolated.
A future hostile-agent mode requires a separate ADR and execution backend.
