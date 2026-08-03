from __future__ import annotations

import secrets
import sys
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Mapping

from researchctl.adapters import (
    AgentCliContract,
    ClaudeCliAdapter,
    CodexCliAdapter,
    GitWorktreeAdapter,
    TmuxAdapter,
    WorktreeSpec,
    deterministic_tmux_session_name,
)
from researchctl.domain.enums import SessionState
from researchctl.domain.models import TaskRecord
from researchctl.domain.types import utc_now
from researchctl.errors import RCPError
from researchctl.runtime import RuntimeSession, RuntimeStore, hash_session_token
from researchctl.services.requests import (
    AgentKind,
    SessionContinueRequest,
    SessionStartRequest,
)


def build_session_prompt(prompt: str, session_id: str) -> str:
    """Add the small cooperative protocol needed by an addressable Session."""

    return (
        "Research Control Plane Session contract:\n"
        f"- Your governed Session ID is {session_id}.\n"
        "- At safe checkpoints and before your final response, run "
        "`researchctl notification list`.\n"
        "- For each pending item, use its exact revision with "
        "`researchctl notification ack` or `researchctl notification reply`.\n"
        "- Never treat a notification as permission to change manager-owned "
        "Task, Decision, Report, policy, or .research state.\n"
        "\n"
        "Assigned work:\n"
        f"{prompt}"
    )


class LocalSessionHarness:
    """Observe-before-mutate supervisor for one local Git/tmux Agent Session."""

    def __init__(
        self,
        *,
        project_id: str,
        repository_root: Path,
        worktrees_directory: Path,
        local_host: str,
        runtime: RuntimeStore,
        git: GitWorktreeAdapter | None = None,
        tmux: TmuxAdapter | None = None,
        agents: Mapping[AgentKind, AgentCliContract] | None = None,
        clock: Callable[[], datetime] = utc_now,
        token_factory: Callable[[], str] | None = None,
        uuid_factory: Callable[[], str] | None = None,
        poll_timeout_seconds: float = 10.0,
        poll_interval_seconds: float = 0.05,
    ) -> None:
        self.project_id = project_id
        self.repository_root = repository_root.resolve()
        self.worktrees_directory = worktrees_directory.resolve()
        self.local_host = local_host
        self.runtime = runtime
        self.git = git or GitWorktreeAdapter()
        self.tmux = tmux or TmuxAdapter()
        self.agents = dict(
            agents
            or {
                AgentKind.CODEX: CodexCliAdapter(),
                AgentKind.CLAUDE: ClaudeCliAdapter(),
            }
        )
        self._clock = clock
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(32))
        self._uuid_factory = uuid_factory or (lambda: str(uuid.uuid4()))
        if poll_timeout_seconds < 0 or poll_interval_seconds <= 0:
            raise ValueError("Session polling durations are invalid")
        self._poll_timeout_seconds = poll_timeout_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._validate_local_paths()

    def _validate_local_paths(self) -> None:
        if not self.repository_root.is_dir() or self.repository_root.is_symlink():
            raise RCPError(
                code="session_repository_invalid",
                message="Session repository root must be an existing non-symlink directory.",
            )
        if (
            not self.worktrees_directory.is_dir()
            or self.worktrees_directory.is_symlink()
        ):
            raise RCPError(
                code="session_worktrees_directory_invalid",
                message="Session worktree directory must be initialized and non-symlinked.",
            )
        if not self.local_host:
            raise ValueError("local_host must be non-empty")

    def _time_after(self, previous: datetime) -> datetime:
        observed = self._clock()
        return observed if observed > previous else previous + timedelta(microseconds=1)

    def _require_local_host(self, host: str) -> None:
        if host != self.local_host:
            raise RCPError(
                code="remote_session_not_supported",
                message="Phase 2 Session execution is limited to the current host.",
                remediation="Use continuation on the owning host or wait for the SSH adapter.",
                context={"requested_host": host, "local_host": self.local_host},
            )

    def _identity(
        self,
        request: SessionStartRequest,
        task: TaskRecord,
        continued_from: str | None,
    ) -> tuple[str, Path, str]:
        branch = f"research/task/{task.key}/{request.session_id}"
        worktree = self.worktrees_directory / request.session_id
        tmux_name = deterministic_tmux_session_name(request.session_id)
        existing = self.runtime.get_session(request.session_id)
        if existing is not None:
            expected = (
                self.project_id,
                task.task_id,
                request.host,
                branch,
                str(worktree),
                continued_from,
                request.agent.value,
                tmux_name,
            )
            observed = (
                existing.project_id,
                existing.task_id,
                existing.host,
                existing.branch,
                existing.worktree_path,
                existing.continued_from,
                existing.metadata.get("agent"),
                existing.metadata.get("tmux_session"),
            )
            if observed != expected:
                raise RCPError(
                    code="session_identity_conflict",
                    message="Session ID is already bound to different execution identity.",
                    context={"session_id": request.session_id},
                )
        return branch, worktree, tmux_name

    def start_or_observe(
        self,
        request: SessionStartRequest,
        task: TaskRecord,
        *,
        continued_from: str | None = None,
    ) -> RuntimeSession:
        self._require_local_host(request.host)
        branch, worktree, tmux_name = self._identity(request, task, continued_from)
        existing = self.runtime.get_session(request.session_id)
        if existing is not None and existing.state is SessionState.LOST:
            raise RCPError(
                code="session_terminal",
                message="A lost Session is terminal; continue with a new Session ID.",
                context={"session_id": request.session_id},
            )
        head = (
            self.git.worktree_head(worktree)
            if existing is not None
            else request.base_commit
        )
        self.git.create_or_observe(
            WorktreeSpec(
                root=self.repository_root,
                base_commit=head,
                branch=branch,
                worktree=worktree,
            )
        )
        if existing is not None:
            if self.tmux.has_session(tmux_name):
                return self._wait_for_native_identity(existing.session_id, tmux_name)
            if existing.state is SessionState.ACTIVE:
                return self.runtime.update_session_state(
                    existing.session_id,
                    SessionState.LOST,
                    self._time_after(existing.updated_at),
                    metadata={**existing.metadata, "loss_reason": "tmux_session_missing"},
                )
            resume = existing.state in {SessionState.IDLE, SessionState.STOPPED}
            native_id = existing.metadata.get("native_session_id")
            if resume and not isinstance(native_id, str):
                return self.runtime.update_session_state(
                    existing.session_id,
                    SessionState.LOST,
                    self._time_after(existing.updated_at),
                    metadata={**existing.metadata, "loss_reason": "native_session_id_missing"},
                )
            token = self._token_factory()
            existing = self.runtime.rotate_session_token(
                existing.session_id,
                hash_session_token(token),
                self._time_after(existing.updated_at),
            )
            existing = self.runtime.update_session_state(
                existing.session_id,
                SessionState.PREPARING,
                self._time_after(existing.updated_at),
                metadata={**existing.metadata, "resume_requested": resume},
            )
        else:
            resume = False
            token = self._token_factory()
            expected_native = (
                self._uuid_factory() if request.agent is AgentKind.CLAUDE else None
            )
            metadata = {
                "agent": request.agent.value,
                "base_commit": request.base_commit,
                "tmux_session": tmux_name,
            }
            if expected_native is not None:
                metadata["expected_native_session_id"] = expected_native
            now = self._clock()
            existing = self.runtime.save_session(
                RuntimeSession(
                    session_id=request.session_id,
                    project_id=self.project_id,
                    task_id=task.task_id,
                    state=SessionState.PREPARING,
                    created_at=now,
                    updated_at=now,
                    host=request.host,
                    branch=branch,
                    worktree_path=str(worktree),
                    continued_from=continued_from,
                    actor_token_digest=hash_session_token(token),
                    metadata=metadata,
                )
            )

        try:
            adapter = self.agents[request.agent]
        except KeyError as error:
            raise RCPError(
                code="agent_adapter_not_configured",
                message="Requested Agent adapter is not configured on this host.",
                context={"agent": request.agent.value},
            ) from error
        expected_native_id = existing.metadata.get("expected_native_session_id")
        governed_prompt = build_session_prompt(request.prompt, request.session_id)
        if resume:
            native_id = existing.metadata.get("native_session_id")
            assert isinstance(native_id, str)
            agent_command = adapter.resume_command(
                worktree=worktree,
                prompt=governed_prompt,
                session_id=native_id,
            )
            expected_native_id = native_id
        else:
            agent_command = adapter.start_command(
                worktree=worktree,
                prompt=governed_prompt,
                session_id=(
                    expected_native_id if isinstance(expected_native_id, str) else None
                ),
            )
        wrapper = self._wrapper_argv(
            request.session_id,
            request.agent,
            agent_command.argv,
            expected_native_id=(
                expected_native_id if isinstance(expected_native_id, str) else None
            ),
        )
        self.tmux.start_session(
            tmux_name,
            cwd=worktree,
            argv=wrapper,
            environment={
                "RESEARCHCTL_SESSION_ID": request.session_id,
                "RESEARCHCTL_SESSION_TOKEN": token,
            },
        )
        return self._wait_for_native_identity(request.session_id, tmux_name)

    def _wrapper_argv(
        self,
        session_id: str,
        agent: AgentKind,
        agent_argv: tuple[str, ...],
        *,
        expected_native_id: str | None,
    ) -> tuple[str, ...]:
        values = [
            sys.executable,
            "-m",
            "researchctl.session_host",
            "--database",
            str(self.runtime.database_path),
            "--session-id",
            session_id,
            "--provider",
            agent.value,
        ]
        if expected_native_id is not None:
            values.extend(("--expected-native-session-id", expected_native_id))
        values.append("--")
        values.extend(agent_argv)
        return tuple(values)

    def _wait_for_native_identity(
        self,
        session_id: str,
        tmux_name: str,
    ) -> RuntimeSession:
        deadline = time.monotonic() + self._poll_timeout_seconds
        while True:
            current = self.runtime.get_session(session_id)
            assert current is not None
            if current.state is not SessionState.PREPARING:
                return current
            if time.monotonic() >= deadline:
                return current
            if not self.tmux.has_session(tmux_name):
                time.sleep(self._poll_interval_seconds)
                continue
            time.sleep(self._poll_interval_seconds)

    def pause_or_observe(self, session_id: str, mode: str) -> RuntimeSession:
        current = self.runtime.get_session(session_id)
        if current is None or current.project_id != self.project_id:
            raise RCPError(code="session_not_found", message="Session was not found.")
        self._require_local_host(current.host or "")
        if current.state is SessionState.LOST:
            return current
        tmux_name = current.metadata.get("tmux_session")
        if not isinstance(tmux_name, str):
            raise RCPError(
                code="session_identity_incomplete",
                message="Session does not have a deterministic tmux identity.",
            )
        if not self.tmux.has_session(tmux_name):
            if current.state in {SessionState.IDLE, SessionState.STOPPED}:
                return current
            return self.runtime.update_session_state(
                session_id,
                SessionState.LOST,
                self._time_after(current.updated_at),
                metadata={**current.metadata, "loss_reason": "tmux_session_missing"},
            )
        self.runtime.update_session_state(
            session_id,
            SessionState.STOPPING,
            self._time_after(current.updated_at),
        )
        self.tmux.send_interrupt(tmux_name)
        deadline = time.monotonic() + self._poll_timeout_seconds
        while True:
            observed = self.runtime.get_session(session_id)
            assert observed is not None
            if observed.state in {SessionState.IDLE, SessionState.STOPPED}:
                if mode == "stop" and observed.state is SessionState.IDLE:
                    return self.runtime.update_session_state(
                        session_id,
                        SessionState.STOPPED,
                        self._time_after(observed.updated_at),
                    )
                return observed
            if not self.tmux.has_session(tmux_name):
                target = SessionState.IDLE if mode == "idle" else SessionState.STOPPED
                return self.runtime.update_session_state(
                    session_id,
                    target,
                    self._time_after(observed.updated_at),
                )
            if time.monotonic() >= deadline:
                return observed
            time.sleep(self._poll_interval_seconds)

    def attach_argv(self, session_id: str) -> tuple[str, ...]:
        session = self.runtime.get_session(session_id)
        if session is None or session.project_id != self.project_id:
            raise RCPError(code="session_not_found", message="Session was not found.")
        self._require_local_host(session.host or "")
        tmux_name = session.metadata.get("tmux_session")
        if not isinstance(tmux_name, str):
            raise RCPError(
                code="session_identity_incomplete",
                message="Session does not have a deterministic tmux identity.",
            )
        return self.tmux.attach_argv(tmux_name, require_existing=True)

    def continue_or_observe(
        self,
        request: SessionContinueRequest,
        source: RuntimeSession,
        task: TaskRecord,
    ) -> RuntimeSession:
        self._require_local_host(request.target_host)
        if source.worktree_path is None:
            raise RCPError(
                code="session_identity_incomplete",
                message="Source Session does not have a worktree identity.",
            )
        agent_value = source.metadata.get("agent")
        try:
            agent = AgentKind(agent_value)
        except ValueError as error:
            raise RCPError(
                code="session_identity_incomplete",
                message="Source Session does not have a supported Agent identity.",
            ) from error
        base_commit = self.git.worktree_head(Path(source.worktree_path))
        start = SessionStartRequest(
            operation_id=request.operation_id,
            idempotency_key=request.idempotency_key,
            session_id=request.new_session_id,
            task_id=source.task_id,
            base_commit=base_commit,
            host=request.target_host,
            agent=agent,
            prompt=request.prompt,
        )
        return self.start_or_observe(
            start,
            task,
            continued_from=source.session_id,
        )
