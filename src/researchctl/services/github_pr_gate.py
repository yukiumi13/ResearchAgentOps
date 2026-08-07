from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from researchctl.services.github_governance import GitHubGovernanceObservation


CheckState = Literal["passed", "pending", "failed", "missing"]
PullRequestGateResult = Literal[
    "ready",
    "review_pending",
    "checks_pending",
    "ci_capacity_pending",
    "validation_failed",
    "governance_misconfigured",
]

_PASSING_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})
_PENDING_STATUSES = frozenset({"queued", "in_progress", "pending", "requested", "waiting"})


@dataclass(frozen=True, slots=True)
class GitHubRequiredCheck:
    name: str
    status: str
    conclusion: str | None
    details_url: str | None = None

    @property
    def state(self) -> CheckState:
        if self.status in _PENDING_STATUSES or self.conclusion is None:
            return "pending"
        if self.conclusion in _PASSING_CONCLUSIONS:
            return "passed"
        return "failed"

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "state": self.state,
            "status": self.status,
            "conclusion": self.conclusion,
            "details_url": self.details_url,
        }


@dataclass(frozen=True, slots=True)
class GitHubWorkflowRun:
    run_id: int
    name: str
    status: str
    conclusion: str | None
    attempt: int
    url: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "name": self.name,
            "status": self.status,
            "conclusion": self.conclusion,
            "attempt": self.attempt,
            "url": self.url,
        }


@dataclass(frozen=True, slots=True)
class GitHubRunnerCapacityEvidence:
    workflow_run_id: int
    job_id: int
    check_name: str
    runner_labels: tuple[str, ...]
    message: str

    def as_dict(self) -> dict[str, object]:
        return {
            "workflow_run_id": self.workflow_run_id,
            "job_id": self.job_id,
            "check_name": self.check_name,
            "runner_labels": list(self.runner_labels),
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class GitHubPullRequestGateObservation:
    repository: str
    pull_request_number: int
    head_sha: str
    base_branch: str
    draft: bool
    mergeable_state: str
    approved_reviewers: tuple[str, ...]
    required_checks: tuple[str, ...]
    checks: tuple[GitHubRequiredCheck, ...]
    workflow_runs: tuple[GitHubWorkflowRun, ...]
    capacity_evidence: tuple[GitHubRunnerCapacityEvidence, ...]

    def as_dict(self) -> dict[str, object]:
        observed = {check.name: check for check in self.checks}
        checks: list[dict[str, object]] = []
        for name in self.required_checks:
            check = observed.get(name)
            checks.append(
                check.as_dict()
                if check is not None
                else {
                    "name": name,
                    "state": "missing",
                    "status": "missing",
                    "conclusion": None,
                    "details_url": None,
                }
            )
        return {
            "repository": self.repository,
            "pull_request_number": self.pull_request_number,
            "head_sha": self.head_sha,
            "base_branch": self.base_branch,
            "draft": self.draft,
            "mergeable_state": self.mergeable_state,
            "approved_reviewers": list(self.approved_reviewers),
            "required_checks": checks,
            "workflow_runs": [item.as_dict() for item in self.workflow_runs],
            "capacity_evidence": [item.as_dict() for item in self.capacity_evidence],
        }


@dataclass(frozen=True, slots=True)
class GitHubPullRequestGateReport:
    terminal_result: PullRequestGateResult
    merge_allowed: bool
    required_approvals: int
    observation: GitHubPullRequestGateObservation
    recommendation: str

    def as_dict(self) -> dict[str, object]:
        return {
            "terminal_result": self.terminal_result,
            "merge_allowed": self.merge_allowed,
            "required_approvals": self.required_approvals,
            "recommendation": self.recommendation,
            "observation": self.observation.as_dict(),
        }


def assess_github_pull_request_gate(
    observation: GitHubPullRequestGateObservation,
    *,
    governance: GitHubGovernanceObservation,
) -> GitHubPullRequestGateReport:
    if observation.base_branch != governance.branch:
        return _report(
            "governance_misconfigured",
            observation,
            governance,
            "Audit the ruleset or branch protection that applies to the pull request base.",
        )
    if not governance.protection_sources or not observation.required_checks:
        return _report(
            "governance_misconfigured",
            observation,
            governance,
            "Install an active merge gate with at least one authenticated required check.",
        )

    observed = {check.name: check for check in observation.checks}
    states = {
        name: observed[name].state if name in observed else "missing"
        for name in observation.required_checks
    }
    unresolved = {name for name, state in states.items() if state != "passed"}
    capacity_checks = {item.check_name for item in observation.capacity_evidence}
    if unresolved & capacity_checks:
        return _report(
            "ci_capacity_pending",
            observation,
            governance,
            (
                "Restore billing or runner capacity, or attach an authorized runner; "
                "do not disable the merge ruleset to substitute for validation."
            ),
        )
    if any(state == "failed" for state in states.values()):
        return _report(
            "validation_failed",
            observation,
            governance,
            "Open the named check, fix its findings, and rerun it for this exact head.",
        )
    if any(state in {"missing", "pending"} for state in states.values()):
        return _report(
            "checks_pending",
            observation,
            governance,
            "Wait for the required checks or inspect their workflow dispatch state.",
        )

    review_count = len(observation.approved_reviewers)
    if (
        observation.draft
        or review_count < governance.required_approvals
        or observation.mergeable_state != "clean"
    ):
        return _report(
            "review_pending",
            observation,
            governance,
            "Obtain the current required Manager/CODEOWNER review and resolve remaining PR gates.",
        )
    return _report(
        "ready",
        observation,
        governance,
        "The observed exact head satisfies the required checks and review count.",
    )


def _report(
    result: PullRequestGateResult,
    observation: GitHubPullRequestGateObservation,
    governance: GitHubGovernanceObservation,
    recommendation: str,
) -> GitHubPullRequestGateReport:
    return GitHubPullRequestGateReport(
        terminal_result=result,
        merge_allowed=result == "ready",
        required_approvals=governance.required_approvals,
        observation=observation,
        recommendation=recommendation,
    )
