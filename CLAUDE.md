<!-- researchctl-agent-guide:project-document-agent-guide.claude.v1:begin -->
## Researchctl Document Workflow

> Renderer: `researchctl-renderer:project-document-agent-guide.claude.v1`

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
   regenerate the paired Markdown; never edit generated Markdown directly.
4. Run `researchctl doc tree --project .` before proposing or committing the
   change. Use `researchctl doc tree --project . --json` when another tool or
   review agent will consume the findings.
5. If `researchctl` or the effective policy is unavailable, stop and report the
   missing prerequisite instead of approximating the checks.

Changes to the document policy are taxonomy changes. They require the
repository's manager/CODEOWNER review and must not be hidden inside a content
proposal. Standalone linting does not require `researchctl init`, a Session,
SQLite, or manager credentials.

### Accepted Routes

| Type | Classification | Contract | Directory | Required relations |
| --- | --- | --- | --- | --- |
| `adr` | `decision/architecture:adr` | `markdown-frontmatter` | `docs/adr` | - |
| `design` | `design/architecture:proposal` | `design-document` | `docs/design` | - |
| `runbook` | `operations/project:runbook` | `markdown-frontmatter` | `docs/runbooks` | - |
| `reference` | `reference/project:document` | `markdown-frontmatter` | `docs/reference` | - |
| `status` | `status/project:snapshot` | `project-status-summary` | `docs/status` | - |

<!-- researchctl-agent-guide:project-document-agent-guide.claude.v1:end -->
