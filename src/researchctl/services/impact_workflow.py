from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from researchctl.adapters.git_impact import GitImpactAdapter, ImpactCommitReceipt
from researchctl.adapters.git_worktree import GitWorktreeAdapter
from researchctl.errors import RCPError
from researchctl.repository import safe_repository_path
from researchctl.services.git_report_impact import (
    GitReportImpactAnalyzer,
    ReportImpactBatchAnalysis,
)
from researchctl.services.impact_delivery import (
    ImpactBranchDelivery,
    ImpactDeliveryPort,
    ImpactPullRequestReceipt,
    render_impact_batch_pull_request,
    render_impact_pull_request,
)
from researchctl.services.report_impact import (
    ReportImpactBatchBundle,
    ReportImpactBundle,
)
from researchctl.services.requests import (
    ImpactBatchCreateRequest,
    ImpactCreateRequest,
)


@dataclass(frozen=True, slots=True)
class PreparedImpactProposal:
    bundle: ReportImpactBundle
    commit: ImpactCommitReceipt


@dataclass(frozen=True, slots=True)
class ImpactProposalReceipt:
    prepared: PreparedImpactProposal
    branch_delivery: ImpactBranchDelivery
    pull_request: ImpactPullRequestReceipt

    @property
    def terminal_result(self) -> str:
        return "proposal_open"

    def as_dict(self) -> dict[str, object]:
        return {
            "terminal_result": self.terminal_result,
            "bundle": self.prepared.bundle.as_dict(),
            "proposal": self.prepared.commit.as_dict(),
            "delivery": {
                "branch": self.branch_delivery.as_dict(),
                "pull_request": self.pull_request.as_dict(),
            },
            "accepted": False,
            "requires_review": True,
            "automatically_runs_experiments": False,
        }


@dataclass(frozen=True, slots=True)
class PreparedImpactBatchProposal:
    analysis: ReportImpactBatchAnalysis
    commit: ImpactCommitReceipt | None

    @property
    def bundle(self) -> ReportImpactBatchBundle | None:
        return self.analysis.bundle


@dataclass(frozen=True, slots=True)
class ImpactBatchProposalReceipt:
    prepared: PreparedImpactBatchProposal
    branch_delivery: ImpactBranchDelivery | None = None
    pull_request: ImpactPullRequestReceipt | None = None

    @property
    def terminal_result(self) -> str:
        return (
            "proposal_open"
            if self.prepared.bundle is not None
            else self.prepared.analysis.terminal_result
        )

    def as_dict(self) -> dict[str, object]:
        bundle = self.prepared.bundle
        commit = self.prepared.commit
        return {
            "terminal_result": self.terminal_result,
            "analysis": self.prepared.analysis.as_dict(),
            "bundle": bundle.as_dict() if bundle is not None else None,
            "proposal": commit.as_dict() if commit is not None else None,
            "delivery": (
                {
                    "branch": self.branch_delivery.as_dict(),
                    "pull_request": self.pull_request.as_dict(),
                }
                if self.branch_delivery is not None
                and self.pull_request is not None
                else None
            ),
            "accepted": False,
            "requires_review": bundle is not None,
            "requires_input": self.terminal_result == "impact_unresolved",
            "automatically_runs_experiments": False,
        }


class ImpactWorkflowService:
    def __init__(
        self,
        *,
        repository_root: Path,
        worktrees_directory: Path,
        default_branch: str,
        analyzer: GitReportImpactAnalyzer | None = None,
        worktrees: GitWorktreeAdapter | None = None,
        commits: GitImpactAdapter | None = None,
        delivery: ImpactDeliveryPort | None = None,
    ) -> None:
        self.repository_root = Path(os.path.abspath(os.fspath(repository_root)))
        self.worktrees_directory = Path(
            os.path.abspath(os.fspath(worktrees_directory))
        )
        self.default_branch = default_branch
        self.analyzer = analyzer or GitReportImpactAnalyzer()
        self.worktrees = worktrees or GitWorktreeAdapter()
        self.commits = commits or GitImpactAdapter(worktrees=self.worktrees)
        self.delivery = delivery

    def propose(
        self,
        request: ImpactCreateRequest,
        *,
        generated_at: datetime,
        event_callback: Callable[[str, dict[str, object]], None] | None = None,
    ) -> ImpactProposalReceipt:
        if self.delivery is None:
            raise RCPError(
                code="impact_delivery_not_configured",
                message="Impact GitHub delivery is not configured.",
            )
        prepared = self.prepare_proposal(request, generated_at=generated_at)
        self._event(
            event_callback,
            "impact_proposal_prepared",
            {
                "impact_id": request.impact_id,
                "report_id": request.report_id,
                "target_commit": request.target_commit,
                "proposal_commit": prepared.commit.commit,
            },
        )
        branch = self.delivery.push_exact(
            repository_root=self.repository_root,
            branch=prepared.commit.branch,
            commit=prepared.commit.commit,
        )
        self._event(
            event_callback,
            "impact_branch_pushed",
            {
                "impact_id": request.impact_id,
                "branch": branch.branch,
                "proposal_commit": branch.commit,
                "effect_applied": branch.pushed,
            },
        )
        title, body = render_impact_pull_request(
            bundle=prepared.bundle,
            proposal_commit=prepared.commit.commit,
        )
        pull_request = self.delivery.open_or_observe(
            impact_id=request.impact_id,
            branch=branch,
            base_branch=self.default_branch,
            title=title,
            body=body,
        )
        self._event(
            event_callback,
            "impact_pr_created" if pull_request.created else "impact_pr_observed",
            {
                "impact_id": request.impact_id,
                "repository": pull_request.repository,
                "pull_request_number": pull_request.number,
                "base_branch": pull_request.base_branch,
                "head_branch": pull_request.head_branch,
                "proposal_commit": pull_request.head_commit,
            },
        )
        return ImpactProposalReceipt(
            prepared=prepared,
            branch_delivery=branch,
            pull_request=pull_request,
        )

    def propose_batch(
        self,
        request: ImpactBatchCreateRequest,
        *,
        event_callback: Callable[[str, dict[str, object]], None] | None = None,
    ) -> ImpactBatchProposalReceipt:
        analysis = self.analyze_batch(request)
        bundle = analysis.bundle
        if bundle is None:
            prepared = PreparedImpactBatchProposal(
                analysis=analysis,
                commit=None,
            )
            self._event(
                event_callback,
                (
                    "impact_batch_unresolved"
                    if analysis.unresolved_report_ids
                    else "impact_batch_no_change"
                ),
                {
                    "impact_id": request.impact_id,
                    "before_commit": request.before_commit,
                    "target_commit": request.target_commit,
                    "report_count": len(prepared.analysis.report_ids),
                    "unresolved_report_ids": list(
                        prepared.analysis.unresolved_report_ids
                    ),
                },
            )
            return ImpactBatchProposalReceipt(prepared=prepared)
        if self.delivery is None:
            raise RCPError(
                code="impact_delivery_not_configured",
                message="Impact GitHub delivery is not configured.",
            )
        prepared = self.prepare_batch(request, analysis=analysis)
        commit = prepared.commit
        assert commit is not None
        self._event(
            event_callback,
            "impact_proposal_prepared",
            {
                "impact_id": request.impact_id,
                "target_commit": request.target_commit,
                "proposal_commit": commit.commit,
                "report_count": len(bundle.report_bundles),
            },
        )
        branch = self.delivery.push_exact(
            repository_root=self.repository_root,
            branch=commit.branch,
            commit=commit.commit,
        )
        self._event(
            event_callback,
            "impact_branch_pushed",
            {
                "impact_id": request.impact_id,
                "branch": branch.branch,
                "proposal_commit": branch.commit,
                "effect_applied": branch.pushed,
            },
        )
        title, body = render_impact_batch_pull_request(
            bundle=bundle,
            proposal_commit=commit.commit,
        )
        pull_request = self.delivery.open_or_observe(
            impact_id=request.impact_id,
            branch=branch,
            base_branch=self.default_branch,
            title=title,
            body=body,
        )
        self._event(
            event_callback,
            "impact_pr_created" if pull_request.created else "impact_pr_observed",
            {
                "impact_id": request.impact_id,
                "repository": pull_request.repository,
                "pull_request_number": pull_request.number,
                "base_branch": pull_request.base_branch,
                "head_branch": pull_request.head_branch,
                "proposal_commit": pull_request.head_commit,
            },
        )
        return ImpactBatchProposalReceipt(
            prepared=prepared,
            branch_delivery=branch,
            pull_request=pull_request,
        )

    def prepare_proposal(
        self,
        request: ImpactCreateRequest,
        *,
        generated_at: datetime,
    ) -> PreparedImpactProposal:
        default_head = self.worktrees.resolve_commit(
            self.repository_root,
            f"refs/heads/{self.default_branch}",
        )
        if default_head != request.target_commit:
            raise RCPError(
                code="stale_impact_target",
                message="Impact target is not the current protected default head.",
                context={
                    "expected_target": request.target_commit,
                    "observed_default_head": default_head,
                },
            )
        bundle = self.analyzer.analyze(
            self.repository_root,
            impact_id=request.impact_id,
            report_id=request.report_id,
            expected_report_revision=request.expected_report_revision,
            target_commit=request.target_commit,
            generated_at=generated_at,
        )
        branch, worktree = self.commits.prepare_worktree(
            repository_root=self.repository_root,
            worktrees_directory=self.worktrees_directory,
            impact_id=request.impact_id,
            target_commit=request.target_commit,
        )
        self._write_bundle(worktree, bundle)
        committed = self.commits.commit_proposal(
            worktree=worktree,
            branch=branch,
            impact_id=request.impact_id,
            operation_id=request.operation_id,
            expected_parent=request.target_commit,
            manifest_digest=bundle.manifest_digest,
            paths=tuple(item.path for item in bundle.files),
            committed_at=generated_at,
        )
        return PreparedImpactProposal(bundle=bundle, commit=committed)

    def prepare_batch(
        self,
        request: ImpactBatchCreateRequest,
        *,
        analysis: ReportImpactBatchAnalysis | None = None,
    ) -> PreparedImpactBatchProposal:
        selected_analysis = analysis or self.analyze_batch(request)
        bundle = selected_analysis.bundle
        if bundle is None:
            return PreparedImpactBatchProposal(
                analysis=selected_analysis,
                commit=None,
            )
        branch, worktree = self.commits.prepare_worktree(
            repository_root=self.repository_root,
            worktrees_directory=self.worktrees_directory,
            impact_id=request.impact_id,
            target_commit=request.target_commit,
        )
        self._write_bundle(worktree, bundle)
        committed = self.commits.commit_proposal(
            worktree=worktree,
            branch=branch,
            impact_id=request.impact_id,
            operation_id=request.operation_id,
            expected_parent=request.target_commit,
            manifest_digest=bundle.manifest_digest,
            paths=tuple(item.path for item in bundle.files),
            command="impact.batch",
            committed_at=request.generated_at,
        )
        return PreparedImpactBatchProposal(
            analysis=selected_analysis,
            commit=committed,
        )

    def analyze_batch(
        self,
        request: ImpactBatchCreateRequest,
    ) -> ReportImpactBatchAnalysis:
        default_head = self.worktrees.resolve_commit(
            self.repository_root,
            f"refs/heads/{self.default_branch}",
        )
        if default_head != request.target_commit:
            raise RCPError(
                code="stale_impact_target",
                message="Impact target is not the current protected default head.",
                context={
                    "expected_target": request.target_commit,
                    "observed_default_head": default_head,
                },
            )
        return self.analyzer.scan(
            self.repository_root,
            impact_id=request.impact_id,
            before_commit=request.before_commit,
            target_commit=request.target_commit,
            generated_at=request.generated_at,
        )

    @staticmethod
    def _write_bundle(
        worktree: Path,
        bundle: ReportImpactBundle | ReportImpactBatchBundle,
    ) -> None:
        for rendered in bundle.files:
            path = safe_repository_path(
                worktree,
                rendered.path,
                managed_only=True,
            )
            if path.exists():
                if path.is_symlink() or not path.is_file():
                    raise RCPError(
                        code="impact_output_conflict",
                        message="Generated Impact path is not a regular file.",
                        context={"path": rendered.path},
                    )
                if path.read_bytes() != rendered.content:
                    raise RCPError(
                        code="impact_output_conflict",
                        message="Existing Impact output differs from regenerated bytes.",
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
