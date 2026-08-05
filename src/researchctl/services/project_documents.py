from __future__ import annotations

import html
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import ValidationError

from researchctl.domain.models import (
    AgentGuideFormat,
    AgentGuideTarget,
    AnalysisBrief,
    DesignDocument,
    DocumentLayoutPolicy,
    DocumentRoute,
    MarkdownFrontmatter,
    ProjectStatusSummary,
)
from researchctl.errors import RCPError
from researchctl.repository import safe_repository_path
from researchctl.serialization import SerializationError, dump_yaml, load_yaml


DESIGN_DOCUMENT_RENDERER_ID = "research-design-document.v1"
PROJECT_STATUS_RENDERER_ID = "project-status-summary.v1"
DOCUMENT_INDEX_RENDERER_ID = "project-document-index.v1"
PROJECT_AGENT_GUIDE_RENDERER_IDS: dict[AgentGuideFormat, str] = {
    "claude": "project-document-agent-guide.claude.v1",
    "agents": "project-document-agent-guide.agents.v1",
}

ProjectDocument = DesignDocument | ProjectStatusSummary
StructuredDocument = ProjectDocument | AnalysisBrief
ProjectDocumentKind = Literal["design_document", "project_status_summary"]


@dataclass(frozen=True, slots=True)
class DocumentFinding:
    kind: Literal["warning", "invalid"]
    code: str
    path: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "code": self.code,
            "path": self.path,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class DocumentLintResult:
    document_kind: ProjectDocumentKind
    document_id: str
    classification: str
    findings: tuple[DocumentFinding, ...]

    @property
    def passed(self) -> bool:
        return not any(finding.kind == "invalid" for finding in self.findings)

    @property
    def terminal_result(self) -> Literal["passed", "invalid"]:
        return "passed" if self.passed else "invalid"

    def as_dict(self) -> dict[str, object]:
        return {
            "document_kind": self.document_kind,
            "document_id": self.document_id,
            "classification": self.classification,
            "terminal_result": self.terminal_result,
            "findings": [finding.as_dict() for finding in self.findings],
        }


@dataclass(frozen=True, slots=True)
class DocumentTreeLintResult:
    root: str
    checked_files: int
    structured_documents: int
    findings: tuple[DocumentFinding, ...]

    @property
    def passed(self) -> bool:
        return not any(finding.kind == "invalid" for finding in self.findings)

    @property
    def terminal_result(self) -> Literal["passed", "invalid"]:
        return "passed" if self.passed else "invalid"

    def as_dict(self) -> dict[str, object]:
        return {
            "root": self.root,
            "checked_files": self.checked_files,
            "structured_documents": self.structured_documents,
            "terminal_result": self.terminal_result,
            "findings": [finding.as_dict() for finding in self.findings],
        }


def load_project_document(path: Path) -> ProjectDocument:
    if path.is_symlink() or not path.is_file():
        raise RCPError(
            code="document_source_invalid",
            message="Document source must be an existing non-symlink regular file.",
            context={"path": str(path)},
        )
    payload = load_yaml(path.read_text(encoding="utf-8"))
    kind = payload.get("document_kind")
    model_type: type[DesignDocument] | type[ProjectStatusSummary]
    if kind == "design_document":
        model_type = DesignDocument
    elif kind == "project_status_summary":
        model_type = ProjectStatusSummary
    else:
        raise RCPError(
            code="document_kind_unsupported",
            message="Structured document_kind is missing or unsupported.",
            context={"path": str(path), "document_kind": kind},
        )
    return model_type.model_validate(payload)


def lint_project_document(
    document: ProjectDocument,
    *,
    policy: DocumentLayoutPolicy | None = None,
) -> DocumentLintResult:
    findings: list[DocumentFinding] = []
    if document.authored_by.role == "external_agent" and document.status == "accepted":
        findings.append(
            DocumentFinding(
                kind="invalid",
                code="external_agent_acceptance_forbidden",
                path="acceptance",
                message="An external Agent cannot author an accepted document.",
            )
        )
    if policy is not None:
        route = policy.route_for_label(document.classification)
        if route is None:
            findings.append(
                DocumentFinding(
                    kind="invalid",
                    code="document_classification_unaccepted",
                    path="classification",
                    message="Document classification is not accepted by project policy.",
                )
            )
        expected_schema = (
            "design-document"
            if document.document_kind == "design_document"
            else "project-status-summary"
        )
        if route is not None and route.contract != expected_schema:
            findings.append(
                DocumentFinding(
                    kind="invalid",
                    code="document_route_schema_mismatch",
                    path="document_kind",
                    message="Document kind does not match its accepted route schema.",
                )
            )
    return DocumentLintResult(
        document_kind=document.document_kind,
        document_id=document.document_id,
        classification=document.classification,
        findings=tuple(findings),
    )


def _text(value: object) -> str:
    rendered = html.escape(str(value), quote=True).replace("\r\n", "\n").replace("\r", "\n")
    for character in ("\\", "`", "*", "_", "[", "]", "#", "|"):
        rendered = rendered.replace(character, f"\\{character}")
    return "<br>".join(rendered.split("\n"))


def _code(value: object) -> str:
    rendered = str(value).replace("\r", " ").replace("\n", " ")
    delimiter = "`" if "`" not in rendered else "``"
    return f"{delimiter}{rendered}{delimiter}"


def _visible_marker(renderer_id: str) -> str:
    return f"> Renderer: {_code(f'researchctl-renderer:{renderer_id}')}"


def render_document_index(policy: DocumentLayoutPolicy) -> bytes:
    lines = [
        "# Documentation",
        "",
        _visible_marker(DOCUMENT_INDEX_RENDERER_ID),
        "",
        "| Type | Classification | Contract | Directory | Required relations |",
        "| --- | --- | --- | --- | --- |",
    ]
    for route in policy.routes:
        relations = ", ".join(route.required_relations) or "-"
        lines.append(
            "| "
            + " | ".join(
                (
                    _code(route.document_type),
                    _code(route.classification),
                    _code(route.contract),
                    _code(route.directory),
                    _text(relations),
                )
            )
            + " |"
        )
    return ("\n".join(lines).rstrip() + "\n").encode("utf-8")


def standalone_document_policy_template(
    guide_format: AgentGuideFormat,
) -> DocumentLayoutPolicy:
    guide_path = "CLAUDE.md" if guide_format == "claude" else "AGENTS.md"
    return DocumentLayoutPolicy(
        generated_index="docs/README.md",
        agent_guides=(AgentGuideTarget(path=guide_path, format=guide_format),),
    )


def render_standalone_document_policy_template(
    guide_format: AgentGuideFormat,
) -> bytes:
    header = (
        "# researchctl standalone document policy candidate\n"
        "# Customize routes for this project; manager/CODEOWNER review is required.\n"
    )
    return (
        header + dump_yaml(standalone_document_policy_template(guide_format))
    ).encode("utf-8")


def agent_guide_markers(guide_format: AgentGuideFormat) -> tuple[str, str]:
    renderer_id = PROJECT_AGENT_GUIDE_RENDERER_IDS[guide_format]
    return (
        f"<!-- researchctl-agent-guide:{renderer_id}:begin -->",
        f"<!-- researchctl-agent-guide:{renderer_id}:end -->",
    )


def render_project_agent_guide(
    policy: DocumentLayoutPolicy,
    guide_format: AgentGuideFormat,
) -> bytes:
    begin, end = agent_guide_markers(guide_format)
    subject = "Claude" if guide_format == "claude" else "Repository agents"
    lines = [
        begin,
        "## Researchctl Document Workflow",
        "",
        _visible_marker(PROJECT_AGENT_GUIDE_RENDERER_IDS[guide_format]),
        "",
        f"{subject} must treat the repository's effective document policy as the only",
        "authority for document classifications, contracts, and paths. Standalone",
        "repositories use `.researchctl-docs.yaml`; managed repositories use",
        "`.research/policies/default.yaml.document_layout`. Never invent a fallback",
        "classification or directory when neither policy exists.",
        "The effective policy also bounds the namespace segments before `:` to",
        (
            f"{policy.classification_depth.minimum} through "
            f"{policy.classification_depth.maximum}; filesystem nesting is governed"
        ),
        "separately by route directories and `max_depth`.",
        "",
        "When creating, moving, or editing project documentation:",
        "",
        "1. Read the effective policy before choosing a path or document type.",
        "2. Select one existing route from the table below. Do not create a new label,",
        "   type, contract, or directory as part of an ordinary document change.",
        "3. For `markdown-frontmatter`, write Markdown with the required strict",
        "   frontmatter. For a structured contract, edit its canonical YAML source and",
        "   regenerate the paired Markdown; never edit generated Markdown directly.",
        "4. Run `researchctl doc tree --project .` before proposing or committing the",
        "   change. Use `researchctl doc tree --project . --json` when another tool or",
        "   review agent will consume the findings.",
        "5. If `researchctl` or the effective policy is unavailable, stop and report the",
        "   missing prerequisite instead of approximating the checks.",
        "",
        "Changes to the document policy are taxonomy changes. They require the",
        "repository's manager/CODEOWNER review and must not be hidden inside a content",
        "proposal. Standalone linting does not require `researchctl init`, a Session,",
        "SQLite, or manager credentials.",
        "",
        "### Accepted Routes",
        "",
        "| Type | Classification | Contract | Directory | Required relations |",
        "| --- | --- | --- | --- | --- |",
    ]
    for route in policy.routes:
        relations = ", ".join(route.required_relations) or "-"
        lines.append(
            "| "
            + " | ".join(
                (
                    _code(route.document_type),
                    _code(route.classification),
                    _code(route.contract),
                    _code(route.directory),
                    _text(relations),
                )
            )
            + " |"
        )
    lines.extend(["", end])
    return ("\n".join(lines).rstrip() + "\n").encode("utf-8")


def _metadata(document: ProjectDocument) -> list[str]:
    values = [
        f"- Document: {_code(document.document_id)}",
        f"- Classification: {_code(document.classification)}",
        f"- Status: {_code(document.status)}",
        f"- Revision: `{document.revision}`",
        f"- Basis commit: {_code(document.basis_commit)}",
        f"- Author: {_code(document.authored_by.actor_id)} ({_code(document.authored_by.role)})",
        f"- Updated: {_code(document.updated_at.isoformat())}",
    ]
    if document.authored_by.session_id is not None:
        values.append(f"- Session: {_code(document.authored_by.session_id)}")
    if document.supersedes is not None:
        values.append(f"- Supersedes: {_code(document.supersedes)}")
    return values


def _bullet_section(lines: list[str], title: str, values: tuple[str, ...]) -> None:
    lines.extend(["", f"## {title}", ""])
    lines.extend(f"- {_text(value)}" for value in values)


def render_design_document(document: DesignDocument) -> bytes:
    result = lint_project_document(document)
    if not result.passed:
        raise RCPError(
            code="design_document_lint_invalid",
            message="Design document does not satisfy the document contract.",
            context=result.as_dict(),
        )
    lines = [
        f"# {_text(document.title)}",
        "",
        _visible_marker(DESIGN_DOCUMENT_RENDERER_ID),
        "",
        *_metadata(document),
        "",
        "## Problem",
        "",
        _text(document.problem),
        "",
        "## Context",
        "",
        _text(document.context),
    ]
    _bullet_section(lines, "Goals", document.goals)
    _bullet_section(lines, "Non-Goals", document.non_goals)
    _bullet_section(lines, "Constraints", document.constraints)
    lines.extend(["", "## Options", ""])
    for option in document.options:
        lines.extend(
            [
                f"### {_text(option.summary)}",
                "",
                f"Disposition: {_code(option.disposition)}",
                "",
                f"Rationale: {_text(option.rationale)}",
                "",
                "Benefits:",
                *(f"- {_text(value)}" for value in option.benefits),
                "",
                "Drawbacks:",
                *(f"- {_text(value)}" for value in option.drawbacks),
                "",
            ]
        )
    lines.extend(["## Components", ""])
    for component in document.components:
        lines.append(f"### {_code(component.key)}")
        lines.extend(["", _text(component.responsibility)])
        if component.interfaces:
            lines.extend(["", "Interfaces:"])
            lines.extend(f"- {_text(value)}" for value in component.interfaces)
        lines.append("")
    lines.extend(["## Workflows", ""])
    for workflow in document.workflows:
        lines.extend([f"### {_text(workflow.name)}", ""])
        lines.extend(
            f"{index}. {_text(step)}"
            for index, step in enumerate(workflow.steps, start=1)
        )
        lines.append("")
    _bullet_section(lines, "Security", document.security_considerations)
    lines.extend(["", "## Failure Modes", ""])
    for failure in document.failure_modes:
        lines.extend(
            [
                f"- **Condition:** {_text(failure.condition)}",
                f"  **Behavior:** {_text(failure.behavior)}",
                f"  **Recovery:** {_text(failure.recovery)}",
            ]
        )
    _bullet_section(lines, "Migration", document.migration_steps)
    lines.extend(["", "## Validation", ""])
    for case in document.validation:
        lines.extend(
            [
                f"- **Case:** {_text(case.case)}",
                f"  **Expected:** {_text(case.expected)}",
                f"  **Evidence:** {_text(case.evidence)}",
            ]
        )
    if document.open_questions:
        _bullet_section(lines, "Open Questions", document.open_questions)
    if document.decision_requests:
        lines.extend(["", "## Decisions Needed", ""])
        for request in document.decision_requests:
            lines.append(f"- {_text(request.question)}")
            lines.extend(f"  - {_text(option)}" for option in request.options)
    lines.extend(["", "## Sources", ""])
    if document.sources:
        lines.extend(
            f"- {_code(source.key)}: {_code(source.location)}"
            + (f" ({_code(source.digest)})" if source.digest is not None else "")
            for source in document.sources
        )
    else:
        lines.append("- None declared.")
    return ("\n".join(lines).rstrip() + "\n").encode("utf-8")


def render_project_status_summary(document: ProjectStatusSummary) -> bytes:
    result = lint_project_document(document)
    if not result.passed:
        raise RCPError(
            code="project_status_summary_lint_invalid",
            message="Project status summary does not satisfy the document contract.",
            context=result.as_dict(),
        )
    lines = [
        f"# {_text(document.title)}",
        "",
        _visible_marker(PROJECT_STATUS_RENDERER_ID),
        "",
        *_metadata(document),
        f"- As of: {_code(document.as_of.isoformat())}",
        "",
        "## Summary",
        "",
        _text(document.executive_summary),
        "",
        "## Capabilities",
        "",
        "| Capability | Status | Current behavior | Evidence | Missing |",
        "| --- | --- | --- | --- | --- |",
    ]
    for capability in document.capabilities:
        lines.append(
            "| "
            + " | ".join(
                (
                    _text(capability.title),
                    _code(capability.status),
                    _text(capability.summary),
                    ", ".join(_code(key) for key in capability.evidence_keys),
                    "<br>".join(_text(value) for value in capability.missing) or "-",
                )
            )
            + " |"
        )
    if document.active_work:
        lines.extend(["", "## Active Work", ""])
        lines.extend(
            f"- **{_text(item.summary)}** ({_code(item.state)}, owner {_code(item.owner)}): "
            f"{_text(item.next_action)}"
            for item in document.active_work
        )
    if document.risks:
        lines.extend(["", "## Risks", ""])
        lines.extend(
            f"- **{_code(risk.severity)}:** {_text(risk.risk)} "
            f"Mitigation: {_text(risk.mitigation)}"
            for risk in document.risks
        )
    if document.decisions_needed:
        lines.extend(["", "## Decisions Needed", ""])
        for request in document.decisions_needed:
            lines.append(f"- {_text(request.question)}")
            lines.extend(f"  - {_text(option)}" for option in request.options)
    _bullet_section(lines, "Next Steps", document.next_steps)
    lines.extend(["", "## Evidence", ""])
    lines.extend(
        f"- {_code(source.key)} ({_code(source.kind)}): {_code(source.location)}"
        + (f" ({_code(source.digest)})" if source.digest is not None else "")
        for source in document.sources
    )
    return ("\n".join(lines).rstrip() + "\n").encode("utf-8")


def render_project_document(document: ProjectDocument) -> bytes:
    if isinstance(document, DesignDocument):
        return render_design_document(document)
    return render_project_status_summary(document)


def load_markdown_frontmatter(content: str, *, path: str) -> tuple[MarkdownFrontmatter, str]:
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.startswith("---\n"):
        raise RCPError(
            code="document_frontmatter_missing",
            message="Markdown document must start with YAML frontmatter.",
            context={"path": path},
        )
    marker = normalized.find("\n---\n", 4)
    if marker < 0:
        raise RCPError(
            code="document_frontmatter_unterminated",
            message="Markdown YAML frontmatter has no closing delimiter.",
            context={"path": path},
        )
    payload = load_yaml(normalized[4:marker])
    body = normalized[marker + 5 :]
    if not body.strip():
        raise RCPError(
            code="document_body_empty",
            message="Markdown document body must not be empty.",
            context={"path": path},
        )
    return MarkdownFrontmatter.model_validate(payload), body


def _frontmatter_relation_values(frontmatter: MarkdownFrontmatter, kind: str) -> tuple[str, ...]:
    return getattr(frontmatter.relations, kind)


def _is_within(path: str, directory: str) -> bool:
    path_parts = PurePosixPath(path).parts
    directory_parts = PurePosixPath(directory).parts
    return path_parts[: len(directory_parts)] == directory_parts and len(path_parts) > len(
        directory_parts
    )


def _collect_files(root: Path, findings: list[DocumentFinding]) -> list[Path]:
    collected: list[Path] = []

    def walk(directory: Path) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as error:
            findings.append(
                DocumentFinding(
                    kind="invalid",
                    code="document_directory_unreadable",
                    path=str(directory),
                    message=f"Document directory cannot be read: {type(error).__name__}.",
                )
            )
            return
        for entry in entries:
            candidate = Path(entry.path)
            if entry.is_symlink():
                findings.append(
                    DocumentFinding(
                        kind="invalid",
                        code="document_symlink_forbidden",
                        path=str(candidate),
                        message="Document trees cannot contain symbolic links.",
                    )
                )
            elif entry.is_dir(follow_symlinks=False):
                walk(candidate)
            elif entry.is_file(follow_symlinks=False):
                collected.append(candidate)
            else:
                findings.append(
                    DocumentFinding(
                        kind="invalid",
                        code="document_special_file_forbidden",
                        path=str(candidate),
                        message="Document trees may contain only directories and regular files.",
                    )
                )

    walk(root)
    return collected


def _route_for_path(policy: DocumentLayoutPolicy, relative: str) -> DocumentRoute | None:
    return next(
        (route for route in policy.routes if _is_within(relative, route.directory)),
        None,
    )


def _lint_agent_guides(
    repository: Path,
    policy: DocumentLayoutPolicy,
    findings: list[DocumentFinding],
) -> int:
    checked = 0
    for target in policy.agent_guides:
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
        expected = render_project_agent_guide(policy, target.format).decode("utf-8")
        begin, end = agent_guide_markers(target.format)
        begin_index = observed.find(begin)
        end_index = observed.find(end)
        if (
            begin_index < 0
            or end_index < begin_index
            or observed.count(begin) != 1
            or observed.count(end) != 1
        ):
            matches = False
        else:
            observed_block = observed[begin_index : end_index + len(end)] + "\n"
            matches = observed_block == expected
        if not matches:
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


def lint_document_tree(
    repository_root: Path,
    policy: DocumentLayoutPolicy,
    *,
    baseline_root: Path | None = None,
    baseline_policy: DocumentLayoutPolicy | None = None,
) -> DocumentTreeLintResult:
    repository = Path(os.path.abspath(os.fspath(repository_root)))
    findings: list[DocumentFinding] = []
    if repository.is_symlink() or not repository.is_dir():
        raise RCPError(
            code="document_repository_invalid",
            message="Document repository root must be an existing non-symlink directory.",
        )
    document_root = repository / policy.root
    if document_root.is_symlink() or not document_root.is_dir():
        return DocumentTreeLintResult(
            root=policy.root,
            checked_files=0,
            structured_documents=0,
            findings=(
                DocumentFinding(
                    kind="invalid",
                    code="document_root_missing",
                    path=policy.root,
                    message="Configured document root is missing or not a regular directory.",
                ),
            ),
        )

    files = _collect_files(document_root, findings)
    agent_guide_count = _lint_agent_guides(repository, policy, findings)
    root_files = set(policy.root_files)
    legacy = {item.path: item for item in policy.legacy_files}
    structured_sources: dict[str, tuple[StructuredDocument, str]] = {}
    generated_paths: set[str] = set()
    manual_documents: dict[str, MarkdownFrontmatter] = {}

    existing_paths = {
        file_path.relative_to(repository).as_posix() for file_path in files
    }
    for root_file in sorted(root_files - existing_paths):
        findings.append(
            DocumentFinding(
                kind="invalid",
                code="document_root_file_missing",
                path=root_file,
                message="Configured document root file is missing.",
            )
        )
    if policy.generated_index is not None and policy.generated_index in existing_paths:
        try:
            observed_index = (repository / policy.generated_index).read_bytes()
        except OSError as error:
            findings.append(
                DocumentFinding(
                    kind="invalid",
                    code="document_index_unreadable",
                    path=policy.generated_index,
                    message=f"Generated document index cannot be read: {type(error).__name__}.",
                )
            )
        else:
            if observed_index != render_document_index(policy):
                findings.append(
                    DocumentFinding(
                        kind="invalid",
                        code="document_index_mismatch",
                        path=policy.generated_index,
                        message="Document index differs from the configured classification routes.",
                    )
                )

    for file_path in files:
        relative = file_path.relative_to(repository).as_posix()
        if relative in root_files:
            continue
        legacy_entry = legacy.get(relative)
        if legacy_entry is not None:
            findings.append(
                DocumentFinding(
                    kind="warning",
                    code="legacy_document_needs_migration",
                    path=relative,
                    message=f"Legacy document must migrate to {legacy_entry.migration_target}.",
                )
            )
            continue
        route = _route_for_path(policy, relative)
        if route is None:
            findings.append(
                DocumentFinding(
                    kind="invalid",
                    code="document_path_unclassified",
                    path=relative,
                    message="Document path is not covered by an accepted classification route.",
                )
            )
            continue
        nested = PurePosixPath(relative).relative_to(route.directory)
        directory_depth = len(nested.parts) - 1
        if directory_depth > policy.max_depth:
            findings.append(
                DocumentFinding(
                    kind="invalid",
                    code="document_path_too_deep",
                    path=relative,
                    message="Document path exceeds the configured maximum directory depth.",
                )
            )
            continue
        if route.contract == "markdown-frontmatter":
            if file_path.suffix.lower() != ".md":
                findings.append(
                    DocumentFinding(
                        kind="invalid",
                        code="document_extension_invalid",
                        path=relative,
                        message="Markdown document routes accept only .md files.",
                    )
                )
                continue
            try:
                frontmatter, _body = load_markdown_frontmatter(
                    file_path.read_text(encoding="utf-8"),
                    path=relative,
                )
            except (
                OSError,
                UnicodeError,
                SerializationError,
                ValidationError,
                RCPError,
            ) as error:
                findings.append(
                    DocumentFinding(
                        kind="invalid",
                        code="document_frontmatter_invalid",
                        path=relative,
                        message=f"Markdown frontmatter failed validation: {type(error).__name__}.",
                    )
                )
                continue
            if frontmatter.type != route.document_type:
                findings.append(
                    DocumentFinding(
                        kind="invalid",
                        code="document_type_path_mismatch",
                        path=relative,
                        message=(
                            f"Frontmatter type {frontmatter.type} does not match "
                            f"route type {route.document_type}."
                        ),
                    )
                )
            for relation in route.required_relations:
                if not _frontmatter_relation_values(frontmatter, relation):
                    findings.append(
                        DocumentFinding(
                            kind="invalid",
                            code="document_required_relation_missing",
                            path=f"{relative}:relations.{relation}",
                            message=f"Route requires a non-empty {relation} relation.",
                        )
                    )
            for reference in frontmatter.references:
                if reference.kind == "external" and reference.location.startswith("/"):
                    findings.append(
                        DocumentFinding(
                            kind="warning",
                            code="document_reference_host_absolute",
                            path=relative,
                            message=(
                                "Host-absolute external reference is not portable; "
                                "prefer a logical URI and immutable digest."
                            ),
                        )
                    )
            manual_documents[relative] = frontmatter
            continue
        if directory_depth != 0:
            findings.append(
                DocumentFinding(
                    kind="invalid",
                    code="structured_document_path_noncanonical",
                    path=relative,
                    message=(
                        "Structured document source/render pairs must be direct "
                        "route children."
                    ),
                )
            )
            continue
        if file_path.suffix.lower() == ".md":
            generated_paths.add(relative)
            continue
        if file_path.suffix.lower() != ".yaml":
            findings.append(
                DocumentFinding(
                    kind="invalid",
                    code="document_extension_invalid",
                    path=relative,
                    message="Structured document routes accept only .yaml and generated .md files.",
                )
            )
            continue
        try:
            if route.contract == "analysis-brief":
                from researchctl.serialization import load_model

                document: StructuredDocument = load_model(file_path, AnalysisBrief)
            else:
                document = load_project_document(file_path)
        except (OSError, UnicodeError, SerializationError, ValidationError, RCPError) as error:
            findings.append(
                DocumentFinding(
                    kind="invalid",
                    code="structured_document_invalid",
                    path=relative,
                    message=f"Structured document failed validation: {type(error).__name__}.",
                )
            )
            continue
        if isinstance(document, (DesignDocument, ProjectStatusSummary)):
            lint = lint_project_document(document, policy=policy)
            findings.extend(
                DocumentFinding(
                    kind=finding.kind,
                    code=finding.code,
                    path=f"{relative}:{finding.path}",
                    message=finding.message,
                )
                for finding in lint.findings
            )
            slug = document.slug
        else:
            slug = file_path.stem
        expected = f"{route.directory}/{slug}.yaml"
        if relative != expected:
            findings.append(
                DocumentFinding(
                    kind="invalid",
                    code="structured_document_path_noncanonical",
                    path=relative,
                    message=f"Document classification and slug require path {expected}.",
                )
            )
        structured_sources[relative] = (document, relative.removesuffix(".yaml") + ".md")

    expected_generated = {generated for _document, generated in structured_sources.values()}
    for source_path, (document, generated_path) in structured_sources.items():
        generated = repository / generated_path
        if generated_path not in generated_paths:
            findings.append(
                DocumentFinding(
                    kind="invalid",
                    code="document_render_missing",
                    path=generated_path,
                    message=f"Structured source {source_path} has no generated Markdown pair.",
                )
            )
            continue
        try:
            observed = generated.read_bytes()
            if isinstance(document, AnalysisBrief):
                from researchctl.services.research_writing import render_analysis_brief

                expected = render_analysis_brief(document)
            else:
                expected = render_project_document(document)
        except OSError as error:
            findings.append(
                DocumentFinding(
                    kind="invalid",
                    code="document_render_unreadable",
                    path=generated_path,
                    message=f"Generated Markdown cannot be read: {type(error).__name__}.",
                )
            )
            continue
        if observed != expected:
            findings.append(
                DocumentFinding(
                    kind="invalid",
                    code="document_render_mismatch",
                    path=generated_path,
                    message="Generated Markdown differs from deterministic renderer output.",
                )
            )
    for orphan in sorted(generated_paths - expected_generated):
        findings.append(
            DocumentFinding(
                kind="invalid",
                code="document_render_orphaned",
                path=orphan,
                message="Generated Markdown has no structured YAML source.",
            )
        )

    for source_path, frontmatter in manual_documents.items():
        for relation_kind in ("supersedes", "derived_from", "see_also"):
            for target in _frontmatter_relation_values(frontmatter, relation_kind):
                target_path = f"{policy.root}/{target}"
                if target_path not in existing_paths:
                    findings.append(
                        DocumentFinding(
                            kind="invalid",
                            code="document_relation_target_missing",
                            path=f"{source_path}:relations.{relation_kind}",
                            message=f"Relation target does not exist: {target_path}.",
                        )
                    )

    artifact_file_count = 0
    for artifact_policy in policy.machine_artifact_roots:
        artifact_root = repository / artifact_policy.directory
        if artifact_root.is_symlink() or not artifact_root.is_dir():
            findings.append(
                DocumentFinding(
                    kind="invalid",
                    code="machine_artifact_root_missing",
                    path=artifact_policy.directory,
                    message=(
                        "Configured machine artifact root is missing or not a regular directory."
                    ),
                )
            )
            continue
        artifact_files = _collect_files(artifact_root, findings)
        artifact_file_count += len(artifact_files)
        allowed_extensions = set(artifact_policy.allowed_extensions)
        for artifact_file in artifact_files:
            relative = artifact_file.relative_to(repository).as_posix()
            extension = artifact_file.suffix.lower()
            if extension not in allowed_extensions:
                findings.append(
                    DocumentFinding(
                        kind="invalid",
                        code="machine_artifact_extension_invalid",
                        path=relative,
                        message=(
                            f"Machine artifact root does not allow extension "
                            f"{extension or '<none>'}."
                        ),
                    )
                )

    if baseline_root is not None:
        baseline_repository = Path(os.path.abspath(os.fspath(baseline_root)))
        selected_baseline_policy = baseline_policy or policy
        _lint_frozen_documents(
            repository,
            baseline_repository,
            selected_baseline_policy,
            findings,
        )

    return DocumentTreeLintResult(
        root=policy.root,
        checked_files=len(files) + artifact_file_count + agent_guide_count,
        structured_documents=len(structured_sources),
        findings=tuple(findings),
    )


def _lint_frozen_documents(
    repository: Path,
    baseline_repository: Path,
    baseline_policy: DocumentLayoutPolicy,
    findings: list[DocumentFinding],
) -> None:
    baseline_document_root = baseline_repository / baseline_policy.root
    if baseline_repository.is_symlink() or not baseline_repository.is_dir():
        raise RCPError(
            code="document_baseline_invalid",
            message="Document baseline must be an existing non-symlink directory.",
        )
    if baseline_document_root.is_symlink() or not baseline_document_root.is_dir():
        findings.append(
            DocumentFinding(
                kind="invalid",
                code="document_baseline_root_missing",
                path=baseline_policy.root,
                message="Baseline document root is missing or not a regular directory.",
            )
        )
        return

    baseline_findings: list[DocumentFinding] = []
    baseline_files = _collect_files(baseline_document_root, baseline_findings)
    for finding in baseline_findings:
        findings.append(
            DocumentFinding(
                kind="invalid",
                code="document_baseline_invalid",
                path=finding.path,
                message=f"Baseline cannot be inspected safely: {finding.message}",
            )
        )
    for baseline_file in baseline_files:
        relative = baseline_file.relative_to(baseline_repository).as_posix()
        route = _route_for_path(baseline_policy, relative)
        if (
            route is None
            or route.contract != "markdown-frontmatter"
            or baseline_file.suffix.lower() != ".md"
        ):
            continue
        try:
            frontmatter, _body = load_markdown_frontmatter(
                baseline_file.read_text(encoding="utf-8"),
                path=relative,
            )
        except (
            OSError,
            UnicodeError,
            SerializationError,
            ValidationError,
            RCPError,
        ):
            continue
        if frontmatter.validity != "frozen":
            continue
        current_file = repository / relative
        try:
            unchanged = (
                current_file.is_file()
                and not current_file.is_symlink()
                and current_file.read_bytes() == baseline_file.read_bytes()
            )
        except OSError:
            unchanged = False
        if not unchanged:
            findings.append(
                DocumentFinding(
                    kind="invalid",
                    code="frozen_document_modified",
                    path=relative,
                    message="A document frozen in the baseline cannot be changed or removed.",
                )
            )
