from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from researchctl.adapters.git_ci import GitCIObjectReader
from researchctl.adapters.git_worktree import GitWorktreeAdapter
from researchctl.domain.enums import (
    ClaimScope,
    EvidenceStatus,
    ReportApplicability,
)
from researchctl.domain.models import StrictModel
from researchctl.domain.types import GitObjectId, ReportId, Sha256Digest
from researchctl.errors import RCPError
from researchctl.serialization import canonical_digest
from researchctl.services.git_report_impact import GitReportImpactAnalyzer
from researchctl.services.requests import ReportStatusRequest


class EffectiveReportStatus(StrictModel):
    report_id: ReportId
    report_revision: int
    report_digest: Sha256Digest
    target_commit: GitObjectId
    target_tree: GitObjectId
    evidence_status: EvidenceStatus
    claim_scope: ClaimScope
    stored_applicability: ReportApplicability
    effective_applicability: ReportApplicability
    validation_basis_tree: GitObjectId | None = None
    comparison: Literal[
        "not_applicable",
        "exact_tree",
        "protocol_only_change",
        "governed_change",
        "basis_unavailable",
    ]
    reason: Literal[
        "snapshot_scope",
        "stored_superseded",
        "stored_stale",
        "validation_basis_matches_target",
        "only_protocol_state_changed",
        "governed_code_changed_since_validation",
        "validation_basis_tree_unavailable",
    ]
    changed_paths: tuple[str, ...] = ()


class ReportStatusService:
    """Derive Report applicability at an exact commit without writing state."""

    def __init__(
        self,
        *,
        repository_root: Path,
        default_branch: str,
        git: GitCIObjectReader | None = None,
        worktrees: GitWorktreeAdapter | None = None,
        reports: GitReportImpactAnalyzer | None = None,
    ) -> None:
        self.repository_root = Path(
            os.path.abspath(os.fspath(repository_root))
        )
        self.default_branch = default_branch
        self.git = git or GitCIObjectReader()
        self.worktrees = worktrees or GitWorktreeAdapter()
        self.reports = reports or GitReportImpactAnalyzer(git=self.git)

    def read(self, request: ReportStatusRequest) -> EffectiveReportStatus:
        target_commit = request.target_commit or self.worktrees.resolve_commit(
            self.repository_root,
            f"refs/heads/{self.default_branch}",
        )
        target = self.git.read_commit(self.repository_root, target_commit)
        report = self.reports.load_latest_report(
            self.repository_root,
            commit=target.object_id,
            report_id=request.report_id,
        )
        common = {
            "report_id": report.report_id,
            "report_revision": report.revision,
            "report_digest": canonical_digest(report),
            "target_commit": target.object_id,
            "target_tree": target.tree,
            "evidence_status": report.evidence_status,
            "claim_scope": report.claim_scope,
            "stored_applicability": report.applicability,
            "validation_basis_tree": (
                report.validation_basis.main_tree
                if report.validation_basis is not None
                else None
            ),
        }
        if report.claim_scope is ClaimScope.SNAPSHOT:
            return EffectiveReportStatus(
                **common,
                effective_applicability=ReportApplicability.SNAPSHOT_ONLY,
                comparison="not_applicable",
                reason="snapshot_scope",
            )
        if report.applicability is ReportApplicability.SUPERSEDED:
            return EffectiveReportStatus(
                **common,
                effective_applicability=ReportApplicability.SUPERSEDED,
                comparison="not_applicable",
                reason="stored_superseded",
            )
        if report.applicability is ReportApplicability.STALE:
            return EffectiveReportStatus(
                **common,
                effective_applicability=ReportApplicability.STALE,
                comparison="not_applicable",
                reason="stored_stale",
            )

        basis = report.validation_basis
        if basis is None:
            raise RCPError(
                code="report_validation_basis_missing",
                message="Baseline Report does not contain a validation basis.",
                context={"report_id": report.report_id},
            )
        if basis.main_tree == target.tree:
            return EffectiveReportStatus(
                **common,
                effective_applicability=ReportApplicability.CURRENT,
                comparison="exact_tree",
                reason="validation_basis_matches_target",
            )
        if self.git.object_type(self.repository_root, basis.main_tree) != "tree":
            return EffectiveReportStatus(
                **common,
                effective_applicability=ReportApplicability.IMPACT_PENDING,
                comparison="basis_unavailable",
                reason="validation_basis_tree_unavailable",
            )
        changed_paths = tuple(
            change.path
            for change in self.git.changes(
                self.repository_root,
                old_commit=basis.main_tree,
                new_commit=target.tree,
            )
            if not change.path.startswith(".research/")
        )
        if not changed_paths:
            return EffectiveReportStatus(
                **common,
                effective_applicability=ReportApplicability.CURRENT,
                comparison="protocol_only_change",
                reason="only_protocol_state_changed",
            )
        return EffectiveReportStatus(
            **common,
            effective_applicability=ReportApplicability.IMPACT_PENDING,
            comparison="governed_change",
            reason="governed_code_changed_since_validation",
            changed_paths=changed_paths,
        )
