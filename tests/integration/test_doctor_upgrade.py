from __future__ import annotations

from pathlib import Path

import pytest

from researchctl.constants import PROTOCOL_VERSION
from researchctl.errors import ProtocolCompatibilityError
from researchctl.services.doctor import DoctorCheck, doctor
from researchctl.services.upgrade import check_upgrade


def _checks_by_name(checks: tuple[DoctorCheck, ...]) -> dict[str, DoctorCheck]:
    return {check.name: check for check in checks}


def test_doctor_accepts_a_clean_portable_repository(
    initialized_repository: Path,
    run_git,
) -> None:
    run_git(initialized_repository, "add", ".researchctl.toml", ".research")
    run_git(initialized_repository, "commit", "-m", "initialize research control plane")

    report = doctor(initialized_repository)
    checks = _checks_by_name(report.checks)

    assert report.repository == initialized_repository.resolve()
    assert report.healthy is True
    assert not [check for check in report.checks if check.status == "error"]
    assert checks["protocol-version"].status == "pass"
    assert checks["project-record"].status == "pass"
    assert checks["project-state"].status == "warn"
    assert checks["environment-lock"].status == "pass"
    assert checks["git-worktree"].status == "pass"
    schema_checks = [
        check for check in report.checks if check.name.startswith("schema:")
    ]
    assert schema_checks
    assert {check.status for check in schema_checks} == {"pass"}


def test_doctor_rejects_a_tampered_generated_schema(
    initialized_repository: Path,
) -> None:
    relative = ".research/schemas/task.schema.json"
    (initialized_repository / relative).write_text("{}\n", encoding="utf-8")

    report = doctor(initialized_repository)
    checks = _checks_by_name(report.checks)

    assert report.healthy is False
    assert checks[f"schema:{relative}"].status == "error"
    assert checks[f"schema:{relative}"].message == (
        "Generated schema file does not match the pinned protocol."
    )
    assert checks[f"schema:{relative}"].remediation is not None


def test_doctor_surfaces_dirty_worktree_as_nonfatal_warning(
    initialized_repository: Path,
    run_git,
    snapshot_tree,
) -> None:
    run_git(initialized_repository, "add", ".researchctl.toml", ".research")
    run_git(initialized_repository, "commit", "-m", "initialize research control plane")
    readme = initialized_repository / "README.md"
    readme.write_text("# Existing research project\n\nnew result\n", encoding="utf-8")
    before = snapshot_tree(initialized_repository)

    report = doctor(initialized_repository)
    git_check = _checks_by_name(report.checks)["git-worktree"]

    assert report.healthy is True
    assert git_check.status == "warn"
    assert git_check.message == "Git worktree has 1 changed or untracked path(s)."
    assert git_check.remediation == "Review changes before creating an immutable run."
    assert snapshot_tree(initialized_repository) == before


def test_upgrade_check_reports_current_protocol(
    initialized_repository: Path,
) -> None:
    report = check_upgrade(initialized_repository)

    assert report.repository == initialized_repository.resolve()
    assert report.current == PROTOCOL_VERSION
    assert report.target == PROTOCOL_VERSION
    assert report.compatible is True
    assert report.migration_required is False
    assert report.changes == ()


def test_upgrade_check_fails_closed_for_future_protocol(
    initialized_repository: Path,
) -> None:
    config_path = initialized_repository / ".researchctl.toml"
    current = config_path.read_text(encoding="utf-8")
    config_path.write_text(
        current.replace(
            f'protocol_version = "{PROTOCOL_VERSION}"',
            'protocol_version = "9.0"',
        ),
        encoding="utf-8",
    )

    with pytest.raises(ProtocolCompatibilityError) as raised:
        check_upgrade(initialized_repository)

    assert raised.value.code == "unsupported_protocol"
    assert raised.value.exit_code == 2
    assert raised.value.context == {"found": "9.0", "supported": PROTOCOL_VERSION}
