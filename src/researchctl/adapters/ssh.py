from __future__ import annotations

import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal

from researchctl.adapters._subprocess import (
    CommandResult,
    CommandRunner,
    SubprocessCommandRunner,
)
from researchctl.errors import RCPError

_SSH_TARGET = re.compile(
    r"^(?:[A-Za-z0-9][A-Za-z0-9._-]{0,63}@)?"
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,252}$"
)
_HOST_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_REMOTE_PROGRAM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/+:-]{0,255}$")


@dataclass(frozen=True, slots=True)
class SSHHostProfile:
    """Credential-free connection details for one outbound-only SSH host."""

    host: str
    target: str
    remote_program: str = "researchctl-remote"
    connect_timeout_seconds: int = 10
    command_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not isinstance(self.host, str) or _HOST_NAME.fullmatch(self.host) is None:
            raise ValueError("host must be a canonical lower-case host name")
        if not isinstance(self.target, str) or _SSH_TARGET.fullmatch(self.target) is None:
            raise ValueError("target must be a safe SSH config alias or user@host")
        if (
            not isinstance(self.remote_program, str)
            or _REMOTE_PROGRAM.fullmatch(self.remote_program) is None
            or "//" in self.remote_program
        ):
            raise ValueError("remote_program must be a safe executable path")
        if (
            not isinstance(self.connect_timeout_seconds, int)
            or isinstance(self.connect_timeout_seconds, bool)
            or not 1 <= self.connect_timeout_seconds <= 300
        ):
            raise ValueError("connect_timeout_seconds must be between 1 and 300")
        if (
            isinstance(self.command_timeout_seconds, bool)
            or not isinstance(self.command_timeout_seconds, (int, float))
            or not 0 < self.command_timeout_seconds <= 3600
        ):
            raise ValueError("command_timeout_seconds must be between 0 and 3600")


@dataclass(frozen=True, slots=True)
class SSHCommandReceipt:
    host: str
    operation: Literal["observe", "mutate"]
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class SSHTransport:
    """Small SSH boundary; mutation recovery remains an application-service concern."""

    def __init__(
        self,
        *,
        runner: CommandRunner | None = None,
        ssh_program: str = "ssh",
    ) -> None:
        if _REMOTE_PROGRAM.fullmatch(ssh_program) is None:
            raise ValueError("ssh_program must be a safe executable path")
        self._runner = runner or SubprocessCommandRunner()
        self._ssh_program = ssh_program

    def observe(
        self,
        profile: SSHHostProfile,
        argv: tuple[str, ...],
        *,
        timeout_seconds: float | None = None,
    ) -> SSHCommandReceipt:
        return self._run(
            profile,
            argv,
            operation="observe",
            timeout_seconds=timeout_seconds,
        )

    def mutate(
        self,
        profile: SSHHostProfile,
        argv: tuple[str, ...],
        *,
        operation_id: str,
        timeout_seconds: float | None = None,
    ) -> SSHCommandReceipt:
        operation = _require_argument(operation_id, "operation_id")
        return self._run(
            profile,
            ("operation", "execute", "--operation-id", operation, "--", *argv),
            operation="mutate",
            timeout_seconds=timeout_seconds,
        )

    def operation_status(
        self,
        profile: SSHHostProfile,
        operation_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> SSHCommandReceipt:
        operation = _require_argument(operation_id, "operation_id")
        return self.observe(
            profile,
            ("operation", "show", "--operation-id", operation, "--json"),
            timeout_seconds=timeout_seconds,
        )

    def _run(
        self,
        profile: SSHHostProfile,
        argv: tuple[str, ...],
        *,
        operation: Literal["observe", "mutate"],
        timeout_seconds: float | None,
    ) -> SSHCommandReceipt:
        command = _validate_argv(argv)
        timeout = _validate_timeout(timeout_seconds, profile.command_timeout_seconds)
        remote_argv = (profile.remote_program, *command)
        local_argv = (
            self._ssh_program,
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            "ClearAllForwardings=yes",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "ForwardAgent=no",
            "-o",
            "PermitLocalCommand=no",
            "-o",
            "RequestTTY=no",
            "-o",
            f"ConnectTimeout={profile.connect_timeout_seconds}",
            "--",
            profile.target,
            shlex.join(remote_argv),
        )
        try:
            result = self._runner.run(
                local_argv,
                cwd=None,
                env=None,
                timeout_seconds=timeout,
            )
        except subprocess.TimeoutExpired as error:
            code = "ssh_mutation_uncertain" if operation == "mutate" else "ssh_timeout"
            remediation = (
                "Observe the same Operation ID on the remote host before retrying."
                if operation == "mutate"
                else "Retry the read-only observation or inspect SSH connectivity."
            )
            raise RCPError(
                code=code,
                message="The SSH command did not produce a bounded response.",
                remediation=remediation,
                context={"host": profile.host, "operation": operation},
            ) from error
        except OSError as error:
            raise RCPError(
                code="ssh_transport_unavailable",
                message="The local SSH transport could not be started.",
                remediation="Install SSH and verify the configured executable path.",
                context={"host": profile.host, "error_type": type(error).__name__},
            ) from error

        if not isinstance(result, CommandResult):
            raise TypeError("SSH command runner returned an invalid result")
        return SSHCommandReceipt(
            host=profile.host,
            operation=operation,
            argv=command,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )


def _validate_timeout(value: float | None, default: float) -> float:
    timeout = default if value is None else value
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not 0 < timeout <= 3600
    ):
        raise ValueError("timeout_seconds must be between 0 and 3600")
    return float(timeout)


def _validate_argv(argv: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(argv, tuple) or not argv:
        raise ValueError("remote argv must be a non-empty tuple")
    return tuple(_require_argument(value, f"argv[{index}]") for index, value in enumerate(argv))


def _require_argument(value: str, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")
    if len(value.encode("utf-8")) > 64 * 1024:
        raise ValueError(f"{field} exceeds 64 KiB")
    if "\x00" in value or "\r" in value or "\n" in value:
        raise ValueError(f"{field} contains forbidden control characters")
    return value


def require_remote_absolute_path(value: str, field: str) -> str:
    candidate = _require_argument(value, field)
    path = PurePosixPath(candidate)
    if not path.is_absolute() or path.as_posix() != candidate or ".." in path.parts:
        raise ValueError(f"{field} must be a canonical absolute POSIX path")
    return candidate
