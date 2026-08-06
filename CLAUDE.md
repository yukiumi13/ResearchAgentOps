<!-- researchctl-agent-guide:project-document-agent-guide.claude:begin -->
## Researchctl Document Workflow

> Renderer: `researchctl-renderer:project-document-agent-guide.claude.v3`
<!-- researchctl-generated:project-document-agent-guide.claude.v3;source=sha256:ef29e5884c51ca8e2c69813e0fb2748d0ee5c7e06f02c1be8f9af0726391d5f9;body=sha256:9d071451ffd8f554f7d245cba1e9ddb00705c2bcdba721f5df6c577f72d8fa76 -->

Claude must treat the repository's effective document policy as the only
authority for document classifications, contracts, and paths. Standalone
repositories use `.researchctl-docs.yaml`; managed repositories use
`.research/policies/default.yaml.document_layout`. Never invent a fallback
classification or directory when neither policy exists.
The effective policy also bounds the namespace segments before `:` to
2 through 4; filesystem nesting is governed
separately by route directories and `max_depth`.

When creating, moving, or editing project documentation:

1. Read the effective policy before choosing a path or document type.
2. Select one existing route from the table below. Do not create a new label,
   type, contract, or directory as part of an ordinary document change.
3. For `markdown-frontmatter`, write Markdown with the required strict
   frontmatter. For a structured contract, edit its canonical YAML source and
   keep it tracked beside the same-stem generated Markdown as direct route
   children; never edit generated Markdown directly. The generated marker's
   source digest hashes canonical model JSON, not the YAML file bytes.
   A structured route may preserve project-owned YAML frontmatter around the
   generated body. The project schema owns those field meanings; researchctl
   checks configured key presence and the renderer-owned body independently.
   Quantitative Markdown claims should declare keyed `sources` and a
   `provenance` item whose basis distinguishes measured, estimated, derived,
   or external values. Estimated and derived values require a method.
4. Run `researchctl doc tree --project .` before committing to a proposal
   branch and opening or updating its PR. Every content proposal still requires
   the repository's CI, CODEOWNER review, and protected merge; an Agent-authored
   commit is not acceptance. Use `researchctl doc tree --project . --json`
   when review automation will consume the findings.
5. If `researchctl` or the effective policy is unavailable, stop and report the
   missing prerequisite instead of approximating the checks.

Changes to the document policy are taxonomy changes. They require the
repository's manager/CODEOWNER review and must not be hidden inside a content
proposal. Standalone linting does not require `researchctl init`, a Session,
SQLite, or manager credentials.

Discover a contract before authoring with `researchctl doc contracts` and
`researchctl doc schema --contract CONTRACT`. Start a manual document with
`researchctl doc scaffold --type TYPE --title TITLE`, then validate any routed
source with `researchctl doc check PATH`. Structured sources are regenerated
with `researchctl doc render PATH --output-file PATH.md`. An AnalysisBrief may
also use `researchctl brief lint PATH` and `researchctl brief render PATH`
without a document policy; those standalone commands do not validate route or
tracked source/render placement.

### Accepted Routes

| Type | Classification | Contract | Directory | Rationale | Required relations | Generated frontmatter |
| --- | --- | --- | --- | --- | --- | --- |
| `adr` | `decision/architecture:adr` | `markdown-frontmatter` | `docs/adr` | Existing ADR files record the project's accepted architectural decisions. | - | - |
| `design` | `design/architecture:proposal` | `design-document` | `docs/design` | Existing design specifications need structured options and validation evidence. | - | - |
| `runbook` | `operations/project:runbook` | `markdown-frontmatter` | `docs/runbooks` | Operational procedures require a dedicated command-oriented location. | - | - |
| `reference` | `reference/project:document` | `markdown-frontmatter` | `docs/reference` | Existing governance and requirement documents are long-lived references. | - | - |
| `status` | `status/project:snapshot` | `project-status-summary` | `docs/status` | Existing workflow coverage and assessment documents represent current state. | - | - |

<!-- researchctl-agent-guide:project-document-agent-guide.claude:end -->
