# ADR 0002: Split RunSpec, RunAttempt, and RunResult

Status: Accepted
Date: 2026-08-02

## Context

The historical design used one run manifest for immutable provenance and mutable
runtime fields. It did not define when or where the manifest became durable, so
a launch could succeed before any recoverable record existed.

## Decision

A run is represented by three records:

- RunSpec is created and content-hashed before launch. It freezes source tree,
  argv, resolved inputs, environment fingerprint, resource request, and artifact
  declarations.
- RunAttempt is an append-only host-local operation journal. Retries create new
  attempts linked with retry_of.
- RunResult is generated once from a terminal observation and carries actual
  execution, outcome, metrics, and artifact attestations.

A standard run branch retains the records. An immutable research-run tag points
to the RunSpec metadata commit and retains its parent code commit. Execution
uses the recorded code commit, never the metadata commit.

The Submission PR copies finalized RunSpec and RunResult into the default branch.
Run refs are not eligible for cleanup until the record is accepted or an
explicit retention decision exists.

## Consequences

Cross-host recovery can begin from Git before contacting the old host. Runtime
updates no longer mutate provenance. A retry is distinguishable from a new
scientific run and cannot overwrite evidence from an earlier attempt.
