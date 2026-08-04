from __future__ import annotations

import re

from researchctl.adapters.github_submission import GitHubSubmissionDelivery
from researchctl.services.submission_delivery import (
    SubmissionBranchDelivery,
    SubmissionPullRequestReceipt,
)


_GIT_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_DECISION_BRANCH = re.compile(
    r"^research/impact-decision/"
    r"(decision_\d{8}T\d{6}Z_[0-9a-f]{24})$"
)


class GitHubImpactDecisionDelivery(GitHubSubmissionDelivery):
    """Deliver one manager-authored ImpactDecision PR on a derived branch."""

    @staticmethod
    def _require_branch_commit(branch: str, commit: str) -> None:
        if (
            _DECISION_BRANCH.fullmatch(branch) is None
            or _GIT_OBJECT_ID.fullmatch(commit) is None
        ):
            GitHubSubmissionDelivery._invalid_request()

    @staticmethod
    def _pull_identity_matches(decision_id: str, branch: str) -> bool:
        matched = _DECISION_BRANCH.fullmatch(branch)
        return matched is not None and matched.group(1) == decision_id

    def open_or_observe(
        self,
        *,
        decision_id: str,
        branch: SubmissionBranchDelivery,
        base_branch: str,
        title: str,
        body: str,
    ) -> SubmissionPullRequestReceipt:
        return super().open_or_observe(
            submission_id=decision_id,
            branch=branch,
            base_branch=base_branch,
            title=title,
            body=body,
        )
