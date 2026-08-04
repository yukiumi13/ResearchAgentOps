from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import BaseModel

from researchctl.domain.models import (
    ImpactDecision,
    ReportProposal,
    ReportRecord,
    ResearchSubmission,
    ReviewDecision,
    RunResult,
    RunSpec,
    TaskRecord,
)
from researchctl.errors import UnsafeRepositoryPathError
from researchctl.serialization import canonical_digest, dump_yaml
from researchctl.services.doctor import doctor


OTHER_SUBMISSION_ID = "submission_20260802T123456Z_" + "7" * 24
OTHER_RUN_ID = "run_20260802T123456Z_" + "8" * 24
OTHER_DECISION_ID = "decision_20260802T123456Z_" + "9" * 24
OTHER_REPORT_ID = "report_20260802T123456Z_" + "6" * 24


def test_doctor_accepts_canonical_impact_decision(
    initialized_repository: Path,
) -> None:
    payload = {
        "schema_version": "0.1",
        "decision_id": OTHER_DECISION_ID,
        "impact_id": "impact_20260802T123456Z_" + "5" * 24,
        "report_id": OTHER_REPORT_ID,
        "expected_report_revision": 2,
        "expected_impact_digest": "sha256:" + "1" * 64,
        "impact_target_commit": "2" * 40,
        "impact_target_tree": "3" * 40,
        "decision_base_commit": "4" * 40,
        "decision_base_tree": "5" * 40,
        "disposition": "keep_stale",
        "reviewer_actor": "manager@example.invalid",
        "reason": "Awaiting a reviewed rerun plan.",
        "decided_at": "2026-08-03T15:00:00Z",
    }
    decision = ImpactDecision.model_validate(
        {**payload, "decision_digest": canonical_digest(payload)}
    )
    relative = f".research/decisions/{decision.decision_id}.yaml"
    _write_model(initialized_repository, relative, decision)

    report = doctor(initialized_repository)
    checks = {check.name: check for check in report.checks}

    assert checks[f"record:{relative}"].status == "pass"


def _write_model(repository: Path, relative: str, record: BaseModel) -> None:
    path = repository / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_yaml(record), encoding="utf-8")


def _install_managed_records(
    repository: Path,
    *,
    task_payload,
    run_spec_payload,
    run_result_payload,
    submission_payload,
    review_decision_payload,
    report_payload,
) -> dict[str, str]:
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
            run_result_ids=[result.result_id],
        )
    )
    report = ReportRecord.model_validate(report_payload(submission_id=submission.submission_id))
    proposal = ReportProposal(
        submission_id=submission.submission_id,
        report_id=report.report_id,
        expected_report_revision=0,
        title=report.title,
        evidence_tree=spec.source_tree,
    )
    decision = ReviewDecision.model_validate(
        review_decision_payload(
            submission_id=submission.submission_id,
            report_id=report.report_id,
        )
    )

    submission_root = f".research/submissions/{submission.submission_id}"
    evidence_root = f"{submission_root}/evidence/{spec.run_id}"
    run_root = f".research/runs/{spec.run_id}"
    records: dict[str, BaseModel] = {
        f".research/tasks/{task.task_id}.yaml": task,
        f"{run_root}/spec.yaml": spec,
        f"{run_root}/result.yaml": result,
        f"{submission_root}/submission.yaml": submission,
        f"{submission_root}/proposed-report.yaml": proposal,
        f"{evidence_root}/spec.yaml": spec,
        f"{evidence_root}/result.yaml": result,
        f".research/decisions/{decision.decision_id}.yaml": decision,
        f".research/reports/{report.report_id}/{report.revision}.yaml": report,
    }
    for relative, record in records.items():
        _write_model(repository, relative, record)
    for filename in ("report-preview.md", "review.md"):
        (repository / submission_root / filename).write_text(
            f"# Generated {filename}\n",
            encoding="utf-8",
        )
    return {
        "submission_id": str(submission.submission_id),
        "submission_root": submission_root,
        "run_id": str(spec.run_id),
        "run_root": run_root,
        "decision_id": str(decision.decision_id),
        "report_id": str(report.report_id),
    }


def _managed_snapshot(repository: Path) -> tuple[tuple[str, str, bytes], ...]:
    managed = repository / ".research"
    observed: list[tuple[str, str, bytes]] = []
    for path in sorted(managed.rglob("*")):
        relative = path.relative_to(repository).as_posix()
        if path.is_symlink():
            observed.append((relative, "symlink", os.readlink(path).encode()))
        elif path.is_dir():
            observed.append((relative, "directory", b""))
        else:
            observed.append((relative, "file", path.read_bytes()))
    return tuple(observed)


def test_doctor_accepts_frozen_nested_record_layout_without_writes(
    initialized_repository: Path,
    task_payload,
    run_spec_payload,
    run_result_payload,
    submission_payload,
    review_decision_payload,
    report_payload,
) -> None:
    identities = _install_managed_records(
        initialized_repository,
        task_payload=task_payload,
        run_spec_payload=run_spec_payload,
        run_result_payload=run_result_payload,
        submission_payload=submission_payload,
        review_decision_payload=review_decision_payload,
        report_payload=report_payload,
    )
    before = _managed_snapshot(initialized_repository)

    report = doctor(initialized_repository)

    checks = {check.name: check for check in report.checks}
    evidence_root = (
        f"{identities['submission_root']}/evidence/{identities['run_id']}"
    )
    expected_passes = (
        f"record:{identities['run_root']}/spec.yaml",
        f"record:{identities['run_root']}/result.yaml",
        f"record:{identities['submission_root']}/submission.yaml",
        f"record:{identities['submission_root']}/proposed-report.yaml",
        f"record:{evidence_root}/spec.yaml",
        f"record:{evidence_root}/result.yaml",
        f"record:.research/decisions/{identities['decision_id']}.yaml",
        f"record:.research/reports/{identities['report_id']}/1.yaml",
        f"record:{identities['submission_root']}/review.md",
        f"record:{identities['submission_root']}/report-preview.md",
    )
    assert report.healthy is True
    assert all(checks[name].status == "pass" for name in expected_passes)
    assert _managed_snapshot(initialized_repository) == before


@pytest.mark.parametrize(
    "mismatch",
    ["submission", "evidence-run", "decision", "report", "revision"],
)
def test_doctor_rejects_record_identity_or_revision_path_mismatch(
    initialized_repository: Path,
    task_payload,
    run_spec_payload,
    run_result_payload,
    submission_payload,
    review_decision_payload,
    report_payload,
    mismatch: str,
) -> None:
    identities = _install_managed_records(
        initialized_repository,
        task_payload=task_payload,
        run_spec_payload=run_spec_payload,
        run_result_payload=run_result_payload,
        submission_payload=submission_payload,
        review_decision_payload=review_decision_payload,
        report_payload=report_payload,
    )
    if mismatch == "submission":
        source = initialized_repository / identities["submission_root"]
        target = source.parent / OTHER_SUBMISSION_ID
        source.rename(target)
        expected = f"record:{target.relative_to(initialized_repository)}/submission.yaml"
    elif mismatch == "evidence-run":
        source = (
            initialized_repository
            / identities["submission_root"]
            / "evidence"
            / identities["run_id"]
        )
        target = source.parent / OTHER_RUN_ID
        source.rename(target)
        expected = f"record:{target.relative_to(initialized_repository)}/spec.yaml"
    elif mismatch == "decision":
        source = (
            initialized_repository
            / ".research"
            / "decisions"
            / f"{identities['decision_id']}.yaml"
        )
        target = source.with_name(f"{OTHER_DECISION_ID}.yaml")
        source.rename(target)
        expected = f"record:{target.relative_to(initialized_repository)}"
    elif mismatch == "report":
        source = (
            initialized_repository / ".research" / "reports" / identities["report_id"]
        )
        target = source.with_name(OTHER_REPORT_ID)
        source.rename(target)
        expected = f"record:{target.relative_to(initialized_repository)}/1.yaml"
    else:
        source = (
            initialized_repository
            / ".research"
            / "reports"
            / identities["report_id"]
            / "1.yaml"
        )
        target = source.with_name("2.yaml")
        source.rename(target)
        expected = f"record:{target.relative_to(initialized_repository)}"

    report = doctor(initialized_repository)
    checks = {check.name: check for check in report.checks}

    assert report.healthy is False
    assert checks[expected].status == "error"
    assert "canonical path" in checks[expected].message


@pytest.mark.parametrize(
    "missing_name",
    ["submission.yaml", "proposed-report.yaml", "evidence/spec.yaml", "evidence/result.yaml"],
)
def test_doctor_rejects_missing_submission_records(
    initialized_repository: Path,
    task_payload,
    run_spec_payload,
    run_result_payload,
    submission_payload,
    review_decision_payload,
    report_payload,
    missing_name: str,
) -> None:
    identities = _install_managed_records(
        initialized_repository,
        task_payload=task_payload,
        run_spec_payload=run_spec_payload,
        run_result_payload=run_result_payload,
        submission_payload=submission_payload,
        review_decision_payload=review_decision_payload,
        report_payload=report_payload,
    )
    relative = f"{identities['submission_root']}/{missing_name}"
    if missing_name.startswith("evidence/"):
        relative = (
            f"{identities['submission_root']}/evidence/{identities['run_id']}/"
            f"{missing_name.removeprefix('evidence/')}"
        )
    (initialized_repository / relative).unlink()

    report = doctor(initialized_repository)
    checks = {check.name: check for check in report.checks}

    assert report.healthy is False
    assert checks[f"record:{relative}"].status == "error"
    assert "missing" in checks[f"record:{relative}"].message


@pytest.mark.parametrize(
    "unexpected_location",
    ["submission-yaml", "decision-directory", "flat-report", "report-markdown"],
)
def test_doctor_fails_closed_on_unexpected_managed_entries(
    initialized_repository: Path,
    task_payload,
    run_spec_payload,
    run_result_payload,
    submission_payload,
    review_decision_payload,
    report_payload,
    unexpected_location: str,
) -> None:
    identities = _install_managed_records(
        initialized_repository,
        task_payload=task_payload,
        run_spec_payload=run_spec_payload,
        run_result_payload=run_result_payload,
        submission_payload=submission_payload,
        review_decision_payload=review_decision_payload,
        report_payload=report_payload,
    )
    if unexpected_location == "submission-yaml":
        relative = f"{identities['submission_root']}/arbitrary.yaml"
        (initialized_repository / relative).write_text("schema_version: '0.1'\n")
    elif unexpected_location == "decision-directory":
        relative = ".research/decisions/nested"
        (initialized_repository / relative).mkdir()
    elif unexpected_location == "flat-report":
        relative = ".research/reports/arbitrary.yaml"
        (initialized_repository / relative).write_text("schema_version: '0.1'\n")
    else:
        relative = f".research/reports/{identities['report_id']}/report.md"
        (initialized_repository / relative).write_text("# Not canonical\n")

    report = doctor(initialized_repository)
    checks = {check.name: check for check in report.checks}

    assert report.healthy is False
    assert checks[f"record:{relative}"].status == "error"


def test_doctor_rejects_nested_record_symlink_without_touching_target(
    initialized_repository: Path,
    tmp_path: Path,
    task_payload,
    run_spec_payload,
    run_result_payload,
    submission_payload,
    review_decision_payload,
    report_payload,
) -> None:
    identities = _install_managed_records(
        initialized_repository,
        task_payload=task_payload,
        run_spec_payload=run_spec_payload,
        run_result_payload=run_result_payload,
        submission_payload=submission_payload,
        review_decision_payload=review_decision_payload,
        report_payload=report_payload,
    )
    external = tmp_path / "external.yaml"
    external.write_text("outside remains unchanged\n", encoding="utf-8")
    linked = initialized_repository / identities["submission_root"] / "arbitrary.yaml"
    linked.symlink_to(external)
    before = external.read_bytes()

    with pytest.raises(UnsafeRepositoryPathError):
        doctor(initialized_repository)

    assert external.read_bytes() == before
    assert linked.is_symlink()
