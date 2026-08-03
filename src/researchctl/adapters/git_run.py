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
from researchctl.errors import RCPError


_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_RUN_ID = re.compile(r"^run_\d{8}T\d{6}Z_[0-9a-f]{24}$")
_RECORD_PATH = re.compile(
    r"^\.research/runs/(run_\d{8}T\d{6}Z_[0-9a-f]{24})/"
    r"(spec|result)\.yaml$"
)
_GIT_ENVIRONMENT_KEYS = {
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_WORK_TREE",
}


@dataclass(frozen=True, slots=True)
class RunRecordCommitReceipt:
    commit: str
    changed: bool
    effect_applied: bool


class GitRunAdapter:
    """Git mutation port for immutable local Run metadata and execution trees."""

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

    def validate_source(
        self,
        repository_root: Path,
        *,
        source_commit: str,
        source_tree: str,
    ) -> None:
        root = self._directory(repository_root, "run_repository_invalid")
        commit = self._object_id(source_commit, "source_commit")
        tree = self._object_id(source_tree, "source_tree")
        observed_commit = self._git(
            root,
            "rev-parse",
            "--verify",
            f"{commit}^{{commit}}",
        ).stdout.strip()
        observed_tree = self._git(
            root,
            "rev-parse",
            "--verify",
            f"{commit}^{{tree}}",
        ).stdout.strip()
        if observed_commit != commit or observed_tree != tree:
            raise RCPError(
                code="run_source_mismatch",
                message="RunSpec source commit or tree does not match Git.",
                context={
                    "source_commit": commit,
                    "expected_tree": tree,
                    "observed_tree": observed_tree,
                },
            )

    def create_or_observe_execution_worktree(
        self,
        *,
        repository_root: Path,
        execution_worktree: Path,
        source_commit: str,
    ) -> bool:
        root = self._directory(repository_root, "run_repository_invalid")
        commit = self._object_id(source_commit, "source_commit")
        candidate = Path(os.path.abspath(os.fspath(execution_worktree)))
        if candidate.is_symlink():
            self._raise_execution_worktree(candidate)
        if not candidate.exists():
            if not candidate.parent.is_dir() or candidate.parent.is_symlink():
                self._raise_execution_worktree(candidate)
            self._git(
                root,
                "worktree",
                "add",
                "--detach",
                "--",
                str(candidate),
                commit,
            )
            changed = True
        else:
            if not candidate.is_dir():
                self._raise_execution_worktree(candidate)
            changed = False
        head = self._git(
            candidate,
            "rev-parse",
            "--verify",
            "HEAD^{commit}",
        ).stdout.strip()
        if head != commit:
            raise RCPError(
                code="run_execution_worktree_conflict",
                message="Execution worktree is not pinned to RunSpec source_commit.",
                context={"expected_commit": commit, "observed_commit": head},
            )
        symbolic = self._git(
            candidate,
            "symbolic-ref",
            "--quiet",
            "HEAD",
            check=False,
        )
        if symbolic.returncode != 1:
            raise RCPError(
                code="run_execution_worktree_conflict",
                message="Execution worktree must use detached HEAD.",
            )
        self._require_same_common_directory(root, candidate)
        return changed

    def commit_spec_or_observe(
        self,
        *,
        worktree: Path,
        branch: str,
        record_path: Path,
        run_id: str,
        spec_digest: str,
        source_commit: str,
    ) -> RunRecordCommitReceipt:
        self._validate_identity(branch=branch, run_id=run_id)
        root, relative = self._record(worktree, record_path, run_id, "spec")
        marker = f"researchctl: freeze run {run_id} {spec_digest}"
        tag = f"research-run/{run_id}"
        tagged = self._resolve_ref(root, f"refs/tags/{tag}")
        if tagged is not None:
            self._verify_spec_commit(
                root,
                commit=tagged,
                relative=relative,
                marker=marker,
                record_path=record_path,
                source_commit=source_commit,
            )
            self._require_ancestor(root, tagged, "HEAD")
            return RunRecordCommitReceipt(tagged, False, True)

        changed = self._commit_path(
            root,
            relative=relative,
            marker=marker,
        )
        head = self._head(root)
        effect = self._matches_commit(
            root,
            commit=head,
            relative=relative,
            marker=marker,
            record_path=record_path,
        )
        if not effect:
            raise RCPError(
                code="run_spec_commit_invalid",
                message="Run branch HEAD is not the deterministic RunSpec commit.",
            )
        self._verify_parent(root, head, source_commit)
        created_tag = self._git(
            root,
            "tag",
            tag,
            head,
            check=False,
        )
        if created_tag.returncode != 0:
            observed = self._resolve_ref(root, f"refs/tags/{tag}")
            if observed != head:
                raise RCPError(
                    code="run_tag_conflict",
                    message="Immutable research-run tag points to another commit.",
                )
        return RunRecordCommitReceipt(head, changed, True)

    def commit_result_or_observe(
        self,
        *,
        worktree: Path,
        branch: str,
        record_path: Path,
        run_id: str,
        result_id: str,
        spec_commit: str,
    ) -> RunRecordCommitReceipt:
        self._validate_identity(branch=branch, run_id=run_id)
        root, relative = self._record(worktree, record_path, run_id, "result")
        marker = f"researchctl: collect run {run_id} {result_id}"
        changed = self._commit_path(root, relative=relative, marker=marker)
        head = self._head(root)
        effect = self._matches_commit(
            root,
            commit=head,
            relative=relative,
            marker=marker,
            record_path=record_path,
        )
        if not effect:
            raise RCPError(
                code="run_result_commit_invalid",
                message="Run branch HEAD is not the deterministic RunResult commit.",
            )
        self._verify_parent(root, head, spec_commit)
        return RunRecordCommitReceipt(head, changed, True)

    def resolve_tag(self, repository_root: Path, run_id: str) -> str | None:
        if not _RUN_ID.fullmatch(run_id):
            self._raise_run_identity()
        root = self._directory(repository_root, "run_repository_invalid")
        return self._resolve_ref(root, f"refs/tags/research-run/{run_id}")

    def _record(
        self,
        worktree: Path,
        record_path: Path,
        run_id: str,
        kind: str,
    ) -> tuple[Path, str]:
        root = self._directory(worktree, "run_metadata_worktree_invalid")
        candidate = Path(os.path.abspath(os.fspath(record_path)))
        try:
            relative = candidate.relative_to(root).as_posix()
        except ValueError as error:
            raise RCPError(
                code="run_record_path_invalid",
                message="Run record is outside its metadata worktree.",
            ) from error
        match = _RECORD_PATH.fullmatch(relative)
        if match is None or match.group(1) != run_id or match.group(2) != kind:
            raise RCPError(
                code="run_record_path_invalid",
                message="Run record path does not match its canonical identity.",
            )
        current = root
        for part in PurePosixPath(relative).parts:
            current = current / part
            if current.is_symlink():
                raise RCPError(
                    code="run_record_path_invalid",
                    message="Run record path must not traverse a symbolic link.",
                )
        if not candidate.is_file():
            raise RCPError(
                code="run_record_path_invalid",
                message="Run record must be an existing regular file.",
            )
        return root, relative

    def _commit_path(self, root: Path, *, relative: str, marker: str) -> bool:
        status = self._git(
            root,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        )
        lines = status.stdout.splitlines()
        if any(len(line) < 4 or line[3:] != relative for line in lines):
            raise RCPError(
                code="run_metadata_worktree_dirty",
                message="Run metadata worktree contains an unexpected change.",
            )
        if not lines:
            return False
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
        if staged.returncode == 0:
            return False
        if staged.returncode != 1:
            self._raise_failed(staged, "inspect staged Run record")
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
            marker,
            "--",
            relative,
        )
        return True

    def _verify_spec_commit(
        self,
        root: Path,
        *,
        commit: str,
        relative: str,
        marker: str,
        record_path: Path,
        source_commit: str,
    ) -> None:
        if not self._matches_commit(
            root,
            commit=commit,
            relative=relative,
            marker=marker,
            record_path=record_path,
        ):
            raise RCPError(
                code="run_tag_conflict",
                message="Immutable research-run tag does not identify this RunSpec.",
            )
        self._verify_parent(root, commit, source_commit)

    def _matches_commit(
        self,
        root: Path,
        *,
        commit: str,
        relative: str,
        marker: str,
        record_path: Path,
    ) -> bool:
        message = self._git(root, "show", "-s", "--format=%B", commit)
        if message.stdout.rstrip("\n") != marker:
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
                code="run_record_commit_invalid",
                message="Run record commit changed an unexpected path.",
            )
        blob = self._git(root, "show", f"{commit}:{relative}")
        try:
            content = record_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise RCPError(
                code="run_record_path_invalid",
                message="Run record must be readable canonical UTF-8.",
            ) from error
        if blob.stdout != content:
            raise RCPError(
                code="run_record_commit_invalid",
                message="Committed Run record differs from its worktree content.",
            )
        return True

    def _validate_identity(self, *, branch: str, run_id: str) -> None:
        if not _RUN_ID.fullmatch(run_id) or branch != f"research/run/{run_id}":
            self._raise_run_identity()

    @staticmethod
    def _raise_run_identity() -> None:
        raise RCPError(
            code="run_identity_invalid",
            message="Run branch requires a canonical Run ID.",
        )

    def _verify_parent(self, root: Path, commit: str, parent: str) -> None:
        expected = self._object_id(parent, "parent")
        observed = self._git(root, "rev-parse", f"{commit}^{{commit}}^").stdout.strip()
        if observed != expected:
            raise RCPError(
                code="run_record_parent_mismatch",
                message="Run record commit has an unexpected parent.",
                context={"expected_parent": expected, "observed_parent": observed},
            )

    def _require_ancestor(self, root: Path, ancestor: str, descendant: str) -> None:
        result = self._git(
            root,
            "merge-base",
            "--is-ancestor",
            ancestor,
            descendant,
            check=False,
        )
        if result.returncode != 0:
            raise RCPError(
                code="run_branch_history_invalid",
                message="Run branch no longer contains its immutable RunSpec commit.",
            )

    def _require_same_common_directory(self, root: Path, worktree: Path) -> None:
        expected = self._common_directory(root)
        observed = self._common_directory(worktree)
        if observed != expected:
            raise RCPError(
                code="run_execution_worktree_conflict",
                message="Execution directory is not a worktree of the expected repository.",
            )

    def _common_directory(self, root: Path) -> Path:
        output = self._git(root, "rev-parse", "--git-common-dir").stdout.strip()
        candidate = Path(output)
        if not candidate.is_absolute():
            candidate = root / candidate
        return candidate.resolve()

    def _resolve_ref(self, root: Path, reference: str) -> str | None:
        result = self._git(
            root,
            "rev-parse",
            "--verify",
            "--quiet",
            f"{reference}^{{commit}}",
            check=False,
        )
        if result.returncode == 1:
            return None
        if result.returncode != 0:
            self._raise_failed(result, "resolve Run ref")
        return self._parse_object_id(result.stdout)

    def _head(self, root: Path) -> str:
        return self._parse_object_id(
            self._git(root, "rev-parse", "--verify", "HEAD^{commit}").stdout
        )

    @staticmethod
    def _parse_object_id(output: str) -> str:
        value = output.strip()
        if not _OBJECT_ID.fullmatch(value):
            raise RCPError(
                code="git_output_invalid",
                message="Git returned an invalid object ID.",
            )
        return value

    @staticmethod
    def _object_id(value: str, field: str) -> str:
        if not _OBJECT_ID.fullmatch(value):
            raise RCPError(
                code="git_revision_invalid",
                message="Run Git revisions must be full lowercase object IDs.",
                context={"field": field},
            )
        return value

    @staticmethod
    def _directory(path: Path, code: str) -> Path:
        candidate = Path(os.path.abspath(os.fspath(path)))
        if candidate.is_symlink() or not candidate.is_dir():
            raise RCPError(
                code=code,
                message="Run Git directory must be an existing non-symlink directory.",
                context={"path": str(candidate)},
            )
        return candidate

    @staticmethod
    def _raise_execution_worktree(path: Path) -> None:
        raise RCPError(
            code="run_execution_worktree_invalid",
            message="Execution worktree path is missing, unsafe, or occupied.",
            context={"path": str(path)},
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
                message="Git Run operation timed out.",
                context={"root": str(root)},
            ) from error
        if check and result.returncode != 0:
            self._raise_failed(result, args[0] if args else "run Git")
        return result

    @staticmethod
    def _raise_failed(result: CommandResult, operation: str) -> None:
        raise RCPError(
            code="git_run_command_failed",
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
