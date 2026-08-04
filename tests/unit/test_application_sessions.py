from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from researchctl.domain.enums import SessionState
from researchctl.domain.models import (
    AgentPolicy,
    ExecutionDomainPolicy,
    ProjectPolicy,
    TaskRecord,
)
from researchctl.errors import RCPError
from researchctl.runtime import RuntimeSession, RuntimeStore, hash_session_token
from researchctl.services.actor import ActorContext, ActorRole, CredentialKind
from researchctl.services.application import ApplicationService
from researchctl.services.requests import (
    SessionAddressRequest,
    SessionAttachRequest,
    SessionContinueRequest,
    SessionListRequest,
    SessionPauseRequest,
    SessionShowRequest,
    SessionStartRequest,
)
from researchctl.services.task_records import TaskRecordRepository


NOW = datetime(2026, 8, 2, 12, 34, 56, tzinfo=UTC)
PROJECT_ID = "project_20260802T123456Z_" + "9" * 24
NATIVE_ID = "0199a213-81c0-7800-8aa1-bbab2a035a53"


def _id(kind: str, fill: str) -> str:
    return f"{kind}_20260802T123456Z_{fill * 24}"


class StubSessionHarness:
    def __init__(self, runtime: RuntimeStore) -> None:
        self.runtime = runtime

    def start_or_observe(
        self,
        request: SessionStartRequest,
        task: TaskRecord,
        *,
        continued_from: str | None = None,
    ) -> RuntimeSession:
        existing = self.runtime.get_session(request.session_id)
        if existing is not None:
            return existing
        return self.runtime.save_session(
            RuntimeSession(
                session_id=request.session_id,
                project_id=PROJECT_ID,
                task_id=task.task_id,
                state=SessionState.ACTIVE,
                created_at=NOW,
                updated_at=NOW,
                host=request.host,
                branch=f"research/task/{task.key}/{request.session_id}",
                worktree_path=f"/worktrees/{request.session_id}",
                continued_from=continued_from,
                actor_token_digest=hash_session_token("not-returned"),
                metadata={
                    "agent": request.agent.value,
                    "tmux_session": f"research-{request.session_id}",
                    "native_session_id": NATIVE_ID,
                },
            )
        )

    def pause_or_observe(self, session_id: str, mode: str) -> RuntimeSession:
        current = self.runtime.get_session(session_id)
        assert current is not None
        state = SessionState.IDLE if mode == "idle" else SessionState.STOPPED
        return self.runtime.update_session_state(
            session_id,
            state,
            current.updated_at + timedelta(microseconds=1),
        )

    def attach_argv(self, session_id: str) -> tuple[str, ...]:
        return ("tmux", "attach-session", "-t", f"research-{session_id}")

    def continue_or_observe(
        self,
        request: SessionContinueRequest,
        source: RuntimeSession,
        task: TaskRecord,
    ) -> RuntimeSession:
        start = SessionStartRequest(
            operation_id=request.operation_id,
            idempotency_key=request.idempotency_key,
            session_id=request.new_session_id,
            task_id=source.task_id,
            base_commit="2" * 40,
            host=request.target_host,
            agent=source.metadata["agent"],
            prompt=request.prompt,
        )
        return self.start_or_observe(start, task, continued_from=source.session_id)


class PendingSessionHarness(StubSessionHarness):
    def start_or_observe(
        self,
        request: SessionStartRequest,
        task: TaskRecord,
        *,
        continued_from: str | None = None,
    ) -> RuntimeSession:
        existing = self.runtime.get_session(request.session_id)
        if existing is not None:
            return existing
        return self.runtime.save_session(
            RuntimeSession(
                session_id=request.session_id,
                project_id=PROJECT_ID,
                task_id=task.task_id,
                state=SessionState.PREPARING,
                created_at=NOW,
                updated_at=NOW,
                host=request.host,
                branch=f"research/task/{task.key}/{request.session_id}",
                worktree_path=f"/worktrees/{request.session_id}",
                continued_from=continued_from,
                actor_token_digest=hash_session_token("pending-token"),
                metadata={"agent": request.agent.value},
            )
        )


class RecordingCommitVerifier:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def require_reachable(self, commit_sha: str, branch: str) -> None:
        self.calls.append((commit_sha, branch))


def _manager() -> ActorContext:
    return ActorContext(
        actor_id="uid-1000",
        role=ActorRole.MANAGER,
        credential_kind=CredentialKind.LOCAL_OS,
    )


def _agent(session_id: str) -> ActorContext:
    return ActorContext(
        actor_id=f"agent-{session_id}",
        role=ActorRole.AGENT,
        credential_kind=CredentialKind.SESSION_CAPABILITY,
        bound_session_id=session_id,
    )


def _service(tmp_path: Path) -> tuple[ApplicationService, RuntimeStore, TaskRecordRepository]:
    (tmp_path / ".research" / "tasks").mkdir(parents=True)
    tasks = TaskRecordRepository(tmp_path)
    runtime = RuntimeStore(tmp_path / "runtime.sqlite3")
    policy = ProjectPolicy(
        agent=AgentPolicy(
            accepted_paths_denied=(
                ".research/decisions/**",
                ".research/policies/**",
                ".research/project.yaml",
                ".research/impacts/**",
                ".research/reports/**",
                ".research/tasks/**",
            )
        ),
        execution_domains=(
            ExecutionDomainPolicy(
                execution_domain="on-prem",
                host_pools=("interactive",),
            ),
        ),
    )
    service = ApplicationService(
        project_id=PROJECT_ID,
        policy=policy,
        tasks=tasks,
        runtime=runtime,
        sessions=StubSessionHarness(runtime),
        clock=lambda: NOW,
    )
    return service, runtime, tasks


def test_session_service_authorizes_attach_pause_and_new_continuation(
    tmp_path: Path,
    task_payload,
) -> None:
    service, runtime, tasks = _service(tmp_path)
    task = TaskRecord.model_validate(task_payload())
    tasks.create(task)
    task_bytes = tasks.path_for(task.task_id).read_bytes()
    session_id = _id("session", "e")
    start = SessionStartRequest(
        operation_id=_id("operation", "1"),
        idempotency_key="start-session-e",
        session_id=session_id,
        task_id=task.task_id,
        base_commit="1" * 40,
        host="host-a",
        agent="codex",
        prompt="Implement the accepted Task and report structured status.",
    )

    started = service.session_start(start, _manager())

    assert started.terminal_result == "active"
    assert started.data["session"]["native_session_id"] == NATIVE_ID
    assert "actor_token_digest" not in repr(started.data)
    attached = service.session_attach(
        SessionAttachRequest(session_id=session_id),
        _agent(session_id),
    )
    assert attached["attach_argv"][-1] == f"research-{session_id}"

    with pytest.raises(RCPError) as cross_session:
        service.session_pause(
            SessionPauseRequest(
                operation_id=_id("operation", "2"),
                idempotency_key="wrong-session-pause",
                session_id=session_id,
            ),
            _agent(_id("session", "f")),
        )
    assert cross_session.value.code == "session_scope_denied"

    paused = service.session_pause(
        SessionPauseRequest(
            operation_id=_id("operation", "3"),
            idempotency_key="pause-session-e",
            session_id=session_id,
        ),
        _agent(session_id),
    )
    assert paused.terminal_result == "idle"
    old = runtime.get_session(session_id)
    assert old is not None
    lost = runtime.update_session_state(
        session_id,
        SessionState.LOST,
        old.updated_at + timedelta(microseconds=1),
    )
    continuation = SessionContinueRequest(
        operation_id=_id("operation", "4"),
        idempotency_key="continue-session-e-as-f",
        source_session_id=session_id,
        new_session_id=_id("session", "f"),
        target_host="host-a",
        prompt="Continue on a new identity and leave the lost Session terminal.",
    )

    continued = service.session_continue_new(continuation, _agent(session_id))

    assert continued.terminal_result == "active"
    assert continued.data["session"]["continued_from"] == lost.session_id
    old_after = runtime.get_session(lost.session_id)
    assert old_after is not None and old_after.state is SessionState.LOST
    assert tasks.path_for(task.task_id).read_bytes() == task_bytes


def test_pending_session_transition_keeps_operation_open_for_observation(
    tmp_path: Path,
    task_payload,
) -> None:
    service, runtime, tasks = _service(tmp_path)
    service.sessions = PendingSessionHarness(runtime)
    task = TaskRecord.model_validate(task_payload(state="ready"))
    tasks.create(task)
    request = SessionStartRequest(
        operation_id=_id("operation", "5"),
        idempotency_key="pending-session-start",
        session_id=_id("session", "5"),
        task_id=task.task_id,
        base_commit="1" * 40,
        host="host-a",
        agent="codex",
        prompt="Start and wait for the native Agent identity.",
    )

    for _ in range(2):
        with pytest.raises(RCPError) as pending:
            service.session_start(request, _manager())
        assert pending.value.code == "session_transition_pending"

    operation = runtime.get_operation(request.operation_id)
    assert operation is not None
    assert operation.state == "running"
    assert not any(event.kind == "operation_failed" for event in operation.events)


def test_session_discovery_and_addressing_are_scoped_and_read_only(
    tmp_path: Path,
    task_payload,
) -> None:
    service, runtime, tasks = _service(tmp_path)
    verifier = RecordingCommitVerifier()
    service.notification_commits = verifier
    task = TaskRecord.model_validate(task_payload(state="ready"))
    tasks.create(task)
    first_id = _id("session", "6")
    second_id = _id("session", "7")
    service.session_start(
        SessionStartRequest(
            operation_id=_id("operation", "6"),
            idempotency_key="start-discoverable-session",
            session_id=first_id,
            task_id=task.task_id,
            base_commit="1" * 40,
            host="host-a",
            agent="codex",
            prompt="Create a discoverable Session.",
        ),
        _manager(),
    )
    runtime.save_session(
        RuntimeSession(
            session_id=second_id,
            project_id=PROJECT_ID,
            task_id=task.task_id,
            state=SessionState.STOPPED,
            created_at=NOW + timedelta(seconds=1),
            updated_at=NOW + timedelta(seconds=1),
            host="host-a",
            branch=f"research/task/{task.key}/{second_id}",
        )
    )

    manager_active = service.session_list(
        SessionListRequest(task_id=task.task_id, state="active", limit=1),
        _manager(),
    )
    assert [item["session_id"] for item in manager_active["items"]] == [first_id]
    assert service.session_list(
        SessionListRequest(state="stopped"),
        _agent(first_id),
    ) == {"items": []}
    own = service.session_list(SessionListRequest(), _agent(first_id))
    assert [item["session_id"] for item in own["items"]] == [first_id]

    shown = service.session_show(SessionShowRequest(session_id=first_id), _agent(first_id))
    assert shown["session"] == {
        "session_id": first_id,
        "task_id": task.task_id,
        "state": "active",
        "host": "host-a",
        "branch": f"research/task/{task.key}/{first_id}",
        "worktree_path": f"/worktrees/{first_id}",
        "continued_from": None,
        "tmux_session": f"research-{first_id}",
        "agent": "codex",
        "native_session_id": NATIVE_ID,
        "last_observed_at": "2026-08-02T12:34:56Z",
    }

    for request, method in (
        (SessionShowRequest(session_id=second_id), service.session_show),
        (
            SessionAddressRequest(session_id=second_id, commit_sha="a" * 40),
            service.session_address,
        ),
    ):
        with pytest.raises(RCPError) as denied:
            method(request, _agent(first_id))
        assert denied.value.code == "session_scope_denied"

    current = runtime.get_session(first_id)
    assert current is not None
    runtime.update_session_state(
        first_id,
        SessionState.LOST,
        current.updated_at + timedelta(microseconds=1),
    )
    before = runtime.list_sessions(PROJECT_ID)
    addressed = service.session_address(
        SessionAddressRequest(session_id=first_id, commit_sha="a" * 40),
        _agent(first_id),
    )

    assert addressed["command_header"] == (
        f"@researchctl-app notify session:{first_id} commit:{'a' * 40}"
    )
    assert addressed["message_required"] is True
    assert addressed["session"]["state"] == "lost"
    assert verifier.calls == [
        ("a" * 40, f"research/task/{task.key}/{first_id}")
    ]
    assert runtime.list_sessions(PROJECT_ID) == before
