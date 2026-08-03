from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from researchctl.adapters.agents import ClaudeCliAdapter, CodexCliAdapter
from researchctl.domain.enums import SessionState
from researchctl.errors import RCPError
from researchctl.runtime import RuntimeStore


_PASSTHROUGH_ENVIRONMENT = {
    "ANTHROPIC_API_KEY",
    "CLAUDE_CONFIG_DIR",
    "CODEX_HOME",
    "COLORTERM",
    "HOME",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "LANG",
    "LOGNAME",
    "NO_PROXY",
    "OPENAI_API_KEY",
    "PATH",
    "PYTHONPATH",
    "RESEARCHCTL_SESSION_ID",
    "RESEARCHCTL_SESSION_TOKEN",
    "SHELL",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "TERM",
    "TMPDIR",
    "USER",
}


def sanitized_agent_environment(source: dict[str, str]) -> dict[str, str]:
    return {
        name: value
        for name, value in source.items()
        if name in _PASSTHROUGH_ENVIRONMENT or name.startswith("LC_")
    }


def _now_after(previous: datetime) -> datetime:
    observed = datetime.now(UTC)
    return observed if observed > previous else previous + timedelta(microseconds=1)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--database", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--provider", choices=("codex", "claude"), required=True)
    parser.add_argument("--expected-native-session-id")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def _command(arguments: argparse.Namespace) -> tuple[str, ...]:
    values = tuple(arguments.command)
    if values[:1] == ("--",):
        values = values[1:]
    if not values or any(not item or "\x00" in item for item in values):
        raise RCPError(
            code="agent_host_command_invalid",
            message="Session host requires one non-empty Agent argv.",
        )
    expected_binary = arguments.provider
    if Path(values[0]).name != expected_binary:
        raise RCPError(
            code="agent_host_provider_mismatch",
            message="Session host Agent command does not match its declared provider.",
        )
    return values


def _adapter(provider: str) -> CodexCliAdapter | ClaudeCliAdapter:
    return CodexCliAdapter() if provider == "codex" else ClaudeCliAdapter()


def _observe_native_id(
    adapter: CodexCliAdapter | ClaudeCliAdapter,
    line: str,
    provider: str,
) -> str | None:
    try:
        return adapter.parse_session_id(line)
    except RCPError as error:
        if error.code == f"{provider}_session_id_missing":
            return None
        raise


def _terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _record_host_failure(
    runtime: RuntimeStore,
    session_id: str,
    reason: str,
) -> None:
    current = runtime.get_session(session_id)
    if current is None or current.state is SessionState.LOST:
        return
    runtime.update_session_state(
        session_id,
        SessionState.STOPPED,
        _now_after(current.updated_at),
        metadata={**current.metadata, "agent_host_error": reason},
    )


def run(arguments: argparse.Namespace) -> int:
    session_id = arguments.session_id
    token = os.environ.get("RESEARCHCTL_SESSION_TOKEN", "")
    bound_session = os.environ.get("RESEARCHCTL_SESSION_ID")
    if bound_session != session_id:
        raise RCPError(
            code="agent_host_session_binding_invalid",
            message="Session host environment is not bound to its requested Session.",
        )
    command = _command(arguments)
    adapter = _adapter(arguments.provider)
    with RuntimeStore(Path(arguments.database)) as runtime:
        session = runtime.authenticate_session(session_id, token)
        if session.worktree_path is None:
            _record_host_failure(runtime, session_id, "agent_worktree_missing")
            raise RCPError(
                code="agent_worktree_invalid",
                message="Session does not have a worktree path.",
            )
        worktree = Path(session.worktree_path)
        if not worktree.is_dir() or worktree.is_symlink():
            _record_host_failure(runtime, session_id, "agent_worktree_invalid")
            raise RCPError(
                code="agent_worktree_invalid",
                message="Session worktree must be an existing non-symlink directory.",
            )
        environment = sanitized_agent_environment(dict(os.environ))
        try:
            process = subprocess.Popen(
                command,
                cwd=worktree,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=None,
                text=True,
                bufsize=1,
            )
        except OSError as error:
            _record_host_failure(runtime, session_id, type(error).__name__)
            raise RCPError(
                code="agent_process_start_failed",
                message="Session host could not start the Agent process.",
                context={"error_type": type(error).__name__},
            ) from error
        metadata = {
            **session.metadata,
            "agent_pid": process.pid,
            "agent_started_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }
        native_id: str | None = None
        assert process.stdout is not None
        try:
            for line in process.stdout:
                sys.stdout.write(line)
                sys.stdout.flush()
                observed = _observe_native_id(adapter, line, arguments.provider)
                if observed is None:
                    continue
                if arguments.expected_native_session_id not in {None, observed}:
                    raise RCPError(
                        code="native_session_id_conflict",
                        message=(
                            "Agent output does not match its expected native Session ID."
                        ),
                    )
                if native_id not in {None, observed}:
                    raise RCPError(
                        code="native_session_id_conflict",
                        message="Agent output declared conflicting native Session IDs.",
                    )
                native_id = observed
                metadata["native_session_id"] = observed
                current = runtime.get_session(session_id)
                assert current is not None
                runtime.update_session_state(
                    session_id,
                    SessionState.ACTIVE,
                    _now_after(current.updated_at),
                    metadata=metadata,
                )
            returncode = process.wait()
        except BaseException as error:
            _terminate_process(process)
            reason = error.code if isinstance(error, RCPError) else type(error).__name__
            _record_host_failure(runtime, session_id, reason)
            raise
        finally:
            process.stdout.close()
        current = runtime.get_session(session_id)
        assert current is not None
        if current.state is not SessionState.LOST:
            final_metadata = {
                **current.metadata,
                "agent_exit_code": returncode,
                "agent_finished_at": datetime.now(UTC).isoformat().replace(
                    "+00:00", "Z"
                ),
            }
            if native_id is None:
                final_metadata["native_session_error"] = "native_session_id_missing"
            target = (
                SessionState.IDLE
                if returncode == 0 and native_id is not None
                else SessionState.STOPPED
            )
            runtime.update_session_state(
                session_id,
                target,
                _now_after(current.updated_at),
                metadata=final_metadata,
            )
        return returncode


def main() -> None:
    try:
        returncode = run(_parser().parse_args())
    except RCPError as error:
        print(f"Session host error [{error.code}]: {error.message}", file=sys.stderr)
        raise SystemExit(error.exit_code) from error
    raise SystemExit(returncode)


if __name__ == "__main__":
    main()
