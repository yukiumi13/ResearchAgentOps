# Requirement Traceability Matrix

Status: Phase 0 baseline
Updated: 2026-08-04

`docs/USER_SCENARIOS.md` is the scenario catalog. The contract column points to
the current authoritative specification and ADRs. Phase 9 rows are deliberate
post-MVP Integrations and do not add those adapters to v0.1 Core. Test IDs are
stable acceptance-suite keys, not filenames.

| Scenario | Requirement | Contract | Earliest phase | Stable test | Planned verification |
|---|---|---|---|---|---|
| `US-001` | Exception inbox and fast answers | Spec Sections 1, 7.7, 11, 19 | 2 | `AT-US-001` | 50-item grouping, stale labels, p95/p99 benchmark |
| `US-002` | Human owns plan and Task state | Spec Sections 3, 4, 6.2; ADR 0001 | 2 | `AT-US-002` | actor authorization and observed-vs-declared state |
| `US-003` | Novice guided Task template | Spec Section 7.1; scenario contract | 2 | `AT-US-003` | guided/non-interactive model parity and error guidance |
| `US-004` | Mobile grouped view | Scenario contract; Spec Sections 3, 11 | 9 Integrations | `AT-US-004` | core-query parity and read-only mobile projection |
| `US-005` | Milestone-only Slack/chat projection | Spec Section 7.7; scenario contract | 9 Integrations | `AT-US-005` | coalescing, stable event ID, disposable messages |
| `US-006` | Linear addresses one Session and receives its reply | Spec Sections 6.3, 11, 20; ADRs 0004, 0012; scenario contract | 8 | `AT-US-006` | authenticated receipt, exact commit route, ack/reply, restart, fallback, no cross-route |
| `US-007` | SSH-only, no-listener onboarding | Spec Sections 1, 13, 18 | 5 | `AT-US-007` | host doctor and end-to-end SSH/tmux with no listener |
| `US-008` | Fixed agent capabilities | Spec Sections 3, 4, 17; ADR 0007 | 2 | `AT-US-008` | allow/deny matrix, audit, secret-safe errors |
| `US-009` | Linear credential architecture | Spec Sections 3, 4.3; ADRs 0001, 0007 | 8 | `AT-US-009` | adapter-only secret, redaction, credential rotation |
| `US-010` | Session attribution without per-session users | Spec Sections 5, 7.7, 18; ADR 0004 | 2 | `AT-US-010` | shared identity with unspoofable session attribution |
| `US-011` | On-prem/cloud grouping and HostPool | Spec Sections 5, 7.1, 12-13; ADR 0011 | 2 | `AT-US-011` | domain/pool eligibility, UI grouping, manager-only policy |
| `US-012` | ExecutionDomain and data boundary | Spec Sections 7.1-7.4, 12-13; ADR 0011 | 3 | `AT-US-012` | domain resolution, unresolved inputs and cross-domain digests |
| `US-013` | Locate, pause, attach, resume, one writer | Spec Sections 6.3, 11, 18; ADRs 0004, 0009 | 2 | `AT-US-013` | positive same-host resume; uncertain/lost continuation creates a new Session |
| `US-014` | Mature tools and complexity budget | Spec Sections 2, 8, 18; Implementation Plan reference policy | 1 | `AT-US-014` | dependency boundary and human/JSON service parity |
| `US-015` | No old/orphan Markdown pollution | Spec Sections 8, 10, 14, 17; ADR 0008 | 3 | `AT-US-015` | pre-existing Markdown hashes and managed-path checks |
| `US-016` | Wrong input retained, not a claim | Spec Sections 5, 7.1-7.5, 12 | 3 | `AT-US-016` | preflight failure and failed-result submission policy |
| `US-017` | Canonical Submission, multiple renderers | Spec Sections 7.5, 7.8, 8, 14, 17; ADRs 0003, 0008, 0010 | 4 | `AT-US-017` | golden renderers, byte comparison and digest stability |
| `US-018` | GitHub/VS Code/CLI review | Spec Sections 9, 14, 17; ADR 0003 | 4 | `AT-US-018` | proposal/digest parity and manager-only acceptance |
| `US-019` | Single-repository truth and retention | Spec Sections 3, 9, 13-14; ADRs 0001, 0002 | 4 | `AT-US-019` | clean-clone validation and reachability disposition |
| `US-020` | Rerun, waive, or stale impact decision | Spec Sections 6.6, 15; ADR 0005 | 6 | `AT-US-020` | effective status, explicit decision materialization and protected regeneration; live providers pending |
| `US-021` | Trusted CI vs SSH experiments | Spec Sections 4.3, 7.8, 12, 17; ADRs 0007, 0010 | 4 | `AT-US-021` | secretless exact-head attestation and separate runner authority |
| `US-022` | Onboarding and glossary | Spec Sections 8, 10, 18; scenario contract | 1 | `AT-US-022` | terminology/help golden tests and no-service start |
| `US-023` | `allowed_write_paths` enforcement | Spec Sections 4.4, 7.1, 11, 17; ADRs 0007, 0011 | 2 | `AT-US-023` | prefix, traversal/symlink/rename/protected-path adversarial tests |
| `US-024` | Batch-safe worktree sync | Spec Section 15; ADRs 0004, 0005 | 6 | `AT-US-024` | preview, safe-point policy, isolated conflict recovery |
| `US-025` | Complete delegated plan and traceability | Spec Sections 20-21; Implementation Plan; Workflow Coverage | 0 | `AT-US-025` | static 33-ID, 77-prompt, phase, test, workflow and status coverage check |
| `US-026` | Merge/abandon/retain-isolated disposition | Spec Sections 9, 14; ADRs 0002, 0005 | 4 | `AT-US-026` | disposition gate, cleanup safety, snapshot claim |
| `US-027` | Local CI and benchmark responsiveness | Spec Sections 19-21 | 1 | `AT-US-027` | supported-envelope latency and queue/execution metrics |
| `US-028` | SSH plus tmux, not Slurm/daemon | Spec Sections 2, 6.3, 11-13 | 2 | `AT-US-028` | deterministic tmux lifecycle and no scheduler dependency |
| `US-029` | Exact on-prem-to-cloud run | Spec Sections 7.2-7.4, 12-13, 18 | 5 | `AT-US-029` | exact tree, preflight, artifact verification, SSH retry |
| `US-030` | Accepted merge, exactly one Linear comment | Spec Sections 3, 7.8, 14, 17-18; ADRs 0001, 0003, 0010 | 8 | `AT-US-030` | target preflight, receipt and crash-safe exactly-once projection |
| `US-031` | Existing-repo init/bootstrap/migration | Spec Sections 8, 10; ADR 0008 | 1 | `AT-US-031` | dirty/idempotent init, accepted bootstrap, explicit upgrade |
| `US-032` | Global GPU queue safety and recovery | Spec Sections 6.7, 12, 16, 18-19; ADR 0006 | 7 | `AT-US-032` | contention, quarantine, external use, restart reconcile |
| `US-033` | Disposable one-way Linear projection | Spec Sections 3, 7.8, 17-18, 20; ADRs 0001, 0010 | 8 | `AT-US-033` | outage continuity, receipt reconciliation, rebuild and ignored mutations |

## Cross-cutting gates

| Gate | Covered scenarios | Verification |
|---|---|---|
| One shared service API; human and agent presentations | `US-003`, `US-014`, `US-022` | application-service contract suite runs once through human CLI and `--json` |
| Human authority and agent containment | `US-002`, `US-008`, `US-010`, `US-023` | actor/path policy matrix and hostile-record tests |
| Durable evidence and review | `US-015`-`US-021`, `US-026` | crash, golden renderer, trusted CI, atomic merge, retention tests |
| Session and remote execution | `US-006`, `US-007`, `US-011`-`US-013`, `US-024`, `US-028`, `US-029` | one-writer observation, domain preflight, SSH ambiguity, artifact tests |
| Optional projections and integrations | `US-004`-`US-006`, `US-009`, `US-030`, `US-033` | delete/outage/replay tests prove no second authority |
| Response and recovery objectives | `US-001`, `US-013`, `US-027`, `US-029`, `US-032` | supported-envelope benchmark and failure injection suite |

`AT-US-025` must parse this matrix and the scenario catalog so missing,
duplicate, or renumbered IDs fail before implementation work is considered
traceable.

## Post-export requirements

Requirements stated after the 77-prompt historical baseline are tracked separately in
`REQUIREMENT_LEDGER.md`; they refine existing scenarios rather than renumbering
the stable `US-*` catalog.

| Requirement | Scenario mapping | Primary contract |
|---|---|---|
| `REQ-20260803-001` | `US-014`, `US-023`, `US-031` | workspace policy and Implementation Plan reference policy |
| `REQ-20260803-002` | `US-003`, `US-008`, `US-014`, `US-022` | Mutation Contracts Sections 1-2 |
| `REQ-20260803-003` | `US-001` through `US-033` | scenario catalog and `AT-US-025` |
| `REQ-20260803-004` | `US-008`, `US-013`, `US-014`, `US-021`, `US-023`, `US-028` | Design Assessment conclusion |
| `REQ-20260803-005` | `US-017`, `US-018`, `US-021`, `US-030`, `US-033` | Spec Sections 14 and 17; ADR 0010 |
| `REQ-20260803-006` | `US-006`, `US-009`, `US-010`, `US-030`, `US-033` | Spec Sections 7.9 and 11.1; ADR 0012 |
| `REQ-20260803-007` | `US-017`, `US-018`, `US-021`, `US-026` | Spec Section 14; Mutation Contracts Section 5; ADR 0003 |
| `REQ-20260803-008` | `US-008`, `US-013`, `US-021`, `US-023`, `US-027` | Spec Sections 7.2, 12, and 17; Mutation Contracts Sections 3 and 5; ExperimentPlan, reviewer-control, Run/Submission/CI tests |
| `REQ-20260803-009` | `US-019`, `US-020`, `US-021`, `US-026` | Spec Section 15; Mutation Contracts Section 5; ADR 0005 |
| `REQ-20260803-010` | `US-014`, `US-020`, `US-025`, `US-027` | Workflow Coverage; ADR 0013; traceability parser test |
| `REQ-20260803-011` | `US-016`, `US-020`, `US-021` | Spec Section 15; ADRs 0005 and 0013; receipt/evaluator tests |
| `REQ-20260803-012` | `US-002`, `US-018`, `US-020`, `US-021` | Spec Sections 4 and 15; Mutation Contracts Section 5; ADR 0005; status/decision/CI tests |
