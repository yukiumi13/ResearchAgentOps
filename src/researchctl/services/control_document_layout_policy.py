from __future__ import annotations

import fcntl
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from researchctl.adapters import GitWorktreeAdapter, WorktreeSpec
from researchctl.adapters.git_control import ControlCommitReceipt, GitControlCommitAdapter
from researchctl.constants import PROJECT_POLICY_PATH
from researchctl.domain.models import DocumentLayoutPolicy, ProjectPolicy
from researchctl.domain.types import Sha256Digest
from researchctl.errors import RCPError
from researchctl.repository import safe_repository_path
from researchctl.serialization import (
    SerializationError,
    canonical_digest,
    dump_yaml,
    load_yaml,
)

_COMMAND = "doc.configure-layout"


@dataclass(frozen=True, slots=True)
class DocumentLayoutPolicyWriteResult:
    document_layout: DocumentLayoutPolicy
    project_policy: ProjectPolicy
    base_commit: str
    previous_policy_digest: Sha256Digest
    policy_digest: Sha256Digest
    path: Path
    changed: bool
    proposal: ControlCommitReceipt


class ControlDocumentLayoutPolicyRepository:
    """Prepare one manager-owned document classification/layout proposal."""

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
        GitControlCommitAdapter.validate_policy_identity(
            operation_id=operation_id,
            branch=branch,
            command=_COMMAND,
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
                message="Document layout control requires a default branch.",
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

    def configure(
        self,
        document_layout: DocumentLayoutPolicy,
    ) -> DocumentLayoutPolicyWriteResult:
        with self._exclusive():
            base = self.git.resolve_commit(
                self.repository_root,
                self.expected_default_head,
            )
            if base != self.expected_default_head:
                raise RCPError(
                    code="control_document_layout_base_invalid",
                    message="Document layout policy base did not resolve exactly.",
                )
            base_content = self.commits.policy_content_at(
                repository_root=self.repository_root,
                commit=base,
                command=_COMMAND,
            )
            if base_content is None:
                raise RCPError(
                    code="control_document_layout_policy_missing",
                    message="The protected base has no Project policy to update.",
                )
            previous = self._project_policy(base_content)
            replacement = previous.model_copy(
                update={"document_layout": document_layout}
            )
            desired = dump_yaml(replacement).encode("utf-8")

            branch_head = self._branch_head_or_none()
            if branch_head is None:
                self._require_current_default()
                branch_head = base
            else:
                changed = self.commits.validate_policy_operation_head(
                    repository_root=self.repository_root,
                    commit=branch_head,
                    operation_id=self.operation_id,
                    expected_base_commit=base,
                    command=_COMMAND,
                )
                if changed:
                    branch_content = self.commits.policy_content_at(
                        repository_root=self.repository_root,
                        commit=branch_head,
                        command=_COMMAND,
                    )
                    if (
                        branch_content is None
                        or self._project_policy(branch_content) != replacement
                    ):
                        raise RCPError(
                            code="control_document_layout_retry_mismatch",
                            message=(
                                "Existing document layout proposal differs from this "
                                "idempotent request."
                            ),
                        )

            self._prepare_worktree(branch_head)
            self.commits.require_policy_worktree_scope(
                worktree=self.worktree,
                branch=self.branch,
                operation_id=self.operation_id,
                command=_COMMAND,
            )
            path = safe_repository_path(
                self.worktree,
                PROJECT_POLICY_PATH,
                managed_only=True,
            )
            current_content = self._read_current(path)
            self._project_policy(current_content)
            content_changed = current_content != desired
            if content_changed:
                self._write_atomic(path, desired)

            proposal = self.commits.commit_policy_or_observe(
                worktree=self.worktree,
                branch=self.branch,
                operation_id=self.operation_id,
                expected_base_commit=base,
                command=_COMMAND,
            )
            return DocumentLayoutPolicyWriteResult(
                document_layout=document_layout,
                project_policy=replacement,
                base_commit=base,
                previous_policy_digest=canonical_digest(previous),
                policy_digest=canonical_digest(replacement),
                path=path,
                changed=content_changed,
                proposal=proposal,
            )

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        lock = self.worktrees_directory / f".document-layout-{self.operation_id}.lock"
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(lock, flags, 0o600)
        except OSError as error:
            raise RCPError(
                code="control_document_layout_lock_invalid",
                message="Document layout policy lock could not be opened safely.",
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
                code="control_document_layout_default_head_changed",
                message="Default branch no longer matches the document layout policy base.",
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
            self.commits.attach_existing_policy_branch(
                repository_root=self.repository_root,
                worktree=self.worktree,
                branch=self.branch,
                expected_head=branch_head,
                operation_id=self.operation_id,
                command=_COMMAND,
            )
            self.git.create_or_observe(spec)

    @staticmethod
    def _read_current(path: Path) -> bytes:
        if path.is_symlink() or not path.is_file():
            raise RCPError(
                code="control_document_layout_policy_path_invalid",
                message="Project policy path must be a regular non-symlink file.",
            )
        try:
            return path.read_bytes()
        except OSError as error:
            raise RCPError(
                code="control_document_layout_policy_path_invalid",
                message="Project policy could not be read.",
            ) from error

    @staticmethod
    def _project_policy(content: bytes) -> ProjectPolicy:
        try:
            policy = ProjectPolicy.model_validate(load_yaml(content.decode("utf-8")))
        except (
            UnicodeError,
            SerializationError,
            TypeError,
            ValueError,
            ValidationError,
        ) as error:
            raise RCPError(
                code="control_document_layout_policy_invalid",
                message="Existing Project policy is malformed.",
            ) from error
        if dump_yaml(policy).encode("utf-8") != content:
            raise RCPError(
                code="control_document_layout_policy_invalid",
                message="Existing Project policy is not canonical YAML.",
            )
        return policy

    @staticmethod
    def _write_atomic(path: Path, content: bytes) -> None:
        parent = path.parent
        if parent.is_symlink() or not parent.is_dir():
            raise RCPError(
                code="control_document_layout_policy_path_invalid",
                message="Project policy directory must be a non-symlink directory.",
            )
        descriptor, name = tempfile.mkstemp(prefix=".document-layout.", dir=parent)
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
                code="control_document_layout_policy_write_failed",
                message="Project policy could not be written atomically.",
            ) from error
        finally:
            temporary.unlink(missing_ok=True)
