from __future__ import annotations

import subprocess
from pathlib import Path

from researchctl.domain.enums import ReportApplicability
from researchctl.domain.models import ReportRecord
from researchctl.serialization import dump_yaml
from researchctl.services.report_status import ReportStatusService
from researchctl.services.requests import ReportStatusRequest


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


def _write_report(
    repository: Path,
    report_payload,
    *,
    basis_tree: str,
    applicability: str = "current",
    claim_scope: str = "baseline",
) -> ReportRecord:
    report = ReportRecord.model_validate(
        report_payload(
            applicability=applicability,
            claim_scope=claim_scope,
            validation_basis=(
                None
                if claim_scope == "snapshot"
                else {
                    "main_tree": basis_tree,
                    "assessed_at": "2026-08-03T12:00:00Z",
                }
            ),
        )
    )
    path = repository / ".research/reports" / report.report_id / "1.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(dump_yaml(report), encoding="utf-8")
    return report


def _status(
    repository: Path,
    report: ReportRecord,
    target_commit: str,
):
    return ReportStatusService(
        repository_root=repository,
        default_branch="main",
    ).read(
        ReportStatusRequest(
            report_id=report.report_id,
            target_commit=target_commit,
        )
    )


def test_protocol_only_report_commit_does_not_make_report_pending(
    initialized_repository: Path,
    report_payload,
) -> None:
    basis_tree = _git(initialized_repository, "rev-parse", "HEAD^{tree}")
    report = _write_report(
        initialized_repository,
        report_payload,
        basis_tree=basis_tree,
    )
    target = _commit(
        initialized_repository,
        "accept report",
        f".research/reports/{report.report_id}/1.yaml",
    )

    status = _status(initialized_repository, report, target)

    assert status.stored_applicability is ReportApplicability.CURRENT
    assert status.effective_applicability is ReportApplicability.CURRENT
    assert status.comparison == "protocol_only_change"
    assert status.reason == "only_protocol_state_changed"
    assert status.changed_paths == ()


def test_governed_code_change_makes_current_report_effectively_pending(
    initialized_repository: Path,
    report_payload,
) -> None:
    source = initialized_repository / "src/evaluator.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    basis_commit = _commit(initialized_repository, "basis", "src/evaluator.py")
    basis_tree = _git(initialized_repository, "rev-parse", f"{basis_commit}^{{tree}}")
    report = _write_report(
        initialized_repository,
        report_payload,
        basis_tree=basis_tree,
    )
    _commit(
        initialized_repository,
        "accept report",
        f".research/reports/{report.report_id}/1.yaml",
    )
    source.write_text("VALUE = 2\n", encoding="utf-8")
    target = _commit(initialized_repository, "change evaluator", "src/evaluator.py")

    status = _status(initialized_repository, report, target)

    assert status.stored_applicability is ReportApplicability.CURRENT
    assert status.effective_applicability is ReportApplicability.IMPACT_PENDING
    assert status.comparison == "governed_change"
    assert status.changed_paths == ("src/evaluator.py",)


def test_stored_stale_status_is_not_revalidated_by_read_model(
    initialized_repository: Path,
    report_payload,
) -> None:
    basis_tree = _git(initialized_repository, "rev-parse", "HEAD^{tree}")
    report = _write_report(
        initialized_repository,
        report_payload,
        basis_tree=basis_tree,
        applicability="stale",
    )
    target = _commit(
        initialized_repository,
        "accept stale report",
        f".research/reports/{report.report_id}/1.yaml",
    )

    status = _status(initialized_repository, report, target)

    assert status.effective_applicability is ReportApplicability.STALE
    assert status.reason == "stored_stale"


def test_missing_validation_basis_tree_fails_closed_as_pending(
    initialized_repository: Path,
    report_payload,
) -> None:
    report = _write_report(
        initialized_repository,
        report_payload,
        basis_tree="f" * 40,
    )
    target = _commit(
        initialized_repository,
        "accept report with unavailable basis",
        f".research/reports/{report.report_id}/1.yaml",
    )

    status = _status(initialized_repository, report, target)

    assert status.effective_applicability is ReportApplicability.IMPACT_PENDING
    assert status.comparison == "basis_unavailable"
    assert status.reason == "validation_basis_tree_unavailable"


def test_snapshot_status_remains_snapshot_only(
    initialized_repository: Path,
    report_payload,
) -> None:
    report = _write_report(
        initialized_repository,
        report_payload,
        basis_tree="f" * 40,
        applicability="snapshot_only",
        claim_scope="snapshot",
    )
    target = _commit(
        initialized_repository,
        "accept snapshot report",
        f".research/reports/{report.report_id}/1.yaml",
    )

    status = _status(initialized_repository, report, target)

    assert status.effective_applicability is ReportApplicability.SNAPSHOT_ONLY
    assert status.reason == "snapshot_scope"
