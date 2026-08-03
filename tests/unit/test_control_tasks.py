from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest

from researchctl.adapters import (
    CommandResult,
    GitWorktreeAdapter,
    SubprocessCommandRunner,
)
from researchctl.adapters.git_control import GitControlCommitAdapter
from researchctl.domain.models import TaskRecord
from researchctl.errors import RCPError
from researchctl.serialization import dump_yaml
from researchctl.services.control_tasks import ControlTaskRecordRepository
from researchctl.services.task_records import TaskRecordRepository


OPERATION_ID = "operation_20260803T120000Z_" + "a" * 24
OTHER_OPERATION_ID = "operation_20260803T120001Z_" + "b" * 24


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._delegate = SubprocessCommandRunner()

    def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path | None,
        env: Mapping[str, str] | None,
        timeout_seconds: float,
    ) -> CommandResult:
        self.calls.append(argv)
        return self._delegate.run(
            argv,
            cwd=cwd,
            env=env,
            timeout_seconds=timeout_seconds,
        )


class RejectingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path | None,
        env: Mapping[str, str] | None,
        timeout_seconds: float,
    ) -> CommandResult:
        self.calls.append(argv)
        raise AssertionError(f"unsafe input reached Git: {argv!r}")


def _git(root: Path, *args: str) -> str:
    environment = os.environ.copy()
    for key in tuple(environment):
        if key.startswith("GIT_CONFIG_") or key in {
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            "GIT_COMMON_DIR",
            "GIT_DIR",
            "GIT_INDEX_FILE",
            "GIT_OBJECT_DIRECTORY",
            "GIT_WORK_TREE",
        }:
            environment.pop(key, None)
    environment.pop("GIT_CONFIG_PARAMETERS", None)
    completed = subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "-C", str(root), *args],
        check=True,
        capture_output=True,
        env=environment,
        shell=False,
        text=True,
    )
    return completed.stdout


@pytest.fixture
def control_project(initialized_repository: Path) -> tuple[Path, Path]:
    _git(initialized_repository, "add", ".researchctl.toml", ".research")
    _git(
        initialized_repository,
        "-c",
        "user.name=Tests",
        "-c",
        "user.email=tests@example.invalid",
        "commit",
        "--no-gpg-sign",
        "-m",
        "initialize research control plane",
    )
    worktrees = initialized_repository / ".git" / "researchctl" / "worktrees"
    worktrees.mkdir(parents=True)
    assert _git(initialized_repository, "status", "--porcelain=v1") == ""
    return initialized_repository, worktrees


def _control_repository(
    project: Path,
    worktrees: Path,
    operation_id: str = OPERATION_ID,
    *,
    runner: RecordingRunner | None = None,
) -> ControlTaskRecordRepository:
    return ControlTaskRecordRepository(
        repository_root=project,
        worktrees_directory=worktrees,
        default_branch="main",
        operation_id=operation_id,
        command="task.create",
        git=GitWorktreeAdapter(runner=runner) if runner else None,
        commits=GitControlCommitAdapter(runner=runner) if runner else None,
    )


def _working_tree_snapshot(root: Path) -> dict[str, tuple[str, bytes]]:
    snapshot: dict[str, tuple[str, bytes]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if relative.parts[0] == ".git":
            continue
        name = relative.as_posix()
        if path.is_symlink():
            snapshot[name] = ("symlink", os.readlink(path).encode())
        elif path.is_dir():
            snapshot[name] = ("directory", b"")
        else:
            snapshot[name] = ("file", path.read_bytes())
    return snapshot


def test_control_task_commit_is_isolated_exact_and_has_no_remote_side_effect(
    control_project: tuple[Path, Path],
    task_payload,
) -> None:
    project, worktrees = control_project
    task = TaskRecord.model_validate(task_payload())
    runner = RecordingRunner()
    repository = _control_repository(project, worktrees, runner=runner)
    before_files = _working_tree_snapshot(project)
    before_status = _git(project, "status", "--porcelain=v1")
    before_head = _git(project, "rev-parse", "HEAD").strip()

    written = repository.create(task)

    receipt = repository.proposal_receipt
    assert receipt is not None
    assert written.path == repository.worktree / ".research" / "tasks" / f"{task.task_id}.yaml"
    assert receipt.branch == f"research/control/{OPERATION_ID}"
    assert receipt.worktree == repository.worktree
    assert receipt.changed is True
    assert receipt.effect_applied is True
    assert _git(project, "rev-parse", receipt.branch).strip() == receipt.commit
    assert _git(project, "rev-parse", "main").strip() == before_head
    assert _git(project, "status", "--porcelain=v1") == before_status
    assert _working_tree_snapshot(project) == before_files

    relative = f".research/tasks/{task.task_id}.yaml"
    assert _git(project, "show", f"{receipt.commit}:{relative}") == dump_yaml(task)
    changed_paths = _git(
        project,
        "diff-tree",
        "--no-commit-id",
        "--name-only",
        "-r",
        receipt.commit,
    ).splitlines()
    assert changed_paths == [relative]

    assert runner.calls
    assert all(argv[0] == "git" for argv in runner.calls)
    forbidden = {"push", "send-pack", "request-pull", "fetch", "pull"}
    assert all(forbidden.isdisjoint(argv) for argv in runner.calls)


def test_operation_identity_selects_a_unique_control_branch_and_worktree(
    control_project: tuple[Path, Path],
    task_payload,
) -> None:
    project, worktrees = control_project
    first = _control_repository(project, worktrees, OPERATION_ID)
    second = _control_repository(project, worktrees, OTHER_OPERATION_ID)
    first_task = TaskRecord.model_validate(task_payload())
    second_task = TaskRecord.model_validate(
        task_payload(
            task_id="task_20260803T120001Z_" + "c" * 24,
            key="MAR-18",
        )
    )

    first.create(first_task)
    second.create(second_task)

    assert first.branch != second.branch
    assert first.worktree != second.worktree
    assert first.worktree.is_dir()
    assert second.worktree.is_dir()
    refs = set(
        _git(
            project,
            "for-each-ref",
            "--format=%(refname:short)",
            "refs/heads/research/control",
        ).splitlines()
    )
    assert refs == {first.branch, second.branch}


def test_identical_retry_observes_the_operation_commit_without_a_second_commit(
    control_project: tuple[Path, Path],
    task_payload,
) -> None:
    project, worktrees = control_project
    task = TaskRecord.model_validate(task_payload())
    first = _control_repository(project, worktrees)
    first_result = first.create(task)
    first_receipt = first.proposal_receipt
    assert first_receipt is not None

    repeated = _control_repository(project, worktrees)
    repeated_result = repeated.create(task)
    repeated_receipt = repeated.proposal_receipt

    assert repeated_receipt is not None
    assert first_result.changed is True
    assert repeated_result.changed is False
    assert repeated_receipt.commit == first_receipt.commit
    assert repeated_receipt.changed is False
    assert repeated_receipt.effect_applied is True
    assert _git(project, "rev-list", "--count", "main.." + repeated.branch).strip() == "1"


def test_retry_recovers_a_task_yaml_written_before_the_control_commit(
    control_project: tuple[Path, Path],
    task_payload,
) -> None:
    project, worktrees = control_project
    task = TaskRecord.model_validate(task_payload())
    interrupted = _control_repository(project, worktrees)
    raw_repository = TaskRecordRepository(interrupted.root)
    raw_result = raw_repository.create(task)

    assert raw_result.changed is True
    assert _git(interrupted.worktree, "status", "--porcelain=v1")
    assert _git(project, "rev-list", "--count", "main.." + interrupted.branch).strip() == "0"

    recovered = _control_repository(project, worktrees)
    recovered_result = recovered.create(task)
    receipt = recovered.proposal_receipt

    assert recovered_result.changed is False
    assert receipt is not None
    assert receipt.changed is True
    assert receipt.effect_applied is True
    assert _git(project, "rev-list", "--count", "main.." + recovered.branch).strip() == "1"
    assert _git(recovered.worktree, "status", "--porcelain=v1") == ""


@pytest.mark.parametrize(
    ("operation_id", "command", "code"),
    [
        ("operation-not-canonical", "task.create", "control_operation_id_invalid"),
        (OPERATION_ID, "task.create\nmalicious", "control_command_invalid"),
        (OPERATION_ID, "session.start", "control_command_invalid"),
    ],
)
def test_invalid_control_identity_is_rejected_before_worktree_mutation(
    tmp_path: Path,
    operation_id: str,
    command: str,
    code: str,
) -> None:
    project = tmp_path / "project"
    worktrees = tmp_path / "worktrees"
    project.mkdir()
    worktrees.mkdir()

    with pytest.raises(RCPError) as raised:
        ControlTaskRecordRepository(
            repository_root=project,
            worktrees_directory=worktrees,
            default_branch="main",
            operation_id=operation_id,
            command=command,
        )

    assert raised.value.code == code
    assert list(worktrees.iterdir()) == []


@pytest.mark.parametrize("unsafe_kind", ["outside", "noncanonical", "symlink"])
def test_control_commit_rejects_unsafe_task_paths_before_git(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    worktree = tmp_path / "worktree"
    tasks = worktree / ".research" / "tasks"
    tasks.mkdir(parents=True)
    canonical_name = "task_20260803T120000Z_" + "d" * 24 + ".yaml"
    if unsafe_kind == "outside":
        task_path = tmp_path / canonical_name
        task_path.write_text("outside\n", encoding="utf-8")
    elif unsafe_kind == "noncanonical":
        task_path = tasks / "not-a-task.yaml"
        task_path.write_text("invalid\n", encoding="utf-8")
    else:
        target = tmp_path / "outside.yaml"
        target.write_text("outside\n", encoding="utf-8")
        task_path = tasks / canonical_name
        task_path.symlink_to(target)
    runner = RejectingRunner()

    with pytest.raises(RCPError) as raised:
        GitControlCommitAdapter(runner=runner).commit_or_observe(
            worktree=worktree,
            branch=f"research/control/{OPERATION_ID}",
            task_path=task_path,
            operation_id=OPERATION_ID,
            command="task.create",
        )

    assert raised.value.code == "control_task_path_invalid"
    assert runner.calls == []


def test_control_commit_rejects_a_symlink_worktree_before_git(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    worktree = tmp_path / "worktree"
    worktree.symlink_to(target, target_is_directory=True)
    task_path = target / ".research" / "tasks" / (
        "task_20260803T120000Z_" + "d" * 24 + ".yaml"
    )
    task_path.parent.mkdir(parents=True)
    task_path.write_text("task\n", encoding="utf-8")
    runner = RejectingRunner()

    with pytest.raises(RCPError) as raised:
        GitControlCommitAdapter(runner=runner).commit_or_observe(
            worktree=worktree,
            branch=f"research/control/{OPERATION_ID}",
            task_path=task_path,
            operation_id=OPERATION_ID,
            command="task.create",
        )

    assert raised.value.code == "control_worktree_invalid"
    assert runner.calls == []


def test_control_commit_refuses_to_stage_in_the_wrong_branch(
    control_project: tuple[Path, Path],
    task_payload,
) -> None:
    project, _ = control_project
    task = TaskRecord.model_validate(task_payload())
    task_path = TaskRecordRepository(project).create(task).path

    with pytest.raises(RCPError) as raised:
        GitControlCommitAdapter().commit_or_observe(
            worktree=project,
            branch=f"research/control/{OPERATION_ID}",
            task_path=task_path,
            operation_id=OPERATION_ID,
            command="task.create",
        )

    assert raised.value.code == "control_branch_mismatch"
    assert _git(project, "diff", "--cached", "--name-only") == ""
