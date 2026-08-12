from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from researchctl.domain.enums import RunAttemptState, RunOutcome
from researchctl.domain.models import RunSpec, TaskRecord
from researchctl.errors import RCPError
from researchctl.serialization import canonical_digest, dump_yaml
from researchctl.services.local_run import LocalRunExecutor
from researchctl.services.run_execution import LocalRunCoordinator
from researchctl.services.run_preflight import (
    IdentityObservation,
    LocalRunPreflight,
    StaticIdentityResolver,
)

ATTEMPT_ONE = "attempt_20260803T130000Z_111111111111111111111111"
ATTEMPT_TWO = "attempt_20260803T130001Z_222222222222222222222222"
RETRY_OPERATION = "operation_20260803T130001Z_333333333333333333333333"
WRONG_TASK_ID = "task_20260803T130000Z_444444444444444444444444"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _commit_source(repository: Path, program: str) -> tuple[str, str]:
    (repository / "experiment.py").write_text(program, encoding="utf-8")
    _git(repository, "add", ".researchctl.toml", ".research", "experiment.py")
    _git(
        repository,
        "-c",
        "user.name=Tests",
        "-c",
        "user.email=tests@example.invalid",
        "commit",
        "--no-gpg-sign",
        "-m",
        "frozen experiment source",
    )
    commit = _git(repository, "rev-parse", "HEAD").strip()
    tree = _git(repository, "rev-parse", "HEAD^{tree}").strip()
    return commit, tree


def _spec(
    run_spec_payload,
    *,
    source_commit: str,
    source_tree: str,
    baseline_commit: str | None = None,
) -> RunSpec:
    base = RunSpec.model_validate(run_spec_payload())
    updates = {
        "source_commit": source_commit,
        "source_tree": source_tree,
        "requested_host": "host-a",
        "argv": ("python", "experiment.py"),
        "inputs": (
            {
                "kind": "dataset",
                "logical_id": "validation-split",
                "version": "2026-08-01",
                "waiver_allowed": False,
            },
        ),
        "artifact_declarations": (
            {
                "name": "result",
                "path": "results/MAR-17/result.txt",
                "media_type": "text/plain",
            },
        ),
    }
    if baseline_commit is not None:
        updates["baseline_commit"] = baseline_commit
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


def _task(task_payload) -> TaskRecord:
    return TaskRecord.model_validate(
        task_payload(
            state="ready",
            allowed_write_paths=["results/MAR-17"],
            execution={
                "preferred_hosts": ["host-a"],
                "preferred_pools": ["interactive"],
                "gpu_count": 0,
            },
        )
    )


def _coordinator(repository: Path, worktrees: Path) -> LocalRunCoordinator:
    identities = StaticIdentityResolver(
        (
            IdentityObservation(
                kind="environment",
                logical_id="trainer-cu128",
                digest="sha256:" + "3" * 64,
            ),
            IdentityObservation(
                kind="dataset",
                logical_id="validation-split",
                version="2026-08-01",
            ),
        )
    )
    python_path = str(Path(os.sys.executable).parent)
    return LocalRunCoordinator(
        repository_root=repository,
        worktrees_directory=worktrees,
        default_branch="main",
        preflight=LocalRunPreflight(
            local_host="host-a",
            identities=identities,
            minimum_free_bytes=0,
            path_environment=python_path,
        ),
        executor=LocalRunExecutor(
            local_host="host-a",
            timeout_seconds=5,
            base_environment={"PATH": python_path, "LINEAR_API_KEY": "never-pass"},
        ),
    )


def _fixture(
    initialized_repository: Path,
    run_spec_payload,
    task_payload,
    program: str,
):
    task = _task(task_payload)
    task_path = (
        initialized_repository
        / ".research"
        / "tasks"
        / f"{task.task_id}.yaml"
    )
    task_path.write_text(dump_yaml(task), encoding="utf-8")
    source_commit, source_tree = _commit_source(initialized_repository, program)
    spec = _spec(
        run_spec_payload,
        source_commit=source_commit,
        source_tree=source_tree,
    )
    worktrees = initialized_repository / ".git" / "researchctl" / "worktrees"
    worktrees.mkdir(parents=True, exist_ok=True)
    return spec, task, worktrees, _coordinator(initialized_repository, worktrees)


def _keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {key for item in value.values() for key in _keys(item)}
    if isinstance(value, list):
        return {key for item in value for key in _keys(item)}
    return set()


def test_success_runs_frozen_source_collects_once_and_preserves_default_worktree(
    initialized_repository: Path,
    run_spec_payload,
    task_payload,
) -> None:
    program = """
from pathlib import Path
output = Path("results/MAR-17/result.txt")
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text("frozen-source", encoding="utf-8")
print("coordinator-finished")
""".lstrip()
    spec, task, worktrees, coordinator = _fixture(
        initialized_repository,
        run_spec_payload,
        task_payload,
        program,
    )
    (initialized_repository / "experiment.py").write_text(
        program.replace("frozen-source", "dirty-default-source"),
        encoding="utf-8",
    )
    default_head = _git(initialized_repository, "rev-parse", "HEAD").strip()
    default_status = _git(initialized_repository, "status", "--porcelain=v1", "-z")
    events = []

    first = coordinator.execute(
        spec=spec,
        task=task,
        attempt_id=ATTEMPT_ONE,
        operation_id=spec.operation_id,
        assigned_gpu_uuids=(),
        event_callback=events.append,
    )
    repeated = coordinator.execute(
        spec=spec,
        task=task,
        attempt_id=ATTEMPT_ONE,
        operation_id=spec.operation_id,
        assigned_gpu_uuids=(),
        event_callback=lambda event: pytest.fail(f"replayed event {event.state}"),
    )

    execution_output = first.frozen.execution_worktree / "results/MAR-17/result.txt"
    assert execution_output.read_text(encoding="utf-8") == "frozen-source"
    assert first.terminal_result == "collected"
    assert first.collection is not None and first.collection.changed is True
    assert first.process_launched is True
    assert first.observed_existing is False
    assert first.stdout_tail_present is True
    assert first.attempt.events[-1].state == RunAttemptState.SUCCEEDED
    assert repeated.terminal_result == "collected"
    assert repeated.collection is not None and repeated.collection.changed is False
    assert repeated.collection.result_commit == first.collection.result_commit
    assert repeated.process_launched is False
    assert repeated.observed_existing is True
    assert repeated.marker_path == first.marker_path
    assert len(events) == 8
    assert _git(
        initialized_repository,
        "rev-list",
        "--count",
        f"{spec.source_commit}..{first.frozen.branch}",
    ).strip() == "2"
    assert _git(initialized_repository, "rev-parse", "HEAD").strip() == default_head
    assert _git(initialized_repository, "status", "--porcelain=v1", "-z") == default_status
    serialized = first.as_dict()
    assert serialized["run_id"] == spec.run_id
    assert serialized["attempt_id"] == ATTEMPT_ONE
    assert serialized["terminal_result"] == "collected"
    assert "stdout_tail" not in _keys(serialized)
    assert "stderr_tail" not in _keys(serialized)
    json.dumps(serialized, sort_keys=True)
    del worktrees


def test_failed_attempt_is_not_collected_and_new_attempt_can_retry(
    initialized_repository: Path,
    run_spec_payload,
    task_payload,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    program = """
from pathlib import Path
import sys
directory = Path("results/MAR-17")
directory.mkdir(parents=True, exist_ok=True)
sentinel = directory / "first-attempt-failed"
if not sentinel.exists():
    sentinel.write_text("seen", encoding="utf-8")
    print("intentional first failure", file=sys.stderr)
    raise SystemExit(9)
(directory / "result.txt").write_text("retry-success", encoding="utf-8")
""".lstrip()
    spec, task, worktrees, coordinator = _fixture(
        initialized_repository,
        run_spec_payload,
        task_payload,
        program,
    )
    scope_calls: list[dict[str, object]] = []
    validate_source = coordinator.write_scope.validate_source

    def observe_scope(**values):
        scope_calls.append(values)
        return validate_source(**values)

    monkeypatch.setattr(coordinator.write_scope, "validate_source", observe_scope)

    failed = coordinator.execute(
        spec=spec,
        task=task,
        attempt_id=ATTEMPT_ONE,
        operation_id=spec.operation_id,
        assigned_gpu_uuids=(),
        event_callback=lambda event: None,
    )
    result_path = (
        failed.frozen.metadata_worktree
        / ".research/runs"
        / spec.run_id
        / "result.yaml"
    )

    assert failed.terminal_result == "attempt_failed"
    assert failed.collection is None
    assert failed.result.outcome == RunOutcome.FAILED
    assert failed.result.exit_code == 9
    assert failed.attempt.events[-1].state == RunAttemptState.FAILED
    assert not result_path.exists()
    assert _git(
        initialized_repository,
        "rev-list",
        "--count",
        f"{spec.source_commit}..{failed.frozen.branch}",
    ).strip() == "1"

    retried = coordinator.execute(
        spec=spec,
        task=task,
        attempt_id=ATTEMPT_TWO,
        operation_id=RETRY_OPERATION,
        assigned_gpu_uuids=(),
        event_callback=lambda event: None,
        retry_of=ATTEMPT_ONE,
    )

    assert retried.terminal_result == "collected"
    assert retried.collection is not None
    assert retried.result.outcome == RunOutcome.COMPLETE
    assert retried.attempt.retry_of == ATTEMPT_ONE
    assert retried.attempt.operation_id == RETRY_OPERATION
    assert result_path.is_file()
    assert (
        retried.frozen.execution_worktree / "results/MAR-17/result.txt"
    ).read_text(encoding="utf-8") == "retry-success"
    assert _git(
        initialized_repository,
        "rev-list",
        "--count",
        f"{spec.source_commit}..{retried.frozen.branch}",
    ).strip() == "2"
    assert len(scope_calls) == 2
    assert all(call["task"] == task for call in scope_calls)
    assert all(call["source_commit"] == spec.source_commit for call in scope_calls)
    assert all(call["baseline_commit"] is None for call in scope_calls)
    assert all(
        call["trusted_base_commit"] == spec.source_commit for call in scope_calls
    )
    del worktrees


def test_out_of_scope_run_source_fails_before_git_or_process_side_effects(
    initialized_repository: Path,
    run_spec_payload,
    task_payload,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_spec, task, worktrees, coordinator = _fixture(
        initialized_repository,
        run_spec_payload,
        task_payload,
        "raise AssertionError('out-of-scope source launched')\n",
    )
    baseline = original_spec.source_commit
    hostile_task = task.model_copy(
        update={"allowed_write_paths": (*task.allowed_write_paths, "README.md")}
    )
    task_path = (
        initialized_repository
        / ".research"
        / "tasks"
        / f"{task.task_id}.yaml"
    )
    task_path.write_text(dump_yaml(hostile_task), encoding="utf-8")
    (initialized_repository / "README.md").write_text(
        "out-of-scope candidate\n",
        encoding="utf-8",
    )
    _git(initialized_repository, "add", "README.md")
    source_commit, source_tree = _commit_source(
        initialized_repository,
        "raise AssertionError('out-of-scope source launched')\n",
    )
    _git(
        initialized_repository,
        "update-ref",
        "refs/heads/main",
        baseline,
        source_commit,
    )
    spec = _spec(
        run_spec_payload,
        source_commit=source_commit,
        source_tree=source_tree,
        baseline_commit=baseline,
    )
    refs_before = _git(
        initialized_repository,
        "for-each-ref",
        "--format=%(refname) %(objectname)",
    )
    worktrees_before = tuple(sorted(path.name for path in worktrees.iterdir()))
    events: list[object] = []

    monkeypatch.setattr(
        coordinator.executor,
        "execute",
        lambda **values: pytest.fail(f"executor called: {sorted(values)}"),
    )
    with pytest.raises(RCPError) as caught:
        coordinator.execute(
            spec=spec,
            task=hostile_task,
            attempt_id=ATTEMPT_ONE,
            operation_id=spec.operation_id,
            assigned_gpu_uuids=(),
            event_callback=events.append,
        )

    assert caught.value.code == "write_scope_violation"
    assert caught.value.context["allowed_write_paths"] == ["results/MAR-17"]
    assert caught.value.context["violations"] == [
        {
            "path": f".research/tasks/{task.task_id}.yaml",
            "reason": "outside_allowed_write_paths",
            "status": "M",
        },
        {
            "path": "README.md",
            "reason": "outside_allowed_write_paths",
            "status": "M",
        }
    ]
    assert events == []
    assert _git(
        initialized_repository,
        "for-each-ref",
        "--format=%(refname) %(objectname)",
    ) == refs_before
    assert tuple(sorted(path.name for path in worktrees.iterdir())) == worktrees_before
    assert not (worktrees / f"run-{spec.run_id}").exists()
    assert not (worktrees / f"run-exec-{spec.run_id}").exists()


def test_collect_without_run_identity_has_zero_side_effects(
    initialized_repository: Path,
    run_spec_payload,
    task_payload,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec, task, worktrees, coordinator = _fixture(
        initialized_repository,
        run_spec_payload,
        task_payload,
        "raise AssertionError('unstarted Run launched')\n",
    )
    refs_before = _git(
        initialized_repository,
        "for-each-ref",
        "--format=%(refname) %(objectname)",
    )
    worktrees_before = tuple(sorted(path.name for path in worktrees.iterdir()))
    monkeypatch.setattr(
        coordinator.executor,
        "marker_path_for",
        lambda *args: pytest.fail(f"marker lookup reached: {args}"),
    )

    with pytest.raises(RCPError) as caught:
        coordinator.collect(
            spec=spec,
            task=task,
            attempt_id=ATTEMPT_ONE,
            operation_id=RETRY_OPERATION,
        )

    assert caught.value.code == "run_not_started"
    assert caught.value.context == {
        "run_id": spec.run_id,
        "branch_ref": f"refs/heads/research/run/{spec.run_id}",
        "tag_ref": f"refs/tags/research-run/{spec.run_id}",
    }
    assert _git(
        initialized_repository,
        "for-each-ref",
        "--format=%(refname) %(objectname)",
    ) == refs_before
    assert tuple(sorted(path.name for path in worktrees.iterdir())) == worktrees_before
    assert not (worktrees / f"run-{spec.run_id}").exists()
    assert not (worktrees / f"run-exec-{spec.run_id}").exists()


def test_explicit_collect_finalizes_failed_evidence_without_relaunch(
    initialized_repository: Path,
    run_spec_payload,
    task_payload,
) -> None:
    spec, task, worktrees, coordinator = _fixture(
        initialized_repository,
        run_spec_payload,
        task_payload,
        "raise SystemExit(17)\n",
    )
    default_head = _git(initialized_repository, "rev-parse", "HEAD").strip()
    default_status = _git(initialized_repository, "status", "--porcelain=v1", "-z")
    failed = coordinator.execute(
        spec=spec,
        task=task,
        attempt_id=ATTEMPT_ONE,
        operation_id=spec.operation_id,
        assigned_gpu_uuids=(),
        event_callback=lambda event: None,
    )
    collection_operation = RETRY_OPERATION

    first = coordinator.collect(
        spec=spec,
        task=task,
        attempt_id=ATTEMPT_ONE,
        operation_id=collection_operation,
    )
    repeated = coordinator.collect(
        spec=spec,
        task=task,
        attempt_id=ATTEMPT_ONE,
        operation_id=collection_operation,
    )

    assert failed.terminal_result == "attempt_failed"
    assert first.terminal_result == "collected"
    assert first.result.outcome == RunOutcome.FAILED
    assert first.result.exit_code == 17
    assert first.collection.changed is True
    assert repeated.terminal_result == "already_collected"
    assert repeated.collection.changed is False
    assert repeated.collection.result_commit == first.collection.result_commit
    assert first.as_dict()["execution"]["process_launched"] is False
    assert _git(
        initialized_repository,
        "rev-list",
        "--count",
        f"{spec.source_commit}..{first.frozen.branch}",
    ).strip() == "2"
    assert _git(initialized_repository, "rev-parse", "HEAD").strip() == default_head
    assert _git(initialized_repository, "status", "--porcelain=v1", "-z") == default_status
    del worktrees


def test_identity_errors_precede_process_launch_and_missing_retry_is_rejected(
    initialized_repository: Path,
    run_spec_payload,
    task_payload,
) -> None:
    spec, task, worktrees, coordinator = _fixture(
        initialized_repository,
        run_spec_payload,
        task_payload,
        "raise SystemExit(0)\n",
    )

    with pytest.raises(RCPError) as operation:
        coordinator.execute(
            spec=spec,
            task=task,
            attempt_id=ATTEMPT_ONE,
            operation_id=RETRY_OPERATION,
            assigned_gpu_uuids=(),
            event_callback=lambda event: None,
        )
    assert operation.value.code == "run_operation_mismatch"
    assert not (worktrees / f"run-exec-{spec.run_id}").exists()

    wrong_task = task.model_copy(update={"task_id": WRONG_TASK_ID})
    with pytest.raises(RCPError) as task_error:
        coordinator.execute(
            spec=spec,
            task=wrong_task,
            attempt_id=ATTEMPT_ONE,
            operation_id=spec.operation_id,
            assigned_gpu_uuids=(),
            event_callback=lambda event: None,
        )
    assert task_error.value.code == "run_task_mismatch"

    with pytest.raises(RCPError) as self_retry:
        coordinator.execute(
            spec=spec,
            task=task,
            attempt_id=ATTEMPT_ONE,
            operation_id=RETRY_OPERATION,
            assigned_gpu_uuids=(),
            event_callback=lambda event: None,
            retry_of=ATTEMPT_ONE,
        )
    assert self_retry.value.code == "run_retry_identity_invalid"

    with pytest.raises(RCPError) as reused_operation:
        coordinator.execute(
            spec=spec,
            task=task,
            attempt_id=ATTEMPT_TWO,
            operation_id=spec.operation_id,
            assigned_gpu_uuids=(),
            event_callback=lambda event: None,
            retry_of=ATTEMPT_ONE,
        )
    assert reused_operation.value.code == "run_retry_identity_invalid"
    assert not (worktrees / f"run-exec-{spec.run_id}").exists()

    with pytest.raises(RCPError) as missing:
        coordinator.execute(
            spec=spec,
            task=task,
            attempt_id=ATTEMPT_TWO,
            operation_id=RETRY_OPERATION,
            assigned_gpu_uuids=(),
            event_callback=lambda event: None,
            retry_of=ATTEMPT_ONE,
        )
    assert missing.value.code == "run_retry_origin_not_found"
    assert not coordinator.executor.marker_path_for(
        worktrees / f"run-exec-{spec.run_id}",
        ATTEMPT_TWO,
    ).exists()


def test_executor_uncertainty_propagates_without_collection(
    initialized_repository: Path,
    run_spec_payload,
    task_payload,
) -> None:
    spec, task, worktrees, coordinator = _fixture(
        initialized_repository,
        run_spec_payload,
        task_payload,
        "raise SystemExit(0)\n",
    )
    original = coordinator.executor.execute

    def uncertain(**kwargs):
        del kwargs
        raise RCPError(
            code="run_execution_uncertain",
            message="external process ownership is uncertain",
            context={"phase": "running"},
        )

    coordinator.executor.execute = uncertain  # type: ignore[method-assign]
    try:
        with pytest.raises(RCPError) as caught:
            coordinator.execute(
                spec=spec,
                task=task,
                attempt_id=ATTEMPT_ONE,
                operation_id=spec.operation_id,
                assigned_gpu_uuids=(),
                event_callback=lambda event: None,
            )
    finally:
        coordinator.executor.execute = original  # type: ignore[method-assign]

    assert caught.value.code == "run_execution_uncertain"
    assert caught.value.context == {"phase": "running"}
    result_path = (
        worktrees
        / f"run-{spec.run_id}"
        / ".research/runs"
        / spec.run_id
        / "result.yaml"
    )
    assert not result_path.exists()
