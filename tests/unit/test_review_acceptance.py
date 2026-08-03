from __future__ import annotations

import hashlib

import pytest

from researchctl.domain.enums import (
    ClaimScope,
    CodeDisposition,
    ReviewDisposition,
)
from researchctl.domain.models import (
    LinearProjectionPolicy,
    ReportProposal,
    ResearchSubmission,
    RunResult,
    RunSpec,
    TaskRecord,
)
from researchctl.errors import RCPError
from researchctl.serialization import canonical_digest
from researchctl.services.linear_preview import build_linear_preview
from researchctl.services.review_acceptance import ReviewAcceptanceBuilder
from researchctl.services.submissions import SubmissionEvidence


WORKSPACE_ID = "11111111-1111-4111-8111-111111111111"
TEAM_ID = "22222222-2222-4222-8222-222222222222"
PROJECT_ID = "33333333-3333-4333-8333-333333333333"
ISSUE_ID = "44444444-4444-4444-8444-444444444444"


def _records(
    task_payload,
    run_spec_payload,
    run_result_payload,
    submission_payload,
    *,
    linear: bool = False,
):
    task = TaskRecord.model_validate(
        task_payload(
            state="ready",
            linear_issue_id=ISSUE_ID if linear else None,
        )
    )
    spec = RunSpec.model_validate(
        run_spec_payload(
            inputs=[
                {
                    "kind": "dataset",
                    "logical_id": "validation-split",
                    "version": "2026-08-01",
                    "waiver_allowed": False,
                }
            ]
        )
    )
    result = RunResult.model_validate(
        run_result_payload(run_spec_digest=spec.spec_digest)
    )
    submission = ResearchSubmission.model_validate(
        submission_payload(
            state="open",
            run_result_ids=[result.result_id],
            limitations=["One seed was evaluated."],
        )
    )
    proposal = ReportProposal(
        submission_id=submission.submission_id,
        report_id="report_20260802T123456Z_" + "9" * 24,
        expected_report_revision=0,
        title="Stopping policy result",
        evidence_tree=spec.source_tree,
    )
    return task, submission, proposal, (SubmissionEvidence(spec, result),)


def test_manager_acceptance_materializes_one_atomic_snapshot_revision(
    task_payload,
    run_spec_payload,
    run_result_payload,
    submission_payload,
) -> None:
    task, submission, proposal, evidence = _records(
        task_payload,
        run_spec_payload,
        run_result_payload,
        submission_payload,
    )
    accepted = ReviewAcceptanceBuilder().build(
        task=task,
        submission=submission,
        proposal=proposal,
        evidence=evidence,
        current_report=None,
        decision_id="decision_20260802T123456Z_" + "7" * 24,
        reviewer_actor="uid-1000",
        decided_at="2026-08-03T12:00:00Z",
        disposition=ReviewDisposition.ACCEPTED,
        conditions=(),
        claim_scope=ClaimScope.SNAPSHOT,
        code_disposition=CodeDisposition.RETAIN_ISOLATED,
        accepted_base_tree="a" * 40,
    )

    assert accepted.submission.state.value == "accepted"
    assert accepted.decision.accepted_submission_digest == canonical_digest(
        accepted.submission
    )
    assert accepted.report.revision == 1
    assert accepted.report.applicability.value == "snapshot_only"
    assert accepted.report.validation_basis is None
    assert [item.path for item in accepted.files] == [
        f".research/decisions/{accepted.decision.decision_id}.yaml",
        f".research/reports/{accepted.report.report_id}/1.md",
        f".research/reports/{accepted.report.report_id}/1.yaml",
        (
            f".research/submissions/{accepted.submission.submission_id}/"
            "submission.yaml"
        ),
    ]
    report_markdown = next(
        item.content for item in accepted.files if item.path.endswith("/1.md")
    )
    assert b"# Stopping policy result" in report_markdown
    assert accepted.report.report_id.encode("ascii") in report_markdown
    assert accepted.source_digest.encode("ascii") in report_markdown
    assert accepted.source_digest.startswith("sha256:")


def test_linear_preview_is_disabled_or_exactly_bound_without_network(
    task_payload,
    run_spec_payload,
    run_result_payload,
    submission_payload,
) -> None:
    task, submission, proposal, evidence = _records(
        task_payload,
        run_spec_payload,
        run_result_payload,
        submission_payload,
        linear=True,
    )
    accepted = ReviewAcceptanceBuilder().build(
        task=task,
        submission=submission,
        proposal=proposal,
        evidence=evidence,
        current_report=None,
        decision_id="decision_20260802T123456Z_" + "7" * 24,
        reviewer_actor="uid-1000",
        decided_at="2026-08-03T12:00:00Z",
        disposition=ReviewDisposition.ACCEPTED,
        conditions=(),
        claim_scope=ClaimScope.BASELINE,
        code_disposition=CodeDisposition.MERGE,
        accepted_base_tree="a" * 40,
    )

    disabled = build_linear_preview(
        policy=None,
        task=task,
        submission=accepted.submission,
        decision=accepted.decision,
        report=accepted.report,
    )
    assert disabled.projection.state == "disabled"
    assert disabled.body is None

    policy = LinearProjectionPolicy(
        workspace_id=WORKSPACE_ID,
        team_id=TEAM_ID,
        project_id=PROJECT_ID,
    )
    configured = build_linear_preview(
        policy=policy,
        task=task,
        submission=accepted.submission,
        decision=accepted.decision,
        report=accepted.report,
    )
    assert configured.projection.state == "configured"
    assert configured.projection.issue_id == ISSUE_ID
    assert configured.projection.workspace_id == WORKSPACE_ID
    assert configured.projection.team_id == TEAM_ID
    assert configured.projection.project_id == PROJECT_ID
    assert configured.body is not None
    assert configured.projection.payload_digest == (
        "sha256:" + hashlib.sha256(configured.body).hexdigest()
    )


def test_failure_record_cannot_claim_baseline_scope(
    task_payload,
    run_spec_payload,
    run_result_payload,
    submission_payload,
) -> None:
    task, submission, proposal, evidence = _records(
        task_payload,
        run_spec_payload,
        run_result_payload,
        submission_payload,
    )
    failed = RunResult.model_validate(
        {
            **evidence[0].result.model_dump(mode="json", exclude_none=True),
            "outcome": "failed",
            "started_at": None,
            "exit_code": 9,
            "failure_class": "command",
        }
    )
    failure_submission = ResearchSubmission.model_validate(
        {
            **submission.model_dump(mode="json", exclude_none=True),
            "category": "failure_record",
        }
    )

    with pytest.raises(RCPError) as caught:
        ReviewAcceptanceBuilder().build(
            task=task,
            submission=failure_submission,
            proposal=proposal,
            evidence=(SubmissionEvidence(evidence[0].spec, failed),),
            current_report=None,
            decision_id="decision_20260802T123456Z_" + "7" * 24,
            reviewer_actor="uid-1000",
            decided_at="2026-08-03T12:00:00Z",
            disposition=ReviewDisposition.ACCEPTED,
            conditions=(),
            claim_scope=ClaimScope.BASELINE,
            code_disposition=CodeDisposition.ABANDON,
            accepted_base_tree="a" * 40,
        )
    assert caught.value.code == "failure_submission_scope_invalid"
