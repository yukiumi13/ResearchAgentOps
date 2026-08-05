---
type: adr
title: Portable project document contracts
owner: person:yl2708
last_updated: 2026-08-04
validity: valid
tags: [documents, lint, ci, governance]
references:
  - kind: repository_path
    location: src/researchctl/services/project_documents.py
  - kind: repository_path
    location: src/researchctl/document_cli.py
  - kind: repository_path
    location: tests/unit/test_project_documents.py
relations:
  supersedes: []
  derived_from: []
  see_also:
    - RESEARCH_CONTROL_PLANE_SPEC.md
    - WORKFLOW_COVERAGE.md
---
# ADR 0014: Portable Project Document Contracts

Status: accepted design, locally verified vertical slice

## Context

Research repositories accumulate human-readable analysis and machine-consumed
artifacts through humans, Agents, scripts, editors, and external tools. A
document taxonomy that exists only in prompts or conventions drifts: new labels
silently create directories, generated Markdown becomes an editable second
source, frozen references change, and prose leaks back into machine data roots.

Requiring `researchctl init` before any validation would make the document
contract unnecessarily coupled to Sessions, SQLite, accepted research records,
and manager credentials. Editors, pre-commit hooks, external Agents, and ordinary
CI need the same deterministic checks without adopting the control plane.

## Decision

Project document validation is a portable static contract with two policy
sources:

```text
standalone repository -> .researchctl-docs.yaml
managed repository    -> .research/policies/default.yaml.document_layout
```

Both modes use the same strict `DocumentLayoutPolicy`, document schemas,
renderers, finding codes, CLI, and JSON output. Standalone commands do not open
`.research`, SQLite, a Session, or a manager context. Managed mode adds authority:
only a manager may prepare `doc.configure-layout`, and protected-base CI permits
that proposal to change only the `document_layout` policy field.

The standalone policy or an explicit `--policy-file` is required. When neither
standalone nor managed policy exists, the CLI fails with
`document_policy_missing`; built-in model defaults cannot silently classify a
real repository.

For first adoption, `doc policy-template` renders a complete strict policy
candidate rather than asking an Agent to invent YAML fields. `doc policy-lint`
validates that file without repository discovery or managed state. The template
remains a proposal: adapting its routes and accepting it as
`.researchctl-docs.yaml` still requires manager/CODEOWNER review.

A route is the exact four-part mapping:

```text
classification + document_type + contract + directory
```

Classifications use canonical `a/b:c` labels. Unknown labels, unknown paths,
overlapping directories, type/path disagreement, excessive depth, missing
relations, unsafe links, invalid frontmatter, stale generated Markdown, and
orphan renders fail closed. A finite `legacy_files` allowlist supports migration
without turning an entire directory into an exemption.

The base label type enforces lowercase slash-separated namespace segments and a
single `:` category. Policy then enforces `classification_depth` on the number
of namespace segments, defaulting to two through four. Filesystem `max_depth`
is separate, bounds nesting below a mapped route, and is constrained to `1..8`.
Both are hard checks and can be adjusted only as explicit policy fields.

An executable policy alone is not discoverable to a newly started standalone
Agent. Projects may therefore declare `agent_guides`, each binding a Markdown
path to a `claude` or `agents` renderer. `researchctl doc agent-guide` inserts or
updates one versioned managed block while preserving unrelated project
instructions. It can write only a configured target. The deterministic block
points to the effective policy, states the no-fallback rule and authority
boundary, gives the required author/render/lint sequence, and renders the
current accepted route table. `doc tree` rejects missing or stale configured
blocks, so policy and Agent instructions cannot silently diverge.

`machine_artifact_roots` are separately configured. Each root has an explicit
extension allowlist and can never permit Markdown. This lets a project retain
stable script paths such as `data/*.json` while enforcing that prose belongs
under the document hierarchy.

Manual Markdown uses strict frontmatter. `validity` is exactly `valid`,
`invalid`, or `frozen`; `invalid` requires a reason. CI may supply an explicit
baseline checkout. Any document already marked `frozen` there must remain at
the same path with identical bytes. The static core accepts directory inputs;
GitHub Actions, another CI system, or an editor integration decides how a
baseline is materialized.

During the first policy-adoption PR, the trusted baseline may not have any
document policy. Only for that exact `document_policy_missing` case, baseline
frozen scanning uses the subject policy's route shape against the old tree. An
invalid, shadowed, or unsafe baseline policy still fails closed. This permits
first adoption without weakening later baseline-policy validation.

Structured YAML is canonical source for design documents, project status
summaries, and analysis briefs. Its Markdown pair is deterministic generated
output and carries a visible versioned renderer marker. A configured generated
index similarly binds the type, classification, schema, and directory table to
the policy.

## Authority And Review

Ordinary document content follows repository CODEOWNERS and branch protection.
Changing `.researchctl-docs.yaml` in standalone mode changes what the linter
accepts, so that file must be manager-owned through CODEOWNERS. In managed mode,
adding a label, directory, contract, generated index, artifact root, or remapping
an existing route requires the manager-only policy proposal. Adding or remapping
an Agent guide target is the same kind of policy change. Tags are descriptive
metadata and never grant path or approval authority.

The linter diagnoses policy compliance; it does not approve a PR. A dedicated
review Agent may consume the JSON findings and inspect semantics, but it remains
read-only and cannot acquire manager authority merely by acting as reviewer.

## Consequences

- An uninitialized Git repository can adopt document lint and CI with one config
  file and CODEOWNERS entry.
- Editors, Agents, pre-commit, and CI can share one deterministic diagnostic
  engine instead of reimplementing the rules.
- A new standalone Claude/Codex session can discover the workflow from its
  ordinary repository instruction file, while CI proves that the managed block
  still describes the effective policy.
- Project-specific taxonomies remain possible, but taxonomy changes are visible
  policy changes rather than an Agent fallback.
- Existing prose can migrate incrementally while machine artifact paths remain
  stable.
- Baseline-free local lint cannot prove frozen immutability; CI must provide a
  trusted baseline checkout for that check.
- The current CLI is the integration surface. A language-server/editor adapter
  may be added later without changing document authority or schema semantics.
- A reusable Skill may teach generic command use and MCP may proxy the same
  diagnostics, but neither is required and neither becomes project authority.

## Verification

Focused tests cover strict route policy, seven project-defined document types,
frontmatter/path agreement, relations, deterministic render pairs and index,
machine artifact separation, frozen baseline comparison, standalone CLI use,
deterministic Agent guide insertion and drift detection, manager-only policy
proposals, protected field-scope replay, CODEOWNERS, and the exact-head source
workflow contract.
