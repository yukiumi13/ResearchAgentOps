from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from researchctl.adapters._subprocess import (
    CommandResult,
    CommandRunner,
    SubprocessCommandRunner,
)
from researchctl.errors import RCPError


_GIT_CONTEXT_ENVIRONMENT = {
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_WORK_TREE",
}
_GIT_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class WorktreeObservationState(StrEnum):
    ABSENT = "absent"
    EXACT = "exact"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class WorktreeSpec:
    root: Path
    base_commit: str
    branch: str
    worktree: Path


@dataclass(frozen=True, slots=True)
class WorktreeObservation:
    state: WorktreeObservationState
    branch_commit: str | None = None
    worktree_commit: str | None = None
    observed_branch: str | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class _WorktreeEntry:
    path: Path
    head: str | None
    branch: str | None


class GitWorktreeAdapter:
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

    def observe(self, spec: WorktreeSpec) -> WorktreeObservation:
        normalized = self._validate_spec(spec)
        self._validate_branch(normalized)

        base_result = self._git(
            normalized.root,
            "rev-parse",
            "--verify",
            f"{normalized.base_commit}^{{commit}}",
            check=False,
        )
        if base_result.returncode != 0:
            raise RCPError(
                code="git_base_commit_not_found",
                message="The requested base commit does not exist in the repository.",
                context={"base_commit": normalized.base_commit},
            )
        expected_commit = base_result.stdout.strip()
        if not _GIT_OBJECT_ID.fullmatch(expected_commit):
            raise RCPError(
                code="git_output_invalid",
                message="Git returned an invalid base commit object ID.",
            )

        ref = f"refs/heads/{normalized.branch}"
        branch_result = self._git(
            normalized.root,
            "rev-parse",
            "--verify",
            "--quiet",
            f"{ref}^{{commit}}",
            check=False,
        )
        if branch_result.returncode not in {0, 1}:
            self._raise_command_failed(branch_result, operation="observe branch")
        branch_commit = (
            branch_result.stdout.strip() if branch_result.returncode == 0 else None
        )
        if branch_commit is not None and not _GIT_OBJECT_ID.fullmatch(branch_commit):
            raise RCPError(
                code="git_output_invalid",
                message="Git returned an invalid branch object ID.",
            )

        listed = self._git(
            normalized.root,
            "worktree",
            "list",
            "--porcelain",
            "-z",
        )
        entries = self._parse_worktree_list(listed.stdout)
        target = self._normalized_path(normalized.worktree)
        target_entries = [
            entry for entry in entries if self._normalized_path(entry.path) == target
        ]
        branch_entries = [entry for entry in entries if entry.branch == ref]

        path_exists = os.path.lexists(normalized.worktree)
        if (
            branch_commit is None
            and not target_entries
            and not branch_entries
            and not path_exists
        ):
            return WorktreeObservation(state=WorktreeObservationState.ABSENT)

        if (
            branch_commit == expected_commit
            and len(target_entries) == 1
            and len(branch_entries) == 1
            and target_entries[0] == branch_entries[0]
            and target_entries[0].branch == ref
            and target_entries[0].head == expected_commit
            and path_exists
        ):
            return WorktreeObservation(
                state=WorktreeObservationState.EXACT,
                branch_commit=branch_commit,
                worktree_commit=target_entries[0].head,
                observed_branch=target_entries[0].branch,
            )

        target_entry = target_entries[0] if len(target_entries) == 1 else None
        return WorktreeObservation(
            state=WorktreeObservationState.CONFLICT,
            branch_commit=branch_commit,
            worktree_commit=target_entry.head if target_entry else None,
            observed_branch=target_entry.branch if target_entry else None,
            reason=self._conflict_reason(
                expected_commit=expected_commit,
                branch_commit=branch_commit,
                target_entries=target_entries,
                branch_entries=branch_entries,
                path_exists=path_exists,
            ),
        )

    def create_or_observe(self, spec: WorktreeSpec) -> WorktreeObservation:
        normalized = self._validate_spec(spec)
        observation = self.observe(normalized)
        if observation.state is WorktreeObservationState.EXACT:
            return observation
        if observation.state is WorktreeObservationState.CONFLICT:
            self._raise_conflict(normalized, observation)

        created = self._git(
            normalized.root,
            "worktree",
            "add",
            "-b",
            normalized.branch,
            "--",
            str(normalized.worktree),
            normalized.base_commit,
            check=False,
        )
        if created.returncode != 0:
            raced = self.observe(normalized)
            if raced.state is WorktreeObservationState.EXACT:
                return raced
            if raced.state is WorktreeObservationState.CONFLICT:
                self._raise_conflict(normalized, raced)
            self._raise_command_failed(created, operation="create worktree")

        final = self.observe(normalized)
        if final.state is not WorktreeObservationState.EXACT:
            self._raise_conflict(normalized, final)
        return final

    def resolve_commit(self, root: Path, revision: str) -> str:
        normalized_root = self._validate_existing_directory(
            root,
            code="git_repository_path_invalid",
            label="Repository root",
            context_key="root",
        )
        validated_revision = self._validate_revision(normalized_root, revision)
        result = self._git(
            normalized_root,
            "rev-parse",
            "--verify",
            "--quiet",
            f"{validated_revision}^{{commit}}",
            check=False,
        )
        if result.returncode == 1:
            raise RCPError(
                code="git_revision_not_found",
                message="The requested Git revision does not resolve to a commit.",
            )
        if result.returncode != 0:
            self._raise_command_failed(result, operation="resolve revision")
        return self._parse_object_id(
            result.stdout,
            message="Git returned an invalid resolved commit object ID.",
        )

    def worktree_head(self, worktree: Path) -> str:
        normalized_worktree = self._validate_existing_directory(
            worktree,
            code="git_worktree_path_invalid",
            label="Worktree path",
            context_key="worktree",
        )
        result = self._git(
            normalized_worktree,
            "rev-parse",
            "--verify",
            "--quiet",
            "HEAD^{commit}",
            check=False,
        )
        if result.returncode == 1:
            raise RCPError(
                code="git_worktree_head_not_found",
                message="The worktree HEAD does not resolve to a commit.",
                context={"worktree": str(normalized_worktree)},
            )
        if result.returncode != 0:
            self._raise_command_failed(result, operation="resolve worktree HEAD")
        return self._parse_object_id(
            result.stdout,
            message="Git returned an invalid worktree HEAD object ID.",
        )

    def _validate_revision(self, root: Path, revision: str) -> str:
        if not isinstance(revision, str) or any(
            character in revision for character in ("\x00", "\r", "\n")
        ):
            self._raise_revision_invalid()
        if _GIT_OBJECT_ID.fullmatch(revision):
            return revision
        prefix = "refs/heads/"
        if not revision.startswith(prefix):
            self._raise_revision_invalid()
        branch = revision.removeprefix(prefix)
        if not branch or branch.startswith("-"):
            self._raise_revision_invalid()
        checked = self._git(
            root,
            "check-ref-format",
            revision,
            check=False,
        )
        if checked.returncode == 1:
            self._raise_revision_invalid()
        if checked.returncode != 0:
            self._raise_command_failed(checked, operation="validate revision")
        return revision

    @staticmethod
    def _raise_revision_invalid() -> None:
        raise RCPError(
            code="git_revision_invalid",
            message=(
                "Git revision must be a full lowercase object ID or a valid "
                "refs/heads reference."
            ),
        )

    @classmethod
    def _validate_existing_directory(
        cls,
        path: Path,
        *,
        code: str,
        label: str,
        context_key: str,
    ) -> Path:
        normalized = cls._normalized_path(path)
        if normalized.is_symlink() or not normalized.is_dir():
            raise RCPError(
                code=code,
                message=f"{label} must be an existing non-symlink directory.",
                context={context_key: str(normalized)},
            )
        return normalized

    @staticmethod
    def _parse_object_id(output: str, *, message: str) -> str:
        object_id = output.removesuffix("\n")
        if not _GIT_OBJECT_ID.fullmatch(object_id):
            raise RCPError(code="git_output_invalid", message=message)
        return object_id

    def _validate_spec(self, spec: WorktreeSpec) -> WorktreeSpec:
        root = self._normalized_path(spec.root)
        worktree = self._normalized_path(spec.worktree)
        if not root.is_absolute() or not worktree.is_absolute():
            raise RCPError(
                code="git_worktree_path_invalid",
                message="Repository root and worktree path must be absolute.",
            )
        if root.is_symlink() or not root.is_dir():
            raise RCPError(
                code="git_repository_path_invalid",
                message="Repository root must be an existing non-symlink directory.",
                context={"root": str(root)},
            )
        if worktree.is_symlink():
            raise RCPError(
                code="git_worktree_path_symlink",
                message="Worktree path must not be a symbolic link.",
                context={"worktree": str(worktree)},
            )
        if not worktree.parent.is_dir() or worktree.parent.is_symlink():
            raise RCPError(
                code="git_worktree_parent_invalid",
                message="Worktree parent must be an existing non-symlink directory.",
                context={"worktree": str(worktree)},
            )
        if not _GIT_OBJECT_ID.fullmatch(spec.base_commit):
            raise RCPError(
                code="git_base_commit_invalid",
                message="Base commit must be a full lowercase Git object ID.",
            )
        if (
            not isinstance(spec.branch, str)
            or not spec.branch
            or "\x00" in spec.branch
        ):
            raise RCPError(
                code="git_worktree_branch_invalid",
                message="Worktree branch is invalid.",
            )
        return WorktreeSpec(
            root=root,
            base_commit=spec.base_commit,
            branch=spec.branch,
            worktree=worktree,
        )

    def _validate_branch(self, spec: WorktreeSpec) -> None:
        result = self._git(
            spec.root,
            "check-ref-format",
            "--branch",
            spec.branch,
            check=False,
        )
        if result.returncode != 0:
            raise RCPError(
                code="git_worktree_branch_invalid",
                message="Worktree branch is not a valid Git branch name.",
                context={"branch": spec.branch},
            )

    def _git(
        self,
        root: Path,
        *args: str,
        check: bool = True,
    ) -> CommandResult:
        argv = (
            "git",
            "-c",
            "core.fsmonitor=false",
            "-C",
            str(root),
            *args,
        )
        try:
            result = self._runner.run(
                argv,
                cwd=None,
                env=self._git_environment(),
                timeout_seconds=self._timeout_seconds,
            )
        except FileNotFoundError as exc:
            raise RCPError(
                code="git_not_found",
                message="git executable was not found.",
                remediation="Install Git and ensure it is available on PATH.",
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise RCPError(
                code="git_timeout",
                message=(
                    f"Git command timed out after "
                    f"{self._timeout_seconds:g} seconds."
                ),
                context={"root": str(root)},
            ) from exc
        if check and result.returncode != 0:
            self._raise_command_failed(result, operation=args[0] if args else "git")
        return result

    @staticmethod
    def _git_environment() -> dict[str, str]:
        environment = os.environ.copy()
        for key in tuple(environment):
            if key in _GIT_CONTEXT_ENVIRONMENT or key.startswith("GIT_CONFIG_"):
                environment.pop(key, None)
        environment.pop("GIT_CONFIG_PARAMETERS", None)
        return environment

    @staticmethod
    def _normalized_path(path: Path) -> Path:
        return Path(os.path.abspath(os.fspath(path)))

    @staticmethod
    def _parse_worktree_list(output: str) -> tuple[_WorktreeEntry, ...]:
        entries: list[_WorktreeEntry] = []
        current: dict[str, str] = {}
        valued_fields = {"HEAD", "branch", "worktree"}
        optional_value_fields = {"locked", "prunable"}
        flag_fields = {"bare", "detached"}
        for field in output.split("\x00"):
            if not field:
                if current:
                    entries.append(GitWorktreeAdapter._entry_from_fields(current))
                    current = {}
                continue
            key, separator, value = field.partition(" ")
            valid = (
                (key in valued_fields and bool(separator))
                or key in optional_value_fields
                or (key in flag_fields and not separator)
            )
            if not valid:
                raise RCPError(
                    code="git_worktree_output_invalid",
                    message="Git returned malformed worktree metadata.",
                )
            if key == "worktree" and current:
                entries.append(GitWorktreeAdapter._entry_from_fields(current))
                current = {}
            if key in current:
                raise RCPError(
                    code="git_worktree_output_invalid",
                    message="Git returned duplicate worktree metadata.",
                )
            current[key] = value
        if current:
            entries.append(GitWorktreeAdapter._entry_from_fields(current))
        return tuple(entries)

    @staticmethod
    def _entry_from_fields(fields: dict[str, str]) -> _WorktreeEntry:
        path = fields.get("worktree")
        if not path:
            raise RCPError(
                code="git_worktree_output_invalid",
                message="Git worktree metadata did not include a path.",
            )
        head = fields.get("HEAD")
        if head is not None and not _GIT_OBJECT_ID.fullmatch(head):
            raise RCPError(
                code="git_worktree_output_invalid",
                message="Git worktree metadata included an invalid object ID.",
            )
        return _WorktreeEntry(
            path=Path(path),
            head=head,
            branch=fields.get("branch"),
        )

    @staticmethod
    def _conflict_reason(
        *,
        expected_commit: str,
        branch_commit: str | None,
        target_entries: list[_WorktreeEntry],
        branch_entries: list[_WorktreeEntry],
        path_exists: bool,
    ) -> str:
        if len(target_entries) > 1:
            return "multiple Git worktrees claim the requested path"
        if len(branch_entries) > 1:
            return "multiple Git worktrees claim the requested branch"
        if target_entries and target_entries[0].branch not in {
            entry.branch for entry in branch_entries
        }:
            return "requested path is registered to another branch"
        if branch_entries and not target_entries:
            return "requested branch is checked out in another worktree"
        if branch_commit is not None and branch_commit != expected_commit:
            return "requested branch does not point to the base commit"
        if target_entries and target_entries[0].head != expected_commit:
            return "requested worktree does not point to the base commit"
        if path_exists and not target_entries:
            return "requested path exists but is not a registered worktree"
        if branch_commit is None:
            return "requested worktree exists without the requested branch"
        return "requested branch and worktree do not match the declared identity"

    @staticmethod
    def _raise_command_failed(result: CommandResult, *, operation: str) -> None:
        raise RCPError(
            code="git_command_failed",
            message=f"Git failed to {operation}.",
            context={"returncode": result.returncode},
        )

    @staticmethod
    def _raise_conflict(
        spec: WorktreeSpec,
        observation: WorktreeObservation,
    ) -> None:
        raise RCPError(
            code="git_worktree_conflict",
            message="Existing Git state conflicts with the requested worktree.",
            context={
                "branch": spec.branch,
                "worktree": str(spec.worktree),
                "reason": observation.reason,
            },
        )
