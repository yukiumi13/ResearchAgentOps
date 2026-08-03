# ADR 0003: Atomic Human Acceptance

Status: Accepted
Date: 2026-08-02

## Context

Merging a proposal was defined as acceptance, while accepted identity, decision,
and canonical report fields could only be known after review. A post-merge bot
would create a second semantic change that was not reviewed atomically.

## Decision

A ResearchSubmission PR begins as an agent-authored proposal. It contains
finalized evidence and a proposed report rendering, but no accepted fields.

After review, an authenticated manager runs researchctl review accept. The
command verifies the expected PR head and report revision, then adds:

- ReviewDecision;
- authenticated reviewer and timestamp;
- acceptance conditions;
- deterministic accepted Report revision.

The same PR is validated again and merged. The merge commit atomically contains
evidence, proposal, decision, and accepted report. Post-merge automation may
project this fact but may not create research semantics.

Bootstrap uses the same pattern through researchctl bootstrap accept.

## Consequences

Acceptance takes one PR and one explicit manager preparation action. Agents
cannot self-accept through a YAML reviewer field. Concurrent updates fail on the
expected report revision instead of silently overwriting one another.
