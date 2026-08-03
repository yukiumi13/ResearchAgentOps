from __future__ import annotations

import hashlib
import os
import signal
import sys
import time
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from researchctl.domain.enums import (
    ArtifactVerification,
    FailureClass,
    RunAttemptState,
    RunOutcome,
)
from researchctl.domain.models import RunAttemptEvent, RunSpec
from researchctl.errors import RCPError
from researchctl.serialization import canonical_digest
from researchctl.services.local_run import LocalRunExecutor
from researchctl.services.run_preflight import (
    IdentityObservation,
    RunPreflightReceipt,
)


ATTEMPT_ID = "attempt_20260803T120000Z_aaaaaaaaaaaaaaaaaaaaaaaa"
OPERATION_ID = "operation_20260803T120000Z_bbbbbbbbbbbbbbbbbbbbbbbb"


def _spec(run_spec_payload, *, argv: tuple[str, ...], artifacts=(), **overrides) -> RunSpec:
    base = RunSpec.model_validate(run_spec_payload())
    values = {
        "argv": list(argv),
        "requested_host": "host-a",
        "artifact_declarations": list(artifacts),
    }
    values.update(overrides)
    normalized = {
        key: TypeAdapter(RunSpec.model_fields[key].annotation).validate_python(value)
        for key, value in values.items()
    }
    draft = base.model_copy(update=normalized)
    payload = draft.model_dump(
        mode="json",
        exclude={"spec_digest"},
        exclude_none=True,
    )
    payload["spec_digest"] = canonical_digest(payload)
    return RunSpec.model_validate(payload)


def _receipt(
    spec: RunSpec,
    *,
    executable: str | None = None,
) -> RunPreflightReceipt:
    environment = IdentityObservation(
        kind=spec.environment.kind.value,
        logical_id=spec.environment.logical_id,
        version=spec.environment.version,
        digest=spec.environment.digest,
        uri=spec.environment.uri,
    )
    material = {
        "run_id": spec.run_id,
        "spec_digest": spec.spec_digest,
        "host": "host-a",
        "working_directory": spec.working_directory,
        "executable": executable or str(Path(spec.argv[0]).resolve()),
        "identities": [environment.as_dict()],
        "gpu_uuids": [],
        "gpu_inventory_observed_at": [],
        "artifact_paths": [item.path for item in spec.artifact_declarations],
        "free_bytes": 1_000_000,
        "allocation_backend": "local_static",
        "global_exclusivity": False,
    }
    return RunPreflightReceipt(
        run_id=spec.run_id,
        spec_digest=spec.spec_digest,
        host="host-a",
        working_directory=spec.working_directory,
        executable=material["executable"],
        identities=(environment,),
        gpu_uuids=(),
        gpu_inventory_observed_at=(),
        artifact_paths=tuple(material["artifact_paths"]),
        free_bytes=material["free_bytes"],
        allocation_backend="local_static",
        global_exclusivity=False,
        receipt_digest=canonical_digest(material),
    )


def _execute(
    executor: LocalRunExecutor,
    *,
    spec: RunSpec,
    worktree: Path,
    events: list[RunAttemptEvent] | None = None,
):
    observed = events if events is not None else []
    return executor.execute(
        spec=spec,
        execution_worktree=worktree,
        preflight=_receipt(spec),
        attempt_id=ATTEMPT_ID,
        operation_id=OPERATION_ID,
        event_callback=observed.append,
    )


def test_real_process_emits_journal_before_launch_and_collects_digest(
    tmp_path: Path,
    run_spec_payload,
) -> None:
    worktree = tmp_path / "frozen"
    worktree.mkdir()
    artifact = worktree / "results" / "metrics.bin"
    payload = b"metric:\x00value\n"
    source = (
        "from pathlib import Path; "
        "p=Path('results/metrics.bin'); p.parent.mkdir(); "
        f"p.write_bytes({payload!r}); print('finished')"
    )
    spec = _spec(
        run_spec_payload,
        argv=(sys.executable, "-c", source),
        artifacts=(
            {
                "name": "metrics",
                "path": "results/metrics.bin",
                "media_type": "application/octet-stream",
            },
        ),
    )
    events: list[RunAttemptEvent] = []

    def journal(event: RunAttemptEvent) -> None:
        if event.state == RunAttemptState.LAUNCHING:
            assert not artifact.exists()
        events.append(event)

    result = LocalRunExecutor(local_host="host-a").execute(
        spec=spec,
        execution_worktree=worktree,
        preflight=_receipt(spec),
        attempt_id=ATTEMPT_ID,
        operation_id=OPERATION_ID,
        event_callback=journal,
    )

    assert result.result.outcome == RunOutcome.COMPLETE
    assert result.result.exit_code == 0
    assert result.result.started_at is not None
    assert result.stdout_tail == "finished\n"
    assert [event.state for event in events] == [
        RunAttemptState.PREPARING,
        RunAttemptState.SNAPSHOTTED,
        RunAttemptState.PREFLIGHTED,
        RunAttemptState.ALLOCATED,
        RunAttemptState.LAUNCHING,
        RunAttemptState.RUNNING,
        RunAttemptState.COLLECTING,
        RunAttemptState.SUCCEEDED,
    ]
    assert [event.sequence for event in result.attempt.events] == list(range(8))
    assert len(result.result.artifacts) == 1
    collected = result.result.artifacts[0]
    assert collected.digest == "sha256:" + hashlib.sha256(payload).hexdigest()
    assert collected.size_bytes == len(payload)
    assert collected.media_type == "application/octet-stream"
    assert collected.verification == ArtifactVerification.PRODUCER_VERIFIED
    assert result.marker_path.parent == worktree.parent / ".researchctl-run-markers"
    assert not (worktree / ".researchctl-run-markers").exists()


def test_nonzero_exit_keeps_bounded_logs_and_successfully_collected_evidence(
    tmp_path: Path,
    run_spec_payload,
) -> None:
    worktree = tmp_path / "frozen"
    worktree.mkdir()
    source = (
        "from pathlib import Path; import sys; "
        "Path('partial.txt').write_text('partial', encoding='utf-8'); "
        "sys.stdout.write('A'*5000); sys.stderr.write('B'*5000); sys.exit(7)"
    )
    spec = _spec(
        run_spec_payload,
        argv=(sys.executable, "-c", source),
        artifacts=(
            {
                "name": "partial",
                "path": "partial.txt",
                "media_type": "text/plain",
            },
        ),
    )

    execution = _execute(
        LocalRunExecutor(local_host="host-a", max_tail_bytes=64),
        spec=spec,
        worktree=worktree,
    )

    assert execution.observation.kind == "exited"
    assert execution.observation.error_code == "run_process_nonzero_exit"
    assert execution.result.outcome == RunOutcome.FAILED
    assert execution.result.failure_class == FailureClass.COMMAND
    assert execution.result.exit_code == 7
    assert execution.attempt.events[-1].state == RunAttemptState.FAILED
    assert execution.result.artifacts[0].digest == (
        "sha256:" + hashlib.sha256(b"partial").hexdigest()
    )
    assert execution.stdout_tail == "A" * 64
    assert execution.stderr_tail == "B" * 64
    assert execution.stdout_truncated is True
    assert execution.stderr_truncated is True
    assert execution.result.log_summary is not None
    assert len(execution.result.log_summary) <= 4096


def test_timeout_terminates_process_group_and_records_typed_failure(
    tmp_path: Path,
    run_spec_payload,
) -> None:
    worktree = tmp_path / "frozen"
    worktree.mkdir()
    spec = _spec(
        run_spec_payload,
        argv=(
            sys.executable,
            "-c",
            "import time; print('started', flush=True); time.sleep(30)",
        ),
    )
    executor = LocalRunExecutor(
        local_host="host-a",
        timeout_seconds=0.05,
        terminate_grace_seconds=0.05,
    )

    started = time.monotonic()
    execution = _execute(executor, spec=spec, worktree=worktree)

    assert time.monotonic() - started < 3
    assert execution.observation.kind == "timed_out"
    assert execution.observation.error_code == "run_process_timeout"
    assert execution.result.outcome == RunOutcome.FAILED
    assert execution.result.exit_code is None
    assert execution.result.failure_class == FailureClass.COMMAND
    assert execution.attempt.events[-1].state == RunAttemptState.FAILED


def test_signal_is_distinct_from_nonzero_exit(
    tmp_path: Path,
    run_spec_payload,
) -> None:
    worktree = tmp_path / "frozen"
    worktree.mkdir()
    spec = _spec(
        run_spec_payload,
        argv=(
            sys.executable,
            "-c",
            "import os, signal; os.kill(os.getpid(), signal.SIGTERM)",
        ),
    )

    execution = _execute(
        LocalRunExecutor(local_host="host-a"),
        spec=spec,
        worktree=worktree,
    )

    assert execution.observation.kind == "signaled"
    assert execution.observation.signal_number == signal.SIGTERM
    assert execution.observation.error_code == "run_process_signaled"
    assert execution.result.exit_code == -signal.SIGTERM


@pytest.mark.parametrize("artifact_kind", ["missing", "symlink"])
def test_required_missing_or_symlink_artifact_fails_without_discarding_result(
    tmp_path: Path,
    run_spec_payload,
    artifact_kind: str,
) -> None:
    worktree = tmp_path / "frozen"
    worktree.mkdir()
    output = worktree / "result.bin"
    source = "pass"
    expected_code = "run_artifact_missing"
    if artifact_kind == "symlink":
        outside = tmp_path / "outside.bin"
        outside.write_bytes(b"outside")
        source = f"import os; os.symlink({str(outside)!r}, 'result.bin')"
        expected_code = "run_artifact_invalid"
    spec = _spec(
        run_spec_payload,
        argv=(sys.executable, "-c", source),
        artifacts=(
            {
                "name": "result",
                "path": "result.bin",
                "media_type": "application/octet-stream",
            },
        ),
    )

    execution = _execute(
        LocalRunExecutor(local_host="host-a"),
        spec=spec,
        worktree=worktree,
    )

    assert execution.observation.kind == "artifact_failed"
    assert execution.observation.error_code == expected_code
    assert execution.result.outcome == RunOutcome.FAILED
    assert execution.result.exit_code == 0
    assert execution.result.artifacts == ()
    assert execution.result.log_summary is not None
    if artifact_kind == "symlink":
        assert output.is_symlink()


def test_environment_is_allowlisted_and_secret_names_are_denied(
    tmp_path: Path,
    run_spec_payload,
) -> None:
    worktree = tmp_path / "frozen"
    worktree.mkdir()
    env_executable = Path("/usr/bin/env")
    assert env_executable.is_file()
    spec = _spec(
        run_spec_payload,
        argv=(str(env_executable),),
    )
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin"),
        "LANG": "C",
        "CUSTOM": "visible",
        "LINEAR_API_KEY": "linear-secret-value",
        "SSH_AUTH_SOCK": "/secret/socket",
        "MY_TOKEN": "token-secret-value",
        "NOT_ALLOWED": "hidden",
    }
    executor = LocalRunExecutor(
        local_host="host-a",
        max_tail_bytes=64 * 1024,
        base_environment=environment,
        environment_allowlist=frozenset(environment),
    )

    execution = _execute(executor, spec=spec, worktree=worktree)
    observed = dict(
        line.split("=", 1) for line in execution.stdout_tail.splitlines()
    )
    marker = execution.marker_path.read_text(encoding="utf-8")

    assert observed == {
        "CUSTOM": "visible",
        "LANG": "C",
        "NOT_ALLOWED": "hidden",
        "PATH": environment["PATH"],
    }
    assert "linear-secret-value" not in marker
    assert "token-secret-value" not in marker
    assert "LINEAR_API_KEY" not in marker
    assert "SSH_AUTH_SOCK" not in marker


def test_argv_is_passed_literally_without_shell_interpretation(
    tmp_path: Path,
    run_spec_payload,
) -> None:
    worktree = tmp_path / "frozen"
    worktree.mkdir()
    injected_target = tmp_path / "must-not-exist"
    literal = f"value; touch {injected_target}"
    source = "import pathlib, sys; pathlib.Path('argv.txt').write_text(sys.argv[1])"
    spec = _spec(
        run_spec_payload,
        argv=(sys.executable, "-c", source, literal),
        artifacts=(
            {"name": "argv", "path": "argv.txt", "media_type": "text/plain"},
        ),
    )

    execution = _execute(
        LocalRunExecutor(local_host="host-a"),
        spec=spec,
        worktree=worktree,
    )

    assert execution.result.outcome == RunOutcome.COMPLETE
    assert (worktree / "argv.txt").read_text(encoding="utf-8") == literal
    assert not injected_target.exists()


def test_launch_uses_the_exact_executable_bound_by_preflight(
    tmp_path: Path,
    run_spec_payload,
) -> None:
    worktree = tmp_path / "frozen"
    worktree.mkdir()
    literal = "remaining argv is unchanged"
    source = (
        "import pathlib, sys; "
        "pathlib.Path('selected.txt').write_text(sys.argv[1], encoding='utf-8')"
    )
    spec = _spec(
        run_spec_payload,
        argv=("/bin/false", "-c", source, literal),
        artifacts=(
            {
                "name": "selected",
                "path": "selected.txt",
                "media_type": "text/plain",
            },
        ),
    )
    receipt = _receipt(spec, executable=str(Path(sys.executable).resolve()))

    execution = LocalRunExecutor(local_host="host-a").execute(
        spec=spec,
        execution_worktree=worktree,
        preflight=receipt,
        attempt_id=ATTEMPT_ID,
        operation_id=OPERATION_ID,
        event_callback=lambda event: None,
    )

    assert execution.result.outcome == RunOutcome.COMPLETE
    assert (worktree / "selected.txt").read_text(encoding="utf-8") == literal


def test_terminal_marker_observes_retry_without_starting_second_process(
    tmp_path: Path,
    run_spec_payload,
) -> None:
    worktree = tmp_path / "frozen"
    worktree.mkdir()
    source = (
        "from pathlib import Path; p=Path('count.txt'); "
        "p.write_text((p.read_text() if p.exists() else '') + 'x')"
    )
    spec = _spec(
        run_spec_payload,
        argv=(sys.executable, "-c", source),
        artifacts=(
            {"name": "count", "path": "count.txt", "media_type": "text/plain"},
        ),
    )
    executor = LocalRunExecutor(local_host="host-a")

    first = _execute(executor, spec=spec, worktree=worktree)
    repeated = executor.execute(
        spec=spec,
        execution_worktree=worktree,
        preflight=_receipt(spec),
        attempt_id=ATTEMPT_ID,
        operation_id=OPERATION_ID,
        event_callback=lambda event: pytest.fail(f"replayed event: {event.state}"),
    )

    assert first.launched is True
    assert repeated.launched is False
    assert repeated.observed_existing is True
    assert repeated.result == first.result
    assert repeated.attempt == first.attempt
    assert (worktree / "count.txt").read_text(encoding="utf-8") == "x"


def test_crash_before_process_creation_blocks_blind_relaunch(
    tmp_path: Path,
    run_spec_payload,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "frozen"
    worktree.mkdir()
    spec = _spec(run_spec_payload, argv=(sys.executable, "-c", "pass"))
    executor = LocalRunExecutor(local_host="host-a")

    def crash(*args, **kwargs):
        del args, kwargs
        raise KeyboardInterrupt

    monkeypatch.setattr("researchctl.services.local_run.subprocess.Popen", crash)
    with pytest.raises(KeyboardInterrupt):
        _execute(executor, spec=spec, worktree=worktree)

    with pytest.raises(RCPError) as caught:
        _execute(executor, spec=spec, worktree=worktree)

    assert caught.value.code == "run_execution_uncertain"
    assert caught.value.context["phase"] == "launch_intent"
