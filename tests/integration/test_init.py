from __future__ import annotations

from pathlib import Path

import pytest

from researchctl.errors import ConflictError, RepositoryNotFoundError
from researchctl.services.init_project import initialize_project


def test_init_preserves_a_dirty_existing_repository(
    git_repository: Path,
    run_git,
) -> None:
    readme = git_repository / "README.md"
    readme.write_text("# Existing research project\n\nwork in progress\n", encoding="utf-8")
    launcher = git_repository / "launch_experiment.sh"
    launcher.write_text("#!/bin/sh\npython train.py\n", encoding="utf-8")

    head_before = run_git(git_repository, "rev-parse", "HEAD").stdout.strip()
    status_before = set(
        run_git(
            git_repository,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ).stdout.splitlines()
    )
    existing_bytes = {
        "README.md": readme.read_bytes(),
        "launch_experiment.sh": launcher.read_bytes(),
        "requirements.lock": (git_repository / "requirements.lock").read_bytes(),
    }

    result = initialize_project(git_repository, name="Transformer Experiments", key="TX")

    assert result.repository == git_repository.resolve()
    assert result.dry_run is False
    assert result.created
    assert run_git(git_repository, "rev-parse", "HEAD").stdout.strip() == head_before
    status_after = set(
        run_git(
            git_repository,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ).stdout.splitlines()
    )
    assert status_before <= status_after
    assert readme.read_bytes() == existing_bytes["README.md"]
    assert launcher.read_bytes() == existing_bytes["launch_experiment.sh"]
    assert (git_repository / "requirements.lock").read_bytes() == existing_bytes[
        "requirements.lock"
    ]
    assert (git_repository / ".researchctl.toml").is_file()
    assert (git_repository / ".research/project.yaml").is_file()


def test_init_dry_run_has_zero_worktree_writes(
    git_repository: Path,
    run_git,
    snapshot_tree,
) -> None:
    before = snapshot_tree(git_repository)
    status_before = run_git(
        git_repository,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    ).stdout

    result = initialize_project(git_repository, dry_run=True)

    assert result.dry_run is True
    assert result.created
    assert snapshot_tree(git_repository) == before
    assert (
        run_git(
            git_repository,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ).stdout
        == status_before
    )
    assert not (git_repository / ".researchctl.toml").exists()
    assert not (git_repository / ".research").exists()


def test_repeated_init_creates_no_second_change(
    git_repository: Path,
    run_git,
    snapshot_tree,
) -> None:
    first = initialize_project(git_repository)
    tree_after_first = snapshot_tree(git_repository)
    status_after_first = run_git(
        git_repository,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    ).stdout

    second = initialize_project(
        git_repository,
        name="A name that must not replace the existing record",
        key="OTHER",
    )

    assert first.created
    assert second.project_id == first.project_id
    assert second.created == ()
    assert set(second.warnings) == {
        "Existing project key was preserved.",
        "Existing project name was preserved.",
    }
    assert snapshot_tree(git_repository) == tree_after_first
    assert (
        run_git(
            git_repository,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ).stdout
        == status_after_first
    )


def test_generated_file_conflict_fails_atomically_without_partial_init(
    git_repository: Path,
    run_git,
    snapshot_tree,
) -> None:
    conflicting_schema = git_repository / ".research/schemas/task.schema.json"
    conflicting_schema.parent.mkdir(parents=True)
    conflicting_schema.write_text('{"owned_by":"existing-project"}\n', encoding="utf-8")
    before = snapshot_tree(git_repository)
    status_before = run_git(
        git_repository,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    ).stdout

    with pytest.raises(ConflictError) as raised:
        initialize_project(git_repository)

    assert raised.value.code == "managed_file_conflict"
    assert raised.value.context == {"paths": [".research/schemas/task.schema.json"]}
    assert snapshot_tree(git_repository) == before
    assert (
        run_git(
            git_repository,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ).stdout
        == status_before
    )
    assert not (git_repository / ".researchctl.toml").exists()
    assert not (git_repository / ".research/project.yaml").exists()
    assert conflicting_schema.read_text(encoding="utf-8") == (
        '{"owned_by":"existing-project"}\n'
    )


def test_init_outside_git_reports_typed_error_without_writing(
    tmp_path: Path,
    snapshot_tree,
) -> None:
    directory = tmp_path / "not-a-repository"
    directory.mkdir()
    existing = directory / "notes.md"
    existing.write_text("keep me\n", encoding="utf-8")
    before = snapshot_tree(directory)

    with pytest.raises(RepositoryNotFoundError) as raised:
        initialize_project(directory)

    assert raised.value.code == "repository_not_found"
    assert raised.value.exit_code == 2
    assert raised.value.context == {"path": str(directory.resolve())}
    assert snapshot_tree(directory) == before
