from __future__ import annotations

import re
from pathlib import Path

from researchctl.adapters.git_submission import (
    GitSubmissionAdapter,
    SubmissionCommitReceipt,
)
from researchctl.adapters.git_worktree import WorktreeSpec
from researchctl.errors import RCPError

_DECISION_ID = re.compile(r"^decision_\d{8}T\d{6}Z_[0-9a-f]{24}$")
_GIT_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_REPORT_ID = re.compile(r"^report_\d{8}T\d{6}Z_[0-9a-f]{24}$")


class GitImpactDecisionAdapter(GitSubmissionAdapter):
    """Commit one closed ImpactDecision bundle over exact protected main."""

    def prepare_worktree(
        self,
        *,
        repository_root: Path,
        worktrees_directory: Path,
        decision_id: str,
        base_commit: str,
    ) -> tuple[str, Path]:
        self._validate_identity(decision_id, base_commit)
        branch = self._branch_for(decision_id)
        worktree = worktrees_directory / f"impact-decision-{decision_id}"
        existing = self._resolve(
            repository_root,
            f"refs/heads/{branch}",
            required=False,
        )
        selected = existing or base_commit
        self.worktrees.create_or_observe(
            WorktreeSpec(
                root=repository_root,
                base_commit=selected,
                branch=branch,
                worktree=worktree,
            )
        )
        return branch, worktree

    def commit_decision(
        self,
        *,
        worktree: Path,
        branch: str,
        decision_id: str,
        report_id: str,
        report_revision: int,
        operation_id: str,
        expected_parent: str,
        paths: tuple[str, ...],
    ) -> SubmissionCommitReceipt:
        if not _REPORT_ID.fullmatch(report_id) or report_revision < 2:
            self._invalid_identity()
        expected = {
            f".research/decisions/{decision_id}.yaml",
            f".research/reports/{report_id}/{report_revision}.md",
            f".research/reports/{report_id}/{report_revision}.yaml",
        }
        if set(paths) != expected or len(paths) != len(expected):
            raise RCPError(
                code="impact_decision_path_invalid",
                message="Impact decision must change the closed generated path set.",
            )
        return self._commit(
            worktree=worktree,
            branch=branch,
            submission_id=decision_id,
            operation_id=operation_id,
            command="impact.decide",
            expected_parent=expected_parent,
            paths=paths,
        )

    @staticmethod
    def _branch_for(decision_id: str) -> str:
        return f"research/impact-decision/{decision_id}"

    @staticmethod
    def _validate_identity(decision_id: str, commit: str) -> None:
        if not _DECISION_ID.fullmatch(decision_id) or not _GIT_OBJECT_ID.fullmatch(
            commit
        ):
            GitImpactDecisionAdapter._invalid_identity()

    @staticmethod
    def _invalid_identity() -> None:
        raise RCPError(
            code="impact_decision_identity_invalid",
            message="Impact decision Git mutation requires canonical identities.",
        )
