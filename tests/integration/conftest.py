from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from researchctl.services.init_project import initialize_project

GitRunner = Callable[..., subprocess.CompletedProcess[str]]
TreeSnapshot = dict[str, tuple[str, bytes]]


def _run_git(repository: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _snapshot_tree(root: Path) -> TreeSnapshot:
    snapshot: TreeSnapshot = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if relative.parts[0] == ".git":
            continue
        key = relative.as_posix()
        if path.is_symlink():
            snapshot[key] = ("symlink", os.readlink(path).encode())
        elif path.is_dir():
            snapshot[key] = ("directory", b"")
        else:
            snapshot[key] = ("file", path.read_bytes())
    return snapshot


@pytest.fixture
def run_git() -> GitRunner:
    return _run_git


@pytest.fixture
def snapshot_tree() -> Callable[[Path], TreeSnapshot]:
    return _snapshot_tree


@pytest.fixture
def git_repository(tmp_path: Path) -> Iterator[Path]:
    repository = tmp_path / "research-project"
    repository.mkdir()
    _run_git(repository, "init", "--initial-branch=main")
    _run_git(repository, "config", "user.name", "RCP Integration Tests")
    _run_git(repository, "config", "user.email", "rcp-tests@example.invalid")

    (repository / "README.md").write_text("# Existing research project\n", encoding="utf-8")
    (repository / "requirements.lock").write_text("pytest==9.0.2\n", encoding="utf-8")
    _run_git(repository, "add", "README.md", "requirements.lock")
    _run_git(repository, "commit", "-m", "initial project")

    yield repository


@pytest.fixture
def initialized_repository(git_repository: Path) -> Path:
    initialize_project(git_repository)
    return git_repository
