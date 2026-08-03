from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from researchctl.adapters import GitWorktreeAdapter, WorktreeSpec
from researchctl.adapters.git_control import ControlCommitReceipt, GitControlCommitAdapter
from researchctl.domain.models import TaskRecord
from researchctl.domain.types import Sha256Digest
from researchctl.errors import RCPError
from researchctl.services.task_records import TaskRecordRepository, TaskWriteResult


class ControlTaskRecordRepository:
    """Lazily writes one manager Task proposal in an isolated control worktree."""

    def __init__(
        self,
        *,
        repository_root: Path,
        worktrees_directory: Path,
        default_branch: str,
        operation_id: str,
        command: str,
        git: GitWorktreeAdapter | None = None,
        commits: GitControlCommitAdapter | None = None,
    ) -> None:
        branch = f"research/control/{operation_id}"
        GitControlCommitAdapter.validate_identity(
            operation_id=operation_id,
            command=command,
            branch=branch,
        )
        self.repository_root = Path(os.path.abspath(os.fspath(repository_root)))
        self.worktrees_directory = Path(
            os.path.abspath(os.fspath(worktrees_directory))
        )
        if self.repository_root.is_symlink() or not self.repository_root.is_dir():
            raise RCPError(
                code="control_repository_invalid",
                message="Control repository must be an existing non-symlink directory.",
            )
        if (
            self.worktrees_directory.is_symlink()
            or not self.worktrees_directory.is_dir()
        ):
            raise RCPError(
                code="control_worktrees_directory_invalid",
                message="Control worktree parent must be an existing non-symlink directory.",
            )
        self.default_branch = default_branch
        self.operation_id = operation_id
        self.command = command
        self.branch = branch
        self.worktree = self.worktrees_directory / f"control-{operation_id}"
        self.git = git or GitWorktreeAdapter()
        self.commits = commits or GitControlCommitAdapter()
        self._repository: TaskRecordRepository | None = None
        self.proposal_receipt: ControlCommitReceipt | None = None

    def _prepare(self) -> TaskRecordRepository:
        if self._repository is not None:
            return self._repository
        try:
            base = self.git.resolve_commit(
                self.repository_root,
                f"refs/heads/{self.branch}",
            )
        except RCPError as error:
            if error.code != "git_revision_not_found":
                raise
            base = self.git.resolve_commit(
                self.repository_root,
                f"refs/heads/{self.default_branch}",
            )
        self.git.create_or_observe(
            WorktreeSpec(
                root=self.repository_root,
                base_commit=base,
                branch=self.branch,
                worktree=self.worktree,
            )
        )
        self._repository = TaskRecordRepository(self.worktree)
        return self._repository

    @property
    def root(self) -> Path:
        return self._prepare().root

    @property
    def directory(self) -> Path:
        return self._prepare().directory

    def path_for(self, task_id: str) -> Path:
        return self._prepare().path_for(task_id)

    def list(self) -> tuple[TaskRecord, ...]:
        return self._prepare().list()

    def load(self, task_id: str) -> TaskRecord:
        return self._prepare().load(task_id)

    def find_by_key(self, key: str) -> TaskRecord | None:
        return self._prepare().find_by_key(key)

    def create(self, record: TaskRecord) -> TaskWriteResult:
        return self._publish(self._prepare().create(record))

    def replace(
        self,
        task_id: str,
        expected_digest: Sha256Digest,
        replacement: TaskRecord,
    ) -> TaskWriteResult:
        return self._publish(
            self._prepare().replace(task_id, expected_digest, replacement)
        )

    def cancel(
        self,
        task_id: str,
        expected_digest: Sha256Digest,
        *,
        updated_at: datetime,
    ) -> TaskWriteResult:
        return self._publish(
            self._prepare().cancel(
                task_id,
                expected_digest,
                updated_at=updated_at,
            )
        )

    def _publish(self, result: TaskWriteResult) -> TaskWriteResult:
        self.proposal_receipt = self.commits.commit_or_observe(
            worktree=self.worktree,
            branch=self.branch,
            task_path=result.path,
            operation_id=self.operation_id,
            command=self.command,
        )
        return result
