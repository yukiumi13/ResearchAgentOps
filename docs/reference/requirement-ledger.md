---
type: reference
title: Post-export requirement ledger
owner: person:yukiumi13
last_updated: 2026-08-13
validity: valid
tags: [requirements, traceability, governance]
references:
  - kind: repository_path
    location: docs/USER_SCENARIOS.md
  - kind: repository_path
    location: docs/TRACEABILITY_MATRIX.md
sources:
  - key: prompt-manifest
    kind: repository_path
    location: docs/HISTORICAL_PROMPT_MANIFEST.json
  - key: scenario-catalog
    kind: repository_path
    location: docs/USER_SCENARIOS.md
provenance:
  - key: historical-baseline
    value: "77 historical prompt anchors"
    basis: derived
    source_keys: [prompt-manifest, scenario-catalog]
    method: Compare the prompt manifest count and scenario source mappings.
relations:
  supersedes: []
  derived_from: []
  see_also: [docs/USER_SCENARIOS.md, docs/TRACEABILITY_MATRIX.md]
---
# Post-export Requirement Ledger

`USER_SCENARIOS.md` maps the 77 historical prompt anchors into stable scenarios
without publishing the private source. This ledger gives stable identities to
later requirements so traceability does not silently stop at that boundary.

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

## REQ-20260803-007 - The assigned Agent proposes the Submission PR

- Source: post-export workflow correction, 2026-08-03.
- Maps to: `US-017`, `US-018`, `US-021`, `US-026`.
- Acceptance: `researchctl submit` validates collected evidence, creates and
  pushes the one derived Submission branch and commit, and creates or observes
  the exact GitHub PR with a deterministic title/body. The operation is not
  `proposal_open` after only a local commit, and the caller cannot select an
  arbitrary repository, head, base, or PR body. The Agent proposes; only the
  manager can prepare acceptance, and only protected exact-head review and merge
  create accepted truth.

## REQ-20260803-008 - Plans cannot hide Agent fallback decisions

- Source: post-export Plan/schema/reviewer correction, 2026-08-03.
- Maps to: `US-008`, `US-013`, `US-021`, `US-023`, `US-027`.
- Acceptance: any supported `PLAN.yaml` is a strict versioned ExperimentPlan
  with generated schema and deterministic RunSpec compilation. Local lint and
  exact-head Submission CI reject missing semantic choices, unknown fields,
  implicit provider/CLI/library defaults, and values without an authenticated
  manager-authenticated accepted Task or explicit Project-policy source. An independent typed
  PlanReview is bound to the exact Plan/Task digests; it may run as a read-only
  background subagent without another persistent Session, but it has a distinct
  invocation identity and can only pass, request input, or report invalidity.
- Implementation: locally implemented and focused-test covered across generated
  schemas, deterministic lint/compile, explicit manager-owned reviewer policy,
  ephemeral Codex/Claude adapters, Run receipt gates, separate Submission
  evidence, and protected-base replay. Live provider/model and installed
  protected-repository canaries remain deployment pending.

## REQ-20260803-009 - Main merges create at most one reviewed Impact batch

- Source: post-export Report-code dependency continuation, 2026-08-03.
- Maps to: `US-019`, `US-020`, `US-021`, `US-026`.
- Acceptance: every accepted main push may invoke `researchctl ci impact` under
  trusted automation. It scans every accepted baseline Report from that
  Report's own validation basis, emits no mutation when no proposals exist,
  otherwise creates one digest-bound batch commit/branch/PR, and never launches
  a Run. Stable event-derived identity and commit timestamps make clean-runner
  retries reproduce the same commit SHA. Protected-base CI rescans all Reports
  and requires the exact generated path set and bytes before merge.

## REQ-20260803-010 - Every use case names its workflow and honest status

- Source: post-export workflow coverage and dependency-framework review,
  2026-08-03.
- Maps to: `US-014`, `US-020`, `US-025`, `US-027`.
- Acceptance: every stable `US-001` through `US-033` appears exactly once in a
  workflow checklist with one existing primary workflow, zero or more existing
  supporting workflows, current proof, and an open acceptance gap. Status must
  distinguish locally verified, partial, deployment-pending, and design-only
  work. Dependency tools may provide typed change evidence, but cannot become a
  second Report authority or silently turn missing lineage into no change.

## REQ-20260803-011 - External dependency evidence fails closed

- Source: post-export typed dependency-receipt continuation, 2026-08-03.
- Maps to: `US-016`, `US-020`, `US-021`.
- Acceptance: every resource/environment receipt binds provider identity and
  version, provider-query digest, Report basis, exact target, sorted
  observations, external basis/target identities, evidence digests,
  observation time, and a canonical receipt digest. `unknown`, absent,
  duplicate, undeclared, or target-mismatched evidence cannot produce a
  no-overlap validity advance. Batch scans expose unresolved Report IDs without
  blocking conservative stale proposals or automatically starting a Run. Live
  third-party adapters and protected provider replay remain a separate gate.

## REQ-20260803-012 - Effective Report state and Impact decisions are explicit

- Source: post-export Impact decision and manager-role continuation,
  2026-08-03.
- Maps to: `US-002`, `US-018`, `US-020`, `US-021`.
- Acceptance: `report status` derives applicability at an exact commit from the
  accepted Report and immutable Git objects. A changed full tree caused only by
  `.research/**` records does not make a newly accepted Report immediately
  pending; a governed source change or unavailable validation basis fails closed
  as `impact_pending`. Rerun, waiver, keep-stale, invalidation, and dependency
  correction are canonical digest-bound `ImpactDecision` records prepared only
  by an authenticated manager. Each decision binds the accepted Impact digest,
  Impact target, current Report revision, exact decision base, reviewer, reason,
  and disposition-specific inputs. Protected-base CI regenerates the exact
  Decision/Report bytes. A rerun decision references a manager-created Task but
  never starts, retries, or collects a Run.

## REQ-20260804-001 - Project document contracts work without research init

- Source: post-export project-document hierarchy and standalone-lint discussion,
  2026-08-04.
- Maps to: `US-014`, `US-015`, `US-021`, `US-022`, `US-031`.
- Acceptance: a Git repository with `.researchctl-docs.yaml` but no `.research`,
  SQLite database, Session, or manager context can run the same strict document
  schema/tree/render diagnostics locally and in CI. Canonical `a/b:c` routes
  bind type, contract, and directory; unknown hierarchy changes fail closed.
  Frontmatter/path agreement, required relations, finite legacy exceptions,
  deterministic YAML/Markdown pairs, visible renderer versions, optional
  generated index freshness, baseline-frozen bytes, and configured machine-only
  artifact roots are checked. Standalone policy is CODEOWNERS-protected; after
  initialization, label/directory/contract/index/artifact-root remapping uses the
  manager-only field-scoped `doc.configure-layout` proposal. Tags do not grant
  routing or authority.

## REQ-20260805-001 - Standalone Agents discover and draft the document contract

- Source: post-export standalone Agent injection, policy-example, and hierarchy-
  depth discussion, 2026-08-05.
- Maps to: `US-014`, `US-015`, `US-021`, `US-022`, `US-031`.
- Acceptance: `doc policy-template` emits a complete schema-valid candidate with
  all policy sections explicit, while `doc policy-lint` validates the customized candidate without a
  repository, managed state, or implicit taxonomy. Project policy may declare
  repository-local Claude or AGENTS guide targets. `doc agent-guide` writes only
  a declared target and deterministically inserts or replaces one visible,
  versioned managed block while preserving unrelated instructions. The block
  identifies policy discovery order, forbids fallback labels and directories,
  renders accepted routes, distinguishes manual from generated documents, and
  requires `doc tree`. Tree lint fails on missing, malformed, symlinked, or stale
  guide blocks. The lexical label grammar is mandatory; project policy bounds
  namespace segments before `:` independently from filesystem `max_depth`.
  Skills and MCP adapters may reuse the workflow but are not required and cannot
  become a second taxonomy or validator. Adopting or changing the candidate
  policy remains a manager/CODEOWNER-reviewed operation.

## REQ-20260805-002 - Standalone document authoring is self-discovering

- Source: external Claude standalone adoption and authoring canary, 2026-08-05.
- Maps to: `US-014`, `US-015`, `US-021`, `US-022`, `US-031`.
- Acceptance: `doc contracts` names every built-in document contract, required
  top-level fields, canonical source form, schema command, and renderer command;
  `doc schema` exposes the complete generated JSON Schema without init;
  `doc scaffold --type` emits a schema-valid route-specific Markdown or YAML
  skeleton; and `doc check PATH --json` dispatches through the effective route.
  Agent guides name these commands including AnalysisBrief. Routes carry a
  structured non-empty rationale, and `policy-lint` rejects unchanged template
  rationales. The template keeps human `docs/README.md` separate from generated
  `docs/INDEX.md`. Generated Markdown binds renderer/source/body digests, allows
  safe refresh only when the old body is unedited, and preserves apostrophes in
  Markdown prose. The marker identifies its source digest as canonical model JSON
  rather than raw YAML bytes. Structured routes may require and preserve a
  project-owned frontmatter envelope while RCP checks only required key presence
  and the renderer-owned body; project lint owns the envelope's value semantics.
  The renderer remains an optional typed-model projection rather than a general
  Markdown/HTML engine. AnalysisBrief schema and lint use canonical `answer`,
  accept legacy `conclusion` only as an input alias, expose field prose limits,
  preserve ordinary arrows, and require quoted display-sensitive decimals.
  Prose output exposes observed/maximum English and CJK budgets.
  Standalone `doctor` emits one mode explanation and document diagnostics rather
  than managed generated-schema errors. Machine review evidence uses
  `doc tree --json` rather than an unstructured summary.

## REQ-20260805-003 - PR authorship and merge gates use distinct principals

- Source: installed PR/branch-protection canary, 2026-08-05.
- Maps to: `US-017`, `US-018`, `US-021`.
- Acceptance: documentation distinguishes Action triggering from required status
  checks. Branch rules require `researchctl/source-tests` and
  `researchctl/exact-head`; those rules gate merge but do not trigger CI. An
  Agent proposal is authored by a distinct GitHub App/bot principal and approved
  by the human manager/CODEOWNER. The Agent cannot approve or merge. A
  single-maintainer PR authored by the same GitHub user cannot satisfy its own
  required approval and must not be placed behind an impossible rule without a
  second author/reviewer principal. A bounded read-only `github doctor` command
  audits classic protection and applicable active rulesets and returns a failed
  machine envelope when any fixed merge gate is absent. It does not count a
  green but non-required check as protection or mutate repository settings.
  `ProjectPolicy.github` provides the strict non-secret identity contract and a
  manager-owned field-specific proposal changes only that field. The separate
  governance apply command defaults to read-only preview; mutation is bound to
  the accepted-policy and observed-state digests, rejects Session/App authority,
  verifies a configured human Manager through GitHub, supports only unambiguous
  classic protection, and audits by read-back. Shared proposal delivery rejects
  missing policy, remote/default-branch drift, and PR receipts authored by a
  different login. No real rule apply is claimed. Pre-create App token
  provenance and post-merge Manager-review verification remain deployment gates.

## REQ-20260806-001 - Baseline enforcement survives policy-schema migration

- Source: standalone canary CI migration deadlock and schema-diagnostic feedback,
  2026-08-06.
- Maps to: `US-014`, `US-015`, `US-021`, `US-022`, `US-031`.
- Acceptance: `doc tree --baseline-project` does not validate an historical
  policy against the complete current `DocumentLayoutPolicy` schema. It reads
  only a safe historical document root and identifies frozen documents from raw
  baseline frontmatter, independent of subject routes. A baseline missing a
  newly required route field therefore permits the migration PR, while modified
  or deleted frozen bytes still fail. Malformed or unsafe baseline policy,
  malformed baseline frontmatter, symlinks, and an unexpectedly missing adopted
  root fail closed. Human schema errors name every invalid field and available
  YAML line/column by default, use an existing remediation command, and retain
  equivalent machine details under `--json`. Manual provenance values remain
  strict quoted display strings that occur verbatim in the body; source mismatch
  diagnostics name keys, and source/relation paths share the repository root.

## REQ-20260806-002 - CI execution is replaceable and capacity failure is typed

- Source: latent-agents hosted-runner acquisition failure and proposal-boundary
  discussion, 2026-08-06.
- Maps to: `US-017`, `US-018`, `US-021`, `US-023`.
- Acceptance: design and help separate runner execution, authenticated status
  publication, and ruleset merge authority. GitHub-hosted Actions is a default,
  not a required compute provider; a dedicated self-hosted runner or external CI
  may execute the protected validator only if the fixed exact-head GitHub check
  remains authenticated and required. `github pr-status` observes one open PR
  without mutation and distinguishes runner acquisition failure, pending checks,
  validator failure, pending review, governance gaps, and ready state. It names
  the workflow/job/runner labels and never recommends disabling the ruleset for
  capacity recovery. Control and untrusted-source runners have separate trust
  domains. An Agent uses a proposal branch for every protected code change, but
  one branch/PR covers one reviewable change set rather than one commit. Code may
  carry required documentation; unrelated documentation is split; policy,
  taxonomy, workflow, and runner-trust changes use a Manager-owned control PR.

## REQ-20260806-003 - AnalysisBrief authoring exposes and aggregates prose rules

- Source: editable-install AnalysisBrief authoring canary, 2026-08-06.
- Maps to: `US-014`, `US-015`, `US-017`, `US-022`, `US-031`.
- Acceptance: AnalysisBrief JSON Schema exposes question, answer, per-item
  interpretation/limitation, and whole-document English/CJK prose limits through
  `x-researchctl-prose`, including explicit field/each-item/document scope.
  `doc contracts` summarizes those limits. After YAML parsing succeeds, brief
  lint and route-aware document check aggregate all detectable schema and prose
  findings, preserving observed and maximum values. YAML parser failures use the
  unambiguous phrase `invalid YAML`, retain exact line/column and parser subtype,
  and do not imply the `protocol` field is invalid. Agent guidance restricts
  numeric-looking scalar quoting to AnalysisBrief `evidence[].values` and
  Markdown `provenance[].value`; it explicitly excludes prose block scalars.

## REQ-20260810-001 - Validated document trees project into replaceable sites

- Source: post-export static document-library and MkDocs/Read the Docs tool
  evaluation, 2026-08-10.
- Maps to: `US-014`, `US-015`, `US-022`, `US-031`.
- Acceptance: `.researchctl-docs.yaml` or managed `DocumentLayoutPolicy` remains
  the only taxonomy and directory authority. `doc site-manifest` runs the full
  tree validator before emitting a strict, deterministic, engine-neutral JSON
  artifact with policy order, publishable Markdown metadata, validity/lifecycle,
  relations, source paths, raw source/content digests, repository identity and
  clean/dirty state, explicit structured-source and legacy exclusions, and a
  self-authenticating canonical digest. The manifest schema is discoverable
  through `doc schema`. An optional MkDocs plugin consumes only the manifest,
  verifies every published/source byte, rejects unlisted Markdown and optionally
  dirty state, filters canonical YAML, derives navigation, and injects display
  metadata. MkDocs remains an optional presentation dependency; hand-maintained
  `nav` cannot become a second taxonomy. Static hosting publishes only accepted
  protected-main builds. Local implementation must not claim that GitHub Pages,
  Read the Docs, or another host has been deployed. A repository can reuse an
  existing source-test runner for strict site validation rather than allocating
  a separate Actions job; presentation/publishing config is manager-owned.

## REQ-20260812-001 - Release review measures coverage, debt, and language fitness

- Source: post-site system stability and implementation review, 2026-08-12.
- Maps to: `US-014`, `US-025`, `US-027`.
- Acceptance: a release review separately verifies the 77-prompt historical
  baseline, later dated requirements, scenario/workflow status, executable test
  evidence, deployment evidence, source quality, structural hotspots, and
  measured performance. A mapped scenario is not reported as implemented.
  Repository CI runs the configured Python linter and tests on the same exact PR
  head, and the accepted lint baseline contains no grandfathered findings.
  Language replacement requires a measured user-facing or compute hotspot that
  cannot be addressed behind an existing port; cold-start cost, local hot-path
  latency, network/runner queue time, and external command time are reported
  separately. Large modules are split incrementally by domain or transaction
  behind the existing public service and SQLite authority, not by introducing a
  second state machine or performing an unmeasured rewrite.

## REQ-20260813-001 - The control plane manages its own project documents

- Source: ResearchAgentOps document-management dogfood and GitHub App deployment
  continuation, 2026-08-13.
- Maps to: `US-014`, `US-015`, `US-021`, `US-022`, `US-031`.
- Acceptance: ResearchAgentOps authors requirements, development design,
  deployment runbooks, status evidence, and architecture decisions through its
  accepted standalone routes and contract-dispatched commands. Migrating one
  legacy document removes exactly its finite exception, refreshes configured
  Agent-guide and generated projections, updates repository references, passes
  baseline-aware tree lint, and appears in the manifest-derived documentation
  site without a hand-maintained MkDocs navigation entry. An ordinary content
  proposal cannot add or remap a route. A policy change required by an
  indivisible legacy migration is isolated to the exact compatibility entry and
  called out for explicit manager/CODEOWNER review.
