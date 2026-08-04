from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from researchctl.domain.models import ReportProposal, ResearchSubmission, TaskRecord
from researchctl.errors import RCPError
from researchctl.services.submissions import SubmissionBundle


_MAX_PULL_REQUEST_BODY_BYTES = 60 * 1024


@dataclass(frozen=True, slots=True)
class SubmissionBranchDelivery:
    remote: str
    branch: str
    ref: str
    commit: str
    pushed: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "remote": self.remote,
            "branch": self.branch,
            "ref": self.ref,
            "commit": self.commit,
            "pushed": self.pushed,
        }


@dataclass(frozen=True, slots=True)
class SubmissionPullRequestReceipt:
    host: str
    repository: str
    number: int
    url: str
    state: str
    base_branch: str
    head_branch: str
    head_commit: str
    created: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "host": self.host,
            "repository": self.repository,
            "number": self.number,
            "url": self.url,
            "state": self.state,
            "base_branch": self.base_branch,
            "head_branch": self.head_branch,
            "head_commit": self.head_commit,
            "created": self.created,
        }


class SubmissionDeliveryPort(Protocol):
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
        submission_id: str,
        branch: SubmissionBranchDelivery,
        base_branch: str,
        title: str,
        body: str,
    ) -> SubmissionPullRequestReceipt: ...


def render_submission_pull_request(
    *,
    task: TaskRecord,
    submission: ResearchSubmission,
    proposal: ReportProposal,
    bundle: SubmissionBundle,
    proposal_commit: str,
) -> tuple[str, str]:
    title = f"researchctl: {task.key} proposal {submission.submission_id}"
    if len(title) > 256:
        raise RCPError(
            code="submission_pr_title_too_large",
            message="Generated Submission pull request title exceeds its bound.",
        )
    review_path = (
        f".research/submissions/{submission.submission_id}/review.md"
    )
    matches = [item for item in bundle.files if item.path == review_path]
    if len(matches) != 1:
        raise RCPError(
            code="submission_pr_render_invalid",
            message="Generated Submission bundle has no unique review document.",
        )
    try:
        review = matches[0].content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RCPError(
            code="submission_pr_render_invalid",
            message="Generated Submission review is not UTF-8.",
        ) from error
    body = (
        review.rstrip("\n")
        + "\n\n## Proposal Identity\n\n"
        + f"- Proposal commit: `{proposal_commit}`\n"
        + f"- Bundle digest: `{bundle.manifest_digest}`\n"
        + f"- Proposed Report: `{proposal.report_id}`\n"
    )
    if len(body.encode("utf-8")) > _MAX_PULL_REQUEST_BODY_BYTES:
        raise RCPError(
            code="submission_pr_body_too_large",
            message="Generated Submission pull request body exceeds its bound.",
        )
    return title, body
