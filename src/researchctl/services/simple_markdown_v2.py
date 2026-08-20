"""Ordinary Markdown under the directory-first contract.

An ordinary document restates nothing about itself. Its type is the section
directory it lives in, its owner comes from CODEOWNERS, and its edit time comes
from Git history, so the frontmatter block holds only what no other source can
supply -- and a document with no frontmatter at all is valid.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import unquote, urlsplit

from pydantic import ValidationError

from researchctl.domain.models import (
    SIMPLE_MARKDOWN_FRONTMATTER_FIELDS,
    SimpleMarkdownFrontmatter,
)
from researchctl.errors import RCPError
from researchctl.repository import safe_repository_path
from researchctl.serialization import SerializationError
from researchctl.services.generated_markdown import inspect_project_frontmatter
from researchctl.services.markdown_source import first_heading_title, link_destinations
from researchctl.services.project_documents import (
    DocumentFinding,
    schema_validation_findings,
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


# --------------------------------------------------------------------------
# What one ordinary document asserts
# --------------------------------------------------------------------------


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
    owners: tuple[str, ...] = ()

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
            "owners": list(self.owners),
        }


# --------------------------------------------------------------------------
# Markdown destinations and frontmatter delimiters
# --------------------------------------------------------------------------


def _is_external_destination(destination: str) -> bool:
    if destination.startswith("//"):
        return True
    return bool(urlsplit(destination).scheme)


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


# --------------------------------------------------------------------------
# Lint and scaffold
# --------------------------------------------------------------------------


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
            schema_validation_findings(
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
    owners: tuple[str, ...] = (),
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
            owners=owners,
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
        owners=owners,
    )
    return findings, facts


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
