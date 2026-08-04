from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from researchctl.adapters._subprocess import (
    CommandResult,
    CommandRunner,
    SubprocessCommandRunner,
)
from researchctl.constants import LINEAR_PROJECTION_POLICY_PATH, PROJECT_POLICY_PATH
from researchctl.errors import RCPError


_OPERATION_ID = re.compile(r"^operation_\d{8}T\d{6}Z_[0-9a-f]{24}$")
_CONTROL_COMMANDS = frozenset({"task.create", "task.update", "task.cancel"})
_LINEAR_COMMAND = "linear.configure"
_PLAN_REVIEW_COMMAND = "plan.configure-review"
_POLICY_CONTROLS = {
    _LINEAR_COMMAND: (
        LINEAR_PROJECTION_POLICY_PATH,
        "Linear policy",
        "control_linear",
    ),
    _PLAN_REVIEW_COMMAND: (
        PROJECT_POLICY_PATH,
        "Plan review policy",
        "control_plan_review",
    ),
}
_TASK_PATH = re.compile(
    r"^\.research/tasks/task_\d{8}T\d{6}Z_[0-9a-f]{24}\.yaml$"
)
_GIT_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_GIT_ENVIRONMENT_KEYS = {
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_WORK_TREE",
}


@dataclass(frozen=True, slots=True)
class ControlCommitReceipt:
    branch: str
    worktree: Path
    commit: str
    changed: bool
    effect_applied: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "branch": self.branch,
            "worktree": str(self.worktree),
            "commit": self.commit,
            "changed": self.changed,
            "effect_applied": self.effect_applied,
            "delivery": "local_control_change",
        }


class GitControlCommitAdapter:
    def __init__(
        self,
        *,
        runner: CommandRunner | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._runner = runner or SubprocessCommandRunner()
        self._timeout_seconds = timeout_seconds

    @staticmethod
    def validate_identity(*, operation_id: str, command: str, branch: str) -> None:
        if not isinstance(operation_id, str) or not _OPERATION_ID.fullmatch(
            operation_id
        ):
            raise RCPError(
                code="control_operation_id_invalid",
                message="Control commit requires a canonical Operation ID.",
            )
        if not isinstance(command, str) or command not in _CONTROL_COMMANDS:
            raise RCPError(
                code="control_command_invalid",
                message="Control commit requires a supported Task command.",
            )
        expected_branch = f"research/control/{operation_id}"
        if branch != expected_branch:
            raise RCPError(
                code="control_branch_invalid",
                message="Control branch does not match its Operation ID.",
                context={"expected_branch": expected_branch},
            )

    @staticmethod
    def validate_linear_identity(*, operation_id: str, branch: str) -> None:
        GitControlCommitAdapter.validate_policy_identity(
            operation_id=operation_id,
            branch=branch,
            command=_LINEAR_COMMAND,
        )

    @staticmethod
    def validate_plan_review_identity(*, operation_id: str, branch: str) -> None:
        GitControlCommitAdapter.validate_policy_identity(
            operation_id=operation_id,
            branch=branch,
            command=_PLAN_REVIEW_COMMAND,
        )

    @staticmethod
    def validate_policy_identity(
        *,
        operation_id: str,
        branch: str,
        command: str,
    ) -> None:
        if command not in _POLICY_CONTROLS:
            raise RCPError(
                code="control_policy_command_invalid",
                message="Policy control requires a supported command.",
            )
        if not isinstance(operation_id, str) or not _OPERATION_ID.fullmatch(
            operation_id
        ):
            label = _POLICY_CONTROLS[command][1]
            raise RCPError(
                code="control_operation_id_invalid",
                message=f"{label} control requires a canonical Operation ID.",
            )
        expected_branch = f"research/control/{operation_id}"
        if branch != expected_branch:
            label = _POLICY_CONTROLS[command][1]
            raise RCPError(
                code="control_branch_invalid",
                message=f"{label} branch does not match its Operation ID.",
                context={"expected_branch": expected_branch},
            )

    def commit_or_observe(
        self,
        *,
        worktree: Path,
        branch: str,
        task_path: Path,
        operation_id: str,
        command: str,
    ) -> ControlCommitReceipt:
        self.validate_identity(
            operation_id=operation_id,
            command=command,
            branch=branch,
        )
        root = Path(os.path.abspath(os.fspath(worktree)))
        if root.is_symlink() or not root.is_dir():
            raise RCPError(
                code="control_worktree_invalid",
                message="Control worktree must be an existing non-symlink directory.",
            )
        candidate = Path(os.path.abspath(os.fspath(task_path)))
        try:
            relative = candidate.relative_to(root).as_posix()
        except ValueError as error:
            raise RCPError(
                code="control_task_path_invalid",
                message="Task record is outside the control worktree.",
            ) from error
        if not _TASK_PATH.fullmatch(relative) or PurePosixPath(relative).is_absolute():
            raise RCPError(
                code="control_task_path_invalid",
                message="Control commit may contain exactly one canonical Task record.",
            )

        current = root
        for part in PurePosixPath(relative).parts:
            current = current / part
            if current.is_symlink():
                raise RCPError(
                    code="control_task_path_invalid",
                    message="Control Task path must not traverse a symbolic link.",
                )
        if not candidate.is_file():
            raise RCPError(
                code="control_task_path_invalid",
                message="Control Task path must be an existing regular file.",
            )

        return self._commit_path_or_observe(
            root=root,
            branch=branch,
            relative=relative,
            working_path=candidate,
            operation_id=operation_id,
            command=command,
            path_error_code="control_task_path_invalid",
            record_label="Task record",
        )

    def commit_linear_policy_or_observe(
        self,
        *,
        worktree: Path,
        branch: str,
        operation_id: str,
        expected_base_commit: str,
    ) -> ControlCommitReceipt:
        return self.commit_policy_or_observe(
            worktree=worktree,
            branch=branch,
            operation_id=operation_id,
            expected_base_commit=expected_base_commit,
            command=_LINEAR_COMMAND,
        )

    def commit_plan_review_policy_or_observe(
        self,
        *,
        worktree: Path,
        branch: str,
        operation_id: str,
        expected_base_commit: str,
    ) -> ControlCommitReceipt:
        return self.commit_policy_or_observe(
            worktree=worktree,
            branch=branch,
            operation_id=operation_id,
            expected_base_commit=expected_base_commit,
            command=_PLAN_REVIEW_COMMAND,
        )

    def commit_policy_or_observe(
        self,
        *,
        worktree: Path,
        branch: str,
        operation_id: str,
        expected_base_commit: str,
        command: str,
    ) -> ControlCommitReceipt:
        self.validate_policy_identity(
            operation_id=operation_id,
            branch=branch,
            command=command,
        )
        relative, label, error_prefix = _POLICY_CONTROLS[command]
        if not _GIT_OBJECT_ID.fullmatch(expected_base_commit):
            raise RCPError(
                code=f"{error_prefix}_base_invalid",
                message=f"{label} control requires an exact base commit.",
            )
        root = Path(os.path.abspath(os.fspath(worktree)))
        if root.is_symlink() or not root.is_dir():
            raise RCPError(
                code="control_worktree_invalid",
                message="Control worktree must be an existing non-symlink directory.",
            )
        policy_path = root / relative
        current = root
        for part in PurePosixPath(relative).parts:
            current = current / part
            if current.is_symlink():
                raise RCPError(
                    code=f"{error_prefix}_policy_path_invalid",
                    message=f"{label} path must not traverse a symbolic link.",
                )
        if not policy_path.is_file():
            raise RCPError(
                code=f"{error_prefix}_policy_path_invalid",
                message=f"{label} must be an existing regular file.",
            )
        return self._commit_path_or_observe(
            root=root,
            branch=branch,
            relative=relative,
            working_path=policy_path,
            operation_id=operation_id,
            command=command,
            path_error_code=f"{error_prefix}_policy_path_invalid",
            record_label=label,
            expected_base_commit=expected_base_commit,
        )

    def validate_linear_operation_head(
        self,
        *,
        repository_root: Path,
        commit: str,
        operation_id: str,
        expected_base_commit: str,
    ) -> bool:
        return self.validate_policy_operation_head(
            repository_root=repository_root,
            commit=commit,
            operation_id=operation_id,
            expected_base_commit=expected_base_commit,
            command=_LINEAR_COMMAND,
        )

    def validate_plan_review_operation_head(
        self,
        *,
        repository_root: Path,
        commit: str,
        operation_id: str,
        expected_base_commit: str,
    ) -> bool:
        return self.validate_policy_operation_head(
            repository_root=repository_root,
            commit=commit,
            operation_id=operation_id,
            expected_base_commit=expected_base_commit,
            command=_PLAN_REVIEW_COMMAND,
        )

    def validate_policy_operation_head(
        self,
        *,
        repository_root: Path,
        commit: str,
        operation_id: str,
        expected_base_commit: str,
        command: str,
    ) -> bool:
        """Accept only the exact base or one fixed-path policy proposal."""

        branch = f"research/control/{operation_id}"
        self.validate_policy_identity(
            operation_id=operation_id,
            branch=branch,
            command=command,
        )
        relative, label, error_prefix = _POLICY_CONTROLS[command]
        if not all(
            _GIT_OBJECT_ID.fullmatch(value)
            for value in (commit, expected_base_commit)
        ):
            raise RCPError(
                code=f"{error_prefix}_base_invalid",
                message=f"{label} control requires exact commit identities.",
            )
        root = Path(os.path.abspath(os.fspath(repository_root)))
        if root.is_symlink() or not root.is_dir():
            raise RCPError(
                code="control_repository_invalid",
                message="Control repository must be an existing non-symlink directory.",
            )
        if commit == expected_base_commit:
            return False
        message = self._git(root, "show", "-s", "--format=%B", commit)
        expected_message = f"researchctl: {command} {operation_id}"
        parents = self._git(root, "show", "-s", "--format=%P", commit)
        paths = self._git(
            root,
            "diff-tree",
            "--root",
            "--no-commit-id",
            "--name-only",
            "-r",
            "-z",
            commit,
        )
        entry = self._git(
            root,
            "ls-tree",
            "-z",
            commit,
            "--",
            relative,
        )
        entry_prefix = "100644 blob "
        entry_suffix = f"\t{relative}\x00"
        if (
            message.stdout.rstrip("\n") != expected_message
            or tuple(parents.stdout.strip().split()) != (expected_base_commit,)
            or paths.stdout != f"{relative}\x00"
            or not entry.stdout.startswith(entry_prefix)
            or not entry.stdout.endswith(entry_suffix)
        ):
            raise RCPError(
                code=f"{error_prefix}_commit_invalid",
                message=(
                    f"Existing {label} branch is not this operation's "
                    "single-file commit over the requested base."
                ),
            )
        return True

    def linear_policy_content_at(
        self,
        *,
        repository_root: Path,
        commit: str,
    ) -> bytes | None:
        return self.policy_content_at(
            repository_root=repository_root,
            commit=commit,
            command=_LINEAR_COMMAND,
        )

    def project_policy_content_at(
        self,
        *,
        repository_root: Path,
        commit: str,
    ) -> bytes | None:
        return self.policy_content_at(
            repository_root=repository_root,
            commit=commit,
            command=_PLAN_REVIEW_COMMAND,
        )

    def policy_content_at(
        self,
        *,
        repository_root: Path,
        commit: str,
        command: str,
    ) -> bytes | None:
        if command not in _POLICY_CONTROLS:
            raise RCPError(
                code="control_policy_command_invalid",
                message="Policy read requires a supported control command.",
            )
        relative, label, error_prefix = _POLICY_CONTROLS[command]
        if not _GIT_OBJECT_ID.fullmatch(commit):
            raise RCPError(
                code=f"{error_prefix}_base_invalid",
                message=f"{label} read requires an exact base commit.",
            )
        root = Path(os.path.abspath(os.fspath(repository_root)))
        if root.is_symlink() or not root.is_dir():
            raise RCPError(
                code="control_repository_invalid",
                message="Control repository must be an existing non-symlink directory.",
            )
        entry = self._git(
            root,
            "ls-tree",
            "-z",
            commit,
            "--",
            relative,
        )
        if not entry.stdout:
            return None
        prefix = "100644 blob "
        suffix = f"\t{relative}\x00"
        if not entry.stdout.startswith(prefix) or not entry.stdout.endswith(suffix):
            raise RCPError(
                code=f"{error_prefix}_base_invalid",
                message=f"Accepted {label} is not a regular non-executable file.",
            )
        content = self._git(
            root,
            "show",
            f"{commit}:{relative}",
        )
        return content.stdout.encode("utf-8")

    def attach_existing_linear_branch(
        self,
        *,
        repository_root: Path,
        worktree: Path,
        branch: str,
        expected_head: str,
        operation_id: str,
    ) -> None:
        self.attach_existing_policy_branch(
            repository_root=repository_root,
            worktree=worktree,
            branch=branch,
            expected_head=expected_head,
            operation_id=operation_id,
            command=_LINEAR_COMMAND,
        )

    def attach_existing_plan_review_branch(
        self,
        *,
        repository_root: Path,
        worktree: Path,
        branch: str,
        expected_head: str,
        operation_id: str,
    ) -> None:
        self.attach_existing_policy_branch(
            repository_root=repository_root,
            worktree=worktree,
            branch=branch,
            expected_head=expected_head,
            operation_id=operation_id,
            command=_PLAN_REVIEW_COMMAND,
        )

    def attach_existing_policy_branch(
        self,
        *,
        repository_root: Path,
        worktree: Path,
        branch: str,
        expected_head: str,
        operation_id: str,
        command: str,
    ) -> None:
        self.validate_policy_identity(
            operation_id=operation_id,
            branch=branch,
            command=command,
        )
        _relative, label, error_prefix = _POLICY_CONTROLS[command]
        if not _GIT_OBJECT_ID.fullmatch(expected_head):
            raise RCPError(
                code=f"{error_prefix}_base_invalid",
                message=f"{label} recovery requires an exact branch head.",
            )
        repository = Path(os.path.abspath(os.fspath(repository_root)))
        target = Path(os.path.abspath(os.fspath(worktree)))
        if repository.is_symlink() or not repository.is_dir():
            raise RCPError(
                code="control_repository_invalid",
                message="Control repository must be an existing non-symlink directory.",
            )
        if (
            target.is_symlink()
            or os.path.lexists(target)
            or not target.parent.is_dir()
            or target.parent.is_symlink()
        ):
            raise RCPError(
                code="control_worktree_invalid",
                message=f"Recovered {label} worktree path is not safely absent.",
            )
        observed = self._git(
            repository,
            "rev-parse",
            "--verify",
            "--quiet",
            f"refs/heads/{branch}^{{commit}}",
            check=False,
        )
        if observed.returncode != 0 or observed.stdout.strip() != expected_head:
            raise RCPError(
                code=f"{error_prefix}_branch_mismatch",
                message=f"Recovered {label} branch changed unexpectedly.",
            )
        self._git(repository, "worktree", "add", "--", str(target), branch)
        recovered = self._git(target, "rev-parse", "--verify", "HEAD^{commit}")
        if recovered.stdout.strip() != expected_head:
            raise RCPError(
                code=f"{error_prefix}_branch_mismatch",
                message=f"Recovered {label} worktree has the wrong HEAD.",
            )

    def require_linear_worktree_scope(
        self,
        *,
        worktree: Path,
        branch: str,
        operation_id: str,
    ) -> None:
        self.require_policy_worktree_scope(
            worktree=worktree,
            branch=branch,
            operation_id=operation_id,
            command=_LINEAR_COMMAND,
        )

    def require_plan_review_worktree_scope(
        self,
        *,
        worktree: Path,
        branch: str,
        operation_id: str,
    ) -> None:
        self.require_policy_worktree_scope(
            worktree=worktree,
            branch=branch,
            operation_id=operation_id,
            command=_PLAN_REVIEW_COMMAND,
        )

    def require_policy_worktree_scope(
        self,
        *,
        worktree: Path,
        branch: str,
        operation_id: str,
        command: str,
    ) -> None:
        self.validate_policy_identity(
            operation_id=operation_id,
            branch=branch,
            command=command,
        )
        relative, label, _error_prefix = _POLICY_CONTROLS[command]
        root = Path(os.path.abspath(os.fspath(worktree)))
        if root.is_symlink() or not root.is_dir():
            raise RCPError(
                code="control_worktree_invalid",
                message="Control worktree must be an existing non-symlink directory.",
            )
        observed_branch = self._git(
            root,
            "symbolic-ref",
            "--quiet",
            "--short",
            "HEAD",
            check=False,
        )
        status = self._git(
            root,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        )
        if (
            observed_branch.returncode != 0
            or observed_branch.stdout.removesuffix("\n") != branch
            or any(
                len(line) < 4
                or line[3:] != relative
                for line in status.stdout.splitlines()
            )
        ):
            raise RCPError(
                code="control_worktree_dirty",
                message=(
                    f"{label} worktree is on the wrong branch or contains "
                    "a change outside its fixed policy path."
                ),
            )

    def _commit_path_or_observe(
        self,
        *,
        root: Path,
        branch: str,
        relative: str,
        working_path: Path,
        operation_id: str,
        command: str,
        path_error_code: str,
        record_label: str,
        expected_base_commit: str | None = None,
    ) -> ControlCommitReceipt:
        observed_branch = self._git(
            root,
            "symbolic-ref",
            "--quiet",
            "--short",
            "HEAD",
            check=False,
        )
        if (
            observed_branch.returncode != 0
            or observed_branch.stdout.removesuffix("\n") != branch
        ):
            raise RCPError(
                code="control_branch_mismatch",
                message="Control worktree is not on the requested operation branch.",
                context={"branch": branch},
            )

        status = self._git(
            root,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        )
        status_lines = status.stdout.splitlines()
        if any(len(line) < 4 or line[3:] != relative for line in status_lines):
            raise RCPError(
                code="control_worktree_dirty",
                message=f"Control worktree contains a change outside its {record_label}.",
            )

        commit_created = False
        if status_lines:
            self._git(root, "add", "--", relative)
            staged = self._git(
                root,
                "diff",
                "--cached",
                "--quiet",
                "--",
                relative,
                check=False,
            )
            if staged.returncode not in {0, 1}:
                self._raise_failed(staged, f"inspect staged {record_label} change")
            if staged.returncode == 1:
                self._git(
                    root,
                    "-c",
                    "user.name=Research Control Plane",
                    "-c",
                    "user.email=researchctl@localhost",
                    "-c",
                    "commit.gpgSign=false",
                    "commit",
                    "--no-gpg-sign",
                    "-m",
                    f"researchctl: {command} {operation_id}",
                    "--",
                    relative,
                )
                commit_created = True
        head = self._git(root, "rev-parse", "--verify", "HEAD^{commit}")
        commit = head.stdout.strip()
        if not _GIT_OBJECT_ID.fullmatch(commit):
            raise RCPError(
                code="git_output_invalid",
                message="Git returned an invalid control commit object ID.",
            )
        effect_applied = self._effect_applied(
            root=root,
            commit=commit,
            relative=relative,
            working_path=working_path,
            operation_id=operation_id,
            command=command,
            path_error_code=path_error_code,
            record_label=record_label,
        )
        if expected_base_commit is not None:
            _relative, _label, error_prefix = _POLICY_CONTROLS[command]
            if effect_applied:
                self.validate_policy_operation_head(
                    repository_root=root,
                    commit=commit,
                    operation_id=operation_id,
                    expected_base_commit=expected_base_commit,
                    command=command,
                )
            elif commit != expected_base_commit:
                raise RCPError(
                    code=f"{error_prefix}_commit_invalid",
                    message="Policy branch does not match its requested base.",
                )
        if commit_created and not effect_applied:
            raise RCPError(
                code="control_commit_invalid",
                message="Created control commit does not match its operation marker.",
            )
        return ControlCommitReceipt(
            branch=branch,
            worktree=root,
            commit=commit,
            changed=commit_created,
            effect_applied=effect_applied,
        )

    def _effect_applied(
        self,
        *,
        root: Path,
        commit: str,
        relative: str,
        working_path: Path,
        operation_id: str,
        command: str,
        path_error_code: str,
        record_label: str,
    ) -> bool:
        message = self._git(root, "show", "-s", "--format=%B", commit)
        expected_message = f"researchctl: {command} {operation_id}"
        if message.stdout.rstrip("\n") != expected_message:
            return False

        paths = self._git(
            root,
            "diff-tree",
            "--root",
            "--no-commit-id",
            "--name-only",
            "-r",
            "-z",
            commit,
        )
        if paths.stdout != f"{relative}\x00":
            raise RCPError(
                code="control_commit_invalid",
                message="Control operation commit changed an unexpected path.",
            )

        committed = self._git(root, "show", f"{commit}:{relative}")
        try:
            working_content = working_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise RCPError(
                code=path_error_code,
                message=f"Control {record_label} must be a readable UTF-8 file.",
            ) from error
        if committed.stdout != working_content:
            raise RCPError(
                code="control_commit_invalid",
                message=f"Control commit content differs from the {record_label}.",
            )
        return True

    def _git(
        self,
        root: Path,
        *args: str,
        check: bool = True,
    ) -> CommandResult:
        try:
            result = self._runner.run(
                ("git", "-c", "core.fsmonitor=false", "-C", str(root), *args),
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
                message="Git control change timed out.",
                context={"worktree": str(root)},
            ) from error
        if check and result.returncode != 0:
            self._raise_failed(result, args[0] if args else "run Git")
        return result

    @staticmethod
    def _raise_failed(result: CommandResult, operation: str) -> None:
        raise RCPError(
            code="git_control_command_failed",
            message=f"Git failed to {operation}.",
            context={"returncode": result.returncode},
        )


def _git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for key in tuple(environment):
        if key in _GIT_ENVIRONMENT_KEYS or key.startswith("GIT_CONFIG_"):
            environment.pop(key, None)
    environment.pop("GIT_CONFIG_PARAMETERS", None)
    return environment
