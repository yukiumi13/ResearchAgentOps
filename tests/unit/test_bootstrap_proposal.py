from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from researchctl.adapters import (
    CommandResult,
    GitWorktreeAdapter,
    SubprocessCommandRunner,
)
from researchctl.adapters.git_bootstrap_proposal import (
    GitBootstrapProposalAdapter,
)
from researchctl.domain.enums import ProjectState
from researchctl.domain.models import ProjectRecord
from researchctl.errors import RCPError
from researchctl.serialization import load_model, load_yaml
from researchctl.services.bootstrap_proposal import BootstrapProposalService
from researchctl.services.control_bootstrap import capture_managed_init_manifest

OPERATION_ID = "operation_20260803T140000Z_" + "a" * 24
OTHER_OPERATION_ID = "operation_20260803T140001Z_" + "b" * 24
BOOTSTRAP_ID = "bootstrap_20260803T140000Z_" + "c" * 24
OTHER_BOOTSTRAP_ID = "bootstrap_20260803T140001Z_" + "d" * 24


class SimulatedCrash(RuntimeError):
    pass


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


class FailBeforeCommit(GitBootstrapProposalAdapter):
    def commit_or_observe(self, **kwargs: Any):
        raise SimulatedCrash("after init copy, before proposal commit")


class FailAfterCommit(GitBootstrapProposalAdapter):
    def commit_or_observe(self, **kwargs: Any):
        super().commit_or_observe(**kwargs)
        raise SimulatedCrash("after proposal commit, before receipt")


def _git_result(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
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
    return subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "-C", str(root), *args],
        check=False,
        capture_output=True,
        env=environment,
        shell=False,
        text=True,
    )


def _git(root: Path, *args: str) -> str:
    completed = _git_result(root, *args)
    if completed.returncode != 0:
        raise AssertionError(completed.stderr)
    return completed.stdout


@pytest.fixture
def uncommitted_init_project(
    initialized_repository: Path,
) -> tuple[Path, Path, str]:
    project = initialized_repository
    initial = load_model(project / ".research/project.yaml", ProjectRecord)
    assert initial.state is ProjectState.BOOTSTRAPPING
    base = _git(project, "rev-parse", "HEAD").strip()
    assert _git_result(
        project,
        "cat-file",
        "-e",
        f"{base}:.research/project.yaml",
    ).returncode != 0
    worktrees = project / ".git" / "researchctl" / "worktrees"
    worktrees.mkdir(parents=True)
    return project, worktrees, base


def _service(
    project: Path,
    worktrees: Path,
    base: str,
    *,
    operation_id: str = OPERATION_ID,
    bootstrap_id: str = BOOTSTRAP_ID,
    runner: RecordingRunner | None = None,
    commits: GitBootstrapProposalAdapter | None = None,
) -> BootstrapProposalService:
    return BootstrapProposalService(
        repository_root=project,
        worktrees_directory=worktrees,
        default_branch="main",
        expected_default_head=base,
        operation_id=operation_id,
        bootstrap_id=bootstrap_id,
        git=GitWorktreeAdapter(runner=runner) if runner else None,
        commits=(
            commits
            or (GitBootstrapProposalAdapter(runner=runner) if runner else None)
        ),
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


def _project_from_git(root: Path, revision: str) -> ProjectRecord:
    text = _git(root, "show", f"{revision}:.research/project.yaml")
    return ProjectRecord.model_validate(load_yaml(text))


def test_uncommitted_init_is_copied_to_an_isolated_proposal_only(
    uncommitted_init_project: tuple[Path, Path, str],
) -> None:
    project, worktrees, base = uncommitted_init_project
    (project / "README.md").write_text("# Project\nlocal edit\n", encoding="utf-8")
    (project / "scratch.txt").write_text("unrelated\n", encoding="utf-8")
    manifest = capture_managed_init_manifest(project, default_branch="main")
    before_files = _working_tree_snapshot(project)
    before_status = _git(project, "status", "--porcelain=v1")
    before_head = _git(project, "rev-parse", "HEAD").strip()
    runner = RecordingRunner()

    receipt = _service(
        project,
        worktrees,
        base,
        runner=runner,
    ).prepare()

    assert receipt.branch == f"research/bootstrap/{BOOTSTRAP_ID}"
    assert receipt.base_commit == base
    assert receipt.changed is True
    assert receipt.changed_paths == tuple(sorted(manifest.file_map()))
    assert _project_from_git(project, receipt.commit).state is ProjectState.BOOTSTRAPPING
    assert load_model(
        project / ".research/project.yaml",
        ProjectRecord,
    ).state is ProjectState.BOOTSTRAPPING
    assert _git_result(
        project,
        "cat-file",
        "-e",
        "main:.research/project.yaml",
    ).returncode != 0
    assert _git(project, "rev-parse", "main").strip() == before_head
    assert _git(project, "status", "--porcelain=v1") == before_status
    assert _working_tree_snapshot(project) == before_files
    assert _git(project, "rev-list", "--count", f"{base}..{receipt.commit}").strip() == "1"

    rendered = receipt.as_dict()
    assert rendered["proposal_only"] is True
    assert rendered["accepted"] is False
    assert rendered["pushed"] is False
    assert rendered["pr_created"] is False
    forbidden = {"push", "send-pack", "request-pull", "fetch", "pull"}
    assert runner.calls
    assert all(call[0] == "git" for call in runner.calls)
    assert all(forbidden.isdisjoint(call) for call in runner.calls)


def test_identical_retry_observes_one_proposal_commit(
    uncommitted_init_project: tuple[Path, Path, str],
) -> None:
    project, worktrees, base = uncommitted_init_project
    first = _service(project, worktrees, base).prepare()
    repeated = _service(project, worktrees, base).prepare()

    assert first.changed is True
    assert repeated.changed is False
    assert repeated.commit == first.commit
    assert repeated.manifest_digest == first.manifest_digest
    assert _git(project, "rev-list", "--count", f"{base}..{first.branch}").strip() == "1"


def test_bootstrap_identity_selects_a_unique_branch_and_worktree(
    uncommitted_init_project: tuple[Path, Path, str],
) -> None:
    project, worktrees, base = uncommitted_init_project
    first = _service(project, worktrees, base).prepare()
    second = _service(
        project,
        worktrees,
        base,
        operation_id=OTHER_OPERATION_ID,
        bootstrap_id=OTHER_BOOTSTRAP_ID,
    ).prepare()

    assert first.branch != second.branch
    assert first.worktree != second.worktree
    assert first.commit != second.commit
    assert first.worktree.is_dir()
    assert second.worktree.is_dir()


def test_existing_proposal_cannot_be_rebound_to_another_operation(
    uncommitted_init_project: tuple[Path, Path, str],
) -> None:
    project, worktrees, base = uncommitted_init_project
    first = _service(project, worktrees, base).prepare()

    with pytest.raises(RCPError) as raised:
        _service(
            project,
            worktrees,
            base,
            operation_id=OTHER_OPERATION_ID,
        ).prepare()

    assert raised.value.code == "bootstrap_proposal_commit_invalid"
    assert _git(project, "rev-list", "--count", f"{base}..{first.branch}").strip() == "1"


def test_existing_branch_with_non_protocol_commit_fails_closed(
    uncommitted_init_project: tuple[Path, Path, str],
) -> None:
    project, worktrees, base = uncommitted_init_project
    service = _service(project, worktrees, base)
    _git(
        project,
        "worktree",
        "add",
        "-b",
        service.branch,
        str(service.worktree),
        base,
    )
    (service.worktree / "ATTACK.txt").write_text("unexpected\n", encoding="utf-8")
    _git(service.worktree, "add", "ATTACK.txt")
    _git(
        service.worktree,
        "-c",
        "user.name=Untrusted",
        "-c",
        "user.email=untrusted@example.invalid",
        "commit",
        "--no-gpg-sign",
        "-m",
        "unrelated branch commit",
    )

    with pytest.raises(RCPError) as raised:
        service.prepare()

    assert raised.value.code == "bootstrap_proposal_commit_invalid"
    assert _git(project, "rev-list", "--count", f"{base}..{service.branch}").strip() == "1"


def test_retry_recovers_branch_created_before_worktree_registration(
    uncommitted_init_project: tuple[Path, Path, str],
) -> None:
    project, worktrees, base = uncommitted_init_project
    branch = f"research/bootstrap/{BOOTSTRAP_ID}"
    _git(project, "branch", branch, base)

    receipt = _service(project, worktrees, base).prepare()

    assert receipt.changed is True
    assert receipt.branch == branch
    assert _git(receipt.worktree, "symbolic-ref", "--short", "HEAD").strip() == branch
    assert _git(project, "rev-list", "--count", f"{base}..{branch}").strip() == "1"


def test_retry_recovers_files_copied_before_commit(
    uncommitted_init_project: tuple[Path, Path, str],
) -> None:
    project, worktrees, base = uncommitted_init_project
    interrupted = _service(
        project,
        worktrees,
        base,
        commits=FailBeforeCommit(),
    )
    with pytest.raises(SimulatedCrash):
        interrupted.prepare()

    assert _git(interrupted.worktree, "status", "--porcelain=v1")
    recovered = _service(project, worktrees, base).prepare()
    assert recovered.changed is True
    assert _git(recovered.worktree, "status", "--porcelain=v1") == ""
    assert _git(project, "rev-list", "--count", f"{base}..{recovered.branch}").strip() == "1"


def test_retry_observes_commit_created_before_receipt_return(
    uncommitted_init_project: tuple[Path, Path, str],
) -> None:
    project, worktrees, base = uncommitted_init_project
    interrupted = _service(
        project,
        worktrees,
        base,
        commits=FailAfterCommit(),
    )
    with pytest.raises(SimulatedCrash):
        interrupted.prepare()

    recovered = _service(project, worktrees, base).prepare()
    assert recovered.changed is False
    assert _git(project, "rev-list", "--count", f"{base}..{recovered.branch}").strip() == "1"


def test_partial_expected_file_is_recovered_in_registered_worktree(
    uncommitted_init_project: tuple[Path, Path, str],
) -> None:
    project, worktrees, base = uncommitted_init_project
    service = _service(project, worktrees, base)
    _git(
        project,
        "worktree",
        "add",
        "-b",
        service.branch,
        str(service.worktree),
        base,
    )
    (service.worktree / ".researchctl.toml").write_text(
        "partial = true\n",
        encoding="utf-8",
    )

    receipt = service.prepare()

    assert receipt.changed is True
    assert _git(service.worktree, "status", "--porcelain=v1") == ""


def test_unexpected_partial_worktree_content_fails_without_commit(
    uncommitted_init_project: tuple[Path, Path, str],
) -> None:
    project, worktrees, base = uncommitted_init_project
    service = _service(project, worktrees, base)
    _git(
        project,
        "worktree",
        "add",
        "-b",
        service.branch,
        str(service.worktree),
        base,
    )
    (service.worktree / "UNRELATED.txt").write_text("unsafe\n", encoding="utf-8")

    with pytest.raises(RCPError) as raised:
        service.prepare()

    assert raised.value.code == "bootstrap_proposal_worktree_dirty"
    assert _git(project, "rev-list", "--count", f"{base}..{service.branch}").strip() == "0"


def test_partial_worktree_symlink_fails_closed(
    uncommitted_init_project: tuple[Path, Path, str],
    tmp_path: Path,
) -> None:
    project, worktrees, base = uncommitted_init_project
    service = _service(project, worktrees, base)
    _git(
        project,
        "worktree",
        "add",
        "-b",
        service.branch,
        str(service.worktree),
        base,
    )
    managed = service.worktree / ".research"
    managed.mkdir()
    outside = tmp_path / "outside-project.yaml"
    outside.write_text("outside\n", encoding="utf-8")
    (managed / "project.yaml").symlink_to(outside)

    with pytest.raises(RCPError) as raised:
        service.prepare()

    assert raised.value.code == "bootstrap_proposal_worktree_symlink"
    assert _git(project, "rev-list", "--count", f"{base}..{service.branch}").strip() == "0"


def test_partial_managed_content_in_default_base_fails_before_branch(
    uncommitted_init_project: tuple[Path, Path, str],
) -> None:
    project, worktrees, _ = uncommitted_init_project
    _git(project, "add", ".researchctl.toml")
    _git(
        project,
        "-c",
        "user.name=Tests",
        "-c",
        "user.email=tests@example.invalid",
        "commit",
        "--no-gpg-sign",
        "-m",
        "partial init",
    )
    partial_base = _git(project, "rev-parse", "HEAD").strip()

    with pytest.raises(RCPError) as raised:
        _service(project, worktrees, partial_base).prepare()

    assert raised.value.code == "bootstrap_proposal_base_not_empty"
    assert _git(project, "branch", "--list", f"research/bootstrap/{BOOTSTRAP_ID}") == ""


def test_default_head_mismatch_fails_before_branch_creation(
    uncommitted_init_project: tuple[Path, Path, str],
) -> None:
    project, worktrees, base = uncommitted_init_project
    (project / "README.md").write_text("# advanced\n", encoding="utf-8")
    _git(project, "add", "README.md")
    _git(
        project,
        "-c",
        "user.name=Tests",
        "-c",
        "user.email=tests@example.invalid",
        "commit",
        "--no-gpg-sign",
        "-m",
        "advance default",
    )

    with pytest.raises(RCPError) as raised:
        _service(project, worktrees, base).prepare()

    assert raised.value.code == "bootstrap_proposal_default_head_changed"
    assert _git(project, "branch", "--list", f"research/bootstrap/{BOOTSTRAP_ID}") == ""


def test_source_manifest_symlink_fails_before_branch_creation(
    uncommitted_init_project: tuple[Path, Path, str],
    tmp_path: Path,
) -> None:
    project, worktrees, base = uncommitted_init_project
    project_path = project / ".research/project.yaml"
    outside = tmp_path / "outside.yaml"
    outside.write_bytes(project_path.read_bytes())
    project_path.unlink()
    project_path.symlink_to(outside)

    with pytest.raises(RCPError) as raised:
        _service(project, worktrees, base).prepare()

    assert raised.value.code == "bootstrap_init_manifest_symlink"
    assert _git(project, "branch", "--list", f"research/bootstrap/{BOOTSTRAP_ID}") == ""


@pytest.mark.parametrize(
    ("operation_id", "bootstrap_id", "code"),
    [
        ("operation-not-canonical", BOOTSTRAP_ID, "bootstrap_proposal_operation_id_invalid"),
        (OPERATION_ID, "bootstrap-not-canonical", "bootstrap_id_invalid"),
        (OPERATION_ID, BOOTSTRAP_ID + "/main", "bootstrap_id_invalid"),
    ],
)
def test_identity_injection_is_rejected_before_git(
    tmp_path: Path,
    operation_id: str,
    bootstrap_id: str,
    code: str,
) -> None:
    repository = tmp_path / "repository"
    worktrees = tmp_path / "worktrees"
    repository.mkdir()
    worktrees.mkdir()

    with pytest.raises(RCPError) as raised:
        BootstrapProposalService(
            repository_root=repository,
            worktrees_directory=worktrees,
            default_branch="main",
            expected_default_head="a" * 40,
            operation_id=operation_id,
            bootstrap_id=bootstrap_id,
        )

    assert raised.value.code == code
    assert list(worktrees.iterdir()) == []


def test_manifest_path_injection_is_rejected_before_git(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    runner = RejectingRunner()

    with pytest.raises(RCPError) as raised:
        GitBootstrapProposalAdapter(runner=runner).commit_or_observe(
            worktree=worktree,
            branch=f"research/bootstrap/{BOOTSTRAP_ID}",
            base_commit="a" * 40,
            operation_id=OPERATION_ID,
            bootstrap_id=BOOTSTRAP_ID,
            manifest_digest="sha256:" + "b" * 64,
            desired_files={
                ".research/project.yaml": b"project\n",
                "../outside": b"unsafe\n",
            },
        )

    assert raised.value.code == "bootstrap_proposal_path_invalid"
    assert runner.calls == []


def test_symlink_worktree_parent_is_rejected(
    uncommitted_init_project: tuple[Path, Path, str],
    tmp_path: Path,
) -> None:
    project, _, base = uncommitted_init_project
    target = tmp_path / "target"
    target.mkdir()
    symlink = tmp_path / "worktrees"
    symlink.symlink_to(target, target_is_directory=True)

    with pytest.raises(RCPError) as raised:
        _service(project, symlink, base)

    assert raised.value.code == "bootstrap_proposal_worktrees_invalid"
    assert list(target.iterdir()) == []
