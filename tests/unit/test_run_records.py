from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from researchctl.domain.models import RunResult, RunSpec
from researchctl.errors import RCPError
from researchctl.serialization import canonical_digest, dump_yaml
from researchctl.services.run_records import GitRunRecordRepository


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _commit_protocol(repository: Path) -> str:
    _git(repository, "add", ".researchctl.toml", ".research")
    _git(
        repository,
        "-c",
        "user.name=Tests",
        "-c",
        "user.email=tests@example.invalid",
        "commit",
        "--no-gpg-sign",
        "-m",
        "protocol fixture",
    )
    return _git(repository, "rev-parse", "HEAD").strip()


def _spec(repository: Path, run_spec_payload) -> RunSpec:
    source_commit = _git(repository, "rev-parse", "HEAD").strip()
    source_tree = _git(repository, "rev-parse", "HEAD^{tree}").strip()
    base = RunSpec.model_validate(run_spec_payload())
    updates = {
        "source_commit": source_commit,
        "source_tree": source_tree,
        "requested_host": "host-a",
    }
    normalized = {
        key: TypeAdapter(RunSpec.model_fields[key].annotation).validate_python(value)
        for key, value in updates.items()
    }
    draft = base.model_copy(update=normalized)
    payload = draft.model_dump(
        mode="json",
        exclude={"spec_digest"},
        exclude_none=True,
    )
    payload["spec_digest"] = canonical_digest(payload)
    return RunSpec.model_validate(payload)


@pytest.fixture
def run_repository(initialized_repository: Path, run_spec_payload):
    source = _commit_protocol(initialized_repository)
    (initialized_repository / "experiment.py").write_text(
        "print('frozen')\n", encoding="utf-8"
    )
    _git(initialized_repository, "add", "experiment.py")
    _git(
        initialized_repository,
        "-c",
        "user.name=Tests",
        "-c",
        "user.email=tests@example.invalid",
        "commit",
        "-m",
        "experiment source",
    )
    del source
    spec = _spec(initialized_repository, run_spec_payload)
    worktrees = initialized_repository / ".git" / "researchctl" / "worktrees"
    worktrees.mkdir(parents=True, exist_ok=True)
    return initialized_repository, spec, worktrees


def test_freeze_keeps_metadata_and_execution_identity_separate_and_immutable(
    run_repository,
) -> None:
    repository, spec, worktrees = run_repository
    default_head = _git(repository, "rev-parse", "HEAD").strip()
    default_status = _git(repository, "status", "--porcelain=v1", "-z")
    records = GitRunRecordRepository(
        repository_root=repository,
        worktrees_directory=worktrees,
        spec=spec,
    )

    receipt = records.freeze()

    assert receipt.changed is True
    assert receipt.effect_applied is True
    assert receipt.source_commit == spec.source_commit
    assert _git(repository, "rev-parse", receipt.tag).strip() == receipt.spec_commit
    assert _git(repository, "rev-parse", f"{receipt.spec_commit}^").strip() == spec.source_commit
    assert _git(records.execution_worktree, "rev-parse", "HEAD").strip() == spec.source_commit
    detached = subprocess.run(
        ["git", "-C", str(records.execution_worktree), "symbolic-ref", "-q", "HEAD"],
        capture_output=True,
        text=True,
    )
    assert detached.returncode == 1
    relative = f".research/runs/{spec.run_id}/spec.yaml"
    assert _git(repository, "show", f"{receipt.spec_commit}:{relative}") == dump_yaml(spec)
    assert _git(repository, "rev-parse", "HEAD").strip() == default_head
    assert _git(repository, "status", "--porcelain=v1", "-z") == default_status


def test_freeze_retry_and_crash_after_file_write_create_only_one_spec_commit(
    run_repository,
) -> None:
    repository, spec, worktrees = run_repository
    interrupted = GitRunRecordRepository(
        repository_root=repository,
        worktrees_directory=worktrees,
        spec=spec,
    )
    interrupted.directory
    interrupted.spec_path.write_text(dump_yaml(spec), encoding="utf-8")

    recovered = GitRunRecordRepository(
        repository_root=repository,
        worktrees_directory=worktrees,
        spec=spec,
    )
    first = recovered.freeze()
    repeated = GitRunRecordRepository(
        repository_root=repository,
        worktrees_directory=worktrees,
        spec=spec,
    ).freeze()

    assert first.changed is True
    assert repeated.changed is False
    assert repeated.effect_applied is True
    assert repeated.spec_commit == first.spec_commit
    assert (
        _git(
            repository,
            "rev-list",
            "--count",
            spec.source_commit + ".." + repeated.branch,
        ).strip()
        == "1"
    )


def test_freeze_recovers_when_only_run_branch_identity_remains(
    run_repository,
) -> None:
    repository, spec, worktrees = run_repository
    records = GitRunRecordRepository(
        repository_root=repository,
        worktrees_directory=worktrees,
        spec=spec,
    )
    frozen = records.freeze()
    _git(repository, "update-ref", "-d", f"refs/tags/{records.tag}")

    recovered = GitRunRecordRepository(
        repository_root=repository,
        worktrees_directory=worktrees,
        spec=spec,
    )
    recovered.require_started()
    receipt = recovered.freeze()

    assert receipt.changed is False
    assert receipt.spec_commit == frozen.spec_commit
    assert _git(repository, "rev-parse", f"refs/heads/{records.branch}").strip() == (
        frozen.spec_commit
    )
    assert _git(repository, "rev-parse", f"refs/tags/{records.tag}").strip() == (
        frozen.spec_commit
    )


def test_freeze_recovers_when_only_immutable_run_tag_remains(
    run_repository,
) -> None:
    repository, spec, worktrees = run_repository
    records = GitRunRecordRepository(
        repository_root=repository,
        worktrees_directory=worktrees,
        spec=spec,
    )
    frozen = records.freeze()
    _git(repository, "worktree", "remove", "--force", str(records.metadata_worktree))
    _git(repository, "worktree", "remove", "--force", str(records.execution_worktree))
    _git(repository, "update-ref", "-d", f"refs/heads/{records.branch}")

    recovered = GitRunRecordRepository(
        repository_root=repository,
        worktrees_directory=worktrees,
        spec=spec,
    )
    recovered.require_started()
    receipt = recovered.freeze()

    assert receipt.changed is False
    assert receipt.spec_commit == frozen.spec_commit
    assert _git(repository, "rev-parse", f"refs/heads/{records.branch}").strip() == (
        frozen.spec_commit
    )
    assert _git(repository, "rev-parse", f"refs/tags/{records.tag}").strip() == (
        frozen.spec_commit
    )
    assert recovered.metadata_worktree.is_dir()
    assert recovered.execution_worktree.is_dir()


def test_collect_appends_one_result_without_moving_frozen_tag(
    run_repository,
    run_result_payload,
) -> None:
    repository, spec, worktrees = run_repository
    records = GitRunRecordRepository(
        repository_root=repository,
        worktrees_directory=worktrees,
        spec=spec,
    )
    frozen = records.freeze()
    result = RunResult.model_validate(
        run_result_payload(
            run_id=spec.run_id,
            run_spec_digest=spec.spec_digest,
        )
    )

    collected = records.collect(result)
    repeated = records.collect(result)

    assert collected.changed is True
    assert repeated.changed is False
    assert repeated.result_commit == collected.result_commit
    assert _git(repository, "rev-parse", records.tag).strip() == frozen.spec_commit
    assert (
        _git(repository, "rev-parse", f"{collected.result_commit}^").strip()
        == frozen.spec_commit
    )
    assert _git(records.execution_worktree, "rev-parse", "HEAD").strip() == spec.source_commit
    relative = f".research/runs/{spec.run_id}/result.yaml"
    assert _git(repository, "show", f"{collected.result_commit}:{relative}") == dump_yaml(result)


def test_run_records_reject_source_result_and_existing_content_conflicts(
    run_repository,
    run_spec_payload,
    run_result_payload,
) -> None:
    repository, spec, worktrees = run_repository
    wrong_tree_draft = spec.model_copy(update={"source_tree": "f" * 40})
    wrong_tree_payload = wrong_tree_draft.model_dump(
        mode="json",
        exclude={"spec_digest"},
        exclude_none=True,
    )
    wrong_tree_payload["spec_digest"] = canonical_digest(wrong_tree_payload)
    wrong_tree = RunSpec.model_validate(wrong_tree_payload)
    with pytest.raises(RCPError) as source:
        GitRunRecordRepository(
            repository_root=repository,
            worktrees_directory=worktrees,
            spec=wrong_tree,
        ).freeze()
    assert source.value.code == "run_source_mismatch"

    records = GitRunRecordRepository(
        repository_root=repository,
        worktrees_directory=worktrees,
        spec=spec,
    )
    records.freeze()
    wrong_result = RunResult.model_validate(
        run_result_payload(run_id=spec.run_id)
    )
    with pytest.raises(RCPError) as digest:
        records.collect(wrong_result)
    assert digest.value.code == "run_result_spec_mismatch"

    records.result_path.write_text("different\n", encoding="utf-8")
    correct = RunResult.model_validate(
        run_result_payload(run_id=spec.run_id, run_spec_digest=spec.spec_digest)
    )
    with pytest.raises(RCPError) as conflict:
        records.collect(correct)
    assert conflict.value.code == "run_result_conflict"
