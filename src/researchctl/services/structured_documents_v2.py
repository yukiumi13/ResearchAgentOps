"""Canonical structured sources and their generated Markdown pairs.

A section may enable exactly one structured contract. That contract selects the
model; the file never does, so a document of the wrong kind cannot satisfy a
section whose classification happens to match. Every reader of a canonical
source -- the tree, ``doc check``, and ``doc render`` -- loads and validates it
here, so none of them can disagree about what a section accepts.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, ValidationError

from researchctl.domain.models import (
    AnalysisBrief,
    DesignDocument,
    ProjectStatusSummary,
    SimpleDocumentSection,
)
from researchctl.errors import RCPError
from researchctl.serialization import SerializationError, load_model
from researchctl.services.generated_markdown import inspect_generated_markdown
from researchctl.services.project_documents import (
    DocumentFinding,
    lint_project_document,
    render_project_document,
    schema_validation_findings,
)
from researchctl.services.research_writing import render_analysis_brief

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
# What one canonical source and its render assert
# --------------------------------------------------------------------------


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


# --------------------------------------------------------------------------
# Load, validate, render, and pair
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
