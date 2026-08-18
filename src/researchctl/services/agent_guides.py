"""Managed Agent guides: one marker identity, one set of drift checks.

A guide is a block researchctl owns inside a file a project owns. The markers
that delimit that block are deliberately independent of the renderer that fills
it, so raising a policy from version 1 to version 2 replaces the same block
rather than leaving two of them behind.

Both policy versions answer the same questions about a configured guide -- is
the path safe, does the file exist, can it be read, and does its managed block
still match what the effective policy would render -- so they ask them here,
once. Only the rendering differs.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

from researchctl.domain.models import (
    AgentGuideFormat,
    AgentGuideTarget,
    SimpleDocumentLayoutPolicy,
)
from researchctl.errors import RCPError
from researchctl.repository import safe_repository_path
from researchctl.services.document_findings import DocumentFinding
from researchctl.services.generated_markdown import (
    markdown_code,
    render_generated_markdown,
    renderer_marker,
)

#: The directory-first guide is a different document from the version 1 guide,
#: so it carries its own renderer id even though it replaces the same block.
SIMPLE_AGENT_GUIDE_RENDERER_IDS: dict[AgentGuideFormat, str] = {
    "claude": "simple-document-agent-guide.claude.v1",
    "agents": "simple-document-agent-guide.agents.v1",
}

_MARKER_PREFIX = "<!-- researchctl-agent-guide:"


def agent_guide_markers(guide_format: AgentGuideFormat) -> tuple[str, str]:
    identity = f"project-document-agent-guide.{guide_format}"
    return (
        f"{_MARKER_PREFIX}{identity}:begin -->",
        f"{_MARKER_PREFIX}{identity}:end -->",
    )


def lint_agent_guide_targets(
    repository: Path,
    targets: Sequence[AgentGuideTarget],
    findings: list[DocumentFinding],
    *,
    render: Callable[[AgentGuideFormat], bytes],
) -> int:
    """Check every configured guide against what the policy would render now."""

    checked = 0
    for target in targets:
        try:
            guide_path = safe_repository_path(repository, target.path)
        except RCPError:
            findings.append(
                DocumentFinding(
                    kind="invalid",
                    code="agent_guide_path_invalid",
                    path=target.path,
                    message="Configured agent guide path contains a symbolic link.",
                )
            )
            continue
        if not guide_path.exists():
            findings.append(
                DocumentFinding(
                    kind="invalid",
                    code="agent_guide_missing",
                    path=target.path,
                    message="Configured agent guide is missing.",
                )
            )
            continue
        if guide_path.is_symlink() or not guide_path.is_file():
            findings.append(
                DocumentFinding(
                    kind="invalid",
                    code="agent_guide_path_invalid",
                    path=target.path,
                    message="Configured agent guide must be a regular non-symlink file.",
                )
            )
            continue
        checked += 1
        try:
            observed = guide_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            findings.append(
                DocumentFinding(
                    kind="invalid",
                    code="agent_guide_unreadable",
                    path=target.path,
                    message=f"Configured agent guide cannot be read: {type(error).__name__}.",
                )
            )
            continue
        expected = render(target.format).decode("utf-8")
        if not _block_matches(observed, expected=expected, guide_format=target.format):
            findings.append(
                DocumentFinding(
                    kind="invalid",
                    code="agent_guide_mismatch",
                    path=target.path,
                    message=(
                        "Agent guide is missing its managed block or differs from the "
                        "effective document policy."
                    ),
                )
            )
    return checked


def _block_matches(
    observed: str,
    *,
    expected: str,
    guide_format: AgentGuideFormat,
) -> bool:
    begin, end = agent_guide_markers(guide_format)
    marker_identity = begin.removeprefix(_MARKER_PREFIX).removesuffix(":begin -->")
    marker_prefix = f"{_MARKER_PREFIX}{marker_identity}"
    begin_index = observed.find(begin)
    end_index = observed.find(end)
    if (
        begin_index < 0
        or end_index < begin_index
        or observed.count(begin) != 1
        or observed.count(end) != 1
        or observed.count(marker_prefix) != 2
    ):
        return False
    return observed[begin_index : end_index + len(end)] + "\n" == expected


def render_simple_agent_guide(
    policy: SimpleDocumentLayoutPolicy,
    guide_format: AgentGuideFormat,
) -> bytes:
    """Render the managed block that teaches Agents a directory-first policy."""

    begin, end = agent_guide_markers(guide_format)
    subject = "Claude" if guide_format == "claude" else "Repository agents"
    root = markdown_code(policy.root)
    lines = [
        begin,
        "## Researchctl Document Workflow",
        "",
        renderer_marker(SIMPLE_AGENT_GUIDE_RENDERER_IDS[guide_format]),
        "",
        f"{subject} must treat the repository's effective document policy as the only",
        f"authority for where documentation lives. This project keeps it under {root},",
        "and the standalone policy is `.researchctl-docs.yaml`. Never invent a section,",
        "a contract, or a directory that the policy does not already accept.",
        "",
        "An ordinary document is plain Markdown. Its section directory is its type:",
        "there is no separate label to keep in sync, and no `a/b:c` classification to",
        "write. Its title is the first level-one heading in the file. Its owners come",
        "from CODEOWNERS, which is the only review authority, and the date it was last",
        "edited comes from Git rather than from anything written in the document.",
        "",
        "Frontmatter is optional and a document with none is valid. When present it",
        "accepts only these fields:",
        "",
        "- `status` -- the document's own lifecycle among the accepted values.",
        "- `tags` -- free-form labels for grouping.",
        "- `reviewed_on` -- the date a human last reviewed the content.",
        "- `locked` -- refuse edits until a reviewer unlocks the document.",
        "- `depends_on` -- documents this one relies on.",
        "- `superseded_by` -- the document that replaces this one.",
        "",
        "Never add an owner, a type, or a classification to frontmatter; CODEOWNERS and",
        "the section directory already state them. Every path written in a document,",
        "including every `depends_on` and `superseded_by` target, is a",
        "repository-root-relative path such as `docs/runbooks/evaluation.md`.",
        "",
        "A structured YAML contract is opt-in per section and is listed in the table",
        "below. Inside such a section, edit only the canonical `.yaml` source, which is",
        "a direct child of the section directory. Its same-stem generated Markdown is",
        "renderer output: regenerate it, never edit it by hand.",
        "",
        "Use these commands:",
        "",
        "1. `researchctl doc contracts` and `researchctl doc schema --contract CONTRACT`",
        "   to discover what a structured section accepts.",
        "2. `researchctl doc scaffold --type SECTION --title TITLE` to start a document.",
        "3. `researchctl doc check PATH` to validate one file.",
        "4. `researchctl doc render PATH --output-file PATH.md` to regenerate the",
        "   Markdown beside a canonical YAML source.",
        "5. `researchctl doc tree --project .` to validate the whole tree before opening",
        "   or updating a pull request. Add `--json` when review automation consumes the",
        "   findings.",
        "",
        "Every change an Agent makes is a proposal and nothing more. It requires the",
        "repository's CI, CODEOWNER review, and a protected merge; an Agent-authored",
        "commit is not acceptance.",
        "",
        "Changes to the document policy, to the set of sections, to a section's",
        "structured contract, to CODEOWNERS, or to this managed guide are governance",
        "changes. They need manager/CODEOWNER review of their own and must not be",
        "hidden inside a content proposal. Standalone linting needs none of it:",
        "no `researchctl init`, no Session, no SQLite, and no manager credentials.",
        "",
        "### Accepted Sections",
        "",
        "| Section | Structured contract | Classification compatibility |",
        "| --- | --- | --- |",
    ]
    for section in policy.sections:
        structured = section.structured
        lines.append(
            "| "
            + " | ".join(
                (
                    markdown_code(section.path),
                    markdown_code(structured.contract) if structured is not None else "-",
                    (
                        markdown_code(structured.classification)
                        if structured is not None and structured.classification is not None
                        else "-"
                    ),
                )
            )
            + " |"
        )
    root_pages = ", ".join(markdown_code(page) for page in policy.root_pages) or "none"
    ownership = (
        f"CODEOWNERS, {'required' if policy.ownership.required else 'optional'}"
        if policy.ownership is not None
        else "not configured"
    )
    lines.extend(
        [
            "",
            f"Directory depth below a section: at most {policy.max_depth}. "
            f"Accepted root pages: {root_pages}. Ownership: {ownership}.",
            "",
            end,
        ]
    )
    return render_generated_markdown(
        lines,
        renderer_id=SIMPLE_AGENT_GUIDE_RENDERER_IDS[guide_format],
        source=policy,
    )
