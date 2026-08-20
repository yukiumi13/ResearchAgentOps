"""Resolve document ownership from GitHub CODEOWNERS, and only from there.

:mod:`researchctl.services.codeowners` owns the file: where GitHub looks for it,
what syntax GitHub accepts, and which rule wins. This module owns the policy
question layered on top -- whether a repository has adopted CODEOWNERS at all,
how strictly an unresolved owner is reported, and which findings that produces.

With no ``ownership`` block configured, no rule is used, no owner is resolved,
and no ownership finding is emitted. Discovery still identifies an effective
``docs/CODEOWNERS`` so review configuration is not published as documentation.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from researchctl.domain.models import SimpleDocumentLayoutPolicy
from researchctl.services.codeowners import (
    CODEOWNERS_LOCATIONS,
    CodeownersRuleset,
    discover_codeowners,
)
from researchctl.services.project_documents import DocumentFinding


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

    With no ``ownership`` block the policy has not adopted CODEOWNERS, so no
    rule is used, no owner is resolved, and no ownership finding is produced.
    Discovery is still used to identify which file GitHub would read, because
    an effective CODEOWNERS inside the document root is review configuration
    and must not be published as a page.
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


def owner_findings(
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
