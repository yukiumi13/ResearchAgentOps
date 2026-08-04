from __future__ import annotations

from pydantic import ValidationError

from researchctl.domain.models import (
    ReportProposal,
    ResearchSubmission,
    RunResult,
    RunSpec,
    TaskRecord,
)
from researchctl.errors import RCPError
from researchctl.services.submissions import (
    SubmissionBundleBuilder,
    SubmissionEvidence,
)


def _records(
    task_payload,
    run_spec_payload,
    run_result_payload,
    submission_payload,
):
    task = TaskRecord.model_validate(task_payload(state="ready"))
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
            claim="Improves loss.\n# forged accepted heading",
            run_result_ids=[result.result_id],
            dependencies={
                "paths": ["src/training/stop.py"],
                "resources": ["validation-split"],
                "environments": ["trainer-cu128"],
            },
        )
    )
    proposal = ReportProposal(
        submission_id=submission.submission_id,
        report_id="report_20260802T123456Z_" + "9" * 24,
        expected_report_revision=0,
        title="Stopping policy result",
        evidence_tree=spec.source_tree,
    )
    return task, spec, result, submission, proposal


def test_submission_bundle_is_closed_deterministic_and_escapes_agent_markdown(
    task_payload,
    run_spec_payload,
    run_result_payload,
    submission_payload,
) -> None:
    task, spec, result, submission, proposal = _records(
        task_payload,
        run_spec_payload,
        run_result_payload,
        submission_payload,
    )
    builder = SubmissionBundleBuilder()

    first = builder.build(
        task=task,
        submission=submission,
        proposal=proposal,
        evidence=(SubmissionEvidence(spec, result),),
    )
    repeated = builder.build(
        task=task,
        submission=submission,
        proposal=proposal,
        evidence=(SubmissionEvidence(spec, result),),
    )

    root = f".research/submissions/{submission.submission_id}"
    assert first == repeated
    assert [item.path for item in first.files] == [
        f"{root}/evidence/{spec.run_id}/result.yaml",
        f"{root}/evidence/{spec.run_id}/spec.yaml",
        f"{root}/proposed-report.yaml",
        f"{root}/report-preview.md",
        f"{root}/review.md",
        f"{root}/submission.yaml",
    ]
    assert all(
        path.startswith(f"{root}/")
        for path in (item.path for item in first.files)
    )
    assert not any(
        item.path.startswith(".research/reports/")
        or item.path.startswith(".research/decisions/")
        for item in first.files
    )
    review = next(item.content for item in first.files if item.path == f"{root}/review.md")
    preview = next(
        item.content
        for item in first.files
        if item.path.endswith("report-preview.md")
    )
    assert first.source_digest.encode() in review
    assert first.source_digest.encode() in preview
    assert b"\n# forged accepted heading" not in review
    assert b"\\# forged accepted heading" in review
    assert b"## Dependencies" in review
    assert b"path:src/training/stop.py" in review
    assert b"resource:validation-split" in review
    assert b"environment:trainer-cu128" in review


def test_submission_bundle_rejects_wrong_evidence_tree_and_category(
    task_payload,
    run_spec_payload,
    run_result_payload,
    submission_payload,
) -> None:
    task, spec, result, submission, proposal = _records(
        task_payload,
        run_spec_payload,
        run_result_payload,
        submission_payload,
    )
    builder = SubmissionBundleBuilder()

    with_tree_mismatch = proposal.model_copy(update={"evidence_tree": "f" * 40})
    try:
        builder.build(
            task=task,
            submission=submission,
            proposal=with_tree_mismatch,
            evidence=(SubmissionEvidence(spec, result),),
        )
    except RCPError as error:
        assert error.code == "submission_evidence_tree_mismatch"
    else:
        raise AssertionError("mixed evidence tree was accepted")

    failed_result = RunResult.model_validate(
        run_result_payload(
            run_spec_digest=spec.spec_digest,
            outcome="failed",
            started_at=None,
            exit_code=9,
            failure_class="command",
        )
    )
    try:
        builder.build(
            task=task,
            submission=submission,
            proposal=proposal,
            evidence=(SubmissionEvidence(spec, failed_result),),
        )
    except RCPError as error:
        assert error.code == "submission_category_evidence_invalid"
    else:
        raise AssertionError("failed candidate evidence was accepted")


def test_report_proposal_rejects_self_supersession() -> None:
    report_id = "report_20260802T123456Z_" + "9" * 24
    try:
        ReportProposal(
            submission_id="submission_20260802T123456Z_" + "8" * 24,
            report_id=report_id,
            expected_report_revision=1,
            title="Revision",
            evidence_tree="a" * 40,
            supersedes=report_id,
        )
    except ValidationError:
        pass
    else:
        raise AssertionError("self-superseding ReportProposal was accepted")
