"""Directory-first document contract for ordinary Markdown.

The section directory is the document type. Nothing in an ordinary Markdown
document restates its taxonomy, its owner, or its edit time, and a document
with no frontmatter at all is valid. Structured sections are modelled here but
their canonical source/render pair is validated by the original contract; this
phase reports them as unvalidated rather than guessing.

Two deliberate gaps remain for the next phase: ordinary sections do not yet
accept static assets such as images or PDFs, and a structured section does not
yet accept unmarked ordinary Markdown beside its generated pairs.
"""

from __future__ import annotations

import os
import posixpath
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from pydantic import ValidationError

from researchctl.domain.models import (
    SIMPLE_MARKDOWN_FRONTMATTER_FIELDS,
    SimpleDocumentLayoutPolicy,
    SimpleDocumentSection,
    SimpleMarkdownFrontmatter,
)
from researchctl.errors import RCPError
from researchctl.repository import safe_repository_path
from researchctl.serialization import SerializationError
from researchctl.services.generated_markdown import inspect_project_frontmatter
from researchctl.services.markdown_source import first_heading_title, link_destinations
from researchctl.services.project_documents import (
    DocumentFinding,
    _collect_files,
    _schema_validation_findings,
)

#: Frontmatter keys the original contract required, and what replaces them.
LEGACY_FRONTMATTER_REPLACEMENTS: dict[str, str] = {
    "type": "delete it; the section directory is the document type",
    "title": "delete it; the first level-one heading is the only title",
    "owner": "delete it; ownership resolves from CODEOWNERS",
    "last_updated": "delete it; edit time is derived from Git history",
    "validity": (
        "use status: draft|active|deprecated|archived, and locked: true for "
        "immutability"
    ),
    "invalid_reason": "use status: deprecated or status: archived",
    "classification": "delete it; the section directory is the taxonomy",
    "references": "use ordinary Markdown links, or depends_on",
    "sources": "use ordinary Markdown links, or depends_on",
    "provenance": "delete it; quantitative provenance belongs to a structured contract",
    "relations": "use depends_on and superseded_by",
}

_ARCHIVED_STATUSES = frozenset({"deprecated", "archived"})


@dataclass(frozen=True, slots=True)
class SimpleDocumentFacts:
    """What one ordinary Markdown document asserts about itself."""

    path: str
    section: str | None
    title: str | None
    status: str
    tags: tuple[str, ...]
    reviewed_on: date | None
    locked: bool
    depends_on: tuple[str, ...]
    superseded_by: str | None
    links: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "section": self.section,
            "title": self.title,
            "status": self.status,
            "tags": list(self.tags),
            "reviewed_on": self.reviewed_on.isoformat() if self.reviewed_on else None,
            "locked": self.locked,
            "depends_on": list(self.depends_on),
            "superseded_by": self.superseded_by,
            "links": list(self.links),
        }


@dataclass(frozen=True, slots=True)
class SimpleDocumentTreeLintResult:
    root: str
    policy_version: int
    checked_files: int
    documents: int
    findings: tuple[DocumentFinding, ...]

    @property
    def passed(self) -> bool:
        return not any(finding.kind == "invalid" for finding in self.findings)

    @property
    def terminal_result(self) -> str:
        return "passed" if self.passed else "invalid"

    @property
    def structured_documents(self) -> int:
        """Kept for the shared lint printer; structured lint arrives later."""

        return 0

    def as_dict(self) -> dict[str, object]:
        return {
            "root": self.root,
            "policy_version": self.policy_version,
            "checked_files": self.checked_files,
            "documents": self.documents,
            "structured_documents": self.structured_documents,
            "terminal_result": self.terminal_result,
            "findings": [finding.as_dict() for finding in self.findings],
        }


def _is_external_destination(destination: str) -> bool:
    if destination.startswith("//"):
        return True
    scheme = urlsplit(destination).scheme
    return bool(scheme)


def resolve_repository_link(
    *,
    document_relative: str,
    destination: str,
) -> str | None:
    """Map one Markdown destination to a repository-relative path.

    Only a relative destination names a repository-local dependency. Everything
    else is presentation and returns ``None``: an absolute URL, a
    protocol-relative URL, a bare fragment, and a site-absolute path such as
    ``/cloud/guide``, which belongs to whichever site publishes the document
    and has no meaning inside this repository.
    """

    if _is_external_destination(destination):
        return None
    target = destination.split("#", maxsplit=1)[0].split("?", maxsplit=1)[0]
    target = unquote(target).strip()
    if not target or target.startswith("/"):
        return None
    parent = posixpath.dirname(document_relative)
    normalized = posixpath.normpath(posixpath.join(parent, target))
    if not normalized or normalized == ".":
        return None
    return normalized


def declares_frontmatter(content: bytes) -> bool:
    """Report whether the first line opens a YAML frontmatter block."""

    first_line, _, _rest = content.partition(b"\n")
    return first_line.rstrip(b"\r") == b"---"


def _frontmatter_findings(
    values: dict[str, object],
    *,
    relative: str,
    source_path: Path,
) -> tuple[list[DocumentFinding], SimpleMarkdownFrontmatter | None]:
    findings: list[DocumentFinding] = []
    accepted = set(SIMPLE_MARKDOWN_FRONTMATTER_FIELDS)
    known: dict[str, object] = {}
    for key, value in values.items():
        name = str(key)
        if name in accepted:
            known[name] = value
            continue
        replacement = LEGACY_FRONTMATTER_REPLACEMENTS.get(name)
        if replacement is not None:
            findings.append(
                DocumentFinding(
                    kind="invalid",
                    code="document_legacy_frontmatter_field",
                    path=f"{relative}:{name}",
                    message=(
                        f"Frontmatter field {name!r} belongs to the classification-route "
                        f"contract; {replacement}."
                    ),
                )
            )
            continue
        findings.append(
            DocumentFinding(
                kind="invalid",
                code="document_frontmatter_unknown_field",
                path=f"{relative}:{name}",
                message=(
                    f"Frontmatter field {name!r} is not accepted. Accepted fields are "
                    + ", ".join(SIMPLE_MARKDOWN_FRONTMATTER_FIELDS)
                    + "."
                ),
            )
        )
    try:
        frontmatter = SimpleMarkdownFrontmatter.model_validate(known)
    except ValidationError as error:
        findings.extend(
            _schema_validation_findings(
                error,
                source_path=source_path,
                relative_path=relative,
            )
        )
        return findings, None
    return findings, frontmatter


def _target_exists(repository: Path, target: str) -> bool | None:
    """Return existence, or ``None`` when the path cannot be inspected safely."""

    try:
        resolved = safe_repository_path(repository, target)
    except RCPError:
        return None
    return resolved.exists()


def lint_simple_markdown_document(
    repository: Path,
    *,
    source: Path,
    relative: str,
    section: str | None,
) -> tuple[list[DocumentFinding], SimpleDocumentFacts]:
    """Validate one ordinary Markdown document and report what it asserts."""

    findings: list[DocumentFinding] = []
    try:
        content = source.read_bytes()
        text = content.decode("utf-8")
    except (OSError, UnicodeError) as error:
        findings.append(
            DocumentFinding(
                kind="invalid",
                code="document_unreadable",
                path=relative,
                message=f"Markdown document cannot be read: {type(error).__name__}.",
            )
        )
        return findings, SimpleDocumentFacts(
            path=relative,
            section=section,
            title=None,
            status="active",
            tags=(),
            reviewed_on=None,
            locked=False,
            depends_on=(),
            superseded_by=None,
            links=(),
        )

    frontmatter: SimpleMarkdownFrontmatter | None = SimpleMarkdownFrontmatter()
    body = text
    unparseable = False
    try:
        envelope = inspect_project_frontmatter(content)
    except (UnicodeError, SerializationError) as error:
        findings.append(
            DocumentFinding(
                kind="invalid",
                code="document_frontmatter_invalid",
                path=relative,
                message=f"Frontmatter is not a strict YAML mapping: {error}",
            )
        )
        envelope = None
        frontmatter = None
        unparseable = True
    if envelope is None and not unparseable and declares_frontmatter(content):
        findings.append(
            DocumentFinding(
                kind="invalid",
                code="document_frontmatter_invalid",
                path=relative,
                message=(
                    "The document opens a `---` frontmatter block that does not close "
                    "into a YAML mapping. Close it with a `---` line followed by a "
                    "newline, or delete the opening delimiter; every frontmatter "
                    "field is optional."
                ),
            )
        )
        frontmatter = None
    if envelope is not None:
        body = envelope.body.decode("utf-8")
        frontmatter_findings, frontmatter = _frontmatter_findings(
            envelope.values,
            relative=relative,
            source_path=source,
        )
        findings.extend(frontmatter_findings)

    title = first_heading_title(body)
    if title is None:
        findings.append(
            DocumentFinding(
                kind="invalid",
                code="document_title_missing",
                path=relative,
                message=(
                    "Ordinary Markdown takes its title from the first level-one "
                    "heading; add one `# Title` line."
                ),
            )
        )

    links: list[str] = []
    for destination in link_destinations(body):
        target = resolve_repository_link(
            document_relative=relative,
            destination=destination,
        )
        if target is None:
            continue
        if target.startswith("../") or target == "..":
            findings.append(
                DocumentFinding(
                    kind="warning",
                    code="document_link_outside_repository",
                    path=f"{relative}:{destination}",
                    message="Markdown link leaves the repository and cannot be checked.",
                )
            )
            continue
        exists = _target_exists(repository, target)
        if exists is None:
            findings.append(
                DocumentFinding(
                    kind="warning",
                    code="document_link_unsafe",
                    path=f"{relative}:{destination}",
                    message="Markdown link cannot be resolved safely inside the repository.",
                )
            )
            continue
        if not exists:
            findings.append(
                DocumentFinding(
                    kind="invalid",
                    code="document_link_target_missing",
                    path=f"{relative}:{destination}",
                    message=f"Markdown link target does not exist: {target}.",
                )
            )
            continue
        links.append(target)

    status = frontmatter.status if frontmatter is not None else "active"
    superseded_by = frontmatter.superseded_by if frontmatter is not None else None
    depends_on = frontmatter.depends_on if frontmatter is not None else ()

    for dependency in depends_on:
        exists = _target_exists(repository, dependency)
        if exists is None:
            findings.append(
                DocumentFinding(
                    kind="invalid",
                    code="document_depends_on_unsafe",
                    path=f"{relative}:depends_on",
                    message=f"Declared dependency is not a safe repository path: {dependency}.",
                )
            )
        elif not exists:
            findings.append(
                DocumentFinding(
                    kind="invalid",
                    code="document_depends_on_target_missing",
                    path=f"{relative}:depends_on",
                    message=f"Declared dependency does not exist: {dependency}.",
                )
            )

    if superseded_by is not None:
        if superseded_by == relative:
            findings.append(
                DocumentFinding(
                    kind="invalid",
                    code="document_superseded_by_self",
                    path=f"{relative}:superseded_by",
                    message="A document cannot supersede itself.",
                )
            )
        else:
            exists = _target_exists(repository, superseded_by)
            if exists is None:
                findings.append(
                    DocumentFinding(
                        kind="invalid",
                        code="document_superseded_by_unsafe",
                        path=f"{relative}:superseded_by",
                        message=(
                            "superseded_by is not a safe repository path: "
                            f"{superseded_by}."
                        ),
                    )
                )
            elif not exists:
                findings.append(
                    DocumentFinding(
                        kind="invalid",
                        code="document_superseded_by_target_missing",
                        path=f"{relative}:superseded_by",
                        message=f"superseded_by target does not exist: {superseded_by}.",
                    )
                )
        if status not in _ARCHIVED_STATUSES:
            findings.append(
                DocumentFinding(
                    kind="invalid",
                    code="document_superseded_status_mismatch",
                    path=f"{relative}:status",
                    message=(
                        "A superseded document must record status: deprecated or "
                        f"status: archived, not {status}."
                    ),
                )
            )

    facts = SimpleDocumentFacts(
        path=relative,
        section=section,
        title=title,
        status=status,
        tags=tuple(frontmatter.tags) if frontmatter is not None else (),
        reviewed_on=frontmatter.reviewed_on if frontmatter is not None else None,
        locked=frontmatter.locked if frontmatter is not None else False,
        depends_on=tuple(depends_on),
        superseded_by=superseded_by,
        links=tuple(links),
    )
    return findings, facts


def lint_simple_document_tree(
    repository_root: Path,
    policy: SimpleDocumentLayoutPolicy,
) -> SimpleDocumentTreeLintResult:
    """Validate every file below the document root against the section layout."""

    repository = Path(os.path.abspath(os.fspath(repository_root)))
    findings: list[DocumentFinding] = []
    if repository.is_symlink() or not repository.is_dir():
        raise RCPError(
            code="document_repository_invalid",
            message="Document repository root must be an existing non-symlink directory.",
        )
    document_root = repository / policy.root
    if document_root.is_symlink() or not document_root.is_dir():
        return SimpleDocumentTreeLintResult(
            root=policy.root,
            policy_version=policy.version,
            checked_files=0,
            documents=0,
            findings=(
                DocumentFinding(
                    kind="invalid",
                    code="document_root_missing",
                    path=policy.root,
                    message="Configured document root is missing or not a regular directory.",
                ),
            ),
        )

    for section in policy.sections:
        directory = repository / policy.section_directory(section)
        if directory.is_symlink() or not directory.is_dir():
            findings.append(
                DocumentFinding(
                    kind="warning",
                    code="document_section_directory_missing",
                    path=policy.section_directory(section),
                    message="Configured section has no directory yet.",
                )
            )

    if policy.ownership is not None:
        required = policy.ownership.required
        findings.append(
            DocumentFinding(
                kind="invalid" if required else "warning",
                code="document_ownership_not_implemented",
                path="ownership",
                message=(
                    f"Policy resolves ownership from {policy.ownership.source}, which "
                    "this policy version does not read yet, so no document can be "
                    + (
                        "shown to have an owner while ownership.required is true."
                        if required
                        else "shown to have an owner."
                    )
                ),
            )
        )

    if policy.agent_guides:
        findings.append(
            DocumentFinding(
                kind="warning",
                code="agent_guide_not_enforced",
                path=", ".join(target.path for target in policy.agent_guides),
                message=(
                    "Configured Agent guides are not yet rendered or drift-checked "
                    "for a version 2 policy."
                ),
            )
        )

    files = _collect_files(document_root, findings)
    root_pages = set(policy.root_page_paths())
    root_parts = PurePosixPath(policy.root).parts
    existing = {file_path.relative_to(repository).as_posix() for file_path in files}
    for missing in sorted(root_pages - existing):
        findings.append(
            DocumentFinding(
                kind="invalid",
                code="document_root_page_missing",
                path=missing,
                message="Configured document root page is missing.",
            )
        )

    structured_with_files: set[str] = set()
    documents = 0
    for file_path in files:
        relative = file_path.relative_to(repository).as_posix()
        parts = PurePosixPath(relative).parts
        if relative in root_pages:
            if file_path.suffix.lower() != ".md":
                findings.append(
                    DocumentFinding(
                        kind="invalid",
                        code="document_extension_invalid",
                        path=relative,
                        message="Document root pages accept only .md files.",
                    )
                )
                continue
            document_findings, _facts = lint_simple_markdown_document(
                repository,
                source=file_path,
                relative=relative,
                section=None,
            )
            findings.extend(document_findings)
            documents += 1
            continue

        if len(parts) == len(root_parts) + 1:
            findings.append(
                DocumentFinding(
                    kind="invalid",
                    code="document_root_file_unclassified",
                    path=relative,
                    message=(
                        "Files directly below the document root must be declared in "
                        "root_pages, or moved into a section directory."
                    ),
                )
            )
            continue

        section = policy.section_for_path(relative)
        if section is None:
            findings.append(
                DocumentFinding(
                    kind="invalid",
                    code="document_section_unknown",
                    path=relative,
                    message=(
                        f"Directory {parts[len(root_parts)]!r} is not an accepted "
                        "section. Accepted sections are "
                        + ", ".join(item.path for item in policy.sections)
                        + "."
                    ),
                )
            )
            continue

        section_directory = policy.section_directory(section)
        nested = PurePosixPath(relative).relative_to(section_directory)
        if len(nested.parts) - 1 > policy.max_depth:
            findings.append(
                DocumentFinding(
                    kind="invalid",
                    code="document_path_too_deep",
                    path=relative,
                    message=(
                        "Document path exceeds the configured maximum directory "
                        f"depth of {policy.max_depth} below its section."
                    ),
                )
            )
            continue

        if section.structured is not None:
            structured_with_files.add(section.path)
            continue

        if file_path.suffix.lower() != ".md":
            findings.append(
                DocumentFinding(
                    kind="invalid",
                    code="document_extension_invalid",
                    path=relative,
                    message="Ordinary Markdown sections accept only .md files.",
                )
            )
            continue

        document_findings, _facts = lint_simple_markdown_document(
            repository,
            source=file_path,
            relative=relative,
            section=section.path,
        )
        findings.extend(document_findings)
        documents += 1

    for section_path in sorted(structured_with_files):
        findings.append(
            DocumentFinding(
                kind="warning",
                code="document_structured_section_unvalidated",
                path=f"{policy.root}/{section_path}",
                message=(
                    "Structured section content is not validated by this policy "
                    "version yet; its canonical source/render pair is unchecked."
                ),
            )
        )

    return SimpleDocumentTreeLintResult(
        root=policy.root,
        policy_version=policy.version,
        checked_files=len(files),
        documents=documents,
        findings=tuple(findings),
    )


def section_for_relative(
    policy: SimpleDocumentLayoutPolicy,
    relative: str,
) -> SimpleDocumentSection:
    section = policy.section_for_path(relative)
    if section is None:
        raise RCPError(
            code="document_section_unknown",
            message="Document path is not inside an accepted section directory.",
            remediation=(
                "Move the document into one of the policy's section directories, "
                "or propose a new section."
            ),
            context={
                "path": relative,
                "sections": [item.path for item in policy.sections],
            },
        )
    return section


def check_simple_document(
    repository: Path,
    policy: SimpleDocumentLayoutPolicy,
    *,
    source: Path,
    relative: str,
) -> tuple[list[DocumentFinding], SimpleDocumentFacts, str | None]:
    """Validate one routed path, returning findings, facts, and its section."""

    root_pages = set(policy.root_page_paths())
    if relative in root_pages:
        section_name: str | None = None
    else:
        section = section_for_relative(policy, relative)
        if section.structured is not None:
            raise RCPError(
                code="document_structured_check_unsupported",
                message=(
                    "Structured section sources are not validated by this policy "
                    "version yet."
                ),
                context={"path": relative, "contract": section.structured.contract},
            )
        section_name = section.path
    if source.suffix.lower() != ".md":
        raise RCPError(
            code="document_extension_invalid",
            message="Ordinary Markdown sections accept only .md files.",
            context={"path": relative},
        )
    findings: list[DocumentFinding] = []
    if section_name is not None:
        nested = PurePosixPath(relative).relative_to(
            f"{policy.root}/{section_name}",
        )
        if len(nested.parts) - 1 > policy.max_depth:
            findings.append(
                DocumentFinding(
                    kind="invalid",
                    code="document_path_too_deep",
                    path=relative,
                    message=(
                        "Document path exceeds the configured maximum directory "
                        f"depth of {policy.max_depth} below its section."
                    ),
                )
            )
    document_findings, facts = lint_simple_markdown_document(
        repository,
        source=source,
        relative=relative,
        section=section_name,
    )
    findings.extend(document_findings)
    return findings, facts, section_name


def scaffold_simple_document(*, title: str) -> bytes:
    """Render the smallest valid ordinary Markdown document."""

    return (
        "---\n"
        "# Every frontmatter field is optional. A document with no frontmatter\n"
        "# block at all is an active, untagged, unreviewed, unlocked document.\n"
        "status: draft\n"
        "tags: []\n"
        "---\n"
        "\n"
        f"# {title}\n"
        "\n"
        "Replace this paragraph with the document body. The heading above is the\n"
        "only title; do not add a `title:` frontmatter field.\n"
    ).encode()
