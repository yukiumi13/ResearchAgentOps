from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from researchctl.domain.enums import (
    EvidenceStatus,
    ImpactDisposition,
    ReportApplicability,
)
from researchctl.domain.models import DependencySet, ImpactDecision, ReportRecord
from researchctl.errors import RCPError
from researchctl.serialization import dump_yaml
from researchctl.services.impact_decision import ImpactDecisionBuilder
from researchctl.services.impact_decision_workflow import (
    ImpactDecisionWorkflowService,
)
from researchctl.services.report_impact import ReportImpactBuilder
from researchctl.services.requests import ImpactDecisionCreateRequest

IMPACT_ID = "impact_20260803T140000Z_" + "1" * 24
DECISION_ID = "decision_20260803T150000Z_" + "2" * 24
OPERATION_ID = "operation_20260803T150000Z_" + "3" * 24
TASK_ID = "task_20260803T150000Z_" + "4" * 24
NOW = "2026-08-03T15:00:00Z"


def _report(report_payload) -> ReportRecord:
    return ReportRecord.model_validate(
        report_payload(
            dependencies={
                "paths": ["src/evaluator/**"],
                "resources": [],
                "environments": [],
            },
        )
    )


def _impact_bundle(report_payload):
    return ReportImpactBuilder().build(
        impact_id=IMPACT_ID,
        report=_report(report_payload),
        target_commit="d" * 40,
        target_tree="e" * 40,
        changed_paths=("src/evaluator/score.py",),
        generated_at="2026-08-03T14:00:00Z",
    )


def _decision(report_payload, disposition: ImpactDisposition, **overrides):
    impact_bundle = _impact_bundle(report_payload)
    return ImpactDecisionBuilder().build(
        impact=impact_bundle.impact,
        report=impact_bundle.proposed_report,
        decision_id=DECISION_ID,
        expected_report_revision=2,
        decision_base_commit="a" * 40,
        decision_base_tree="b" * 40,
        disposition=disposition,
        reviewer_actor="manager@example.invalid",
        reason="Reviewed the exact affected evaluator change.",
        decided_at=NOW,
        **overrides,
    )


@pytest.mark.parametrize(
    ("disposition", "applicability", "evidence_status"),
    [
        (ImpactDisposition.WAIVE, ReportApplicability.CURRENT, EvidenceStatus.VERIFIED),
        (
            ImpactDisposition.KEEP_STALE,
            ReportApplicability.STALE,
            EvidenceStatus.VERIFIED,
        ),
        (
            ImpactDisposition.INVALIDATE,
            ReportApplicability.STALE,
            EvidenceStatus.INVALID,
        ),
    ],
)
def test_decision_materializes_explicit_report_transition(
    report_payload,
    disposition: ImpactDisposition,
    applicability: ReportApplicability,
    evidence_status: EvidenceStatus,
) -> None:
    bundle = _decision(report_payload, disposition)

    assert bundle.decision.disposition is disposition
    assert bundle.report.revision == 3
    assert bundle.report.applicability is applicability
    assert bundle.report.evidence_status is evidence_status
    assert bundle.report.evidence_tree == bundle.current_report.evidence_tree
    assert bundle.as_dict()["automatically_runs_experiments"] is False
    assert [item.path for item in bundle.files] == sorted(
        [
            f".research/decisions/{DECISION_ID}.yaml",
            f".research/reports/{bundle.report.report_id}/3.md",
            f".research/reports/{bundle.report.report_id}/3.yaml",
        ]
    )
    if disposition is ImpactDisposition.WAIVE:
        assert bundle.report.validation_basis is not None
        assert bundle.report.validation_basis.main_tree == "b" * 40


def test_rerun_binds_task_but_does_not_create_run(report_payload) -> None:
    bundle = _decision(
        report_payload,
        ImpactDisposition.RERUN,
        rerun_task_id=TASK_ID,
    )

    assert bundle.decision.rerun_task_id == TASK_ID
    assert bundle.report.applicability is ReportApplicability.STALE
    assert bundle.as_dict()["automatically_runs_experiments"] is False
    markdown = next(item for item in bundle.files if item.path.endswith(".md"))
    assert TASK_ID.encode("utf-8") in markdown.content
    assert b"does not start, retry, or collect a Run" in markdown.content


def test_dependency_fix_changes_declaration_and_remains_stale(
    report_payload,
) -> None:
    replacement = DependencySet(paths=("src/new-evaluator/**",))
    bundle = _decision(
        report_payload,
        ImpactDisposition.DEPENDENCY_FIX,
        replacement_dependencies=replacement,
    )

    assert bundle.decision.replacement_dependencies == replacement
    assert bundle.report.dependencies == replacement
    assert bundle.report.applicability is ReportApplicability.STALE


def test_dependency_fix_rejects_noop_declaration(report_payload) -> None:
    impact = _impact_bundle(report_payload)

    with pytest.raises(RCPError) as raised:
        ImpactDecisionBuilder().build(
            impact=impact.impact,
            report=impact.proposed_report,
            decision_id=DECISION_ID,
            expected_report_revision=2,
            decision_base_commit="a" * 40,
            decision_base_tree="b" * 40,
            disposition=ImpactDisposition.DEPENDENCY_FIX,
            reviewer_actor="manager@example.invalid",
            reason="No actual correction.",
            decided_at=NOW,
            replacement_dependencies=impact.proposed_report.dependencies,
        )

    assert raised.value.code == "impact_dependency_fix_no_change"


def test_impact_decision_digest_rejects_tampering(report_payload) -> None:
    bundle = _decision(report_payload, ImpactDisposition.KEEP_STALE)
    payload = bundle.decision.model_dump(mode="json", exclude_none=True)
    payload["reason"] = "Changed after digesting."

    with pytest.raises(ValidationError, match="decision_digest"):
        ImpactDecision.model_validate(payload)


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(repository: Path, message: str, *paths: str) -> str:
    _git(repository, "add", "--", *paths)
    _git(
        repository,
        "-c",
        "user.name=Tests",
        "-c",
        "user.email=tests@example.invalid",
        "commit",
        "--no-gpg-sign",
        "-m",
        message,
    )
    return _git(repository, "rev-parse", "HEAD")


def test_workflow_loads_accepted_impact_and_prepares_closed_manager_commit(
    initialized_repository: Path,
    report_payload,
) -> None:
    source = initialized_repository / "src/evaluator/score.py"
    source.parent.mkdir(parents=True)
    source.write_text("SCORE = 1\n", encoding="utf-8")
    basis_commit = _commit(
        initialized_repository,
        "basis evaluator",
        "src/evaluator/score.py",
    )
    basis_tree = _git(initialized_repository, "rev-parse", f"{basis_commit}^{{tree}}")
    report = ReportRecord.model_validate(
        report_payload(
            validation_basis={"main_tree": basis_tree, "assessed_at": NOW},
            dependencies={
                "paths": ["src/evaluator/**"],
                "resources": [],
                "environments": [],
            },
        )
    )
    report_path = (
        initialized_repository
        / ".research/reports"
        / report.report_id
        / "1.yaml"
    )
    report_path.parent.mkdir(parents=True)
    report_path.write_text(dump_yaml(report), encoding="utf-8")
    _commit(
        initialized_repository,
        "accept report",
        report_path.relative_to(initialized_repository).as_posix(),
    )
    source.write_text("SCORE = 2\n", encoding="utf-8")
    impact_target = _commit(
        initialized_repository,
        "change evaluator",
        "src/evaluator/score.py",
    )
    target_tree = _git(initialized_repository, "rev-parse", f"{impact_target}^{{tree}}")
    impact_bundle = ReportImpactBuilder().build(
        impact_id=IMPACT_ID,
        report=report,
        target_commit=impact_target,
        target_tree=target_tree,
        changed_paths=("src/evaluator/score.py",),
        generated_at="2026-08-03T14:00:00Z",
    )
    for rendered in impact_bundle.files:
        path = initialized_repository / rendered.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(rendered.content)
    accepted_impact = _commit(
        initialized_repository,
        "accept impact",
        *(item.path for item in impact_bundle.files),
    )
    worktrees = initialized_repository / ".git/researchctl/worktrees"
    worktrees.mkdir(parents=True, exist_ok=True)
    request = ImpactDecisionCreateRequest(
        operation_id=OPERATION_ID,
        idempotency_key="keep-evaluator-report-stale",
        decision_id=DECISION_ID,
        impact_id=IMPACT_ID,
        report_id=report.report_id,
        expected_report_revision=2,
        expected_impact_digest=impact_bundle.impact.impact_digest,
        target_commit=accepted_impact,
        disposition=ImpactDisposition.KEEP_STALE,
        reason="Awaiting a dedicated evaluator rerun.",
    )

    prepared = ImpactDecisionWorkflowService(
        repository_root=initialized_repository,
        worktrees_directory=worktrees,
        default_branch="main",
    ).prepare(
        request,
        reviewer_actor="manager@example.invalid",
        decided_at=NOW,
    )

    assert prepared.commit.command == "impact.decide"
    assert prepared.commit.parent_commit == accepted_impact
    assert prepared.commit.branch == f"research/impact-decision/{DECISION_ID}"
    assert prepared.bundle.decision.expected_impact_digest == (
        impact_bundle.impact.impact_digest
    )
    assert prepared.bundle.report.revision == 3
    assert set(prepared.commit.paths) == {
        f".research/decisions/{DECISION_ID}.yaml",
        f".research/reports/{report.report_id}/3.md",
        f".research/reports/{report.report_id}/3.yaml",
    }
