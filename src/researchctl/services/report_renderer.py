from __future__ import annotations

import html
import json
from collections.abc import Sequence

from researchctl.domain.models import (
    ReportProposal,
    ReportRecord,
    ResearchSubmission,
    ReviewDecision,
    RunResult,
    TaskRecord,
)


REPORT_RENDERER_ID = "research-report.v2"
REPORT_RENDERER_VERSION = 2


def _text(value: object) -> str:
    escaped = html.escape(str(value), quote=False).replace("\\", "\\\\")
    for character in ("`", "*", "_", "[", "]", "#", "|"):
        escaped = escaped.replace(character, f"\\{character}")
    normalized = escaped.replace("\r\n", "\n").replace("\r", "\n")
    return "<br>\n".join(normalized.split("\n"))


def _items(values: Sequence[object], *, empty: str = "None.") -> list[str]:
    if not values:
        return [empty]
    return [f"- {_text(value)}" for value in values]


def _metrics(value: dict[str, object]) -> list[str]:
    if not value:
        return ["None."]
    content = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    return [f"<pre>{html.escape(content, quote=True)}</pre>"]


def render_submission_review(
    *,
    task: TaskRecord,
    submission: ResearchSubmission,
    proposal: ReportProposal,
    results: Sequence[RunResult],
    source_digest: str,
) -> bytes:
    lines = [
        f"# Research submission: {_text(task.key)}",
        "",
        "> Proposal only. This document is generated and is not an accepted Report.",
        "",
        f"- Submission: `{submission.submission_id}`",
        f"- Task: `{task.task_id}`",
        f"- Session: `{submission.session_id}`",
        f"- Category: `{submission.category.value}`",
        f"- Proposed Report: `{proposal.report_id}`",
        f"- Expected current Report revision: "
        f"`{proposal.expected_report_revision}`",
        f"- Evidence tree: `{proposal.evidence_tree}`",
        f"- Canonical source digest: `{source_digest}`",
        "",
        "## Claim",
        "",
        _text(submission.claim),
        "",
        "## Evidence",
        "",
    ]
    lines.extend(
        (
            f"- `{result.result_id}`: "
            f"`{result.outcome.value}` "
            f"from Run `{result.run_id}`"
        )
        for result in results
    )
    lines.extend(["", "## Metrics", "", *_metrics(submission.metrics)])
    lines.extend(["", "## Limitations", "", *_items(submission.limitations)])
    dependencies = [
        *(f"path:{value}" for value in submission.dependencies.paths),
        *(f"resource:{value}" for value in submission.dependencies.resources),
        *(
            f"environment:{value}"
            for value in submission.dependencies.environments
        ),
    ]
    lines.extend(["", "## Dependencies", "", *_items(dependencies)])
    lines.extend(["", "## Decision Requests", ""])
    if submission.decision_needed:
        for decision in submission.decision_needed:
            lines.append(f"- {_text(decision.question)}")
            lines.extend(f"  - {_text(option)}" for option in decision.options)
    else:
        lines.append("None.")
    lines.extend(["", "## Review Bundle", ""])
    if submission.review_bundle:
        lines.extend(
            (
                f"- `{item.name}`: `{item.digest}`, "
                f"{item.size_bytes} bytes, `{_text(item.uri)}`"
            )
            for item in submission.review_bundle
        )
    else:
        lines.append("None.")
    return ("\n".join(lines) + "\n").encode("utf-8")


def render_report_preview(
    *,
    task: TaskRecord,
    submission: ResearchSubmission,
    proposal: ReportProposal,
    results: Sequence[RunResult],
    source_digest: str,
) -> bytes:
    lines = [
        f"# {_text(proposal.title)}",
        "",
        "> Proposed Report preview. It has no accepted authority.",
        "",
        f"- Report: `{proposal.report_id}`",
        f"- Next revision: `{proposal.expected_report_revision + 1}`",
        f"- Submission: `{submission.submission_id}`",
        f"- Task: `{task.task_id}`",
        f"- Evidence tree: `{proposal.evidence_tree}`",
        f"- Canonical source digest: `{source_digest}`",
        "",
        "## Claim",
        "",
        _text(submission.claim),
        "",
        "## Run Results",
        "",
    ]
    lines.extend(
        f"- `{result.result_id}`: "
        f"`{result.outcome.value}`"
        for result in results
    )
    lines.extend(["", "## Dependencies", ""])
    dependencies = [
        *(f"path:{value}" for value in submission.dependencies.paths),
        *(f"resource:{value}" for value in submission.dependencies.resources),
        *(f"environment:{value}" for value in submission.dependencies.environments),
    ]
    lines.extend(_items(dependencies))
    return ("\n".join(lines) + "\n").encode("utf-8")


def render_accepted_report(
    *,
    task: TaskRecord,
    submission: ResearchSubmission,
    decision: ReviewDecision,
    report: ReportRecord,
    results: Sequence[RunResult],
    source_digest: str,
) -> bytes:
    lines = [
        f"# {_text(report.title)}",
        "",
        f"- Report: `{report.report_id}` revision "
        f"`{report.revision}`",
        f"- Submission: `{submission.submission_id}`",
        f"- Task: `{task.task_id}`",
        f"- Decision: `{decision.decision_id}`",
        f"- Evidence status: `{report.evidence_status.value}`",
        f"- Applicability: `{report.applicability.value}`",
        f"- Claim scope: `{report.claim_scope.value}`",
        f"- Code disposition: `{decision.code_disposition.value}`",
        f"- Evidence tree: `{report.evidence_tree}`",
        f"- Accepted base tree: `{report.accepted_at_main_tree}`",
        f"- Canonical source digest: `{source_digest}`",
        "",
        "## Claim",
        "",
        _text(report.claim),
        "",
        "## Run Results",
        "",
    ]
    lines.extend(
        f"- `{result.result_id}`: "
        f"`{result.outcome.value}`"
        for result in results
    )
    lines.extend(["", "## Metrics", "", *_metrics(submission.metrics)])
    lines.extend(["", "## Conditions", "", *_items(decision.conditions)])
    lines.extend(["", "## Limitations", "", *_items(submission.limitations)])
    return ("\n".join(lines) + "\n").encode("utf-8")
