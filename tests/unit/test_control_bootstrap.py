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
from researchctl.adapters.git_bootstrap import GitBootstrapCommitAdapter
from researchctl.domain.enums import ProjectState
from researchctl.domain.models import ProjectRecord
from researchctl.errors import RCPError
from researchctl.serialization import dump_yaml, load_model, load_yaml
from researchctl.services.control_bootstrap import (
    ControlBootstrapAcceptance,
    capture_managed_init_manifest,
)

OPERATION_ID = "operation_20260803T130000Z_" + "a" * 24
OTHER_OPERATION_ID = "operation_20260803T130001Z_" + "b" * 24


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


class FailBeforeCommit(GitBootstrapCommitAdapter):
    def commit_or_observe(self, **kwargs: Any):
        raise SimulatedCrash("after manifest write, before commit")


class FailAfterCommit(GitBootstrapCommitAdapter):
    def commit_or_observe(self, **kwargs: Any):
        super().commit_or_observe(**kwargs)
        raise SimulatedCrash("after commit, before receipt")


class AdvancingDefaultGit(GitWorktreeAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.default_resolutions = 0

    def resolve_commit(self, root: Path, revision: str) -> str:
        resolved = super().resolve_commit(root, revision)
        if revision == "refs/heads/main":
            self.default_resolutions += 1
            if self.default_resolutions == 2:
                return "f" * len(resolved)
        return resolved


def _git(root: Path, *args: str, check: bool = True) -> str:
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
        check=False,
        capture_output=True,
        env=environment,
        shell=False,
        text=True,
    )
    if check and completed.returncode != 0:
        raise AssertionError(completed.stderr)
    return completed.stdout


@pytest.fixture
def bootstrap_project(initialized_repository: Path) -> tuple[Path, Path, str]:
    project = initialized_repository
    initialized = load_model(project / ".research/project.yaml", ProjectRecord)
    assert initialized.state is ProjectState.BOOTSTRAPPING
    _git(project, "add", ".researchctl.toml", ".research")
    _git(
        project,
        "-c",
        "user.name=Tests",
        "-c",
        "user.email=tests@example.invalid",
        "commit",
        "--no-gpg-sign",
        "-m",
        "initialize research control plane",
    )
    proposal = _git(project, "rev-parse", "HEAD").strip()
    worktrees = project / ".git" / "researchctl" / "worktrees"
    worktrees.mkdir(parents=True)
    return project, worktrees, proposal


def _service(
    project: Path,
    worktrees: Path,
    proposal: str,
    *,
    operation_id: str = OPERATION_ID,
    runner: RecordingRunner | None = None,
    commits: GitBootstrapCommitAdapter | None = None,
    git: GitWorktreeAdapter | None = None,
) -> ControlBootstrapAcceptance:
    return ControlBootstrapAcceptance(
        repository_root=project,
        worktrees_directory=worktrees,
        default_branch="main",
        operation_id=operation_id,
        proposal_commit=proposal,
        git=git or (GitWorktreeAdapter(runner=runner) if runner else None),
        commits=(
            commits
            or (GitBootstrapCommitAdapter(runner=runner) if runner else None)
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


def test_prepare_isolated_acceptance_leaves_default_and_dirty_caller_unchanged(
    bootstrap_project: tuple[Path, Path, str],
) -> None:
    project, worktrees, proposal = bootstrap_project
    (project / "README.md").write_text("# Project\nlocal notes\n", encoding="utf-8")
    (project / "scratch.txt").write_text("unrelated\n", encoding="utf-8")
    before_files = _working_tree_snapshot(project)
    before_status = _git(project, "status", "--porcelain=v1")
    before_head = _git(project, "rev-parse", "HEAD").strip()
    runner = RecordingRunner()

    receipt = _service(
        project,
        worktrees,
        proposal,
        runner=runner,
    ).prepare()

    assert receipt.branch == f"research/control/{OPERATION_ID}"
    assert receipt.proposal_commit == proposal
    assert receipt.changed is True
    assert receipt.changed_paths == (".research/project.yaml",)
    assert receipt.as_dict()["accepted"] is False
    assert receipt.as_dict()["requires_merge"] is True
    assert _project_from_git(project, receipt.commit).state is ProjectState.MANAGED
    assert _project_from_git(project, "main").state is ProjectState.BOOTSTRAPPING
    assert load_model(
        project / ".research/project.yaml",
        ProjectRecord,
    ).state is ProjectState.BOOTSTRAPPING
    assert _git(project, "rev-parse", "main").strip() == before_head
    assert _git(project, "status", "--porcelain=v1") == before_status
    assert _working_tree_snapshot(project) == before_files
    assert _git(project, "rev-list", "--count", f"{proposal}..{receipt.commit}").strip() == "1"

    forbidden = {"push", "send-pack", "request-pull", "fetch", "pull"}
    assert runner.calls
    assert all(call[0] == "git" for call in runner.calls)
    assert all(forbidden.isdisjoint(call) for call in runner.calls)


def test_identical_retry_observes_one_existing_acceptance_commit(
    bootstrap_project: tuple[Path, Path, str],
) -> None:
    project, worktrees, proposal = bootstrap_project
    first = _service(project, worktrees, proposal).prepare()
    repeated = _service(project, worktrees, proposal).prepare()

    assert first.changed is True
    assert repeated.changed is False
    assert repeated.commit == first.commit
    assert repeated.manifest_digest == first.manifest_digest
    assert _git(project, "rev-list", "--count", f"{proposal}..{first.branch}").strip() == "1"


def test_operation_identity_selects_a_unique_branch_and_worktree(
    bootstrap_project: tuple[Path, Path, str],
) -> None:
    project, worktrees, proposal = bootstrap_project
    first = _service(project, worktrees, proposal).prepare()
    second = _service(
        project,
        worktrees,
        proposal,
        operation_id=OTHER_OPERATION_ID,
    ).prepare()

    assert first.branch != second.branch
    assert first.worktree != second.worktree
    assert first.commit != second.commit
    assert first.worktree.is_dir()
    assert second.worktree.is_dir()
    assert _git(project, "rev-list", "--count", f"{proposal}..{first.branch}").strip() == "1"
    assert _git(project, "rev-list", "--count", f"{proposal}..{second.branch}").strip() == "1"


def test_retry_recovers_branch_created_before_worktree_registration(
    bootstrap_project: tuple[Path, Path, str],
) -> None:
    project, worktrees, proposal = bootstrap_project
    branch = f"research/control/{OPERATION_ID}"
    _git(project, "branch", branch, proposal)

    receipt = _service(project, worktrees, proposal).prepare()

    assert receipt.changed is True
    assert receipt.branch == branch
    assert receipt.worktree.is_dir()
    assert _git(receipt.worktree, "symbolic-ref", "--short", "HEAD").strip() == branch
    assert _git(project, "rev-list", "--count", f"{proposal}..{branch}").strip() == "1"


def test_retry_recovers_manifest_write_before_commit(
    bootstrap_project: tuple[Path, Path, str],
) -> None:
    project, worktrees, proposal = bootstrap_project
    interrupted = _service(
        project,
        worktrees,
        proposal,
        commits=FailBeforeCommit(),
    )

    with pytest.raises(SimulatedCrash):
        interrupted.prepare()

    assert ".research/project.yaml" in _git(
        interrupted.worktree,
        "status",
        "--porcelain=v1",
    )
    recovered = _service(project, worktrees, proposal).prepare()
    assert recovered.changed is True
    assert _git(recovered.worktree, "status", "--porcelain=v1") == ""
    assert _git(project, "rev-list", "--count", f"{proposal}..{recovered.branch}").strip() == "1"


def test_retry_observes_commit_created_before_receipt_return(
    bootstrap_project: tuple[Path, Path, str],
) -> None:
    project, worktrees, proposal = bootstrap_project
    interrupted = _service(
        project,
        worktrees,
        proposal,
        commits=FailAfterCommit(),
    )

    with pytest.raises(SimulatedCrash):
        interrupted.prepare()

    recovered = _service(project, worktrees, proposal).prepare()
    assert recovered.changed is False
    assert recovered.effect_applied is True
    assert _git(project, "rev-list", "--count", f"{proposal}..{recovered.branch}").strip() == "1"


def test_default_head_change_before_commit_fails_compare_and_swap(
    bootstrap_project: tuple[Path, Path, str],
) -> None:
    project, worktrees, proposal = bootstrap_project
    service = _service(
        project,
        worktrees,
        proposal,
        git=AdvancingDefaultGit(),
    )

    with pytest.raises(RCPError) as raised:
        service.prepare()

    assert raised.value.code == "bootstrap_default_head_changed"
    assert _git(project, "rev-list", "--count", f"{proposal}..{service.branch}").strip() == "0"


def test_source_symlink_fails_before_branch_creation(
    bootstrap_project: tuple[Path, Path, str],
    tmp_path: Path,
) -> None:
    project, worktrees, proposal = bootstrap_project
    project_path = project / ".research/project.yaml"
    outside = tmp_path / "outside-project.yaml"
    outside.write_bytes(project_path.read_bytes())
    project_path.unlink()
    project_path.symlink_to(outside)

    with pytest.raises(RCPError) as raised:
        _service(project, worktrees, proposal).prepare()

    assert raised.value.code == "bootstrap_init_manifest_symlink"
    assert _git(project, "branch", "--list", f"research/control/{OPERATION_ID}") == ""


def test_proposal_tree_symlink_fails_before_control_branch_creation(
    bootstrap_project: tuple[Path, Path, str],
    tmp_path: Path,
) -> None:
    project, worktrees, _ = bootstrap_project
    proposal_worktree = tmp_path / "proposal"
    _git(
        project,
        "worktree",
        "add",
        "-b",
        "research/bootstrap/test",
        str(proposal_worktree),
        "main",
    )
    policy = proposal_worktree / ".research/policies/default.yaml"
    policy.unlink()
    policy.symlink_to("outside-policy.yaml")
    _git(proposal_worktree, "add", ".research/policies/default.yaml")
    _git(
        proposal_worktree,
        "-c",
        "user.name=Tests",
        "-c",
        "user.email=tests@example.invalid",
        "commit",
        "--no-gpg-sign",
        "-m",
        "unsafe proposal symlink",
    )
    proposal = _git(proposal_worktree, "rev-parse", "HEAD").strip()

    with pytest.raises(RCPError) as raised:
        _service(project, worktrees, proposal).prepare()

    assert raised.value.code == "bootstrap_tree_invalid"
    assert _git(project, "branch", "--list", f"research/control/{OPERATION_ID}") == ""


def test_proposal_cannot_predeclare_itself_managed(
    bootstrap_project: tuple[Path, Path, str],
    tmp_path: Path,
) -> None:
    project, worktrees, _ = bootstrap_project
    proposal_worktree = tmp_path / "self-accepted-proposal"
    _git(
        project,
        "worktree",
        "add",
        "-b",
        "research/bootstrap/self-accepted",
        str(proposal_worktree),
        "main",
    )
    project_path = proposal_worktree / ".research/project.yaml"
    bootstrapping = load_model(project_path, ProjectRecord)
    payload = bootstrapping.model_dump(mode="python")
    payload["state"] = ProjectState.MANAGED
    project_path.write_text(
        dump_yaml(ProjectRecord.model_validate(payload)),
        encoding="utf-8",
    )
    _git(proposal_worktree, "add", ".research/project.yaml")
    _git(
        proposal_worktree,
        "-c",
        "user.name=Untrusted Proposal",
        "-c",
        "user.email=agent@example.invalid",
        "commit",
        "--no-gpg-sign",
        "-m",
        "claim managed state",
    )
    proposal = _git(proposal_worktree, "rev-parse", "HEAD").strip()

    with pytest.raises(RCPError) as raised:
        _service(project, worktrees, proposal).prepare()

    assert raised.value.code == "bootstrap_project_state_invalid"
    assert _git(project, "branch", "--list", f"research/control/{OPERATION_ID}") == ""


def test_proposal_unexpected_managed_content_fails_closed(
    bootstrap_project: tuple[Path, Path, str],
    tmp_path: Path,
) -> None:
    project, worktrees, _ = bootstrap_project
    proposal_worktree = tmp_path / "extra-managed-content"
    _git(
        project,
        "worktree",
        "add",
        "-b",
        "research/bootstrap/extra-content",
        str(proposal_worktree),
        "main",
    )
    unexpected = proposal_worktree / ".research/tasks/unreviewed.yaml"
    unexpected.write_text("state: done\n", encoding="utf-8")
    _git(proposal_worktree, "add", ".research/tasks/unreviewed.yaml")
    _git(
        proposal_worktree,
        "-c",
        "user.name=Untrusted Proposal",
        "-c",
        "user.email=agent@example.invalid",
        "commit",
        "--no-gpg-sign",
        "-m",
        "add unexpected managed content",
    )
    proposal = _git(proposal_worktree, "rev-parse", "HEAD").strip()

    with pytest.raises(RCPError) as raised:
        _service(project, worktrees, proposal).prepare()

    assert raised.value.code == "bootstrap_unexpected_managed_content"
    assert _git(project, "branch", "--list", f"research/control/{OPERATION_ID}") == ""


def test_unexpected_managed_content_fails_before_branch_creation(
    bootstrap_project: tuple[Path, Path, str],
) -> None:
    project, worktrees, proposal = bootstrap_project
    (project / ".research/reports/unreviewed.yaml").write_text(
        "claim: not accepted\n",
        encoding="utf-8",
    )

    with pytest.raises(RCPError) as raised:
        _service(project, worktrees, proposal).prepare()

    assert raised.value.code == "bootstrap_unexpected_managed_content"
    assert _git(project, "branch", "--list", f"research/control/{OPERATION_ID}") == ""


def test_non_bootstrapping_source_fails_closed(
    bootstrap_project: tuple[Path, Path, str],
) -> None:
    project, worktrees, proposal = bootstrap_project
    path = project / ".research/project.yaml"
    current = load_model(path, ProjectRecord)
    payload = current.model_dump(mode="python")
    payload["state"] = ProjectState.MANAGED
    path.write_text(
        dump_yaml(ProjectRecord.model_validate(payload)),
        encoding="utf-8",
    )

    with pytest.raises(RCPError) as raised:
        _service(project, worktrees, proposal).prepare()

    assert raised.value.code == "bootstrap_project_state_invalid"
    assert _git(project, "branch", "--list", f"research/control/{OPERATION_ID}") == ""


def test_default_branch_managed_state_cannot_prepare_another_acceptance(
    bootstrap_project: tuple[Path, Path, str],
) -> None:
    project, worktrees, _ = bootstrap_project
    path = project / ".research/project.yaml"
    bootstrapping = load_model(path, ProjectRecord)
    payload = bootstrapping.model_dump(mode="python")
    payload["state"] = ProjectState.MANAGED
    path.write_text(
        dump_yaml(ProjectRecord.model_validate(payload)),
        encoding="utf-8",
    )
    _git(project, "add", ".research/project.yaml")
    _git(
        project,
        "-c",
        "user.name=Tests",
        "-c",
        "user.email=tests@example.invalid",
        "commit",
        "--no-gpg-sign",
        "-m",
        "already managed",
    )
    managed_head = _git(project, "rev-parse", "HEAD").strip()
    path.write_text(dump_yaml(bootstrapping), encoding="utf-8")

    with pytest.raises(RCPError) as raised:
        _service(project, worktrees, managed_head).prepare()

    assert raised.value.code == "bootstrap_project_state_invalid"
    assert _git(project, "branch", "--list", f"research/control/{OPERATION_ID}") == ""


def test_dirty_control_worktree_outside_manifest_fails_without_second_commit(
    bootstrap_project: tuple[Path, Path, str],
) -> None:
    project, worktrees, proposal = bootstrap_project
    interrupted = _service(
        project,
        worktrees,
        proposal,
        commits=FailBeforeCommit(),
    )
    with pytest.raises(SimulatedCrash):
        interrupted.prepare()
    (interrupted.worktree / "UNRELATED.txt").write_text("unsafe\n", encoding="utf-8")

    with pytest.raises(RCPError) as raised:
        _service(project, worktrees, proposal).prepare()

    assert raised.value.code == "bootstrap_worktree_dirty"
    assert _git(project, "rev-list", "--count", f"{proposal}..{interrupted.branch}").strip() == "0"


@pytest.mark.parametrize(
    "operation_id",
    [
        "operation-not-canonical",
        "operation_20260803T130000Z_" + "a" * 23 + "/",
        "operation_20260803T130000Z_" + "A" * 24,
    ],
)
def test_operation_identity_injection_is_rejected_before_git(
    tmp_path: Path,
    operation_id: str,
) -> None:
    repository = tmp_path / "repository"
    worktrees = tmp_path / "worktrees"
    repository.mkdir()
    worktrees.mkdir()

    with pytest.raises(RCPError) as raised:
        ControlBootstrapAcceptance(
            repository_root=repository,
            worktrees_directory=worktrees,
            default_branch="main",
            operation_id=operation_id,
        )

    assert raised.value.code == "bootstrap_operation_id_invalid"
    assert list(worktrees.iterdir()) == []


def test_manifest_path_injection_is_rejected_before_git(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    runner = RejectingRunner()

    with pytest.raises(RCPError) as raised:
        GitBootstrapCommitAdapter(runner=runner).commit_or_observe(
            worktree=worktree,
            branch=f"research/control/{OPERATION_ID}",
            proposal_commit="a" * 40,
            operation_id=OPERATION_ID,
            manifest_digest="sha256:" + "b" * 64,
            desired_files={
                ".research/project.yaml": b"project\n",
                "../outside": b"unsafe\n",
            },
        )

    assert raised.value.code == "bootstrap_path_invalid"
    assert runner.calls == []


def test_symlink_worktree_parent_is_rejected(
    bootstrap_project: tuple[Path, Path, str],
    tmp_path: Path,
) -> None:
    project, _, proposal = bootstrap_project
    target = tmp_path / "target"
    target.mkdir()
    symlink = tmp_path / "worktrees"
    symlink.symlink_to(target, target_is_directory=True)

    with pytest.raises(RCPError) as raised:
        _service(project, symlink, proposal, operation_id=OTHER_OPERATION_ID)

    assert raised.value.code == "bootstrap_worktrees_directory_invalid"
    assert list(target.iterdir()) == []


def test_capture_manifest_is_digest_only_and_starts_bootstrapping(
    bootstrap_project: tuple[Path, Path, str],
) -> None:
    project, _, _ = bootstrap_project
    manifest = capture_managed_init_manifest(project, default_branch="main")

    rendered = manifest.as_dict()
    assert rendered["source_state"] == "bootstrapping"
    assert rendered["digest"].startswith("sha256:")
    assert all(set(item) == {"path", "digest"} for item in rendered["files"])
    assert "content" not in repr(rendered)
