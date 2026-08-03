from __future__ import annotations

import os
import re
import shlex
import subprocess
from collections.abc import Mapping
from pathlib import Path

from researchctl.adapters._subprocess import (
    CommandResult,
    CommandRunner,
    SubprocessCommandRunner,
)
from researchctl.errors import RCPError


_SESSION_ID = re.compile(
    r"^session_\d{8}T\d{6}Z_[0-9a-f]{24}$"
)
_SESSION_NAME = re.compile(
    r"^research-session_\d{8}T\d{6}Z_[0-9a-f]{24}$"
)
_ENVIRONMENT_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")


def deterministic_tmux_session_name(session_id: str) -> str:
    if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
        raise RCPError(
            code="tmux_session_id_invalid",
            message="A canonical Session ID is required for the tmux session name.",
        )
    return f"research-{session_id}"


class TmuxAdapter:
    def __init__(
        self,
        *,
        runner: CommandRunner | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._runner = runner or SubprocessCommandRunner()
        self._timeout_seconds = timeout_seconds

    def has_session(self, name: str) -> bool:
        validated = self._validate_name(name)
        result = self._tmux("has-session", "-t", validated, check=False)
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        self._raise_command_failed(result, operation="observe session")

    def start_session(
        self,
        name: str,
        *,
        cwd: Path,
        argv: tuple[str, ...],
        environment: Mapping[str, str] | None = None,
    ) -> bool:
        validated = self._validate_name(name)
        working_directory = self._validate_cwd(cwd)
        command = self._validate_command(argv)
        environment_options = self._environment_options(environment)
        shell_command = f"exec {shlex.join(command)}"
        if self.has_session(validated):
            return False

        result = self._tmux(
            "start-session",
            "-d",
            "-s",
            validated,
            "-c",
            str(working_directory),
            *environment_options,
            shell_command,
            check=False,
        )
        if result.returncode == 0:
            return True
        if self.has_session(validated):
            return False
        self._raise_command_failed(result, operation="start session")

    def send_interrupt(self, name: str) -> None:
        validated = self._validate_name(name)
        if not self.has_session(validated):
            self._raise_not_found(validated)

        result = self._tmux(
            "send-keys",
            "-t",
            validated,
            "C-c",
            check=False,
        )
        if result.returncode == 0:
            return
        if not self.has_session(validated):
            self._raise_not_found(validated)
        self._raise_command_failed(result, operation="interrupt session")

    def attach_argv(
        self,
        name: str,
        *,
        require_existing: bool = True,
    ) -> tuple[str, ...]:
        validated = self._validate_name(name)
        if require_existing and not self.has_session(validated):
            self._raise_not_found(validated)
        return ("tmux", "attach-session", "-t", validated)

    def interrupt_argv(self, name: str) -> tuple[str, ...]:
        validated = self._validate_name(name)
        return ("tmux", "send-keys", "-t", validated, "C-c")

    def _tmux(
        self,
        *args: str,
        check: bool = True,
    ) -> CommandResult:
        argv = ("tmux", *args)
        try:
            result = self._runner.run(
                argv,
                cwd=None,
                env=None,
                timeout_seconds=self._timeout_seconds,
            )
        except FileNotFoundError as exc:
            raise RCPError(
                code="tmux_not_found",
                message="tmux executable was not found.",
                remediation="Install tmux and ensure it is available on PATH.",
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise RCPError(
                code="tmux_timeout",
                message=(
                    f"tmux command timed out after "
                    f"{self._timeout_seconds:g} seconds."
                ),
            ) from exc
        if check and result.returncode != 0:
            self._raise_command_failed(result, operation=args[0] if args else "tmux")
        return result

    @staticmethod
    def _validate_name(name: str) -> str:
        if not isinstance(name, str) or not _SESSION_NAME.fullmatch(name):
            raise RCPError(
                code="tmux_session_name_invalid",
                message="tmux session name is not a deterministic research Session name.",
            )
        return name

    @staticmethod
    def _validate_cwd(cwd: Path) -> Path:
        path = Path(os.path.abspath(os.fspath(cwd)))
        if not path.is_absolute() or path.is_symlink() or not path.is_dir():
            raise RCPError(
                code="tmux_cwd_invalid",
                message="tmux cwd must be an existing non-symlink directory.",
                context={"cwd": str(path)},
            )
        return path

    @staticmethod
    def _validate_command(argv: tuple[str, ...]) -> tuple[str, ...]:
        if (
            not isinstance(argv, tuple)
            or not argv
            or not argv[0]
            or any(
                not isinstance(item, str) or any(char in item for char in "\x00\r\n")
                for item in argv
            )
        ):
            raise RCPError(
                code="tmux_command_invalid",
                message="tmux command must be a non-empty argv tuple.",
            )
        return argv

    @staticmethod
    def _environment_options(
        environment: Mapping[str, str] | None,
    ) -> tuple[str, ...]:
        if environment is None:
            return ()
        if not isinstance(environment, Mapping):
            raise RCPError(
                code="tmux_environment_invalid",
                message="tmux environment must be a string mapping.",
            )
        items = list(environment.items())
        if any(
            not isinstance(name, str)
            or not _ENVIRONMENT_NAME.fullmatch(name)
            or not isinstance(value, str)
            or "\x00" in value
            for name, value in items
        ):
            raise RCPError(
                code="tmux_environment_invalid",
                message="tmux environment contains an invalid name or value.",
            )
        return tuple(
            item
            for name, value in sorted(items)
            for item in ("-e", f"{name}={value}")
        )

    @staticmethod
    def _raise_not_found(name: str) -> None:
        raise RCPError(
            code="tmux_session_not_found",
            message="The requested tmux session does not exist.",
            context={"session_name": name},
        )

    @staticmethod
    def _raise_command_failed(result: CommandResult, *, operation: str) -> None:
        raise RCPError(
            code="tmux_command_failed",
            message=f"tmux failed to {operation}.",
            context={"returncode": result.returncode},
        )
