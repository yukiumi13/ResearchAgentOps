from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from researchctl.errors import RCPError
from researchctl.services.report_impact import (
    ReportImpactBatchBundle,
    ReportImpactBundle,
)


_MAX_PULL_REQUEST_BODY_BYTES = 60 * 1024


@dataclass(frozen=True, slots=True)
class ImpactBranchDelivery:
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
class ImpactPullRequestReceipt:
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


class ImpactDeliveryPort(Protocol):
    def push_exact(
        self,
        *,
        repository_root: Path,
        branch: str,
        commit: str,
    ) -> ImpactBranchDelivery: ...

    def open_or_observe(
        self,
        *,
        impact_id: str,
        branch: ImpactBranchDelivery,
        base_branch: str,
        title: str,
        body: str,
    ) -> ImpactPullRequestReceipt: ...


def render_impact_pull_request(
    *,
    bundle: ReportImpactBundle,
    proposal_commit: str,
) -> tuple[str, str]:
    impact = bundle.impact
    title = (
        f"researchctl: {impact.outcome} impact for "
        f"{impact.report_id} r{impact.expected_report_revision}"
    )
    if len(title) > 256:
        raise RCPError(
            code="impact_pr_title_too_large",
            message="Generated Impact pull request title exceeds its bound.",
        )
    report_path = (
        f".research/reports/{impact.report_id}/"
        f"{impact.expected_report_revision + 1}.md"
    )
    matches = [item for item in bundle.files if item.path == report_path]
    if len(matches) != 1:
        raise RCPError(
            code="impact_pr_render_invalid",
            message="Generated Impact bundle has no unique review document.",
        )
    try:
        review = matches[0].content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RCPError(
            code="impact_pr_render_invalid",
            message="Generated Impact review is not UTF-8.",
        ) from error
    body = (
        review.rstrip("\n")
        + "\n\n## Proposal Identity\n\n"
        + f"- Proposal commit: `{proposal_commit}`\n"
        + f"- Bundle digest: `{bundle.manifest_digest}`\n"
        + f"- Impact digest: `{impact.impact_digest}`\n"
    )
    if len(body.encode("utf-8")) > _MAX_PULL_REQUEST_BODY_BYTES:
        raise RCPError(
            code="impact_pr_body_too_large",
            message="Generated Impact pull request body exceeds its bound.",
        )
    return title, body


def render_impact_batch_pull_request(
    *,
    bundle: ReportImpactBatchBundle,
    proposal_commit: str,
) -> tuple[str, str]:
    batch = bundle.batch
    title = f"researchctl: Report impact batch ({len(batch.impacts)} Reports)"
    rows = [
        "| Report | Revision | Outcome | Proposed applicability |",
        "| --- | ---: | --- | --- |",
    ]
    for impact in batch.impacts:
        rows.append(
            f"| `{impact.report_id}` | {impact.expected_report_revision + 1} "
            f"| `{impact.outcome}` | `{impact.proposed_applicability.value}` |"
        )
    body = "\n".join(
        [
            "# Report impact review",
            "",
            "> Batched proposal only. No Report applicability changes until review and merge.",
            "",
            *rows,
            "",
            "## Scan Identity",
            "",
            f"- Before commit: `{batch.before_commit}`",
            f"- Target commit: `{batch.target_commit}`",
            f"- Target tree: `{batch.target_tree}`",
            f"- Snapshot Reports: `{len(batch.snapshot_report_ids)}`",
            f"- Ineligible Reports: `{len(batch.ineligible_report_ids)}`",
            f"- Already up to date: `{len(batch.up_to_date_report_ids)}`",
            f"- Protocol-only changes: `{len(batch.no_code_change_report_ids)}`",
            f"- Unresolved external dependency evidence: "
            f"`{len(batch.unresolved_report_ids)}`",
            *(
                f"  - `{report_id}`"
                for report_id in batch.unresolved_report_ids
            ),
            "",
            "## Proposal Identity",
            "",
            f"- Proposal commit: `{proposal_commit}`",
            f"- Bundle digest: `{bundle.manifest_digest}`",
            f"- Batch digest: `{batch.batch_digest}`",
            "",
            "This proposal never starts or retries an experiment.",
        ]
    ) + "\n"
    if len(title) > 256:
        raise RCPError(
            code="impact_pr_title_too_large",
            message="Generated Impact pull request title exceeds its bound.",
        )
    if len(body.encode("utf-8")) > _MAX_PULL_REQUEST_BODY_BYTES:
        raise RCPError(
            code="impact_pr_body_too_large",
            message="Generated Impact pull request body exceeds its bound.",
        )
    return title, body
