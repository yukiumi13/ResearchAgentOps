# Post-export Requirement Ledger

Status: normative implementation input
Updated: 2026-08-03

`USER_SCENARIOS.md` preserves anonymous anchors for all 77 prompts in the
private historical research. This ledger gives stable identities to later
requirements so traceability does not silently stop at that boundary.

## REQ-20260803-001 - Workspace and reference-repository boundary

- Source: current implementation review, 2026-08-03.
- Maps to: `US-014`, `US-023`, `US-031`.
- Acceptance: implementation writes only inside its checked-out workspace;
  external compatibility repositories remain read-only references and receive
  no test, cache, formatting, initialization, or generated files.

## REQ-20260803-002 - One simple service for humans, Agents, and Python

- Source: current implementation review, 2026-08-03.
- Maps to: `US-003`, `US-008`, `US-014`, `US-022`.
- Acceptance: human CLI, strict Agent JSON, and supported Python automation use
  the same `ApplicationService`, request models, actor checks, state transitions,
  and error codes; no general HTTP API, broker, daemon, or second business state
  machine is introduced.

## REQ-20260803-003 - Historical questions are real acceptance cases

- Source: current implementation review, 2026-08-03.
- Maps to: `US-001` through `US-033`.
- Acceptance: all 77 historical prompt anchors remain covered by the scenario
  catalog and traceability test; a scenario is not marked implemented merely
  because its requirement is cataloged.

## REQ-20260803-004 - Product is an Agent harness

- Source: current implementation review, 2026-08-03.
- Maps to: `US-008`, `US-013`, `US-014`, `US-021`, `US-023`, `US-028`.
- Acceptance: RCP is described and implemented as an Agent harness plus a
  lightweight research control plane around Codex/Claude, Git, tmux, SSH,
  GitHub, and Linear; it is not represented as an Agent model, experiment
  framework, scheduler, or replacement for those systems.

## REQ-20260803-005 - CI validates Report bytes; post-merge worker publishes

- Source: current implementation review, 2026-08-03.
- Maps to: `US-017`, `US-018`, `US-021`, `US-030`, `US-033`.
- Acceptance: exact-head CI reconstructs and byte-compares accepted Report YAML,
  Markdown, and credential-free Linear preview and emits an external attestation.
  CI neither accepts nor publishes. Only trusted post-merge automation may
  revalidate the accepted merge, enqueue, publish, and store a delivery receipt;
  an Agent never selects the accepted destination, body, renderer, or credential.

## REQ-20260803-006 - Shared app addresses and hears one Session

- Source: current implementation review, 2026-08-03.
- Maps to: `US-006`, `US-009`, `US-010`, `US-030`, `US-033`.
- Acceptance: one visible app identity such as `researchctl-app` accepts
  authenticated `notify session:<full-id> commit:<full-sha>` and receipt-bound contextual
  `reply commit:<full-sha>` commands. The exact Session gets a durable inbox
  revision and can list, acknowledge, or queue a same-thread reply; stopped or
  lost Sessions reroute visibly to the manager. App comments and receipts retain
  Agent, Session, Task, optional Report, event, and payload attribution without
  per-Session email accounts or external users.
