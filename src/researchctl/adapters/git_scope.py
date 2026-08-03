from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from researchctl.adapters._subprocess import (
    CommandResult,
    CommandRunner,
    SubprocessCommandRunner,
)
from researchctl.adapters.git_ci import GitCIObjectReader
from researchctl.domain.models import TaskRecord
from researchctl.errors import RCPError
from researchctl.serialization import dump_yaml, load_yaml


_GIT_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_TASK_ID = re.compile(r"^task_\d{8}T\d{6}Z_[0-9a-f]{24}$")
_RAW_HEADER = re.compile(
    r"^:(?P<old_mode>[0-7]{6}) (?P<new_mode>[0-7]{6}) "
    r"(?P<old_id>[0-9a-f]{40}|[0-9a-f]{64}) "
    r"(?P<new_id>[0-9a-f]{40}|[0-9a-f]{64}) (?P<status>[A-Z])$"
)
_GIT_ENVIRONMENT_KEYS = {
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_WORK_TREE",
}
_UNSAFE_MODES = {"120000": "symlink", "160000": "submodule"}
_REGULAR_FILE_MODES = {"100644", "100755"}


class GitScopeChange(Protocol):
    path: str
    status: str
    old_mode: str
    new_mode: str


@dataclass(frozen=True, slots=True)
class GitDiffEntry:
    path: str
    status: str
    old_mode: str
    new_mode: str


@dataclass(frozen=True, slots=True)
class WriteScopeReceipt:
    base_commit: str
    head_commit: str
    branch: str
    paths: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "base_commit": self.base_commit,
            "head_commit": self.head_commit,
            "branch": self.branch,
            "paths": list(self.paths),
        }


@dataclass(frozen=True, slots=True)
class SourceWriteScopeReceipt:
    trusted_base_commit: str
    baseline_commit: str
    source_commit: str
    paths: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "trusted_base_commit": self.trusted_base_commit,
            "baseline_commit": self.baseline_commit,
            "source_commit": self.source_commit,
            "paths": list(self.paths),
        }


class GitWriteScopeValidator:
    """Validates a committed proposal against one Task's declared write scope."""

    def __init__(
        self,
        *,
        runner: CommandRunner | None = None,
        object_reader: GitCIObjectReader | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._runner = runner or SubprocessCommandRunner()
        self._objects = object_reader or GitCIObjectReader(
            runner=self._runner,
            timeout_seconds=timeout_seconds,
        )
        self._timeout_seconds = timeout_seconds

    def validate_commit(
        self,
        *,
        task: TaskRecord,
        worktree: Path,
        expected_branch: str,
        base_commit: str,
        head_commit: str | None = None,
    ) -> WriteScopeReceipt:
        root = self._validate_worktree(worktree)
        base = self._require_object_id(base_commit, "base_commit")
        observed_head = self._resolve_head(root)
        head = observed_head
        if head_commit is not None:
            head = self._require_object_id(head_commit, "head_commit")
            if head != observed_head:
                raise RCPError(
                    code="write_scope_head_mismatch",
                    message="Proposal commit is not the checked-out branch tip.",
                    context={
                        "expected_head": head,
                        "observed_head": observed_head,
                    },
                )
        branch = self._current_branch(root)
        if branch != expected_branch:
            raise RCPError(
                code="write_scope_worktree_mismatch",
                message="Proposal worktree is checked out on an unexpected branch.",
                context={"expected_branch": expected_branch, "observed_branch": branch},
            )
        self._objects.read_commit(root, base)
        self._objects.read_commit(root, head)
        if not self._objects.is_ancestor(
            root,
            ancestor=base,
            descendant=head,
        ):
            raise RCPError(
                code="write_scope_base_unreachable",
                message="Proposal branch tip does not descend from its declared base.",
                context={"base_commit": base, "head_commit": head},
            )

        entries = self._diff(root, base, head)
        paths = self.validate_changes(task=task, changes=entries)
        return WriteScopeReceipt(
            base_commit=base,
            head_commit=head,
            branch=branch,
            paths=paths,
        )

    def resolve_branch_head(
        self,
        *,
        repository_root: Path,
        branch: str,
    ) -> str:
        """Resolve one exact local branch ref without accepting revision syntax."""

        root = self._validate_worktree(repository_root)
        if not isinstance(branch, str) or not branch or branch.startswith("-"):
            self._raise_branch_invalid(branch)
        reference = f"refs/heads/{branch}"
        checked = self._git(root, "check-ref-format", reference, check=False)
        if checked.returncode != 0:
            self._raise_branch_invalid(branch)
        resolved = self._git(
            root,
            "show-ref",
            "--verify",
            "--hash",
            reference,
            check=False,
        )
        if resolved.returncode in {1, 128}:
            raise RCPError(
                code="write_scope_protected_branch_not_found",
                message="Protected default branch was not found in Git.",
                context={"branch": branch},
            )
        if resolved.returncode != 0:
            self._raise_git_failed(resolved, "resolve protected branch")
        return self._parse_object_id(resolved.stdout, label="protected branch")

    def load_protected_task(
        self,
        *,
        repository_root: Path,
        protected_commit: str,
        task_id: str,
    ) -> TaskRecord:
        """Load one canonical Task directly from an exact protected commit."""

        commit = self._require_object_id(protected_commit, "protected_commit")
        if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
            raise RCPError(
                code="write_scope_task_identity_invalid",
                message="Protected Task lookup requires a canonical Task ID.",
            )
        path = f".research/tasks/{task_id}.yaml"
        content = self._objects.read_blob_at(
            repository_root,
            commit=commit,
            path=path,
            required=False,
        )
        if content is None:
            raise RCPError(
                code="write_scope_task_missing",
                message="Canonical Task is missing from the protected default branch.",
                context={"commit": commit, "path": path},
            )
        try:
            task = TaskRecord.model_validate(load_yaml(content.decode("utf-8")))
        except (UnicodeError, TypeError, ValueError) as error:
            raise RCPError(
                code="write_scope_task_invalid",
                message="Protected default branch contains a malformed Task.",
                context={"commit": commit, "path": path},
            ) from error
        if task.task_id != task_id:
            raise RCPError(
                code="write_scope_task_identity_mismatch",
                message="Protected Task identity does not match its canonical path.",
                context={
                    "expected_task_id": task_id,
                    "observed_task_id": task.task_id,
                },
            )
        if dump_yaml(task).encode("utf-8") != content:
            raise RCPError(
                code="write_scope_task_not_canonical",
                message="Protected Task must use canonical protocol YAML.",
                context={"commit": commit, "path": path},
            )
        return task

    def validate_source(
        self,
        *,
        task: TaskRecord,
        repository_root: Path,
        trusted_base_commit: str,
        source_commit: str,
        baseline_commit: str | None = None,
    ) -> SourceWriteScopeReceipt:
        """Validate one immutable Session source snapshot against a trusted base."""

        trusted_base = self._require_object_id(
            trusted_base_commit,
            "trusted_base_commit",
        )
        source = self._require_object_id(source_commit, "source_commit")
        baseline = self._require_object_id(
            baseline_commit if baseline_commit is not None else trusted_base,
            "baseline_commit",
        )
        self._objects.read_commit(repository_root, trusted_base)
        self._objects.read_commit(repository_root, baseline)
        self._objects.read_commit(repository_root, source)
        if not self._objects.is_ancestor(
            repository_root,
            ancestor=baseline,
            descendant=trusted_base,
        ):
            raise RCPError(
                code="write_scope_baseline_untrusted",
                message="Run source baseline is not protected default-branch history.",
                remediation=(
                    "Sync the Session to a protected baseline and create a new RunSpec."
                ),
                context={
                    "task_id": task.task_id,
                    "baseline_commit": baseline,
                    "trusted_base_commit": trusted_base,
                },
            )
        if not self._objects.is_ancestor(
            repository_root,
            ancestor=baseline,
            descendant=source,
        ):
            raise RCPError(
                code="write_scope_source_lineage_invalid",
                message="Run source commit does not descend from its declared baseline.",
                remediation=(
                    "Create the RunSpec from the bound Session branch and baseline."
                ),
                context={
                    "task_id": task.task_id,
                    "baseline_commit": baseline,
                    "source_commit": source,
                },
            )
        changes = self._objects.changes(
            repository_root,
            old_commit=baseline,
            new_commit=source,
        )
        paths = self.validate_changes(task=task, changes=changes)
        return SourceWriteScopeReceipt(
            trusted_base_commit=trusted_base,
            baseline_commit=baseline,
            source_commit=source,
            paths=paths,
        )

    @staticmethod
    def validate_changes(
        *,
        task: TaskRecord,
        changes: tuple[GitScopeChange, ...],
    ) -> tuple[str, ...]:
        violations: list[dict[str, str]] = []
        for entry in changes:
            if not _is_canonical_changed_path(entry.path):
                violations.append(
                    {
                        "path": entry.path,
                        "reason": "path_invalid",
                        "status": entry.status,
                    }
                )
                continue
            unsafe_kind = _UNSAFE_MODES.get(entry.old_mode) or _UNSAFE_MODES.get(
                entry.new_mode
            )
            if unsafe_kind is not None:
                violations.append(
                    {
                        "path": entry.path,
                        "reason": f"{unsafe_kind}_change_forbidden",
                        "status": entry.status,
                    }
                )
            elif not _is_safe_regular_file_change(entry):
                violations.append(
                    {
                        "path": entry.path,
                        "reason": "file_mode_or_operation_forbidden",
                        "status": entry.status,
                    }
                )
            elif not task.permits_write_path(entry.path):
                violations.append(
                    {
                        "path": entry.path,
                        "reason": "outside_allowed_write_paths",
                        "status": entry.status,
                    }
                )
        if violations:
            raise RCPError(
                code="write_scope_violation",
                message="Git proposal contains changes outside the Task write scope.",
                remediation="Remove the listed changes or update the Task through manager review.",
                context={
                    "task_id": task.task_id,
                    "allowed_write_paths": list(task.allowed_write_paths),
                    "violations": violations,
                },
            )
        return tuple(entry.path for entry in changes)

    def _validate_worktree(self, worktree: Path) -> Path:
        candidate = worktree.expanduser()
        if candidate.is_symlink():
            self._raise_worktree_invalid(candidate)
        root = candidate.resolve()
        if not root.is_dir():
            self._raise_worktree_invalid(root)
        result = self._git(root, "rev-parse", "--show-toplevel")
        output = result.stdout.removesuffix("\n")
        if "\n" in output or not output:
            raise RCPError(
                code="git_output_invalid",
                message="Git returned an invalid worktree root.",
            )
        observed = Path(output).resolve()
        if observed != root:
            raise RCPError(
                code="write_scope_worktree_mismatch",
                message="Write-scope validation must run at the exact Git worktree root.",
                context={"expected_root": str(root), "observed_root": str(observed)},
            )
        return root

    @staticmethod
    def _raise_worktree_invalid(path: Path) -> None:
        raise RCPError(
            code="write_scope_worktree_invalid",
            message="Write-scope worktree must be an existing non-symlink directory.",
            context={"worktree": str(path)},
        )

    @staticmethod
    def _require_object_id(value: str | None, field: str) -> str:
        if value is None or not _GIT_OBJECT_ID.fullmatch(value):
            raise RCPError(
                code="git_revision_invalid",
                message="Write-scope revisions must be full lowercase Git object IDs.",
                context={"field": field},
            )
        return value

    def _resolve_head(self, root: Path) -> str:
        result = self._git(root, "rev-parse", "--verify", "HEAD^{commit}")
        return self._parse_object_id(result.stdout, label="HEAD")

    def _current_branch(self, root: Path) -> str:
        result = self._git(
            root,
            "symbolic-ref",
            "--quiet",
            "--short",
            "HEAD",
            check=False,
        )
        if result.returncode == 1:
            raise RCPError(
                code="write_scope_worktree_mismatch",
                message="Proposal worktree must have a checked-out branch tip.",
                context={"observed_branch": None},
            )
        if result.returncode != 0:
            self._raise_git_failed(result, "resolve worktree branch")
        branch = result.stdout.removesuffix("\n")
        if not branch or "\n" in branch or branch.startswith("-"):
            raise RCPError(
                code="git_output_invalid",
                message="Git returned an invalid worktree branch.",
            )
        return branch

    def _diff(self, root: Path, base: str, head: str) -> tuple[GitDiffEntry, ...]:
        result = self._git(
            root,
            "diff",
            "--raw",
            "-z",
            "--no-abbrev",
            "--no-renames",
            "--no-ext-diff",
            base,
            head,
            "--",
        )
        fields = result.stdout.split("\0")
        if fields[-1:] == [""]:
            fields.pop()
        if len(fields) % 2 != 0:
            self._raise_raw_diff_invalid()
        entries: list[GitDiffEntry] = []
        for index in range(0, len(fields), 2):
            header = fields[index]
            path = fields[index + 1]
            match = _RAW_HEADER.fullmatch(header)
            if match is None or not path:
                self._raise_raw_diff_invalid()
            entries.append(
                GitDiffEntry(
                    path=path,
                    status=match.group("status"),
                    old_mode=match.group("old_mode"),
                    new_mode=match.group("new_mode"),
                )
            )
        return tuple(entries)

    @staticmethod
    def _raise_raw_diff_invalid() -> None:
        raise RCPError(
            code="git_output_invalid",
            message="Git returned a malformed NUL-delimited raw diff.",
        )

    @staticmethod
    def _parse_object_id(output: str, *, label: str) -> str:
        object_id = output.removesuffix("\n")
        if not _GIT_OBJECT_ID.fullmatch(object_id):
            raise RCPError(
                code="git_output_invalid",
                message=f"Git returned an invalid {label} object ID.",
            )
        return object_id

    @staticmethod
    def _raise_branch_invalid(branch: object) -> None:
        raise RCPError(
            code="write_scope_protected_branch_invalid",
            message="Protected default branch is not a valid full Git branch reference.",
            context={"branch": branch},
        )

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
                message="Git write-scope validation timed out.",
                context={"worktree": str(root)},
            ) from error
        if check and result.returncode != 0:
            self._raise_git_failed(
                result,
                args[0] if args else "validate write scope",
            )
        return result

    @staticmethod
    def _raise_git_failed(result: CommandResult, operation: str) -> None:
        raise RCPError(
            code="git_write_scope_command_failed",
            message=f"Git failed to {operation}.",
            context={"returncode": result.returncode},
        )


def _is_canonical_changed_path(path: str) -> bool:
    if (
        not isinstance(path, str)
        or not path
        or path.startswith("/")
        or "\\" in path
        or "\x00" in path
        or "\n" in path
        or "\r" in path
    ):
        return False
    return all(part not in {"", ".", ".."} for part in path.split("/"))


def _is_safe_regular_file_change(change: GitScopeChange) -> bool:
    if change.status == "A":
        return change.old_mode == "000000" and change.new_mode in _REGULAR_FILE_MODES
    if change.status == "D":
        return change.old_mode in _REGULAR_FILE_MODES and change.new_mode == "000000"
    if change.status == "M":
        return (
            change.old_mode == change.new_mode
            and change.old_mode in _REGULAR_FILE_MODES
        )
    return False


def _git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for key in tuple(environment):
        if key in _GIT_ENVIRONMENT_KEYS or key.startswith("GIT_CONFIG_"):
            environment.pop(key, None)
    environment.pop("GIT_CONFIG_PARAMETERS", None)
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    return environment
