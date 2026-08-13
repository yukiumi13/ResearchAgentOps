from __future__ import annotations

import argparse
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from researchctl.adapters import WorktreeObservation, WorktreeObservationState, WorktreeSpec
from researchctl.domain.enums import SessionState
from researchctl.domain.models import TaskRecord
from researchctl.runtime import RuntimeSession, RuntimeStore, hash_session_token
from researchctl.services.requests import (
    AgentKind,
    SessionContinueRequest,
    SessionStartRequest,
)
from researchctl.services.session_harness import LocalSessionHarness, build_session_prompt
from researchctl.session_host import run as run_session_host
from researchctl.session_host import sanitized_agent_environment

NOW = datetime(2026, 8, 2, 12, 34, 56, tzinfo=UTC)
PROJECT_ID = "project_20260802T123456Z_" + "9" * 24
NATIVE_ID = "0199a213-81c0-7800-8aa1-bbab2a035a53"


def _id(kind: str, fill: str) -> str:
    return f"{kind}_20260802T123456Z_{fill * 24}"


class FakeGitWorktrees:
    def __init__(self) -> None:
        self.specs: dict[Path, WorktreeSpec] = {}

    def create_or_observe(self, spec: WorktreeSpec) -> WorktreeObservation:
        known = self.specs.get(spec.worktree)
        if known is not None and known != spec:
            raise AssertionError("conflicting worktree spec")
        spec.worktree.mkdir(parents=False, exist_ok=True)
        self.specs[spec.worktree] = spec
        return WorktreeObservation(
            state=WorktreeObservationState.EXACT,
            branch_commit=spec.base_commit,
            worktree_commit=spec.base_commit,
            observed_branch=f"refs/heads/{spec.branch}",
        )

    def worktree_head(self, worktree: Path) -> str:
        return self.specs[worktree].base_commit


class FakeTmux:
    def __init__(self, runtime: RuntimeStore) -> None:
        self.runtime = runtime
        self.sessions: set[str] = set()
        self.environments: list[dict[str, str]] = []
        self.fail_next_start = False

    def has_session(self, name: str) -> bool:
        return name in self.sessions

    def start_session(
        self,
        name: str,
        *,
        cwd: Path,
        argv: tuple[str, ...],
        environment: Mapping[str, str] | None = None,
    ) -> bool:
        assert cwd.is_dir()
        assert argv[:3] and argv[1:3] == ("-m", "researchctl.session_host")
        values = dict(environment or {})
        self.environments.append(values)
        if self.fail_next_start:
            self.fail_next_start = False
            raise RuntimeError("injected crash before tmux side effect")
        session_id = values["RESEARCHCTL_SESSION_ID"]
        current = self.runtime.authenticate_session(
            session_id,
            values["RESEARCHCTL_SESSION_TOKEN"],
        )
        metadata = {**current.metadata, "native_session_id": NATIVE_ID}
        self.runtime.update_session_state(
            session_id,
            SessionState.ACTIVE,
            current.updated_at + timedelta(microseconds=1),
            metadata=metadata,
        )
        self.sessions.add(name)
        return True

    def send_interrupt(self, name: str) -> None:
        self.sessions.remove(name)
        session_id = name.removeprefix("research-")
        current = self.runtime.get_session(session_id)
        assert current is not None
        self.runtime.update_session_state(
            session_id,
            SessionState.IDLE,
            current.updated_at + timedelta(microseconds=1),
        )

    def attach_argv(self, name: str, *, require_existing: bool = True) -> tuple[str, ...]:
        assert not require_existing or name in self.sessions
        return ("tmux", "attach-session", "-t", name)


def _harness(tmp_path: Path) -> tuple[LocalSessionHarness, RuntimeStore, FakeTmux]:
    repository = tmp_path / "repository"
    repository.mkdir()
    worktrees = tmp_path / "worktrees"
    worktrees.mkdir()
    runtime = RuntimeStore(tmp_path / "runtime.sqlite3")
    tmux = FakeTmux(runtime)
    ticks = iter(NOW + timedelta(microseconds=index) for index in range(100))
    harness = LocalSessionHarness(
        project_id=PROJECT_ID,
        repository_root=repository,
        worktrees_directory=worktrees,
        local_host="host-a",
        runtime=runtime,
        git=FakeGitWorktrees(),  # type: ignore[arg-type]
        tmux=tmux,  # type: ignore[arg-type]
        clock=lambda: next(ticks),
        token_factory=iter(("first-token", "rotated-token", "third-token")).__next__,
        poll_timeout_seconds=0,
    )
    return harness, runtime, tmux


def _start(task: TaskRecord, fill: str = "e") -> SessionStartRequest:
    return SessionStartRequest(
        operation_id=_id("operation", fill),
        idempotency_key=f"start-{fill}",
        session_id=_id("session", fill),
        task_id=task.task_id,
        base_commit="1" * 40,
        host="host-a",
        agent=AgentKind.CODEX,
        prompt="Implement the accepted Task and publish structured status.",
    )


def test_session_prompt_requires_safe_checkpoint_notification_polling() -> None:
    session_id = _id("session", "d")
    assigned = "Inspect the exact experiment commit and summarize its evidence."

    prompt = build_session_prompt(assigned, session_id)

    assert session_id in prompt
    assert "researchctl notification list" in prompt
    assert "researchctl notification ack" in prompt
    assert "researchctl notification reply" in prompt
    assert "safe checkpoints" in prompt
    assert prompt.endswith(assigned)


def test_start_observes_exact_native_id_and_isolates_two_worktrees(
    tmp_path: Path,
    task_payload,
) -> None:
    harness, runtime, tmux = _harness(tmp_path)
    task = TaskRecord.model_validate(task_payload())

    first = harness.start_or_observe(_start(task, "e"), task)
    second = harness.start_or_observe(_start(task, "f"), task)

    assert first.state is SessionState.ACTIVE
    assert first.metadata["native_session_id"] == NATIVE_ID
    assert second.state is SessionState.ACTIVE
    assert first.branch != second.branch
    assert first.worktree_path != second.worktree_path
    assert Path(first.worktree_path or "").is_dir()
    assert Path(second.worktree_path or "").is_dir()
    assert runtime.authenticate_session(first.session_id, "first-token") == first
    assert all("LINEAR" not in repr(value) for value in runtime.list_sessions())
    assert len(tmux.sessions) == 2


def test_retry_rotates_digest_after_crash_before_tmux_launch(
    tmp_path: Path,
    task_payload,
) -> None:
    harness, runtime, tmux = _harness(tmp_path)
    task = TaskRecord.model_validate(task_payload())
    request = _start(task)
    tmux.fail_next_start = True

    with pytest.raises(RuntimeError, match="injected crash"):
        harness.start_or_observe(request, task)
    prepared = runtime.get_session(request.session_id)
    assert prepared is not None and prepared.state is SessionState.PREPARING
    first_digest = prepared.actor_token_digest

    recovered = harness.start_or_observe(request, task)

    assert recovered.state is SessionState.ACTIVE
    assert recovered.actor_token_digest != first_digest
    runtime.authenticate_session(request.session_id, "rotated-token")


def test_pause_and_lost_continuation_preserve_old_terminal_session(
    tmp_path: Path,
    task_payload,
) -> None:
    harness, runtime, tmux = _harness(tmp_path)
    task = TaskRecord.model_validate(task_payload())
    request = _start(task)
    active = harness.start_or_observe(request, task)

    paused = harness.pause_or_observe(active.session_id, "idle")
    assert paused.state is SessionState.IDLE
    lost = runtime.update_session_state(
        active.session_id,
        SessionState.LOST,
        paused.updated_at + timedelta(microseconds=1),
    )
    continuation = SessionContinueRequest(
        operation_id=_id("operation", "f"),
        idempotency_key="continue-lost",
        source_session_id=lost.session_id,
        new_session_id=_id("session", "f"),
        target_host="host-a",
        prompt="Continue from the observed source commit without reviving the old Session.",
    )

    continued = harness.continue_or_observe(continuation, lost, task)

    assert continued.state is SessionState.ACTIVE
    assert continued.continued_from == lost.session_id
    assert continued.session_id != lost.session_id
    old = runtime.get_session(lost.session_id)
    assert old is not None and old.state is SessionState.LOST
    assert len(tmux.sessions) == 1


def test_agent_environment_excludes_control_plane_and_infrastructure_secrets() -> None:
    source = {
        "PATH": "/usr/bin",
        "HOME": "/home/research",
        "OPENAI_API_KEY": "model-key",
        "RESEARCHCTL_SESSION_TOKEN": "session-capability",
        "LINEAR_API_KEY": "linear-secret",
        "SSH_AUTH_SOCK": "/tmp/ssh.sock",
        "AWS_SECRET_ACCESS_KEY": "cloud-secret",
        "GOOGLE_APPLICATION_CREDENTIALS": "/tmp/gcp.json",
        "GIT_CONFIG_COUNT": "1",
    }

    cleaned = sanitized_agent_environment(source)

    assert cleaned == {
        "PATH": "/usr/bin",
        "HOME": "/home/research",
        "OPENAI_API_KEY": "model-key",
        "RESEARCHCTL_SESSION_TOKEN": "session-capability",
    }


def test_session_host_persists_codex_thread_id_without_persisting_plaintext_token(
    tmp_path: Path,
    monkeypatch,
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    database = tmp_path / "runtime.sqlite3"
    session_id = _id("session", "e")
    token = "one-time-session-capability"
    with RuntimeStore(database) as runtime:
        runtime.save_session(
            RuntimeSession(
                session_id=session_id,
                project_id=PROJECT_ID,
                task_id=_id("task", "a"),
                state=SessionState.PREPARING,
                created_at=NOW,
                updated_at=NOW,
                host="host-a",
                branch=f"research/task/MAR-17/{session_id}",
                worktree_path=str(worktree),
                actor_token_digest=hash_session_token(token),
                metadata={"agent": "codex", "tmux_session": f"research-{session_id}"},
            )
        )
    executable = tmp_path / "codex"
    executable.write_text(
        "#!/bin/sh\n"
        "test -z \"$LINEAR_API_KEY\" || exit 41\n"
        "test -z \"$SSH_AUTH_SOCK\" || exit 42\n"
        f"printf '%s\\n' '{{\"type\":\"thread.started\",\"thread_id\":\"{NATIVE_ID}\"}}'\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    monkeypatch.setenv("RESEARCHCTL_SESSION_ID", session_id)
    monkeypatch.setenv("RESEARCHCTL_SESSION_TOKEN", token)
    monkeypatch.setenv("LINEAR_API_KEY", "must-not-enter-agent")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/must-not-enter-agent")
    arguments = argparse.Namespace(
        database=str(database),
        session_id=session_id,
        provider="codex",
        expected_native_session_id=None,
        command=["--", str(executable), "exec", "--json"],
    )

    assert run_session_host(arguments) == 0

    with RuntimeStore(database) as runtime:
        saved = runtime.get_session(session_id)
        assert saved is not None
        assert saved.state is SessionState.IDLE
        assert saved.metadata["native_session_id"] == NATIVE_ID
        assert saved.actor_token_digest == hash_session_token(token)
        assert token not in repr(saved)
