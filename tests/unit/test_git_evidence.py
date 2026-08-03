from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest

from researchctl.adapters._subprocess import (
    CommandResult,
    SubprocessCommandRunner,
)
from researchctl.adapters.git_evidence import GitRunEvidenceReader
from researchctl.domain.models import RunResult, RunSpec
from researchctl.errors import RCPError
from researchctl.serialization import canonical_digest, dump_yaml
from researchctl.services.run_records import GitRunRecordRepository


def _git(
    root: Path,
    *args: str,
    input_text: str | None = None,
    environment: Mapping[str, str] | None = None,
) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        input=input_text,
        env=dict(environment) if environment is not None else None,
    )
    return completed.stdout


def _commit_protocol(repository: Path) -> None:
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
    (repository / "experiment.py").write_text("print('frozen')\n", encoding="utf-8")
    _git(repository, "add", "experiment.py")
    _git(
        repository,
        "-c",
        "user.name=Tests",
        "-c",
        "user.email=tests@example.invalid",
        "commit",
        "--no-gpg-sign",
        "-m",
        "experiment source",
    )


def _spec(repository: Path, run_spec_payload) -> RunSpec:
    payload = run_spec_payload(
        source_commit=_git(repository, "rev-parse", "HEAD").strip(),
        source_tree=_git(repository, "rev-parse", "HEAD^{tree}").strip(),
        requested_host="host-a",
    )
    return RunSpec.model_validate(payload)


def _tree_with_files(
    repository: Path,
    tmp_path: Path,
    *,
    base: str,
    files: Mapping[str, str],
) -> str:
    index = tmp_path / "evidence-test.index"
    environment = os.environ.copy()
    environment["GIT_INDEX_FILE"] = str(index)
    _git(repository, "read-tree", base, environment=environment)
    try:
        for path, content in files.items():
            blob = _git(
                repository,
                "hash-object",
                "-w",
                "--stdin",
                input_text=content,
            ).strip()
            _git(
                repository,
                "update-index",
                "--add",
                "--cacheinfo",
                "100644",
                blob,
                path,
                environment=environment,
            )
        return _git(repository, "write-tree", environment=environment).strip()
    finally:
        index.unlink(missing_ok=True)


def _commit_tree(repository: Path, *, tree: str, parent: str, message: str) -> str:
    return _git(
        repository,
        "-c",
        "user.name=Research Control Plane",
        "-c",
        "user.email=researchctl@localhost",
        "commit-tree",
        tree,
        "-p",
        parent,
        "-m",
        message,
    ).strip()


@pytest.fixture
def frozen_run(initialized_repository: Path, run_spec_payload):
    _commit_protocol(initialized_repository)
    spec = _spec(initialized_repository, run_spec_payload)
    worktrees = initialized_repository / ".git" / "researchctl" / "worktrees"
    worktrees.mkdir(parents=True, exist_ok=True)
    records = GitRunRecordRepository(
        repository_root=initialized_repository,
        worktrees_directory=worktrees,
        spec=spec,
    )
    frozen = records.freeze()
    return initialized_repository, spec, records, frozen


@pytest.fixture
def collected_run(frozen_run, run_result_payload):
    repository, spec, records, frozen = frozen_run
    result = RunResult.model_validate(
        run_result_payload(
            run_id=spec.run_id,
            run_spec_digest=spec.spec_digest,
        )
    )
    collected = records.collect(result)
    return repository, spec, result, records, frozen, collected


class _RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.environments: list[Mapping[str, str]] = []
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
        self.environments.append(dict(env or {}))
        return self._delegate.run(
            argv,
            cwd=cwd,
            env=env,
            timeout_seconds=timeout_seconds,
        )


def test_reader_uses_git_objects_and_does_not_mutate_local_state(collected_run) -> None:
    repository, spec, result, records, frozen, collected = collected_run
    records.spec_path.write_text("not the committed spec\n", encoding="utf-8")
    records.result_path.write_text("not the committed result\n", encoding="utf-8")
    refs_before = _git(
        repository,
        "for-each-ref",
        "--format=%(refname)%00%(objectname)",
    )
    worktrees_before = _git(repository, "worktree", "list", "--porcelain", "-z")
    default_status_before = _git(repository, "status", "--porcelain=v1", "-z")
    metadata_status_before = _git(
        records.metadata_worktree,
        "status",
        "--porcelain=v1",
        "-z",
    )
    runner = _RecordingRunner()

    evidence = GitRunEvidenceReader(runner=runner).read(repository, spec.run_id)

    assert evidence.spec == spec
    assert evidence.result == result
    assert evidence.spec_commit == frozen.spec_commit
    assert evidence.result_commit == collected.result_commit
    assert _git(
        repository,
        "for-each-ref",
        "--format=%(refname)%00%(objectname)",
    ) == refs_before
    assert _git(repository, "worktree", "list", "--porcelain", "-z") == worktrees_before
    assert _git(repository, "status", "--porcelain=v1", "-z") == default_status_before
    assert (
        _git(records.metadata_worktree, "status", "--porcelain=v1", "-z")
        == metadata_status_before
    )
    assert {call[5] for call in runner.calls} <= {
        "cat-file",
        "diff-tree",
        "ls-tree",
        "rev-parse",
    }
    assert all(env.get("GIT_OPTIONAL_LOCKS") == "0" for env in runner.environments)
    assert all(env.get("GIT_NO_REPLACE_OBJECTS") == "1" for env in runner.environments)


def test_reader_reports_missing_tag_and_uncollected_result(
    initialized_repository: Path,
    frozen_run,
) -> None:
    run_id = "run_20260802T123456Z_999999999999999999999999"
    with pytest.raises(RCPError) as absent:
        GitRunEvidenceReader().read(initialized_repository, run_id)
    assert absent.value.code == "run_spec_not_found"

    repository, spec, _records, _frozen = frozen_run
    with pytest.raises(RCPError) as uncollected:
        GitRunEvidenceReader().read(repository, spec.run_id)
    assert uncollected.value.code == "run_result_not_found"


def test_reader_rejects_wrong_result_marker_and_merge_parent(collected_run) -> None:
    repository, spec, result, _records, frozen, collected = collected_run
    result_tree = _git(repository, "rev-parse", f"{collected.result_commit}^{{tree}}").strip()
    wrong_marker = _commit_tree(
        repository,
        tree=result_tree,
        parent=frozen.spec_commit,
        message="researchctl: collect run forged",
    )
    branch = f"refs/heads/research/run/{spec.run_id}"
    _git(repository, "update-ref", branch, wrong_marker)
    with pytest.raises(RCPError) as marker:
        GitRunEvidenceReader().read(repository, spec.run_id)
    assert marker.value.code == "run_record_commit_invalid"

    merge = _git(
        repository,
        "-c",
        "user.name=Research Control Plane",
        "-c",
        "user.email=researchctl@localhost",
        "commit-tree",
        result_tree,
        "-p",
        frozen.spec_commit,
        "-p",
        spec.source_commit,
        "-m",
        f"researchctl: collect run {spec.run_id} {result.result_id}",
    ).strip()
    _git(repository, "update-ref", branch, merge)
    with pytest.raises(RCPError) as parent:
        GitRunEvidenceReader().read(repository, spec.run_id)
    assert parent.value.code == "run_record_parent_mismatch"


def test_reader_rejects_spec_commit_with_an_extra_path(
    collected_run,
    tmp_path: Path,
) -> None:
    repository, spec, _result, _records, _frozen, _collected = collected_run
    tree = _tree_with_files(
        repository,
        tmp_path,
        base=spec.source_commit,
        files={
            f".research/runs/{spec.run_id}/spec.yaml": dump_yaml(spec),
            ".research/runs/unexpected.txt": "unexpected\n",
        },
    )
    commit = _commit_tree(
        repository,
        tree=tree,
        parent=spec.source_commit,
        message=f"researchctl: freeze run {spec.run_id} {spec.spec_digest}",
    )
    _git(repository, "update-ref", f"refs/tags/research-run/{spec.run_id}", commit)

    with pytest.raises(RCPError) as raised:
        GitRunEvidenceReader().read(repository, spec.run_id)
    assert raised.value.code == "run_record_commit_invalid"


def test_reader_rejects_noncanonical_spec_and_source_tree_mismatch(
    collected_run,
    tmp_path: Path,
) -> None:
    repository, spec, _result, _records, _frozen, _collected = collected_run
    path = f".research/runs/{spec.run_id}/spec.yaml"
    noncanonical_tree = _tree_with_files(
        repository,
        tmp_path,
        base=spec.source_commit,
        files={path: "---\n" + dump_yaml(spec)},
    )
    noncanonical = _commit_tree(
        repository,
        tree=noncanonical_tree,
        parent=spec.source_commit,
        message=f"researchctl: freeze run {spec.run_id} {spec.spec_digest}",
    )
    tag = f"refs/tags/research-run/{spec.run_id}"
    _git(repository, "update-ref", tag, noncanonical)
    with pytest.raises(RCPError) as canonical:
        GitRunEvidenceReader().read(repository, spec.run_id)
    assert canonical.value.code == "run_spec_invalid"

    payload = spec.model_dump(mode="json", exclude={"spec_digest"}, exclude_none=True)
    payload["source_tree"] = "f" * 40
    payload["spec_digest"] = canonical_digest(payload)
    wrong_tree_spec = RunSpec.model_validate(payload)
    wrong_tree = _tree_with_files(
        repository,
        tmp_path,
        base=spec.source_commit,
        files={path: dump_yaml(wrong_tree_spec)},
    )
    wrong_tree_commit = _commit_tree(
        repository,
        tree=wrong_tree,
        parent=spec.source_commit,
        message=(
            f"researchctl: freeze run {spec.run_id} "
            f"{wrong_tree_spec.spec_digest}"
        ),
    )
    _git(repository, "update-ref", tag, wrong_tree_commit)
    with pytest.raises(RCPError) as source:
        GitRunEvidenceReader().read(repository, spec.run_id)
    assert source.value.code == "run_source_mismatch"


@pytest.mark.parametrize(
    ("updates", "expected_code"),
    [
        (
            {"run_id": "run_20260802T123456Z_999999999999999999999999"},
            "run_result_identity_mismatch",
        ),
        ({"run_spec_digest": "sha256:" + "f" * 64}, "run_result_spec_mismatch"),
    ],
)
def test_reader_rejects_result_identity_and_digest_mismatch(
    collected_run,
    tmp_path: Path,
    updates: dict[str, str],
    expected_code: str,
) -> None:
    repository, spec, result, _records, frozen, _collected = collected_run
    payload = result.model_dump(mode="json", exclude_none=True)
    payload.update(updates)
    mismatched = RunResult.model_validate(payload)
    result_path = f".research/runs/{spec.run_id}/result.yaml"
    tree = _tree_with_files(
        repository,
        tmp_path,
        base=frozen.spec_commit,
        files={result_path: dump_yaml(mismatched)},
    )
    commit = _commit_tree(
        repository,
        tree=tree,
        parent=frozen.spec_commit,
        message=f"researchctl: collect run {spec.run_id} {mismatched.result_id}",
    )
    _git(repository, "update-ref", f"refs/heads/research/run/{spec.run_id}", commit)

    with pytest.raises(RCPError) as raised:
        GitRunEvidenceReader().read(repository, spec.run_id)
    assert raised.value.code == expected_code
