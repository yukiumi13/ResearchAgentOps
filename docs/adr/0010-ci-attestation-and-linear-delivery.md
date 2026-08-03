# ADR 0010: CI Attestation and Linear Delivery

Status: Accepted
Date: 2026-08-02

## Context

An agent-authored status message, a CI result, an accepted research Report, and
a Linear comment answer different questions. Treating any of them as the same
"report" would let an agent claim that CI passed, let a projection create new
research meaning, or send a valid result to the wrong Linear issue.

PR workflows also run on untrusted changes and therefore cannot receive Linear
credentials. A successful GitHub check alone does not prove that a future
projection resolved the intended issue or rendered the required payload.

## Decision

The system keeps the workflow envelope and three domain/transport facts
distinct:

1. Trusted PR CI runs `researchctl ci dispatch` and emits a strict canonical
   outer envelope for the exact PR head. It always binds the base/head/tree and
   refs, dispatcher/workflow/check identities, PR type, applicability, named
   evidence digests, and result. This envelope is a workflow artifact, not a
   schema-registered `.research` record.
2. Only a Submission dispatch embeds a typed `CIValidationAttestation`. It
   identifies the protected validator, schema manifest, generated outputs,
   Submission/Decision/Report evidence, and, when Linear is configured, a
   credential-free projection preview containing the bound issue ID, renderer
   version, and payload digest. Task, bootstrap, and ordinary-source envelopes
   contain no invented Report or projection fields.
3. A canonical Report becomes accepted research truth only through the atomic
   manager acceptance and merge workflow in ADR 0003. CI cannot accept it.
4. Trusted post-merge automation revalidates the accepted merge. Shadow mode
   emits a canonical credential-free observation with no live outbox write.
   Authenticated enqueue records the stable event, after which the projection
   worker resolves the manager-owned Linear binding, performs a read-only remote
   target preflight, renders the accepted Report deterministically, and
   publishes it. Agents never receive the credential or call the mutation.

The dispatcher supports Submission, generated Task control, bootstrap proposal,
bootstrap acceptance, and Linear policy control changes. Unknown or mixed
protocol changes fail closed. An ordinary source PR is `not_applicable`; this
proves only the absence of protocol-path changes and does not test or approve
source. A separate
credential-free `pull_request` workflow executes source tests at the exact head.
The protected-base workflow reads the PR head only as Git objects and never
executes PR source.

The target is an immutable Linear issue ID bound to the canonical Task plus an
allowlisted workspace and team or project in manager-owned integration config.
The worker refuses to post if the issue is missing, archived, or outside that
boundary. It never falls back to a default issue or searches by title.

Each accepted projection has a stable outbox event ID and embeds an external
marker containing that ID and the payload digest. Before retrying, the worker
looks for the marker. After the API call it stores a `ProjectionReceipt` with
the event ID, accepted merge, issue ID, comment ID, renderer version, payload
digest, response observation, and delivery time. Reconciliation compares the
receipt and remote marker without importing Linear edits into canonical state.

If Linear is not configured, CI records `projection: disabled`. If configured,
an invalid local binding or rendering contract fails the named projection
contract check. A remote outage or target mismatch after merge does not undo or
block the accepted Git state; it leaves the event retryable or dead-lettered and
visible in the exception inbox.

The repository includes a local Git accepted-merge reader, credential-free
one-shot shadow host, enqueue/delivery core, and fake-port transport tests.
GitHub-authenticated artifact ingress and a real Linear publisher adapter are
deployment work; this ADR does not claim they are live.

## Consequences

An agent submits evidence and can render the same preview through the shared
application API, but it cannot attest CI success or publish to Linear. Humans,
agents, and CI see one deterministic format. Correct destination, exact content,
and exactly-once visible delivery are independently observable rather than
inferred from a green check or an agent message. The installed CODEOWNERS
baseline and branch protection must be reviewed, configured, and verified
before protected merge can certify acceptance.
