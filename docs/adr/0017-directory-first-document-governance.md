---
type: adr
title: Directory-first document governance
owner: person:yl2708
last_updated: 2026-08-18
validity: valid
tags: [documents, governance, codeowners, migration, mkdocs]
references:
  - kind: repository_path
    location: src/researchctl/services/document_policy.py
  - kind: repository_path
    location: src/researchctl/services/project_documents_v2.py
  - kind: repository_path
    location: src/researchctl/services/agent_guides.py
  - kind: repository_path
    location: src/researchctl/services/simple_document_site.py
  - kind: repository_path
    location: tests/unit/test_project_documents_v2.py
relations:
  supersedes: []
  derived_from:
    - docs/adr/0014-portable-project-document-contracts.md
  see_also:
    - docs/WORKFLOW_COVERAGE.md
    - docs/reference/requirement-ledger.md
---
# ADR 0017: Directory-First Document Governance

Status: accepted design; standalone version 2 implemented, managed migration and
publication pilots pending

## Context

ADR 0014 made document validation portable, deterministic, and init-free, and
that part is not in question here. What dogfooding exposed is the cost of its
authoring surface rather than its enforcement model.

Facts were duplicated at two separate layers, and both cost something.

Inside a document, strict frontmatter restated what the repository already knew.
`type` repeated the directory the file sat in. `owner` repeated CODEOWNERS.
`last_updated` repeated Git. `title` repeated the first heading. Each of those
fields could disagree with its real source, and several of them regularly did,
so the linter spent its authority reconciling a document with itself.

Inside the policy, a route restated the directory a second time as an `a/b:c`
classification label. That label never appeared in ordinary Markdown; it was
route metadata. But it still had to be invented, kept unique, kept within
`classification_depth`, and kept in agreement with the directory it named, and
adding an ordinary prose type meant a manager-owned route carrying that label, a
document type, a contract, a directory, and a written rationale, even when the
repository already had the directory and nothing about the contract changed.
That is the correct amount of ceremony for introducing a schema; it is too much
for deciding that runbooks live in `docs/runbooks`.

Google's internal g3doc model answers the same problem differently: Markdown
lives in ordinary directories, the directory says what the document is, the
`OWNERS` file says who reviews it, version control says when it changed, and the
first heading says what it is called. Nothing is written twice, so nothing can
drift.

## Decision

Version 2 of the document policy is directory-first and is the recommended model
for repositories adopting document governance now. Version 1 remains supported
and is not deprecated by this decision.

The policy file declares which contract it uses with an explicit `version` key.
A file with no `version`, or `version: 1`, is the ADR 0014 route policy. Only
`version: 2` selects the model below, and any other value fails closed.

### The section directory is the primary type

Direct child directories of the document root are sections, and a section
directory is the document type. There is no separate label, no `document_type`
field, and no `a/b:c` classification for ordinary Markdown.

Folders deeper than a section are organization and versioning: grouping by
subject, by component, or by dated and numbered variants of the same kind of
document. They do not create types, and `max_depth` bounds how far they may
nest. Tags remain available and remain secondary; they group documents for
readers and never determine routing, ownership, or approval.

### Ownership, review, and freshness are not document fields

CODEOWNERS is the sole owner and review authority for a version 2 tree. A policy
that sets `ownership.required` refuses a document no CODEOWNERS rule matches, so
an unowned document is a lint failure rather than a `person:` string an author
typed. The last edited date comes from Git history for that path. A repository
with no commits reports absent history instead of inventing a date, and a Git
state broken enough to be unreadable fails closed rather than reporting every
document as never edited.

The title is the first level-one heading in the file.

### Frontmatter is optional and small

An ordinary document satisfies the `simple-markdown-frontmatter` contract:
Markdown, with an optional YAML block that has no required fields. A document
with no frontmatter at all is valid.

When present, the block accepts only `status`, `tags`, `reviewed_on`, `locked`,
`depends_on`, and `superseded_by`. Each of the version 1 keys the model no longer
accepts is diagnosed with the replacement that supersedes it, so an author
migrating a file is told to delete `owner` rather than merely that it is
forbidden.

`depends_on` and `superseded_by` are deliberately lightweight lineage. They are
repository-root-relative paths that the linter resolves, not a relation graph
with required edges per type. `locked` is the immutability marker, and the
baseline reader is version-blind: it recognizes both `locked: true` and the
version 1 `validity: frozen` in a protected base, so a policy upgrade in the same
change set cannot release a document the base had immobilized.

### Structured YAML stays opt-in, per section

A section may declare one structured contract. Inside such a section the
canonical `.yaml` source is a direct child, its same-stem generated Markdown is
renderer output that is never hand-edited, and ordinary Markdown remains legal
beside them. A section that declares nothing holds only ordinary Markdown, which
still satisfies `simple-markdown-frontmatter`; what it needs is no structured
YAML contract, no canonical source, and no renderer. Classification survives
only where a structured envelope genuinely has that field, as a policy-level
value to compare against, and never as a taxonomy for prose.

### There is no formatter for ordinary Markdown

RCP validates ordinary Markdown and never rewrites it. The only Markdown bytes
the tool owns are renderer output beside a canonical YAML source and the managed
block inside a configured Agent guide. Wrapping, heading style, table alignment,
and prose formatting belong to the author and to whatever formatter the project
already runs.

### Enforcement is CI; presentation is MkDocs

`doc policy-lint`, `doc tree`, and a strict site build from an ephemeral
manifest are the checks. They run locally, in an editor, or in any CI without
`researchctl init`, a Session, SQLite, or manager credentials.

The version 2 manifest is a closed enumeration: every published page, every
static asset it references, and every deliberate exclusion is listed, so the
MkDocs adapter can reject anything in the document root that the validated tree
did not produce. MkDocs owns Markdown-to-HTML, navigation rendering, search, and
themes, and owns no part of the taxonomy. Hand-written `nav` remains forbidden
as a second directory truth.

### An Agent branch is still only a proposal

Nothing in this ADR changes the authority boundary of ADR 0014 or the proposal
boundary of ADR 0016. An Agent may author documents and push them to a proposal
branch. Repository CI, CODEOWNER review, and a protected merge decide
acceptance, and a passing document lint is not acceptance. Changing the policy,
the set of sections, a section's structured contract, CODEOWNERS, or a managed
guide block is a governance change that cannot ride inside a content proposal.

## Compatibility With Route Policies

A version 1 repository keeps working unchanged. Its routes, `a/b:c`
classifications, strict frontmatter, generated index, machine artifact roots,
legacy allowlist, and frozen-baseline enforcement behave exactly as ADR 0014
specified, and `doc policy-template --policy-version 1` still renders the
original candidate.

Both versions share one CLI, one finding-code vocabulary, one JSON envelope
shape, and one Agent-guide marker identity. The vocabulary is shared, not the
code list: findings keep the same kind/code/path/message shape and the same
`document_*` naming, and many codes are common, but each version also raises
codes only its own model can produce. Because the markers are shared and only the
renderer id differs, raising a policy from version 1 to version 2 replaces the
same managed block in `CLAUDE.md` or `AGENTS.md` rather than leaving two behind.

Adoption is not a bulk rewrite. A version 2 policy template ships with an empty
`sections` list and intentionally fails `doc policy-lint` until an adopter
inventories the repository and writes down the directories it actually has.
Directory names are facts about a specific project, and a template that linted
while empty would invite copying another repository's layout.

## Current Limitation

Version 2 is standalone-policy-first. A `.researchctl-docs.yaml` declaring
`version: 2` is fully supported end to end. A managed repository's
`.research/policies/default.yaml.document_layout` is still read as a version 1
route policy, and manager-only `doc configure-layout` still carries the version 1
shape, so a managed repository cannot yet adopt the directory-first model.
Extending the managed policy, its proposal path, and its protected field-scope
replay to version 2 is a separate manager-owned migration and is deliberately not
bundled here.

`doc index` is also not implemented for version 2 and fails closed with an
explicit unsupported-command error, because the generated index table is a
projection of the route taxonomy this model does not have. Its replacement, if
one is wanted, is the site manifest rather than a tracked Markdown table.

ResearchAgentOps itself still runs on its version 1 policy. Migrating this
repository is a later proposal, so every claim here about version 2 rests on the
test suite and on fixture repositories rather than on this tree.

## Consequences

- Adding a document type is still governance. A new section is a change to the
  policy's `sections` list and needs the same manager/CODEOWNER review that a new
  route needed. What disappears is the five-field mapping behind it: the type
  is the directory name, so there is no separate classification label and no
  written rationale to invent and keep in agreement with it. The ordinary
  contract is fixed at `simple-markdown-frontmatter`, so there is no ordinary
  contract to choose either; a section may still explicitly opt into one
  structured contract on top of it.
- Ordinary prose stops restating its own type, owner, date, and title, so those
  facts can no longer disagree with the directory, CODEOWNERS, Git, or the first
  heading.
- Schema strictness is confined to the sections that asked for it, and prose
  sections keep the low authoring cost that makes people write documents.
- Ownership becomes a review fact rather than a metadata string, which also means
  a repository without CODEOWNERS gets weaker guarantees than one with it.
- Two supported policy versions are two code paths to keep honest; the shared
  CLI, finding-code vocabulary, and guide markers are what keep that cost
  bounded.
- Managed repositories gain nothing yet, and the split between standalone and
  managed behaviour is visible until that migration lands.

## Verification

Focused tests cover version discrimination and fail-closed rejection of any other
value, section and depth rules, case-insensitive Markdown handling, canonical
`.yaml` suffix enforcement, CODEOWNERS resolution and required ownership,
Git-derived edit time including unborn and broken repositories, the optional
frontmatter model and its legacy-key diagnostics, lineage resolution, locked
baseline immutability across a version upgrade, structured pair rendering,
contract and schema discovery, the deliberately incomplete version 2 template,
the managed Agent guide including in-place replacement of a version 1 block, the
closed-world site manifest, and a real strict MkDocs build over a version 2
fixture. Version 1 behaviour is pinned by its own unchanged suite.

No static host has been deployed, no managed repository has been migrated, and
no long-lived repository has yet been converted from routes to sections. Those
remain the open pilots.
