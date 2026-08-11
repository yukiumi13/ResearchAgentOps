---
type: adr
title: Portable project document contracts
owner: person:yl2708
last_updated: 2026-08-06
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
    - docs/RESEARCH_CONTROL_PLANE_SPEC.md
    - docs/WORKFLOW_COVERAGE.md
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

For first adoption, `doc policy-template` renders a complete structural policy
candidate rather than asking an Agent to invent YAML fields. Every route has a
schema-owned `rationale`; template rationales are explicit placeholders that
`doc policy-lint` rejects until backed by existing project artifacts. Validation
does not require repository discovery or managed state. The template remains a
proposal: adapting its routes and accepting it as
`.researchctl-docs.yaml` still requires manager/CODEOWNER review.

A route is the exact five-part mapping:

```text
classification + document_type + contract + directory + rationale
```

The project policy maps a project-specific type to one contract from the closed
built-in set; it is not a schema-definition language. For example, a project may
map `experiment` to `analysis-brief`, but it cannot add, remove, or reinterpret
`AnalysisBrief` fields in route YAML. Changing a built-in contract requires a
versioned researchctl protocol/model, schema, linter, scaffold, renderer, and
test change. Agents may author contract instances but cannot redefine their
accepted shape from a project repository.

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
blocks, so policy and Agent instructions cannot silently diverge. The block also
points to contract/schema discovery, route-specific scaffolding, and unified
check/render dispatch, including AnalysisBrief.

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

The baseline reader is deliberately narrower than current-policy validation.
It reads only the baseline's standalone `root` or managed
`document_layout.root`, validates that repository-relative path, and scans every
Markdown file below it for raw `validity: frozen` frontmatter. It does not parse
the baseline's routes or validate them against the currently installed policy
schema. Therefore adding a required policy field cannot block the PR that
upgrades an older baseline, while the trusted base checkout alone still decides
which bytes were frozen. The subject/PR policy cannot hide a previously frozen
file by changing a route.

During first policy adoption, a baseline with no policy uses the subject root
only as a discovery fallback and may omit that root. A present malformed,
shadowed, symlinked, or unsafe baseline policy fails closed. Malformed
frontmatter inside the baseline root also fails closed, as does a missing root
after policy adoption. This permits schema migration without weakening frozen
document enforcement.

Manual provenance values are exact display strings and must occur verbatim in
the Markdown body. Numeric-looking YAML values must therefore be quoted so
significant digits survive. Every declared source must be used and unknown
source keys are rejected with the offending keys named. Source locations and
document relations are both repository-root relative; the linter diagnoses the
old document-root-relative relation form with its exact replacement.

Human-readable validation errors are part of the Agent interface. Pydantic
failures print every field location and, when source YAML is available, its line
and column; JSON output preserves the same structured details. Remediation names
the real route-aware command, `researchctl doc check PATH`.

AnalysisBrief JSON Schema exposes `x-researchctl-prose` at document scope and on
every budgeted field. Array fields mark limits as `scope: each_item`; the root
declares the whole-brief English and CJK totals. `doc contracts` summarizes the
same extension. Once YAML parses, lint reports all detectable schema and prose
length findings in one result, even when schema construction itself fails. A
YAML scanner/parser error necessarily stops semantic lint because no reliable
field tree exists, but it reports the exact line/column as `invalid YAML` rather
than the ambiguous `invalid protocol YAML` phrase.

Quoting guidance is field-specific. Display-sensitive numeric scalars are
quoted under AnalysisBrief `evidence[].values` and Markdown frontmatter
`provenance[].value`; prose block scalars do not include literal quote characters.

Structured YAML is canonical source for design documents, project status
summaries, and analysis briefs. Its Markdown pair is deterministic generated
output and carries a visible versioned renderer marker plus source/body digests.
An unedited renderer-owned file can be refreshed atomically after source changes;
a body digest mismatch still blocks replacement. The source digest covers the
canonical validated model JSON, not the original YAML bytes; the marker declares
that format so callers do not mistake it for `sha256sum SOURCE.yaml`. Governed
routes keep the canonical YAML and generated Markdown tracked beside each other.
A configured generated index similarly binds the type, classification, schema,
and directory table to the policy.

Rendering is a thin, optional projection, not a general Markdown engine. RCP
owns typed-model validation, prose budgets, deterministic section/table layout,
provenance markers, and byte comparison. It does not own Markdown-to-HTML,
themes, extensions, or arbitrary Markdown parsing. Existing Markdown libraries
solve those downstream concerns but do not replace the project-specific
`typed model -> stable Markdown source` projection. Adding such a dependency
would not remove the contract logic and would enlarge the compatibility surface,
so no general Markdown framework is part of the core.

### Documentation Site Projection

A documentation library is a downstream view of the validated tree, not a new
taxonomy. `researchctl doc site-manifest` therefore runs the complete tree lint
and emits a strict engine-neutral JSON projection. Every publishable Markdown
page carries its policy route order, title, type, classification, contract,
validity or structured lifecycle, relations, exact content digest, and optional
same-stem canonical YAML path plus raw-byte digest. Root and finite legacy pages
remain explicit. Canonical YAML, legacy non-Markdown, and non-Markdown root files
are recorded as exclusions rather than silently disappearing. Repository HEAD,
sanitized remote, clean/dirty state, policy digest, and a canonical manifest
digest bind the build input.

The first optional adapter targets MkDocs Core. It consumes only the manifest,
requires `docs_dir` to match the governed root, verifies every page and
structured-source byte, filters exclusions, rejects Markdown missing from the
manifest, derives navigation from route order, and injects display-only metadata
and immutable forge source links where possible. `require_clean` defaults to
true for publication and may be disabled for a local preview.

MkDocs was selected because it is a mature Python static-site engine with a
small plugin boundary, strict builds, live reload, search, and broad theme and
hosting support. Material for MkDocs is an optional theme rather than a contract.
Read the Docs and GitHub Pages are hosting/deployment choices that can consume
the same strict build. Sphinx is stronger for API cross-reference domains but
adds unnecessary authoring machinery for this policy-routed Markdown corpus;
Docusaurus and VitePress add a separate JavaScript toolchain without replacing
validation. None is allowed to infer or own taxonomy. In particular,
`mkdocs.yml nav` is generated in memory and must not become a hand-maintained
second directory truth.

The manifest is replaceable build output, not accepted repository state. It
should be written to `/tmp` or an ignored build directory. Static publication
must originate from protected `main`; PR preview output is review evidence, not
accepted truth. No hosting deployment is claimed by this local vertical slice.
ResearchAgentOps uses a CODEOWNERS-protected `mkdocs.yml` and appends the strict
build to its existing source-test job, avoiding another runner allocation. That
canary validates presentation only; it neither publishes a preview nor deploys
Pages.

Some repositories already require their own generated-document frontmatter. A
structured route may declare `generated_markdown_frontmatter.required_fields`.
The Markdown target must contain that project-owned envelope before its first
routed render. RCP preserves it byte-for-byte, checks configured key presence,
and validates or refreshes only the renderer-owned body. The project remains
responsible for the fields' schema and semantics. This envelope mode is separate
from the built-in `markdown-frontmatter` contract for manually authored
Markdown; route policy cannot use it to redefine a built-in structured schema.

## Authority And Review

An Agent may author an ordinary document instance and commit it only to a
proposal branch. Every content proposal still follows repository CI,
CODEOWNERS review, and protected merge; authorship or a passing document lint is
not acceptance. Changing `.researchctl-docs.yaml` in standalone mode changes
what the linter accepts, so that file must be manager-owned through CODEOWNERS.
In managed mode, adding a label, directory, contract, generated index, artifact
root, or remapping an existing route requires the manager-only policy proposal.
Adding or remapping
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
- Existing project frontmatter can wrap generated bodies without duplicating or
  weakening RCP's built-in document models.
- RCP remains usable as a schema/lint engine when a project chooses another
  Markdown/HTML presentation stack; only deterministic paired Markdown uses its
  projection functions.
- A validated tree can feed a static site without copying policy into a second
  navigation file; the first MkDocs adapter remains optional and replaceable.
- Baseline-free local lint cannot prove frozen immutability; CI must provide a
  trusted baseline checkout for that check.
- The current CLI is the integration surface. A language-server/editor adapter
  may be added later without changing document authority or schema semantics.
- A reusable Skill may teach generic command use and MCP may proxy the same
  diagnostics, but neither is required and neither becomes project authority.

## Verification

Focused tests cover strict route policy, seven project-defined document types,
frontmatter/path agreement, relations, deterministic render pairs and index,
project-owned frontmatter preservation around generated bodies, machine artifact
separation, frozen baseline comparison, standalone CLI use, deterministic Agent
guide insertion and drift detection, manager-only policy proposals, protected
field-scope replay, legacy-policy baseline migration, malformed baseline
fail-closed behavior, human field/line diagnostics, CODEOWNERS, and the
exact-head source workflow contract.
Additional site tests cover deterministic manifest/digest generation, root,
manual, structured, legacy, invalid, frozen, lifecycle, raw source/content
digests, exclusions, clean/dirty state, CLI/schema discovery, safe manifest
replacement, generated navigation, metadata/source links, drift rejection, and
a real MkDocs Core strict build. The repository source workflow repeats that
strict build from an ephemeral clean manifest without adding another CI job.
