from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from researchctl.adapters.git_scope import GitDiffEntry, GitWriteScopeValidator
from researchctl.domain.models import TaskRecord
from researchctl.errors import RCPError


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(repository: Path, message: str) -> str:
    _git(repository, "add", "-A")
    _git(
        repository,
        "-c",
        "user.name=Tests",
        "-c",
        "user.email=tests@example.invalid",
        "commit",
        "-m",
        message,
    )
    return _git(repository, "rev-parse", "HEAD")


@pytest.fixture
def scoped_repository(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=research/task/MAR-17/session")
    (repository / "src" / "training").mkdir(parents=True)
    (repository / "src" / "training" / "model.py").write_text("old\n", encoding="utf-8")
    (repository / "README.md").write_text("accepted\n", encoding="utf-8")
    return repository, _commit(repository, "base")


def _task(task_payload) -> TaskRecord:
    return TaskRecord.model_validate(
        task_payload(allowed_write_paths=["src/training"], state="ready")
    )


def test_write_scope_accepts_only_declared_paths(
    scoped_repository: tuple[Path, str],
    task_payload,
) -> None:
    repository, base = scoped_repository
    (repository / "src" / "training" / "model.py").write_text("new\n", encoding="utf-8")
    head = _commit(repository, "allowed")

    receipt = GitWriteScopeValidator().validate_commit(
        task=_task(task_payload),
        worktree=repository,
        expected_branch="research/task/MAR-17/session",
        base_commit=base,
        head_commit=head,
    )

    assert receipt.paths == ("src/training/model.py",)
    assert receipt.base_commit == base
    assert receipt.head_commit == head


def test_write_scope_rejects_explicit_commit_behind_branch_tip(
    scoped_repository: tuple[Path, str],
    task_payload,
) -> None:
    repository, base = scoped_repository
    target = repository / "src" / "training" / "model.py"
    target.write_text("candidate\n", encoding="utf-8")
    non_tip = _commit(repository, "candidate")
    target.write_text("tip\n", encoding="utf-8")
    tip = _commit(repository, "tip")

    with pytest.raises(RCPError) as caught:
        GitWriteScopeValidator().validate_commit(
            task=_task(task_payload),
            worktree=repository,
            expected_branch="research/task/MAR-17/session",
            base_commit=base,
            head_commit=non_tip,
        )

    assert caught.value.code == "write_scope_head_mismatch"
    assert caught.value.context == {
        "expected_head": non_tip,
        "observed_head": tip,
    }


def test_write_scope_rejects_base_unreachable_from_branch_tip(
    scoped_repository: tuple[Path, str],
    task_payload,
) -> None:
    repository, head = scoped_repository
    tree = _git(repository, "rev-parse", f"{head}^{{tree}}")
    unrelated = _git(
        repository,
        "-c",
        "user.name=Tests",
        "-c",
        "user.email=tests@example.invalid",
        "commit-tree",
        tree,
        "-m",
        "unrelated base",
    )

    with pytest.raises(RCPError) as caught:
        GitWriteScopeValidator().validate_commit(
            task=_task(task_payload),
            worktree=repository,
            expected_branch="research/task/MAR-17/session",
            base_commit=unrelated,
            head_commit=head,
        )

    assert caught.value.code == "write_scope_base_unreachable"
    assert caught.value.context == {
        "base_commit": unrelated,
        "head_commit": head,
    }


def test_write_scope_rejects_detached_head(
    scoped_repository: tuple[Path, str],
    task_payload,
) -> None:
    repository, head = scoped_repository
    _git(repository, "checkout", "--detach", head)

    with pytest.raises(RCPError) as caught:
        GitWriteScopeValidator().validate_commit(
            task=_task(task_payload),
            worktree=repository,
            expected_branch="research/task/MAR-17/session",
            base_commit=head,
            head_commit=head,
        )

    assert caught.value.code == "write_scope_worktree_mismatch"
    assert caught.value.context == {"observed_branch": None}


def test_write_scope_rejects_outside_protected_and_rename_escapes(
    scoped_repository: tuple[Path, str],
    task_payload,
) -> None:
    repository, base = scoped_repository
    (repository / ".research" / "reports").mkdir(parents=True)
    (repository / ".research" / "reports" / "invented.md").write_text(
        "claim\n", encoding="utf-8"
    )
    (repository / "README.md").write_text("changed\n", encoding="utf-8")
    (repository / "src" / "training" / "model.py").rename(repository / "escaped.py")
    head = _commit(repository, "escape")

    with pytest.raises(RCPError) as caught:
        GitWriteScopeValidator().validate_commit(
            task=_task(task_payload),
            worktree=repository,
            expected_branch="research/task/MAR-17/session",
            base_commit=base,
            head_commit=head,
        )

    assert caught.value.code == "write_scope_violation"
    violations = caught.value.context["violations"]
    assert {item["path"] for item in violations} == {
        ".research/reports/invented.md",
        "README.md",
        "escaped.py",
    }


@pytest.mark.parametrize("kind", ["symlink", "submodule"])
def test_write_scope_rejects_git_link_types_even_under_allowed_prefix(
    scoped_repository: tuple[Path, str],
    task_payload,
    kind: str,
) -> None:
    repository, base = scoped_repository
    target = repository / "src" / "training" / "link"
    if kind == "symlink":
        target.symlink_to("../../../README.md")
        head = _commit(repository, "symlink")
    else:
        blob = _git(repository, "rev-parse", "HEAD")
        _git(
            repository,
            "update-index",
            "--add",
            "--cacheinfo",
            "160000",
            blob,
            "src/training/link",
        )
        _git(
            repository,
            "-c",
            "user.name=Tests",
            "-c",
            "user.email=tests@example.invalid",
            "commit",
            "-m",
            "gitlink",
        )
        head = _git(repository, "rev-parse", "HEAD")

    with pytest.raises(RCPError) as caught:
        GitWriteScopeValidator().validate_commit(
            task=_task(task_payload),
            worktree=repository,
            expected_branch="research/task/MAR-17/session",
            base_commit=base,
            head_commit=head,
        )

    assert caught.value.code == "write_scope_violation"
    assert caught.value.context["violations"][0]["reason"] == f"{kind}_change_forbidden"


def test_write_scope_rejects_wrong_root_branch_and_revision(
    scoped_repository: tuple[Path, str],
    task_payload,
) -> None:
    repository, base = scoped_repository
    validator = GitWriteScopeValidator()

    with pytest.raises(RCPError) as child:
        validator.validate_commit(
            task=_task(task_payload),
            worktree=repository / "src",
            expected_branch="research/task/MAR-17/session",
            base_commit=base,
        )
    assert child.value.code == "write_scope_worktree_mismatch"

    with pytest.raises(RCPError) as branch:
        validator.validate_commit(
            task=_task(task_payload),
            worktree=repository,
            expected_branch="research/task/OTHER/session",
            base_commit=base,
        )
    assert branch.value.code == "write_scope_worktree_mismatch"

    with pytest.raises(RCPError) as revision:
        validator.validate_commit(
            task=_task(task_payload),
            worktree=repository,
            expected_branch="research/task/MAR-17/session",
            base_commit="--output=/tmp/escape",
        )
    assert revision.value.code == "git_revision_invalid"


def test_write_scope_rejects_executable_mode_under_allowed_prefix(
    scoped_repository: tuple[Path, str],
    task_payload,
) -> None:
    repository, base = scoped_repository
    target = repository / "src" / "training" / "model.py"
    target.chmod(0o755)
    head = _commit(repository, "executable")

    with pytest.raises(RCPError) as caught:
        GitWriteScopeValidator().validate_commit(
            task=_task(task_payload),
            worktree=repository,
            expected_branch="research/task/MAR-17/session",
            base_commit=base,
            head_commit=head,
        )

    assert caught.value.code == "write_scope_violation"
    assert caught.value.context["violations"] == [
        {
            "path": "src/training/model.py",
            "reason": "file_mode_or_operation_forbidden",
            "status": "M",
        }
    ]


def test_source_scope_rejects_agent_selected_baseline_not_in_protected_history(
    scoped_repository: tuple[Path, str],
    task_payload,
) -> None:
    repository, trusted_base = scoped_repository
    (repository / "src" / "training" / "model.py").write_text(
        "candidate\n",
        encoding="utf-8",
    )
    source = _commit(repository, "candidate")

    with pytest.raises(RCPError) as caught:
        GitWriteScopeValidator().validate_source(
            task=_task(task_payload),
            repository_root=repository,
            trusted_base_commit=trusted_base,
            baseline_commit=source,
            source_commit=source,
        )

    assert caught.value.code == "write_scope_baseline_untrusted"
    assert caught.value.context == {
        "task_id": _task(task_payload).task_id,
        "baseline_commit": source,
        "trusted_base_commit": trusted_base,
    }


def test_source_scope_rejects_source_outside_declared_baseline_lineage(
    scoped_repository: tuple[Path, str],
    task_payload,
) -> None:
    repository, baseline = scoped_repository
    tree = _git(repository, "rev-parse", f"{baseline}^{{tree}}")
    unrelated = _git(
        repository,
        "-c",
        "user.name=Tests",
        "-c",
        "user.email=tests@example.invalid",
        "commit-tree",
        tree,
        "-m",
        "unrelated source",
    )

    with pytest.raises(RCPError) as caught:
        GitWriteScopeValidator().validate_source(
            task=_task(task_payload),
            repository_root=repository,
            trusted_base_commit=baseline,
            baseline_commit=baseline,
            source_commit=unrelated,
        )

    assert caught.value.code == "write_scope_source_lineage_invalid"


@pytest.mark.parametrize(
    "path",
    (
        "../escape.py",
        "src/training/../../escape.py",
        "/absolute.py",
        "src//training/model.py",
        "src\\training\\model.py",
    ),
)
def test_shared_write_scope_policy_rejects_noncanonical_paths(
    task_payload,
    path: str,
) -> None:
    change = GitDiffEntry(
        path=path,
        status="A",
        old_mode="000000",
        new_mode="100644",
    )

    with pytest.raises(RCPError) as caught:
        GitWriteScopeValidator.validate_changes(
            task=_task(task_payload),
            changes=(change,),
        )

    assert caught.value.code == "write_scope_violation"
    assert caught.value.context["allowed_write_paths"] == ["src/training"]
    assert caught.value.context["violations"] == [
        {"path": path, "reason": "path_invalid", "status": "A"}
    ]
