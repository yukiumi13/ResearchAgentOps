from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

from pydantic import TypeAdapter

from researchctl.adapters._subprocess import CommandResult, SubprocessCommandRunner
from researchctl.adapters.reconcile import LocalReconcileObserver
from researchctl.domain.enums import RunAttemptState
from researchctl.domain.models import (
    RunAttempt,
    RunAttemptEvent,
    RunResult,
    RunSpec,
)
from researchctl.serialization import canonical_digest, canonical_json_bytes
from researchctl.services.reconcile import (
    LocalReconcileService,
    ReconcileClassification,
    ReconcileOutcome,
    RunReconcileState,
)
from researchctl.services.run_records import GitRunRecordRepository

NOW = datetime(2026, 8, 3, 15, 0, 0, tzinfo=UTC)
PROJECT_ID = "project_20260803T150000Z_" + "a" * 24
ATTEMPT_ID = "attempt_20260803T150000Z_" + "b" * 24


class _NoTmuxRunner:
    def __init__(self) -> None:
        self.delegate = SubprocessCommandRunner()

    def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path | None,
        env: Mapping[str, str] | None,
        timeout_seconds: float,
    ) -> CommandResult:
        if argv[0] == "tmux":
            return CommandResult(1, stderr="no server running on test socket")
        return self.delegate.run(
            argv,
            cwd=cwd,
            env=env,
            timeout_seconds=timeout_seconds,
        )


class _EmptyRuntime:
    def list_sessions(self, project_id: str | None = None) -> tuple[object, ...]:
        assert project_id == PROJECT_ID
        return ()


class _FailRunBlobRunner(_NoTmuxRunner):
    def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path | None,
        env: Mapping[str, str] | None,
        timeout_seconds: float,
    ) -> CommandResult:
        if argv[0] == "git" and argv[5] == "show":
            return CommandResult(2, stderr="record read failed")
        return super().run(
            argv,
            cwd=cwd,
            env=env,
            timeout_seconds=timeout_seconds,
        )


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _prepare(
    repository: Path,
    run_spec_payload: Callable[..., dict[str, object]],
) -> tuple[RunSpec, GitRunRecordRepository, Path]:
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
        "managed protocol",
    )
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
    spec = RunSpec.model_validate(payload)
    worktrees = repository / ".git" / "researchctl" / "worktrees"
    worktrees.mkdir(parents=True)
    records = GitRunRecordRepository(
        repository_root=repository,
        worktrees_directory=worktrees,
        spec=spec,
    )
    records.freeze()
    return spec, records, worktrees


def _result(
    spec: RunSpec,
    run_result_payload: Callable[..., dict[str, object]],
) -> RunResult:
    return RunResult.model_validate(
        run_result_payload(
            run_id=spec.run_id,
            run_spec_digest=spec.spec_digest,
            attempt_ids=[ATTEMPT_ID],
        )
    )


def _terminal_marker(spec: RunSpec, result: RunResult) -> bytes:
    event = RunAttemptEvent(
        operation_id=spec.operation_id,
        sequence=0,
        state=RunAttemptState.SUCCEEDED,
        observed_at=NOW,
        idempotency_key=f"local-run:{ATTEMPT_ID}:0:succeeded",
        host="host-a",
    )
    attempt = RunAttempt(
        attempt_id=ATTEMPT_ID,
        run_id=spec.run_id,
        operation_id=spec.operation_id,
        events=(event,),
    )
    return canonical_json_bytes(
        {
            "marker_version": 1,
            "run_id": spec.run_id,
            "spec_digest": spec.spec_digest,
            "attempt_id": ATTEMPT_ID,
            "operation_id": spec.operation_id,
            "host": "host-a",
            "phase": "terminal",
            "attempt": attempt.model_dump(mode="json", exclude_none=True),
            "result": result.model_dump(mode="json", exclude_none=True),
            "observation": {"kind": "exited", "exit_code": 0},
            "stdout_tail": "",
            "stderr_tail": "",
            "stdout_truncated": False,
            "stderr_truncated": False,
            "updated_at": "2026-08-03T15:00:00.000000Z",
        }
    )


def _claimed_marker(spec: RunSpec) -> bytes:
    return canonical_json_bytes(
        {
            "marker_version": 1,
            "run_id": spec.run_id,
            "spec_digest": spec.spec_digest,
            "attempt_id": ATTEMPT_ID,
            "operation_id": spec.operation_id,
            "host": "host-a",
            "phase": "claimed",
            "created_at": "2026-08-03T15:00:00.000000Z",
        }
    )


def _write_marker(worktrees: Path, content: bytes, *, name: str | None = None) -> Path:
    directory = worktrees / ".researchctl-run-markers"
    directory.mkdir(mode=0o700)
    path = directory / (name or f"{ATTEMPT_ID}.json")
    path.write_bytes(content)
    path.chmod(0o600)
    return path


def _plan(
    repository: Path,
    worktrees: Path,
    *,
    runner: _NoTmuxRunner | None = None,
):
    observer = LocalReconcileObserver(
        repository,
        runner=runner or _NoTmuxRunner(),  # type: ignore[arg-type]
        run_marker_directory=worktrees / ".researchctl-run-markers",
    )
    return LocalReconcileService(
        project_id=PROJECT_ID,
        local_host="host-a",
        worktrees_directory=worktrees,
        observer=observer,
        runtime=_EmptyRuntime(),  # type: ignore[arg-type]
        clock=lambda: NOW,
    ).plan()


def test_coherent_collected_run_is_clean_and_reconcile_is_read_only(
    initialized_repository: Path,
    run_spec_payload,
    run_result_payload,
) -> None:
    spec, records, worktrees = _prepare(initialized_repository, run_spec_payload)
    result = _result(spec, run_result_payload)
    records.collect(result)
    marker = _write_marker(worktrees, _terminal_marker(spec, result))
    before_refs = _git(initialized_repository, "show-ref")
    before_status = _git(initialized_repository, "status", "--porcelain=v1", "-z")
    before_marker = marker.read_bytes()

    plan = _plan(initialized_repository, worktrees)

    assert len(plan.run_items) == 1
    item = plan.run_items[0]
    assert item.run_id == spec.run_id
    assert item.classification is ReconcileClassification.CLEAN
    assert item.state is RunReconcileState.COLLECTED
    assert item.result_id == result.result_id
    assert item.proposed_actions == ()
    assert item.markers[0].terminal is True
    assert item.markers[0].valid is True
    assert _git(initialized_repository, "show-ref") == before_refs
    assert _git(initialized_repository, "status", "--porcelain=v1", "-z") == before_status
    assert marker.read_bytes() == before_marker


def test_terminal_marker_without_result_is_explicit_collect_candidate(
    initialized_repository: Path,
    run_spec_payload,
    run_result_payload,
) -> None:
    spec, records, worktrees = _prepare(initialized_repository, run_spec_payload)
    result = _result(spec, run_result_payload)
    _write_marker(worktrees, _terminal_marker(spec, result))
    assert not records.result_path.exists()

    item = _plan(initialized_repository, worktrees).run_items[0]

    assert item.state is RunReconcileState.COLLECT_CANDIDATE
    assert item.classification is ReconcileClassification.RECOVERABLE
    assert item.result_id is None
    assert item.proposed_actions == ("collect_terminal_attempt_explicitly",)
    assert "terminal_marker_without_run_result" in item.reasons
    assert not records.result_path.exists()


def test_claimed_marker_requires_manual_observation_and_never_blind_retry(
    initialized_repository: Path,
    run_spec_payload,
) -> None:
    spec, _records, worktrees = _prepare(initialized_repository, run_spec_payload)
    _write_marker(worktrees, _claimed_marker(spec))

    item = _plan(initialized_repository, worktrees).run_items[0]

    assert item.state is RunReconcileState.EXECUTION_UNCERTAIN
    assert item.classification is ReconcileClassification.UNCERTAIN
    assert item.markers[0].phase == "claimed"
    assert item.proposed_actions == (
        "manual_observation_required",
        "do_not_retry_attempt_blindly",
    )


def test_run_result_without_local_marker_is_inconsistent(
    initialized_repository: Path,
    run_spec_payload,
    run_result_payload,
) -> None:
    spec, records, worktrees = _prepare(initialized_repository, run_spec_payload)
    records.collect(_result(spec, run_result_payload))
    marker_directory = worktrees / ".researchctl-run-markers"
    assert not marker_directory.exists()

    item = _plan(initialized_repository, worktrees).run_items[0]

    assert item.state is RunReconcileState.INCONSISTENT
    assert item.classification is ReconcileClassification.UNCERTAIN
    assert "run_result_without_matching_terminal_marker" in item.reasons
    assert item.proposed_actions == ("manual_observation_required",)
    assert not marker_directory.exists()


def test_incomplete_run_record_read_is_partial_observation_not_inconsistency(
    initialized_repository: Path,
    run_spec_payload,
) -> None:
    spec, _records, worktrees = _prepare(initialized_repository, run_spec_payload)

    plan = _plan(
        initialized_repository,
        worktrees,
        runner=_FailRunBlobRunner(),
    )

    assert plan.outcome is ReconcileOutcome.PARTIAL_OBSERVATION
    assert plan.observation_failures[0].component == "run_records"
    item = next(item for item in plan.run_items if item.run_id == spec.run_id)
    assert item.state is RunReconcileState.EXECUTION_UNCERTAIN
    assert "observation_incomplete:run_records" in item.reasons


def test_malformed_ref_record_and_marker_are_visible_inconsistent_items(
    initialized_repository: Path,
    run_spec_payload,
) -> None:
    spec, records, worktrees = _prepare(initialized_repository, run_spec_payload)
    records.spec_path.write_text("not: a RunSpec\n", encoding="utf-8")
    _git(records.metadata_worktree, "add", "--", records.spec_path.as_posix())
    _git(
        records.metadata_worktree,
        "-c",
        "user.name=Tests",
        "-c",
        "user.email=tests@example.invalid",
        "commit",
        "--no-gpg-sign",
        "-m",
        "malformed run record fixture",
    )
    _git(initialized_repository, "branch", "research/run/not-canonical", "HEAD")
    _write_marker(worktrees, b"{not-json", name=f"{ATTEMPT_ID}.json")

    plan = _plan(initialized_repository, worktrees)

    canonical = next(item for item in plan.run_items if item.run_id == spec.run_id)
    assert canonical.state is RunReconcileState.INCONSISTENT
    assert "run_spec_malformed_on_branch" in canonical.reasons
    assert "run_branch_advanced_without_result" in canonical.reasons
    standalone_reasons = {
        reason
        for item in plan.run_items
        if item.run_id is None
        for reason in item.reasons
    }
    assert "run_ref_identity_invalid" in standalone_reasons
    assert "run_marker_malformed" in standalone_reasons
    assert all(
        item.classification is ReconcileClassification.UNCERTAIN
        for item in plan.run_items
        if item.run_id is None
    )
