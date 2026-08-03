from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import researchctl.services.init_project as init_service
from researchctl.config import load_project_config
from researchctl.domain.models import (
    ProjectRecord,
    ReportRecord,
    ResearchSubmission,
    ReviewDecision,
    RunAttempt,
    TaskRecord,
)
from researchctl.errors import ProtocolLockError, UnsafeRepositoryPathError
from researchctl.repository import discover_repository
from researchctl.schema import schema_manifest_digest
from researchctl.serialization import dump_yaml, load_model
from researchctl.services.doctor import doctor
from researchctl.services.init_project import apply_init, initialize_project, plan_init


PROJECT_ROOT = Path(__file__).resolve().parents[2]
OBSERVED_AT = "2026-08-02T12:34:56Z"


def _invoke_cli(*args: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    source_path = str(PROJECT_ROOT / "src")
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        f"{source_path}{os.pathsep}{existing_pythonpath}"
        if existing_pythonpath
        else source_path
    )
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "researchctl", *args],
        cwd=PROJECT_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


def _snapshot_external_target(target: Path) -> dict[str, bytes]:
    return {
        path.relative_to(target).as_posix(): path.read_bytes()
        for path in target.rglob("*")
        if path.is_file()
    }


def _overdeep_yaml() -> str:
    lines = [f"{'  ' * index}level_{index}:" for index in range(66)]
    lines.append(f"{'  ' * 66}value: terminal")
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize("linked_relative", [".research", ".research/schemas"])
def test_init_rejects_managed_symlink_traversal_without_writing_target(
    git_repository: Path,
    tmp_path: Path,
    linked_relative: str,
) -> None:
    external_target = tmp_path / ("external-" + linked_relative.replace("/", "-"))
    external_target.mkdir()
    marker = external_target / "owned.txt"
    marker.write_text("outside stays untouched\n", encoding="utf-8")
    linked_path = git_repository / linked_relative
    linked_path.parent.mkdir(parents=True, exist_ok=True)
    linked_path.symlink_to(external_target, target_is_directory=True)
    before = _snapshot_external_target(external_target)

    with pytest.raises(UnsafeRepositoryPathError) as raised:
        initialize_project(git_repository)

    assert raised.value.code == "unsafe_repository_path"
    assert _snapshot_external_target(external_target) == before
    assert not (git_repository / ".researchctl.toml").exists()


@pytest.mark.parametrize("linked_relative", [".research", ".research/tasks"])
def test_doctor_rejects_managed_symlink_traversal_without_writing_target(
    initialized_repository: Path,
    tmp_path: Path,
    linked_relative: str,
) -> None:
    linked_path = initialized_repository / linked_relative
    preserved = initialized_repository / (
        ".research-preserved" if linked_relative == ".research" else ".tasks-preserved"
    )
    linked_path.rename(preserved)
    external_target = tmp_path / ("doctor-external-" + linked_relative.replace("/", "-"))
    external_target.mkdir()
    (external_target / "owned.txt").write_text(
        "outside stays untouched\n",
        encoding="utf-8",
    )
    linked_path.symlink_to(external_target, target_is_directory=True)
    before = _snapshot_external_target(external_target)

    with pytest.raises(UnsafeRepositoryPathError) as raised:
        doctor(initialized_repository)

    assert raised.value.code == "unsafe_repository_path"
    assert _snapshot_external_target(external_target) == before


def test_init_never_persists_http_remote_credentials_or_query_tokens(
    git_repository: Path,
    run_git,
) -> None:
    secret_user = "automation-user"
    secret_password = "password-secret"
    secret_query = "query-token-secret"
    remote = (
        f"https://{secret_user}:{secret_password}@example.invalid/org/research.git"
        f"?access_token={secret_query}#fragment-secret"
    )
    run_git(git_repository, "remote", "add", "origin", remote)

    initialize_project(git_repository, default_branch="main")

    project = load_model(
        git_repository / ".research/project.yaml",
        ProjectRecord,
    )
    assert project.repository.remote_url == "https://example.invalid/org/research.git"
    persisted = b"\n".join(
        path.read_bytes()
        for path in (git_repository / ".research").rglob("*")
        if path.is_file()
    )
    for secret in (secret_user, secret_password, secret_query, "fragment-secret"):
        assert secret.encode() not in persisted


def test_remote_without_origin_head_requires_an_explicit_default_branch(
    git_repository: Path,
    run_git,
    snapshot_tree,
) -> None:
    run_git(
        git_repository,
        "remote",
        "add",
        "origin",
        "https://example.invalid/org/research.git",
    )
    before = snapshot_tree(git_repository)

    ambiguous = _invoke_cli("init", str(git_repository), "--json")

    assert ambiguous.returncode == 2
    assert ambiguous.stderr == ""
    failure = json.loads(ambiguous.stdout)
    assert failure["success"] is False
    assert failure["errors"] == [
        {
            "code": "ambiguous_default_branch",
            "message": "The repository remote does not declare its default branch.",
            "remediation": "Repeat init with --default-branch BRANCH.",
            "context": {"observed_branch": "main"},
        }
    ]
    assert snapshot_tree(git_repository) == before

    explicit = _invoke_cli(
        "init",
        str(git_repository),
        "--default-branch",
        "main",
        "--json",
    )

    assert explicit.returncode == 0
    assert explicit.stderr == ""
    success = json.loads(explicit.stdout)
    assert success["success"] is True
    project = load_model(
        git_repository / ".research/project.yaml",
        ProjectRecord,
    )
    assert project.repository.default_branch == "main"


def test_invalid_explicit_default_branch_is_a_typed_zero_write_failure(
    git_repository: Path,
    snapshot_tree,
) -> None:
    before = snapshot_tree(git_repository)

    result = _invoke_cli(
        "init",
        str(git_repository),
        "--default-branch",
        "invalid branch",
        "--json",
    )

    assert result.returncode == 2
    assert result.stderr == ""
    envelope = json.loads(result.stdout)
    assert envelope["success"] is False
    assert envelope["errors"][0]["code"] == "invalid_default_branch"
    assert envelope["errors"][0]["context"] == {
        "default_branch": "invalid branch"
    }
    assert snapshot_tree(git_repository) == before


def test_discover_repository_ignores_git_context_environment(
    git_repository: Path,
    tmp_path: Path,
    run_git,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decoy = tmp_path / "decoy-repository"
    decoy.mkdir()
    run_git(decoy, "init", "--initial-branch=decoy")
    polluted = {
        "GIT_DIR": str(decoy / ".git"),
        "GIT_WORK_TREE": str(decoy),
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.bare",
        "GIT_CONFIG_VALUE_0": "true",
    }
    for key, value in polluted.items():
        monkeypatch.setenv(key, value)

    repository = discover_repository(git_repository)

    assert repository.root == git_repository.resolve()
    assert repository.default_branch == "main"
    assert repository.default_branch_source == "current_branch"
    assert repository.remote_url is None
    for key, value in polluted.items():
        assert os.environ[key] == value


def test_schema_manifest_lock_mismatch_fails_closed_without_config_disclosure(
    initialized_repository: Path,
    snapshot_tree,
) -> None:
    config_path = initialized_repository / ".researchctl.toml"
    config = load_project_config(config_path)
    expected_digest = schema_manifest_digest()
    assert config.schema_manifest_digest == expected_digest
    tampered_digest = "sha256:" + "0" * 64
    assert tampered_digest != expected_digest
    tampered_bytes = config_path.read_bytes().replace(
        expected_digest.encode(),
        tampered_digest.encode(),
    )
    config_path.write_bytes(tampered_bytes)
    before = snapshot_tree(initialized_repository)

    with pytest.raises(ProtocolLockError) as raised:
        initialize_project(initialized_repository)

    assert raised.value.code == "protocol_lock_mismatch"
    assert raised.value.context == {
        "component": "schema manifest",
        "found": tampered_digest,
        "expected": expected_digest,
    }
    assert config.project_id not in str(raised.value)
    assert snapshot_tree(initialized_repository) == before

    cli_result = _invoke_cli("init", str(initialized_repository), "--json")

    assert cli_result.returncode == 2
    assert cli_result.stderr == ""
    envelope = json.loads(cli_result.stdout)
    assert envelope["success"] is False
    assert envelope["errors"][0]["code"] == "protocol_lock_mismatch"
    assert envelope["errors"][0]["context"] == raised.value.context
    assert config.project_id not in cli_result.stdout
    assert snapshot_tree(initialized_repository) == before

    report = doctor(initialized_repository)
    checks = {check.name: check for check in report.checks}

    assert report.healthy is False
    assert checks["schema-lock"].status == "error"
    assert config.project_id not in checks["schema-lock"].message
    assert config_path.read_bytes() == tampered_bytes
    assert snapshot_tree(initialized_repository) == before


@pytest.mark.parametrize(
    "document",
    [
        "record: [unterminated\n",
        "loop: &loop\n  self: *loop\n",
        "payload: " + "x" * (2 * 1024 * 1024) + "\n",
    ],
    ids=["parser-error", "recursive-alias", "oversized"],
)
def test_doctor_cli_reports_serialization_failures_as_json_without_traceback(
    initialized_repository: Path,
    document: str,
) -> None:
    (initialized_repository / ".research/project.yaml").write_text(
        document,
        encoding="utf-8",
    )

    result = _invoke_cli("doctor", str(initialized_repository), "--json")

    assert result.returncode == 2
    assert result.stderr == ""
    assert "Traceback" not in result.stdout
    envelope = json.loads(result.stdout)
    assert envelope["command"] == "doctor"
    assert envelope["success"] is False
    assert envelope["data"]["healthy"] is False
    assert any(error["code"] == "project-record" for error in envelope["errors"])


def test_doctor_cli_reports_overdeep_yaml_as_json_without_traceback(
    initialized_repository: Path,
) -> None:
    (initialized_repository / ".research/project.yaml").write_text(
        _overdeep_yaml(),
        encoding="utf-8",
    )

    result = _invoke_cli("doctor", str(initialized_repository), "--json")

    assert result.returncode == 2
    assert result.stderr == ""
    assert "Traceback" not in result.stdout
    envelope = json.loads(result.stdout)
    assert envelope["success"] is False
    assert any(error["code"] == "project-record" for error in envelope["errors"])


@pytest.mark.parametrize("mode", ["missing", "invalid"])
def test_doctor_reports_missing_or_invalid_default_policy(
    initialized_repository: Path,
    mode: str,
) -> None:
    policy = initialized_repository / ".research/policies/default.yaml"
    if mode == "missing":
        policy.rename(initialized_repository / ".research/policies/default.yaml.missing")
    else:
        policy.write_text("agent: [not-a-policy\n", encoding="utf-8")

    report = doctor(initialized_repository)

    checks = {check.name: check for check in report.checks}
    assert report.healthy is False
    assert checks["project-policy"].status == "error"


def _record_payloads() -> dict[str, dict[str, Any]]:
    def record_id(kind: str, fill: str) -> str:
        return f"{kind}_20260802T123456Z_{fill * 24}"

    return {
        "tasks": {
            "schema_version": "0.1",
            "task_id": record_id("task", "a"),
            "key": "TASK-1",
            "title": "A task",
            "goal": "Exercise doctor validation.",
            "done_when": ["The record is checked."],
            "execution_domain": "on-prem",
            "allowed_write_paths": ["src"],
            "deliverables": ["A validated record."],
            "created_at": OBSERVED_AT,
            "updated_at": OBSERVED_AT,
        },
        "runs": {
            "schema_version": "0.1",
            "attempt_id": record_id("attempt", "b"),
            "run_id": record_id("run", "c"),
            "operation_id": record_id("operation", "d"),
            "events": [
                {
                    "sequence": 0,
                    "operation_id": record_id("operation", "d"),
                    "state": "preparing",
                    "observed_at": OBSERVED_AT,
                    "idempotency_key": "doctor-test",
                }
            ],
        },
        "submissions": {
            "schema_version": "0.1",
            "submission_id": record_id("submission", "e"),
            "task_id": record_id("task", "a"),
            "session_id": record_id("session", "f"),
            "category": "candidate_result",
            "claim": "The declared observation is reproducible.",
            "run_result_ids": [record_id("result", "1")],
            "created_at": OBSERVED_AT,
        },
        "decisions": {
            "schema_version": "0.1",
            "decision_id": record_id("decision", "2"),
            "submission_id": record_id("submission", "e"),
            "disposition": "accepted",
            "reviewer_actor": "manager",
            "decided_at": OBSERVED_AT,
            "claim_scope": "snapshot",
            "code_disposition": "retain_isolated",
            "report_id": record_id("report", "3"),
            "expected_report_revision": 1,
            "accepted_submission_digest": "sha256:" + "4" * 64,
        },
        "reports": {
            "schema_version": "0.1",
            "report_id": record_id("report", "3"),
            "revision": 1,
            "title": "A report",
            "claim": "The declared observation is reproducible.",
            "claim_scope": "snapshot",
            "evidence_status": "verified",
            "applicability": "snapshot_only",
            "submission_id": record_id("submission", "e"),
            "run_result_ids": [record_id("result", "1")],
            "evidence_tree": "5" * 40,
            "accepted_at_main_tree": "6" * 40,
        },
    }


@pytest.mark.parametrize("directory", list(_record_payloads()))
@pytest.mark.parametrize("record_kind", ["malformed", "future-version"])
def test_doctor_reports_invalid_records_in_every_managed_record_directory(
    initialized_repository: Path,
    directory: str,
    record_kind: str,
) -> None:
    payload = _record_payloads()[directory]
    if record_kind == "future-version":
        model_types = {
            "tasks": TaskRecord,
            "runs": RunAttempt,
            "submissions": ResearchSubmission,
            "decisions": ReviewDecision,
            "reports": ReportRecord,
        }
        model_types[directory].model_validate(payload)
        payload["schema_version"] = "9.0"
        document = dump_yaml(payload)
    else:
        document = "record: [unterminated\n"
    relative = f".research/{directory}/security-regression.yaml"
    (initialized_repository / relative).write_text(document, encoding="utf-8")

    report = doctor(initialized_repository)

    checks = {check.name: check for check in report.checks}
    assert report.healthy is False
    assert checks[f"record:{relative}"].status == "error"


def test_publish_exposes_only_the_complete_final_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "managed" / "record.yaml"
    expected = b"schema_version: '0.1'\nrecord: complete\n"
    real_link = init_service.os.link
    observed_temporary: list[bytes] = []

    def inspect_then_link(source: Path, target: Path, **kwargs: Any) -> None:
        assert not target.exists()
        observed_temporary.append(Path(source).read_bytes())
        real_link(source, target, **kwargs)

    monkeypatch.setattr(init_service.os, "link", inspect_then_link)

    identity = init_service._publish_exclusive(
        destination,
        expected,
        display_path=".research/record.yaml",
    )

    assert identity is not None
    assert observed_temporary == [expected]
    assert destination.read_bytes() == expected
    assert not list(destination.parent.glob(".record.yaml.researchctl-*"))


def test_apply_init_treats_concurrent_identical_publish_as_preserved(
    git_repository: Path,
) -> None:
    plan = plan_init(git_repository)
    raced = plan.creates[0]
    assert raced.content is not None
    destination = git_repository / raced.path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(raced.content)

    result = apply_init(plan)

    assert raced.path in result.preserved
    assert raced.path not in result.created
    assert destination.read_bytes() == raced.content
    assert (git_repository / ".research/project.yaml").is_file()


def test_apply_init_rolls_back_published_files_after_write_failure(
    git_repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = plan_init(git_repository)
    original_publish = init_service._publish_exclusive
    calls = 0

    def fail_after_one_publish(
        destination: Path,
        content: bytes,
        *,
        display_path: str,
    ) -> tuple[int, int] | None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected publish failure")
        return original_publish(destination, content, display_path=display_path)

    monkeypatch.setattr(init_service, "_publish_exclusive", fail_after_one_publish)

    with pytest.raises(OSError, match="injected publish failure"):
        apply_init(plan)

    assert calls == 2
    for action in plan.creates:
        assert not (git_repository / action.path).exists()
    assert not list(git_repository.rglob("*.researchctl-*"))
