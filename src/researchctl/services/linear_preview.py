from __future__ import annotations

import hashlib
import html
from dataclasses import dataclass

from researchctl.domain.models import (
    LinearProjectionConfigured,
    LinearProjectionDisabled,
    LinearProjectionPolicy,
    ReportRecord,
    ResearchSubmission,
    ReviewDecision,
    TaskRecord,
)
from researchctl.errors import RCPError

LINEAR_RENDERER_ID = "linear.accepted-result-markdown.v2"
LINEAR_RENDERER_VERSION = 2


def _text(value: object) -> str:
    escaped = html.escape(str(value), quote=False).replace("\\", "\\\\")
    for character in ("`", "*", "_", "[", "]", "#", "|"):
        escaped = escaped.replace(character, f"\\{character}")
    normalized = escaped.replace("\r\n", "\n").replace("\r", "\n")
    return "<br>\n".join(normalized.split("\n"))


@dataclass(frozen=True, slots=True)
class LinearPreview:
    projection: LinearProjectionConfigured | LinearProjectionDisabled
    body: bytes | None

    def as_dict(self) -> dict[str, object]:
        return {
            "projection": self.projection.model_dump(mode="json", exclude_none=True),
            "body": self.body.decode("utf-8") if self.body is not None else None,
        }


def disabled_linear_preview(
    reason: str = "integration_not_configured",
) -> LinearPreview:
    return LinearPreview(
        projection=LinearProjectionDisabled(reason=reason),
        body=None,
    )


def render_linear_accepted_result(
    *,
    task: TaskRecord,
    submission: ResearchSubmission,
    decision: ReviewDecision,
    report: ReportRecord,
) -> bytes:
    if (
        decision.submission_id != submission.submission_id
        or report.submission_id != submission.submission_id
        or decision.report_id != report.report_id
    ):
        raise RCPError(
            code="linear_render_linkage_invalid",
            message="Accepted Report, Decision, and Submission linkage is invalid.",
        )
    lines = [
        f"<!-- researchctl-renderer:{LINEAR_RENDERER_ID} -->",
        "## Accepted research result",
        "",
        _text(report.claim),
        "",
        f"- Task: `{task.key}` (`{task.task_id}`)",
        f"- Agent: `agent-{submission.session_id}`",
        f"- Session: `{submission.session_id}`",
        f"- Report: `{report.report_id}` revision "
        f"`{report.revision}`",
        f"- Submission: `{submission.submission_id}`",
        f"- Decision: `{decision.decision_id}`",
        f"- Evidence: `{report.evidence_status.value}`",
        f"- Applicability: `{report.applicability.value}`",
        f"- Scope: `{report.claim_scope.value}`",
        f"- Evidence tree: `{report.evidence_tree}`",
    ]
    if decision.conditions:
        lines.extend(["", "Conditions:"])
        lines.extend(f"- {_text(value)}" for value in decision.conditions)
    if submission.limitations:
        lines.extend(["", "Limitations:"])
        lines.extend(f"- {_text(value)}" for value in submission.limitations)
    return ("\n".join(lines) + "\n").encode("utf-8")


def build_linear_preview(
    *,
    policy: LinearProjectionPolicy | None,
    task: TaskRecord,
    submission: ResearchSubmission,
    decision: ReviewDecision,
    report: ReportRecord,
) -> LinearPreview:
    if policy is None:
        return disabled_linear_preview()
    if task.linear_issue_id is None:
        raise RCPError(
            code="linear_binding_incomplete",
            message="Configured Linear projection requires an exact Task issue UUID.",
            remediation="Add a manager-reviewed linear_issue_id to the canonical Task.",
            context={"task_id": task.task_id},
        )
    body = render_linear_accepted_result(
        task=task,
        submission=submission,
        decision=decision,
        report=report,
    )
    digest = f"sha256:{hashlib.sha256(body).hexdigest()}"
    return LinearPreview(
        projection=LinearProjectionConfigured(
            workspace_id=policy.workspace_id,
            team_id=policy.team_id,
            project_id=policy.project_id,
            issue_id=task.linear_issue_id,
            renderer_id=policy.renderer_id,
            renderer_version=policy.renderer_version,
            payload_digest=digest,
        ),
        body=body,
    )
