from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from researchctl.adapters.git_ci import GitCIObjectReader
from researchctl.adapters.git_impact_decision import GitImpactDecisionAdapter
from researchctl.adapters.git_submission import SubmissionCommitReceipt
from researchctl.adapters.git_worktree import GitWorktreeAdapter
from researchctl.errors import RCPError
from researchctl.repository import safe_repository_path
from researchctl.services.git_report_impact import GitReportImpactAnalyzer
from researchctl.services.impact_decision import (
    ImpactDecisionBuilder,
    ImpactDecisionBundle,
)
from researchctl.services.impact_decision_delivery import (
    ImpactDecisionDeliveryPort,
    render_impact_decision_pull_request,
)
from researchctl.services.requests import ImpactDecisionCreateRequest
from researchctl.services.submission_delivery import (
    SubmissionBranchDelivery,
    SubmissionPullRequestReceipt,
)


@dataclass(frozen=True, slots=True)
class PreparedImpactDecision:
    bundle: ImpactDecisionBundle
    commit: SubmissionCommitReceipt


@dataclass(frozen=True, slots=True)
class ImpactDecisionProposalReceipt:
    prepared: PreparedImpactDecision
    branch_delivery: SubmissionBranchDelivery
    pull_request: SubmissionPullRequestReceipt

    @property
    def terminal_result(self) -> str:
        return "proposal_open"

    def as_dict(self) -> dict[str, object]:
        commit = self.prepared.commit
        return {
            "terminal_result": self.terminal_result,
            "bundle": self.prepared.bundle.as_dict(),
            "proposal": commit.as_dict(),
            "delivery": {
                "branch": self.branch_delivery.as_dict(),
                "pull_request": self.pull_request.as_dict(),
            },
            "accepted": False,
            "requires_review": True,
            "automatically_runs_experiments": False,
        }


class ImpactDecisionWorkflowService:
    def __init__(
        self,
        *,
        repository_root: Path,
        worktrees_directory: Path,
        default_branch: str,
        git: GitCIObjectReader | None = None,
        worktrees: GitWorktreeAdapter | None = None,
        reports: GitReportImpactAnalyzer | None = None,
        builder: ImpactDecisionBuilder | None = None,
        commits: GitImpactDecisionAdapter | None = None,
        delivery: ImpactDecisionDeliveryPort | None = None,
    ) -> None:
        self.repository_root = Path(
            os.path.abspath(os.fspath(repository_root))
        )
        self.worktrees_directory = Path(
            os.path.abspath(os.fspath(worktrees_directory))
        )
        self.default_branch = default_branch
        self.git = git or GitCIObjectReader()
        self.worktrees = worktrees or GitWorktreeAdapter()
        self.reports = reports or GitReportImpactAnalyzer(git=self.git)
        self.builder = builder or ImpactDecisionBuilder()
        self.commits = commits or GitImpactDecisionAdapter(
            worktrees=self.worktrees
        )
        self.delivery = delivery

    def propose(
        self,
        request: ImpactDecisionCreateRequest,
        *,
        reviewer_actor: str,
        decided_at: datetime,
        event_callback: Callable[[str, dict[str, object]], None] | None = None,
    ) -> ImpactDecisionProposalReceipt:
        if self.delivery is None:
            raise RCPError(
                code="impact_decision_delivery_not_configured",
                message="Impact decision GitHub delivery is not configured.",
            )
        prepared = self.prepare(
            request,
            reviewer_actor=reviewer_actor,
            decided_at=decided_at,
        )
        commit = prepared.commit
        self._event(
            event_callback,
            "impact_decision_prepared",
            {
                "decision_id": request.decision_id,
                "impact_id": request.impact_id,
                "report_id": request.report_id,
                "proposal_commit": commit.commit,
            },
        )
        branch = self.delivery.push_exact(
            repository_root=self.repository_root,
            branch=commit.branch,
            commit=commit.commit,
        )
        self._event(
            event_callback,
            "impact_decision_branch_pushed",
            {
                "decision_id": request.decision_id,
                "branch": branch.branch,
                "proposal_commit": branch.commit,
                "effect_applied": branch.pushed,
            },
        )
        title, body = render_impact_decision_pull_request(
            bundle=prepared.bundle,
            proposal_commit=commit.commit,
        )
        pull_request = self.delivery.open_or_observe(
            decision_id=request.decision_id,
            branch=branch,
            base_branch=self.default_branch,
            title=title,
            body=body,
        )
        self._event(
            event_callback,
            (
                "impact_decision_pr_created"
                if pull_request.created
                else "impact_decision_pr_observed"
            ),
            {
                "decision_id": request.decision_id,
                "repository": pull_request.repository,
                "pull_request_number": pull_request.number,
                "base_branch": pull_request.base_branch,
                "head_branch": pull_request.head_branch,
                "proposal_commit": pull_request.head_commit,
            },
        )
        return ImpactDecisionProposalReceipt(
            prepared=prepared,
            branch_delivery=branch,
            pull_request=pull_request,
        )

    def prepare(
        self,
        request: ImpactDecisionCreateRequest,
        *,
        reviewer_actor: str,
        decided_at: datetime,
    ) -> PreparedImpactDecision:
        default_head = self.worktrees.resolve_commit(
            self.repository_root,
            f"refs/heads/{self.default_branch}",
        )
        if default_head != request.target_commit:
            raise RCPError(
                code="stale_impact_decision_target",
                message="Impact decision target is not current protected main.",
                context={
                    "expected_target": request.target_commit,
                    "observed_default_head": default_head,
                },
            )
        target = self.git.read_commit(self.repository_root, default_head)
        impact = self.reports.load_impact(
            self.repository_root,
            commit=target.object_id,
            impact_id=request.impact_id,
            report_id=request.report_id,
        )
        if impact.impact_digest != request.expected_impact_digest:
            raise RCPError(
                code="stale_impact_digest",
                message="Impact record differs from the manager-reviewed digest.",
                context={
                    "expected_impact_digest": request.expected_impact_digest,
                    "observed_impact_digest": impact.impact_digest,
                },
            )
        report = self.reports.load_latest_report(
            self.repository_root,
            commit=target.object_id,
            report_id=request.report_id,
        )
        bundle = self.builder.build(
            impact=impact,
            report=report,
            decision_id=request.decision_id,
            expected_report_revision=request.expected_report_revision,
            decision_base_commit=target.object_id,
            decision_base_tree=target.tree,
            disposition=request.disposition,
            reviewer_actor=reviewer_actor,
            reason=request.reason,
            decided_at=decided_at,
            rerun_task_id=request.rerun_task_id,
            replacement_dependencies=request.replacement_dependencies,
        )
        branch, worktree = self.commits.prepare_worktree(
            repository_root=self.repository_root,
            worktrees_directory=self.worktrees_directory,
            decision_id=request.decision_id,
            base_commit=target.object_id,
        )
        self._write_bundle(worktree, bundle)
        commit = self.commits.commit_decision(
            worktree=worktree,
            branch=branch,
            decision_id=request.decision_id,
            report_id=bundle.report.report_id,
            report_revision=bundle.report.revision,
            operation_id=request.operation_id,
            expected_parent=target.object_id,
            paths=tuple(item.path for item in bundle.files),
        )
        return PreparedImpactDecision(bundle=bundle, commit=commit)

    @staticmethod
    def _write_bundle(worktree: Path, bundle: ImpactDecisionBundle) -> None:
        for rendered in bundle.files:
            path = safe_repository_path(
                worktree,
                rendered.path,
                managed_only=True,
            )
            if path.exists():
                if (
                    path.is_symlink()
                    or not path.is_file()
                    or path.read_bytes() != rendered.content
                ):
                    raise RCPError(
                        code="impact_decision_output_conflict",
                        message=(
                            "Existing decision output differs from regenerated "
                            "bytes."
                        ),
                        context={"path": rendered.path},
                    )
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(rendered.content)

    @staticmethod
    def _event(
        callback: Callable[[str, dict[str, object]], None] | None,
        kind: str,
        payload: dict[str, object],
    ) -> None:
        if callback is not None:
            callback(kind, payload)
