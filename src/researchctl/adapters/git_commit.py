from __future__ import annotations

import os
import subprocess
from pathlib import Path

from researchctl.adapters._subprocess import (
    CommandResult,
    CommandRunner,
    SubprocessCommandRunner,
)
from researchctl.errors import RCPError


class GitSessionCommitVerifier:
    """Verify a full commit ID is reachable from one exact Session branch."""

    def __init__(
        self,
        repository_root: Path,
        *,
        runner: CommandRunner | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._root = Path(os.path.abspath(os.fspath(repository_root)))
        if self._root.is_symlink() or not self._root.is_dir():
            raise RCPError(
                code="notification_repository_invalid",
                message="Notification commit verification requires a Git directory.",
                context={"path": str(self._root)},
            )
        self._runner = runner or SubprocessCommandRunner()
        self._timeout_seconds = timeout_seconds

    def require_reachable(self, commit_sha: str, branch: str) -> None:
        reference = f"refs/heads/{branch}"
        valid_ref = self._git("check-ref-format", reference, check=False)
        if valid_ref.returncode != 0:
            raise RCPError(
                code="notification_session_branch_invalid",
                message="Session branch is not a valid full Git branch reference.",
                context={"branch": branch},
            )
        branch_result = self._git(
            "show-ref",
            "--verify",
            "--hash",
            reference,
            check=False,
        )
        if branch_result.returncode in {1, 128}:
            raise RCPError(
                code="notification_session_branch_not_found",
                message="Session branch was not found in Git.",
                context={"branch": branch},
            )
        if branch_result.returncode != 0:
            self._raise_failed(branch_result, "resolve the Session branch")
        branch_head = branch_result.stdout.strip()

        object_type = self._git("cat-file", "-t", commit_sha, check=False)
        if object_type.returncode != 0 or object_type.stdout.strip() != "commit":
            raise RCPError(
                code="notification_commit_not_found",
                message="Requested notification commit is not a Git commit.",
                context={"commit_sha": commit_sha},
            )
        reachable = self._git(
            "merge-base",
            "--is-ancestor",
            commit_sha,
            branch_head,
            check=False,
        )
        if reachable.returncode == 1:
            raise RCPError(
                code="notification_commit_unreachable",
                message="Requested commit is not reachable from the Session branch.",
                context={
                    "commit_sha": commit_sha,
                    "branch": branch,
                    "branch_head": branch_head,
                },
            )
        if reachable.returncode != 0:
            self._raise_failed(reachable, "check Session commit reachability")

    def _git(self, *args: str, check: bool = True) -> CommandResult:
        try:
            result = self._runner.run(
                (
                    "git",
                    "-c",
                    "core.fsmonitor=false",
                    "-C",
                    str(self._root),
                    *args,
                ),
                cwd=None,
                env=_git_environment(),
                timeout_seconds=self._timeout_seconds,
            )
        except FileNotFoundError as error:
            raise RCPError(
                code="git_not_found",
                message="git executable was not found.",
            ) from error
        except subprocess.TimeoutExpired as error:
            raise RCPError(
                code="git_timeout",
                message="Session commit verification timed out.",
                context={"root": str(self._root)},
            ) from error
        except UnicodeError as error:
            raise RCPError(
                code="git_output_invalid",
                message="Git returned non-UTF-8 commit verification output.",
            ) from error
        if check and result.returncode != 0:
            self._raise_failed(result, args[0] if args else "verify a commit")
        return result

    @staticmethod
    def _raise_failed(result: CommandResult, operation: str) -> None:
        raise RCPError(
            code="git_notification_command_failed",
            message=f"Git failed to {operation}.",
            context={"returncode": result.returncode},
        )


def _git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for key in tuple(environment):
        if key in {
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            "GIT_COMMON_DIR",
            "GIT_DIR",
            "GIT_INDEX_FILE",
            "GIT_OBJECT_DIRECTORY",
            "GIT_WORK_TREE",
        } or key.startswith("GIT_CONFIG_"):
            environment.pop(key, None)
    environment.pop("GIT_CONFIG_PARAMETERS", None)
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    return environment
