"""Directory-first document contract for ordinary Markdown.

The section directory is the document type. Nothing in an ordinary Markdown
document restates its taxonomy, its owner, or its edit time, and a document with
no frontmatter at all is valid.

A section may additionally enable one structured contract. That is strictly an
addition: an unmarked ``.md`` file is always an ordinary document, even inside a
structured section, while a direct-child ``.yaml`` file is a canonical source
whose same-stem generated Markdown is validated by the original renderers. Any
other regular file is a static asset, published as-is and never parsed.

Ownership is resolved from GitHub CODEOWNERS and nowhere else. Researchctl reads
that file; it never writes it, and a resolved owner is display metadata rather
than an authority grant.
"""

from __future__ import annotations

import os
import posixpath
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, ValidationError

from researchctl.domain.models import (
    SIMPLE_MARKDOWN_FRONTMATTER_FIELDS,
    AnalysisBrief,
    DesignDocument,
    ProjectStatusSummary,
    SimpleDocumentLayoutPolicy,
    SimpleDocumentSection,
    SimpleMarkdownFrontmatter,
)
from researchctl.errors import RCPError
from researchctl.repository import safe_repository_path
from researchctl.serialization import SerializationError, load_model
from researchctl.services.codeowners import (
    CODEOWNERS_LOCATIONS,
    CodeownersRuleset,
    discover_codeowners,
)
from researchctl.services.generated_markdown import (
    claims_generated_markdown,
    inspect_generated_markdown,
    inspect_project_frontmatter,
)
from researchctl.services.markdown_source import first_heading_title, link_destinations
from researchctl.services.project_documents import (
    DocumentFinding,
    collect_document_files,
    lint_locked_baseline_documents,
    lint_project_document,
    render_project_document,
    schema_validation_findings,
)
from researchctl.services.research_writing import render_analysis_brief

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

#: Structured contracts whose canonical source carries its own envelope label.
_ENVELOPE_CONTRACTS = frozenset({"design-document", "project-status-summary"})
_ARCHIVED_STATUSES = frozenset({"deprecated", "archived"})

#: Every model a version 2 section may configure.
StructuredDocument = DesignDocument | ProjectStatusSummary | AnalysisBrief

#: The configured contract selects exactly one model. Nothing may be inferred
#: from the source itself: a section that configures a design document must
#: reject a project status summary even when its classification happens to
#: match, because the section directory is the only statement of type.
_CONTRACT_MODELS: dict[str, type[BaseModel]] = {
    "design-document": DesignDocument,
    "project-status-summary": ProjectStatusSummary,
    "analysis-brief": AnalysisBrief,
}
_CONTRACT_DOCUMENT_KINDS: dict[str, str | None] = {
    "design-document": "design_document",
    "project-status-summary": "project_status_summary",
    "analysis-brief": None,
}


class _StructuredKindPeek(BaseModel):
    """Read only the discriminator, under the same size and parser limits."""

    model_config = ConfigDict(extra="allow")

    document_kind: str | None = None


class StructuredContractMismatch(RCPError):
    """The source is a different kind of document than the section configures."""

    def __init__(self, *, contract: str, expected: str | None, observed: str | None) -> None:
        super().__init__(
            code="document_contract_kind_mismatch",
            message=(
                f"This section configures the {contract} contract, but the source "
                f"declares document_kind {observed!r}."
                if expected is None
                else (
                    f"This section configures the {contract} contract, which requires "
                    f"document_kind {expected!r}, but the source declares "
                    f"{observed!r}."
                )
            ),
            remediation=(
                "Move the document to a section that configures its contract, or "
                "rewrite it under this section's contract."
            ),
            context={
                "contract": contract,
                "expected_document_kind": expected,
                "document_kind": observed,
            },
        )


# --------------------------------------------------------------------------
# What a validated tree knows
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


@dataclass(frozen=True, slots=True)
class SimpleStructuredFacts:
    """What one canonical source and its generated render assert."""

    source_path: str
    render_path: str
    section: str
    contract: str
    classification: str | None
    title: str
    lifecycle: str | None
    owners: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "source_path": self.source_path,
            "render_path": self.render_path,
            "section": self.section,
            "contract": self.contract,
            "classification": self.classification,
            "title": self.title,
            "lifecycle": self.lifecycle,
            "owners": list(self.owners),
        }


@dataclass(frozen=True, slots=True)
class SimpleDocumentTreeLintResult:
    root: str
    policy_version: int
    checked_files: int
    documents: int
    findings: tuple[DocumentFinding, ...]
    structured_documents: int = 0
    assets: int = 0
    codeowners_path: str | None = None
    document_facts: tuple[SimpleDocumentFacts, ...] = ()
    structured_facts: tuple[SimpleStructuredFacts, ...] = ()
    asset_paths: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return not any(finding.kind == "invalid" for finding in self.findings)

    @property
    def terminal_result(self) -> str:
        return "passed" if self.passed else "invalid"

    def as_dict(self) -> dict[str, object]:
        return {
            "root": self.root,
            "policy_version": self.policy_version,
            "checked_files": self.checked_files,
            "documents": self.documents,
            "structured_documents": self.structured_documents,
            "assets": self.assets,
            "codeowners_path": self.codeowners_path,
            "terminal_result": self.terminal_result,
            "findings": [finding.as_dict() for finding in self.findings],
            # The facts are the point of a validated tree: whatever builds a
            # sidebar, an owner report, or an impact set reads them from here
            # rather than reparsing the documents.
            "document_facts": [facts.as_dict() for facts in self.document_facts],
            "structured_facts": [facts.as_dict() for facts in self.structured_facts],
            "asset_paths": list(self.asset_paths),
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
# Ownership
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OwnershipResolution:
    """The effective CODEOWNERS file, if any, and how strictly it is required."""

    configured: bool
    required: bool
    path: str | None = None
    ruleset: CodeownersRuleset | None = None
    findings: tuple[DocumentFinding, ...] = ()
    excluded_paths: frozenset[str] = frozenset()

    def owners_for(self, relative: str) -> tuple[str, ...]:
        if self.ruleset is None:
            return ()
        return self.ruleset.owners_for(relative)


def _codeowners_inside_root(policy: SimpleDocumentLayoutPolicy) -> tuple[str, ...]:
    prefix = f"{policy.root}/"
    return tuple(
        candidate for candidate in CODEOWNERS_LOCATIONS if candidate.startswith(prefix)
    )


def resolve_ownership(
    repository: Path,
    policy: SimpleDocumentLayoutPolicy,
) -> OwnershipResolution:
    """Resolve ownership exactly as far as the policy asks for it.

    With no ``ownership`` block the policy has not adopted CODEOWNERS, so
    nothing is parsed, no owner is resolved, and no finding is produced. The one
    thing discovery is still used for is knowing which file GitHub would read,
    because an effective CODEOWNERS that happens to live inside the document
    root is review configuration and must not be published as a page.
    """

    configured = policy.ownership is not None
    required = bool(policy.ownership is not None and policy.ownership.required)
    discovery = discover_codeowners(repository)
    effective_inside_root = (
        frozenset({discovery.path})
        if discovery.resolved
        and discovery.path is not None
        and discovery.path.startswith(f"{policy.root}/")
        else frozenset()
    )
    if not configured:
        return OwnershipResolution(
            configured=False,
            required=False,
            excluded_paths=effective_inside_root,
        )

    findings: list[DocumentFinding] = []
    excluded = set(effective_inside_root)
    # A lower-precedence CODEOWNERS inside the document root looks like
    # ownership but GitHub never reads it, so it is reported rather than
    # published. This is only meaningful once the policy adopts CODEOWNERS.
    for candidate in _codeowners_inside_root(policy):
        if candidate == discovery.path:
            continue
        location = repository / candidate
        if location.is_symlink() or not location.is_file():
            continue
        excluded.add(candidate)
        findings.append(
            DocumentFinding(
                kind="invalid",
                code="document_codeowners_shadowed",
                path=candidate,
                message=(
                    f"GitHub reads {discovery.path}, so this file assigns nothing. "
                    "Merge its rules into the effective CODEOWNERS and delete it."
                    if discovery.resolved
                    else (
                        "This CODEOWNERS file could not be used, so it assigns "
                        "nothing. Repair or delete it."
                    )
                ),
            )
        )
    if discovery.error is not None:
        findings.append(
            DocumentFinding(
                kind="invalid",
                code="document_codeowners_unreadable",
                path=discovery.path or "CODEOWNERS",
                message=discovery.error,
            )
        )
        return OwnershipResolution(
            configured=True,
            required=required,
            findings=tuple(findings),
            excluded_paths=frozenset(excluded),
        )
    if discovery.ruleset is None:
        findings.append(
            DocumentFinding(
                kind="invalid" if required else "warning",
                code="document_codeowners_missing",
                path="CODEOWNERS",
                message=(
                    "Policy resolves ownership from CODEOWNERS but none of "
                    + ", ".join(CODEOWNERS_LOCATIONS)
                    + " exists."
                ),
            )
        )
        return OwnershipResolution(
            configured=True,
            required=required,
            findings=tuple(findings),
            excluded_paths=frozenset(excluded),
        )

    findings.extend(
        DocumentFinding(
            kind="invalid",
            code="document_codeowners_syntax_invalid",
            path=f"{discovery.ruleset.path}:{problem.line}",
            message=problem.message,
        )
        for problem in discovery.ruleset.problems
    )
    return OwnershipResolution(
        configured=True,
        required=required,
        path=discovery.ruleset.path,
        ruleset=discovery.ruleset,
        findings=tuple(findings),
        excluded_paths=frozenset(excluded),
    )


def _owner_findings(
    ownership: OwnershipResolution,
    *,
    relative: str,
    owners: tuple[str, ...],
) -> list[DocumentFinding]:
    if not ownership.configured or ownership.ruleset is None or owners:
        return []
    return [
        DocumentFinding(
            kind="invalid" if ownership.required else "warning",
            code="document_owner_unresolved",
            path=relative,
            message=(
                f"No rule in {ownership.path} assigns an owner to this document."
            ),
        )
    ]


# --------------------------------------------------------------------------
# Ordinary Markdown
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


# --------------------------------------------------------------------------
# Structured source and render pairs
# --------------------------------------------------------------------------


def load_structured_source(source: Path, *, contract: str) -> StructuredDocument:
    """Load one canonical source as exactly the model its section configures.

    The contract selects the model; the file never does. Loading through
    ``load_project_document`` would let the source pick its own kind, so a
    project status summary could satisfy a design-document section whose
    classification happened to match.
    """

    peek = load_model(source, _StructuredKindPeek)
    expected = _CONTRACT_DOCUMENT_KINDS[contract]
    if peek.document_kind != expected:
        raise StructuredContractMismatch(
            contract=contract,
            expected=expected,
            observed=peek.document_kind,
        )
    return load_model(source, _CONTRACT_MODELS[contract])


def render_structured_source(document: StructuredDocument) -> bytes:
    """Render one canonical source with its own deterministic renderer."""

    if isinstance(document, AnalysisBrief):
        return render_analysis_brief(document)
    return render_project_document(document)


def structured_contract_findings(
    document: StructuredDocument,
    *,
    section: SimpleDocumentSection,
    relative: str,
) -> list[DocumentFinding]:
    """Check one loaded source against the contract its section configures."""

    assert section.structured is not None
    findings: list[DocumentFinding] = []
    if isinstance(document, AnalysisBrief):
        return findings
    findings.extend(
        DocumentFinding(
            kind=finding.kind,
            code=finding.code,
            path=f"{relative}:{finding.path}",
            message=finding.message,
        )
        for finding in lint_project_document(document).findings
    )
    if document.classification != section.structured.classification:
        findings.append(
            DocumentFinding(
                kind="invalid",
                code="document_classification_section_mismatch",
                path=f"{relative}:classification",
                message=(
                    f"Envelope classification {document.classification} does not "
                    f"match the section's configured "
                    f"{section.structured.classification}."
                ),
            )
        )
    stem = PurePosixPath(relative).stem
    if document.slug != stem:
        findings.append(
            DocumentFinding(
                kind="invalid",
                code="structured_document_path_noncanonical",
                path=relative,
                message=f"Document slug requires the file name {document.slug}.yaml.",
            )
        )
    return findings


def render_structured_document(
    *,
    section: SimpleDocumentSection,
    source: Path,
    relative: str,
) -> bytes:
    """Load, validate, and render one canonical source under its contract.

    ``doc render`` and ``doc tree`` must agree about what a section accepts, so
    both reach the renderer through this one load-and-validate path.
    """

    assert section.structured is not None
    if source.suffix.lower() != ".yaml":
        raise RCPError(
            code="structured_document_path_noncanonical",
            message=(
                "Canonical structured sources are direct section children with a "
                ".yaml suffix."
            ),
            context={"path": relative, "section": section.path},
        )
    document = load_structured_source(source, contract=section.structured.contract)
    findings = structured_contract_findings(
        document,
        section=section,
        relative=relative,
    )
    invalid = [finding for finding in findings if finding.kind == "invalid"]
    if invalid:
        raise RCPError(
            code="document_lint_invalid",
            message="Document does not satisfy its section's structured contract.",
            context={"findings": [finding.as_dict() for finding in invalid]},
        )
    return render_structured_source(document)


def lint_structured_pair(
    repository: Path,
    *,
    section: SimpleDocumentSection,
    source: Path,
    relative: str,
    owners: tuple[str, ...] = (),
    render_present: bool,
    render_is_ordinary_markdown: bool = False,
) -> tuple[list[DocumentFinding], SimpleStructuredFacts | None]:
    """Validate one canonical source against its generated Markdown pair."""

    assert section.structured is not None
    contract = section.structured.contract
    findings: list[DocumentFinding] = []
    render_path = relative.removesuffix(".yaml") + ".md"
    try:
        document = load_structured_source(source, contract=contract)
    except StructuredContractMismatch as mismatch:
        findings.append(
            DocumentFinding(
                kind="invalid",
                code=mismatch.code,
                path=relative,
                message=f"{mismatch.message} {mismatch.remediation}",
            )
        )
        return findings, None
    except ValidationError as error:
        findings.extend(
            schema_validation_findings(
                error,
                source_path=source,
                relative_path=relative,
            )
        )
        return findings, None
    except (OSError, UnicodeError, SerializationError, RCPError) as error:
        findings.append(
            DocumentFinding(
                kind="invalid",
                code="structured_document_invalid",
                path=relative,
                message=f"Structured document failed validation: {type(error).__name__}.",
            )
        )
        return findings, None

    findings.extend(
        structured_contract_findings(document, section=section, relative=relative)
    )
    if isinstance(document, AnalysisBrief):
        classification: str | None = None
        lifecycle: str | None = None
        title = document.question
    else:
        classification = document.classification
        lifecycle = document.status
        title = document.title

    if not render_present:
        findings.append(
            DocumentFinding(
                kind="invalid",
                code=(
                    "document_render_marker_missing"
                    if render_is_ordinary_markdown
                    else "document_render_missing"
                ),
                path=render_path,
                message=(
                    (
                        "A same-stem Markdown file exists but claims no renderer "
                        "ownership, so it cannot be told apart from a stale render. "
                        "Regenerate it with `researchctl doc render`, or give the "
                        "ordinary document a different file name."
                    )
                    if render_is_ordinary_markdown
                    else f"Structured source {relative} has no generated Markdown pair."
                ),
            )
        )
        return findings, None

    try:
        observed = (repository / render_path).read_bytes()
        expected = render_structured_source(document)
    except (OSError, UnicodeError, SerializationError, RCPError) as error:
        findings.append(
            DocumentFinding(
                kind="invalid",
                code="document_render_unreadable",
                path=render_path,
                message=f"Generated Markdown cannot be read: {type(error).__name__}.",
            )
        )
        return findings, None

    if inspect_generated_markdown(observed) is None:
        findings.append(
            DocumentFinding(
                kind="invalid",
                code="document_render_marker_invalid",
                path=render_path,
                message=(
                    "The file claims to be renderer output but carries no intact "
                    "provenance marker for this body. Regenerate it with "
                    "`researchctl doc render`."
                ),
            )
        )
    elif observed != expected:
        findings.append(
            DocumentFinding(
                kind="invalid",
                code="document_render_mismatch",
                path=render_path,
                message="Generated Markdown differs from deterministic renderer output.",
            )
        )

    facts = SimpleStructuredFacts(
        source_path=relative,
        render_path=render_path,
        section=section.path,
        contract=contract,
        classification=classification,
        title=title,
        lifecycle=lifecycle,
        owners=owners,
    )
    return findings, facts


# --------------------------------------------------------------------------
# Tree
# --------------------------------------------------------------------------


@dataclass(slots=True)
class _Classified:
    """One collected file, sorted into the role its section gives it."""

    markdown: list[tuple[Path, str, str | None]] = field(default_factory=list)
    generated: dict[str, Path] = field(default_factory=dict)
    sources: list[tuple[Path, str, SimpleDocumentSection]] = field(default_factory=list)
    assets: list[str] = field(default_factory=list)
    ambiguous_renders: set[str] = field(default_factory=set)


def lint_simple_document_tree(
    repository_root: Path,
    policy: SimpleDocumentLayoutPolicy,
    *,
    baseline_root: Path | None = None,
    baseline_document_root: str | None = None,
    baseline_policy_missing: bool = False,
) -> SimpleDocumentTreeLintResult:
    """Validate every file below the document root against the section layout."""

    repository = Path(os.path.abspath(os.fspath(repository_root)))
    findings: list[DocumentFinding] = []
    if repository.is_symlink() or not repository.is_dir():
        raise RCPError(
            code="document_repository_invalid",
            message="Document repository root must be an existing non-symlink directory.",
        )
    ownership = resolve_ownership(repository, policy)
    document_root = repository / policy.root
    if document_root.is_symlink() or not document_root.is_dir():
        return SimpleDocumentTreeLintResult(
            root=policy.root,
            policy_version=policy.version,
            checked_files=0,
            documents=0,
            codeowners_path=ownership.path,
            findings=(
                *ownership.findings,
                DocumentFinding(
                    kind="invalid",
                    code="document_root_missing",
                    path=policy.root,
                    message="Configured document root is missing or not a regular directory.",
                ),
            ),
        )

    findings.extend(ownership.findings)

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

    files = collect_document_files(document_root, findings)
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

    # CODEOWNERS governs review; it is never a published page.
    excluded_paths = ownership.excluded_paths
    considered = [
        file_path
        for file_path in files
        if file_path.relative_to(repository).as_posix() not in excluded_paths
    ]

    classified = _Classified()
    yaml_stems: dict[str, set[str]] = {}
    for file_path in considered:
        relative = file_path.relative_to(repository).as_posix()
        if file_path.suffix.lower() == ".yaml":
            parent = posixpath.dirname(relative)
            yaml_stems.setdefault(parent, set()).add(PurePosixPath(relative).stem)

    for file_path in considered:
        relative = file_path.relative_to(repository).as_posix()
        parts = PurePosixPath(relative).parts
        suffix = file_path.suffix.lower()

        if relative in root_pages:
            if suffix != ".md":
                findings.append(
                    DocumentFinding(
                        kind="invalid",
                        code="document_extension_invalid",
                        path=relative,
                        message="Document root pages accept only .md files.",
                    )
                )
                continue
            classified.markdown.append((file_path, relative, None))
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
        depth = len(nested.parts) - 1
        if depth > policy.max_depth:
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

        if suffix == ".md":
            try:
                content = file_path.read_bytes()
            except OSError as error:
                findings.append(
                    DocumentFinding(
                        kind="invalid",
                        code="document_unreadable",
                        path=relative,
                        message=(
                            "Markdown document cannot be read: "
                            f"{type(error).__name__}."
                        ),
                    )
                )
                continue
            if claims_generated_markdown(content):
                classified.generated[relative] = file_path
                continue
            same_stem_source = (
                section.structured is not None
                and depth == 0
                and PurePosixPath(relative).stem
                in yaml_stems.get(posixpath.dirname(relative), set())
            )
            if same_stem_source:
                classified.ambiguous_renders.add(relative)
                continue
            classified.markdown.append((file_path, relative, section.path))
            continue

        if section.structured is not None and depth == 0 and suffix == ".yml":
            findings.append(
                DocumentFinding(
                    kind="invalid",
                    code="document_structured_extension_invalid",
                    path=relative,
                    message=(
                        "Canonical structured sources use the .yaml suffix; rename "
                        "this file so it is validated rather than published as an "
                        "asset."
                    ),
                )
            )
            continue

        if section.structured is not None and depth == 0 and suffix == ".yaml":
            classified.sources.append((file_path, relative, section))
            continue

        classified.assets.append(relative)

    document_facts: list[SimpleDocumentFacts] = []
    for file_path, relative, section_name in classified.markdown:
        owners = ownership.owners_for(relative)
        findings.extend(_owner_findings(ownership, relative=relative, owners=owners))
        document_findings, facts = lint_simple_markdown_document(
            repository,
            source=file_path,
            relative=relative,
            section=section_name,
            owners=owners,
        )
        findings.extend(document_findings)
        document_facts.append(facts)

    structured_facts: list[SimpleStructuredFacts] = []
    expected_renders: set[str] = set()
    for file_path, relative, section in classified.sources:
        render_path = relative.removesuffix(".yaml") + ".md"
        expected_renders.add(render_path)
        owners = ownership.owners_for(render_path)
        findings.extend(
            _owner_findings(ownership, relative=render_path, owners=owners)
        )
        pair_findings, facts = lint_structured_pair(
            repository,
            section=section,
            source=file_path,
            relative=relative,
            owners=owners,
            render_present=render_path in classified.generated,
            render_is_ordinary_markdown=render_path in classified.ambiguous_renders,
        )
        findings.extend(pair_findings)
        if facts is not None:
            structured_facts.append(facts)

    for orphan in sorted(set(classified.generated) - expected_renders):
        findings.append(
            DocumentFinding(
                kind="invalid",
                code="document_render_orphaned",
                path=orphan,
                message="Generated Markdown has no canonical structured source.",
            )
        )

    if baseline_root is not None:
        lint_locked_baseline_documents(
            repository,
            Path(os.path.abspath(os.fspath(baseline_root))),
            baseline_document_root or policy.root,
            findings,
            allow_missing_root=baseline_policy_missing,
        )

    return SimpleDocumentTreeLintResult(
        root=policy.root,
        policy_version=policy.version,
        checked_files=len(considered),
        documents=len(document_facts),
        structured_documents=len(structured_facts),
        assets=len(classified.assets),
        codeowners_path=ownership.path,
        findings=tuple(findings),
        document_facts=tuple(document_facts),
        structured_facts=tuple(structured_facts),
        asset_paths=tuple(sorted(classified.assets)),
    )


# --------------------------------------------------------------------------
# Single-document entry points
# --------------------------------------------------------------------------


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


def require_structured_section(
    policy: SimpleDocumentLayoutPolicy,
    relative: str,
) -> SimpleDocumentSection:
    """Return the section owning a canonical source, or fail closed."""

    section = section_for_relative(policy, relative)
    if section.structured is None:
        raise RCPError(
            code="document_structured_section_unconfigured",
            message=(
                f"Section {section.path!r} does not enable a structured contract."
            ),
            remediation=(
                "Author ordinary Markdown here, or propose a structured contract "
                "for this section."
            ),
            context={"path": relative, "section": section.path},
        )
    nested = PurePosixPath(relative).relative_to(policy.section_directory(section))
    if len(nested.parts) != 1:
        raise RCPError(
            code="structured_document_path_noncanonical",
            message="Canonical structured sources must be direct section children.",
            context={"path": relative, "section": section.path},
        )
    return section


def check_simple_document(
    repository: Path,
    policy: SimpleDocumentLayoutPolicy,
    *,
    source: Path,
    relative: str,
) -> tuple[list[DocumentFinding], dict[str, object]]:
    """Validate one routed path, returning findings plus what it asserts."""

    ownership = resolve_ownership(repository, policy)
    suffix = source.suffix.lower()
    if suffix == ".yaml":
        section = require_structured_section(policy, relative)
        assert section.structured is not None
        render_path = relative.removesuffix(".yaml") + ".md"
        render = repository / render_path
        render_present = render.is_file() and not render.is_symlink()
        marker = (
            claims_generated_markdown(render.read_bytes())
            if render_present
            else False
        )
        owners = ownership.owners_for(render_path)
        findings = list(ownership.findings)
        findings.extend(_owner_findings(ownership, relative=render_path, owners=owners))
        pair_findings, facts = lint_structured_pair(
            repository,
            section=section,
            source=source,
            relative=relative,
            owners=owners,
            render_present=render_present and marker,
            render_is_ordinary_markdown=render_present and not marker,
        )
        findings.extend(pair_findings)
        payload: dict[str, object] = {
            "kind": "structured",
            "section": section.path,
            "contract": section.structured.contract,
        }
        if facts is not None:
            payload.update(facts.as_dict())
        return findings, payload

    root_pages = set(policy.root_page_paths())
    if relative in root_pages:
        section_name: str | None = None
    else:
        section = section_for_relative(policy, relative)
        section_name = section.path
    if suffix != ".md":
        raise RCPError(
            code="document_asset_not_checkable",
            message=(
                "Static assets are published as-is and have no document contract "
                "to check."
            ),
            context={"path": relative},
        )
    try:
        renderer_owned = claims_generated_markdown(source.read_bytes())
    except OSError as error:
        raise RCPError(
            code="document_unreadable",
            message=f"Markdown document cannot be read: {type(error).__name__}.",
            context={"path": relative},
        ) from error
    if renderer_owned:
        # Generated Markdown is an output, not a source. Linting it as ordinary
        # prose would report on text nobody edits and would quietly accept a
        # damaged render, so the canonical YAML is the only thing to check.
        canonical = relative.removesuffix(".md") + ".yaml"
        raise RCPError(
            code="document_generated_markdown_not_checkable",
            message=(
                "This file claims to be renderer output, so it has no document "
                "contract of its own."
            ),
            remediation=(
                f"Check its canonical source instead: researchctl doc check "
                f"{canonical}. If the render is damaged, regenerate it with "
                "researchctl doc render, and if it is meant to be an ordinary "
                "document, remove the renderer marker and give it a stem no "
                "canonical source uses."
            ),
            context={"path": relative, "canonical_source": canonical},
        )
    findings = list(ownership.findings)
    if section_name is not None:
        nested = PurePosixPath(relative).relative_to(f"{policy.root}/{section_name}")
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
    owners = ownership.owners_for(relative)
    findings.extend(_owner_findings(ownership, relative=relative, owners=owners))
    document_findings, facts = lint_simple_markdown_document(
        repository,
        source=source,
        relative=relative,
        section=section_name,
        owners=owners,
    )
    findings.extend(document_findings)
    payload = {"kind": "markdown", "contract": "markdown", **facts.as_dict()}
    return findings, payload


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
