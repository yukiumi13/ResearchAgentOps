from __future__ import annotations

import fcntl
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from pydantic import ValidationError

from researchctl.adapters import GitWorktreeAdapter, WorktreeSpec
from researchctl.adapters.git_control import ControlCommitReceipt, GitControlCommitAdapter
from researchctl.constants import LINEAR_PROJECTION_POLICY_PATH
from researchctl.domain.models import LinearProjectionPolicy
from researchctl.domain.types import Sha256Digest
from researchctl.errors import RCPError
from researchctl.repository import safe_repository_path
from researchctl.serialization import (
    SerializationError,
    canonical_digest,
    dump_yaml,
    load_yaml,
)


@dataclass(frozen=True, slots=True)
class LinearPolicyWriteResult:
    policy: LinearProjectionPolicy
    base_commit: str
    previous_digest: Sha256Digest | None
    digest: Sha256Digest
    path: Path
    changed: bool
    proposal: ControlCommitReceipt


class ControlLinearPolicyRepository:
    """Prepare one canonical Linear policy proposal without contacting Linear."""

    def __init__(
        self,
        *,
        repository_root: Path,
        worktrees_directory: Path,
        default_branch: str,
        operation_id: str,
        expected_default_head: str,
        git: GitWorktreeAdapter | None = None,
        commits: GitControlCommitAdapter | None = None,
    ) -> None:
        branch = f"research/control/{operation_id}"
        GitControlCommitAdapter.validate_linear_identity(
            operation_id=operation_id,
            branch=branch,
        )
        root = Path(os.path.abspath(os.fspath(repository_root)))
        worktrees = Path(os.path.abspath(os.fspath(worktrees_directory)))
        if root.is_symlink() or not root.is_dir():
            raise RCPError(
                code="control_repository_invalid",
                message="Control repository must be an existing non-symlink directory.",
            )
        if worktrees.is_symlink() or not worktrees.is_dir():
            raise RCPError(
                code="control_worktrees_directory_invalid",
                message="Control worktree parent must be an existing non-symlink directory.",
            )
        if not isinstance(default_branch, str) or not default_branch:
            raise RCPError(
                code="control_default_branch_invalid",
                message="Linear policy control requires a default branch.",
            )

        self.repository_root = root
        self.worktrees_directory = worktrees
        self.default_branch = default_branch
        self.operation_id = operation_id
        self.expected_default_head = expected_default_head
        self.branch = branch
        self.worktree = worktrees / f"control-{operation_id}"
        self.git = git or GitWorktreeAdapter()
        self.commits = commits or GitControlCommitAdapter()

    def configure(self, policy: LinearProjectionPolicy) -> LinearPolicyWriteResult:
        desired = dump_yaml(policy).encode("utf-8")
        with self._exclusive():
            base = self.git.resolve_commit(
                self.repository_root,
                self.expected_default_head,
            )
            if base != self.expected_default_head:
                raise RCPError(
                    code="control_linear_base_invalid",
                    message="Linear policy base did not resolve exactly.",
                )
            previous_content = self.commits.linear_policy_content_at(
                repository_root=self.repository_root,
                commit=base,
            )
            previous = self._policy_or_none(previous_content)
            previous_digest = (
                canonical_digest(previous) if previous is not None else None
            )

            branch_head = self._branch_head_or_none()
            if branch_head is None:
                self._require_current_default()
                branch_head = base
            else:
                self.commits.validate_linear_operation_head(
                    repository_root=self.repository_root,
                    commit=branch_head,
                    operation_id=self.operation_id,
                    expected_base_commit=base,
                )
            self._prepare_worktree(branch_head)
            self.commits.require_linear_worktree_scope(
                worktree=self.worktree,
                branch=self.branch,
                operation_id=self.operation_id,
            )

            path = safe_repository_path(
                self.worktree,
                LINEAR_PROJECTION_POLICY_PATH,
                managed_only=True,
            )
            current_content = self._read_current(path)
            if current_content is not None:
                self._policy_or_none(current_content)
            changed = current_content != desired
            if changed:
                self._write_atomic(path, desired)

            proposal = self.commits.commit_linear_policy_or_observe(
                worktree=self.worktree,
                branch=self.branch,
                operation_id=self.operation_id,
                expected_base_commit=base,
            )
            return LinearPolicyWriteResult(
                policy=policy,
                base_commit=base,
                previous_digest=previous_digest,
                digest=canonical_digest(policy),
                path=path,
                changed=changed,
                proposal=proposal,
            )

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        lock = self.worktrees_directory / f".linear-configure-{self.operation_id}.lock"
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(lock, flags, 0o600)
        except OSError as error:
            raise RCPError(
                code="control_linear_lock_invalid",
                message="Linear policy control lock could not be opened safely.",
            ) from error
        try:
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _branch_head_or_none(self) -> str | None:
        try:
            return self.git.resolve_commit(
                self.repository_root,
                f"refs/heads/{self.branch}",
            )
        except RCPError as error:
            if error.code == "git_revision_not_found":
                return None
            raise

    def _require_current_default(self) -> None:
        observed = self.git.resolve_commit(
            self.repository_root,
            f"refs/heads/{self.default_branch}",
        )
        if observed != self.expected_default_head:
            raise RCPError(
                code="control_linear_default_head_changed",
                message="Default branch no longer matches the Linear policy base.",
                context={
                    "expected_head": self.expected_default_head,
                    "observed_head": observed,
                },
            )

    def _prepare_worktree(self, branch_head: str) -> None:
        spec = WorktreeSpec(
            root=self.repository_root,
            base_commit=branch_head,
            branch=self.branch,
            worktree=self.worktree,
        )
        try:
            self.git.create_or_observe(spec)
        except RCPError as error:
            branch_only = (
                error.code == "git_worktree_conflict"
                and not os.path.lexists(self.worktree)
            )
            if not branch_only:
                raise
            self.commits.attach_existing_linear_branch(
                repository_root=self.repository_root,
                worktree=self.worktree,
                branch=self.branch,
                expected_head=branch_head,
                operation_id=self.operation_id,
            )
            self.git.create_or_observe(spec)

    @staticmethod
    def _read_current(path: Path) -> bytes | None:
        if not os.path.lexists(path):
            return None
        if path.is_symlink() or not path.is_file():
            raise RCPError(
                code="control_linear_policy_path_invalid",
                message="Linear policy path must be a regular non-symlink file.",
            )
        try:
            return path.read_bytes()
        except OSError as error:
            raise RCPError(
                code="control_linear_policy_path_invalid",
                message="Linear policy could not be read.",
            ) from error

    @staticmethod
    def _policy_or_none(content: bytes | None) -> LinearProjectionPolicy | None:
        if content is None:
            return None
        try:
            policy = LinearProjectionPolicy.model_validate(
                load_yaml(content.decode("utf-8"))
            )
        except (
            UnicodeError,
            SerializationError,
            TypeError,
            ValueError,
            ValidationError,
        ) as error:
            raise RCPError(
                code="control_linear_policy_invalid",
                message="Existing Linear projection policy is malformed.",
            ) from error
        if dump_yaml(policy).encode("utf-8") != content:
            raise RCPError(
                code="control_linear_policy_invalid",
                message="Existing Linear projection policy is not canonical YAML.",
            )
        return policy

    @staticmethod
    def _write_atomic(path: Path, content: bytes) -> None:
        parent = path.parent
        if parent.is_symlink() or not parent.is_dir():
            raise RCPError(
                code="control_linear_policy_path_invalid",
                message="Linear policy directory must be a non-symlink directory.",
            )
        descriptor, name = tempfile.mkstemp(prefix=".linear.", dir=parent)
        temporary = Path(name)
        try:
            os.fchmod(descriptor, 0o644)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            directory = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError as error:
            raise RCPError(
                code="control_linear_policy_write_failed",
                message="Linear policy could not be written atomically.",
            ) from error
        finally:
            temporary.unlink(missing_ok=True)
