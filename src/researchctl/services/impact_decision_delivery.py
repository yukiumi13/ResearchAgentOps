from __future__ import annotations

from pathlib import Path
from typing import Protocol

from researchctl.errors import RCPError
from researchctl.services.impact_decision import ImpactDecisionBundle
from researchctl.services.submission_delivery import (
    SubmissionBranchDelivery,
    SubmissionPullRequestReceipt,
)


class ImpactDecisionDeliveryPort(Protocol):
    def push_exact(
        self,
        *,
        repository_root: Path,
        branch: str,
        commit: str,
    ) -> SubmissionBranchDelivery: ...

    def open_or_observe(
        self,
        *,
        decision_id: str,
        branch: SubmissionBranchDelivery,
        base_branch: str,
        title: str,
        body: str,
    ) -> SubmissionPullRequestReceipt: ...


def render_impact_decision_pull_request(
    *,
    bundle: ImpactDecisionBundle,
    proposal_commit: str,
) -> tuple[str, str]:
    decision = bundle.decision
    title = (
        f"researchctl: {decision.disposition.value} for "
        f"{decision.report_id} r{bundle.report.revision}"
    )
    report_path = (
        f".research/reports/{bundle.report.report_id}/{bundle.report.revision}.md"
    )
    matches = [item for item in bundle.files if item.path == report_path]
    if len(matches) != 1:
        raise RCPError(
            code="impact_decision_pr_render_invalid",
            message="Impact decision bundle has no unique review document.",
        )
    try:
        review = matches[0].content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RCPError(
            code="impact_decision_pr_render_invalid",
            message="Impact decision review is not UTF-8.",
        ) from error
    body = (
        review.rstrip("\n")
        + "\n\n## Proposal Identity\n\n"
        + f"- Proposal commit: `{proposal_commit}`\n"
        + f"- Bundle digest: `{bundle.manifest_digest}`\n"
        + f"- Decision digest: `{decision.decision_digest}`\n"
    )
    if len(title) > 256 or len(body.encode("utf-8")) > 60 * 1024:
        raise RCPError(
            code="impact_decision_pr_render_invalid",
            message="Impact decision pull request metadata exceeds its bound.",
        )
    return title, body
