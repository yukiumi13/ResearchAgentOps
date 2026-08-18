"""Directory-first document contract: the public surface and the tree walk.

The section directory is the document type. Nothing in an ordinary Markdown
document restates its taxonomy, its owner, or its edit time, and a document with
no frontmatter at all is valid.

A section may additionally enable one structured contract. That is strictly an
addition: an unmarked ``.md`` file is always an ordinary document, even inside a
structured section, while a direct-child ``.yaml`` file is a canonical source
whose same-stem generated Markdown is validated by the original renderers. Any
other regular file is a static asset, published as-is and never parsed.

This module decides which of those a given path is, and is the import surface
the CLI uses. The rules for each kind live beside the facts they produce:
:mod:`researchctl.services.simple_markdown_v2`,
:mod:`researchctl.services.structured_documents_v2`, and
:mod:`researchctl.services.document_ownership`.
"""

from __future__ import annotations

import os
import posixpath
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from researchctl.domain.models import (
    SimpleDocumentLayoutPolicy,
    SimpleDocumentSection,
)
from researchctl.errors import RCPError
from researchctl.services.agent_guides import (
    lint_agent_guide_targets,
    render_simple_agent_guide,
)
from researchctl.services.document_ownership import (
    OwnershipResolution,
    owner_findings,
    resolve_ownership,
)
from researchctl.services.generated_markdown import claims_generated_markdown
from researchctl.services.project_documents import (
    DocumentFinding,
    collect_document_files,
    lint_locked_baseline_documents,
)
from researchctl.services.simple_markdown_v2 import (
    LEGACY_FRONTMATTER_REPLACEMENTS,
    SimpleDocumentFacts,
    declares_frontmatter,
    lint_simple_markdown_document,
    resolve_repository_link,
    scaffold_simple_document,
)
from researchctl.services.structured_documents_v2 import (
    SimpleStructuredFacts,
    StructuredContractMismatch,
    StructuredDocument,
    lint_structured_pair,
    load_structured_source,
    render_structured_document,
    render_structured_source,
    structured_contract_findings,
)

__all__ = [
    "LEGACY_FRONTMATTER_REPLACEMENTS",
    "OwnershipResolution",
    "SimpleDocumentFacts",
    "SimpleDocumentTreeLintResult",
    "SimpleStructuredFacts",
    "StructuredContractMismatch",
    "StructuredDocument",
    "check_simple_document",
    "declares_frontmatter",
    "lint_simple_document_tree",
    "lint_simple_markdown_document",
    "lint_structured_pair",
    "load_structured_source",
    "owner_findings",
    "render_structured_document",
    "render_structured_source",
    "require_structured_section",
    "resolve_ownership",
    "resolve_repository_link",
    "scaffold_simple_document",
    "section_for_relative",
    "structured_contract_findings",
]


# --------------------------------------------------------------------------
# What a validated tree knows
# --------------------------------------------------------------------------


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

    agent_guide_count = lint_agent_guide_targets(
        repository,
        policy.agent_guides,
        findings,
        render=lambda guide_format: render_simple_agent_guide(policy, guide_format),
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
        # Only a literally lowercase .yaml is a canonical source, so only one
        # can make a same-stem .md ambiguous.
        if file_path.suffix == ".yaml":
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

        if section.structured is not None and depth == 0 and suffix in {".yaml", ".yml"}:
            # The canonical suffix is literally lowercase .yaml. A near miss is
            # rejected rather than accepted, because the render path is derived
            # from it: docs/design/x.YAML would name a render docs/design/x.YAML.md,
            # which is not the page any reader or manifest expects.
            if file_path.suffix != ".yaml":
                findings.append(
                    DocumentFinding(
                        kind="invalid",
                        code="document_structured_extension_invalid",
                        path=relative,
                        message=(
                            "Canonical structured sources use the lowercase .yaml "
                            "suffix; rename this file so it is validated rather "
                            "than published as an asset."
                        ),
                    )
                )
                continue
            classified.sources.append((file_path, relative, section))
            continue

        classified.assets.append(relative)

    document_facts: list[SimpleDocumentFacts] = []
    for file_path, relative, section_name in classified.markdown:
        owners = ownership.owners_for(relative)
        findings.extend(owner_findings(ownership, relative=relative, owners=owners))
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
            owner_findings(ownership, relative=render_path, owners=owners)
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
        checked_files=len(considered) + agent_guide_count,
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
    if PurePosixPath(relative).suffix != ".yaml":
        # Same rule the tree walk applies: the render path is derived by
        # stripping a literal ".yaml", so anything else derives a render nobody
        # publishes.
        raise RCPError(
            code="document_structured_extension_invalid",
            message=(
                "Canonical structured sources use the lowercase .yaml suffix."
            ),
            remediation=(
                "Rename the file to end in .yaml so it is validated and its "
                "render path is derived correctly."
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
        findings.extend(owner_findings(ownership, relative=render_path, owners=owners))
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
    findings.extend(owner_findings(ownership, relative=relative, owners=owners))
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
