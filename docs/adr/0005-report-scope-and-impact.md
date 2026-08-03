# ADR 0005: Report Scope and Conservative Impact

Status: Accepted
Date: 2026-08-02

## Context

Research evidence may come from task-local code that is never merged to main.
A single validated_through commit incorrectly assumes the evidence commit is an
ancestor of main. Path dependencies can also omit real semantic dependencies.

## Decision

Every Report separates evidence validity from applicability and declares:

- claim_scope as snapshot or baseline;
- evidence tree and immutable RunResult;
- accepted-at main tree;
- validation basis for baseline claims;
- reviewed code, input, environment, and resource dependencies.

Snapshot claims remain historical facts when main advances. Baseline claims
enter impact_pending when their validation basis is behind. Impact is evaluated
against each report basis, not only the latest push range.

No-overlap means only that declared dependencies did not overlap. The MVP never
automatically advances validity. Impact PRs use an expected Report revision and
must be regenerated against current main before merge.

## Consequences

Accepting isolated experimental code remains possible without pretending that
the result describes current main. False negatives remain a reviewed governance
risk instead of being hidden by automatic validation advancement.
