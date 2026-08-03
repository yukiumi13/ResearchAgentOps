from __future__ import annotations

import fcntl
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from researchctl.adapters.git_submission import (
    GitSubmissionAdapter,
    SubmissionCommitReceipt,
)
from researchctl.errors import RCPError
from researchctl.services.review_acceptance import AcceptanceBundle
from researchctl.services.submissions import SubmissionBundle


class SubmissionRecordRepository:
    def __init__(
        self,
        *,
        repository_root: Path,
        worktrees_directory: Path,
        git: GitSubmissionAdapter | None = None,
    ) -> None:
        self.repository_root = repository_root.resolve()
        self.worktrees_directory = worktrees_directory.resolve()
        self.git = git or GitSubmissionAdapter()

    def write_proposal(
        self,
        *,
        operation_id: str,
        base_commit: str,
        bundle: SubmissionBundle,
    ) -> SubmissionCommitReceipt:
        branch, worktree = self.git.prepare_worktree(
            repository_root=self.repository_root,
            worktrees_directory=self.worktrees_directory,
            submission_id=bundle.submission_id,
            base_commit=base_commit,
        )
        with self._exclusive(worktree):
            for item in bundle.files:
                self._write_once(worktree, item.path, item.content)
            return self.git.commit_proposal(
                worktree=worktree,
                branch=branch,
                submission_id=bundle.submission_id,
                operation_id=operation_id,
                expected_parent=base_commit,
                paths=tuple(item.path for item in bundle.files),
            )

    def write_acceptance(
        self,
        *,
        operation_id: str,
        expected_head: str,
        bundle: AcceptanceBundle,
        expected_open_submission: bytes,
    ) -> SubmissionCommitReceipt:
        branch, worktree = self.git.prepare_worktree(
            repository_root=self.repository_root,
            worktrees_directory=self.worktrees_directory,
            submission_id=bundle.submission.submission_id,
            base_commit=expected_head,
            expected_head=expected_head,
        )
        submission_path = (
            f".research/submissions/{bundle.submission.submission_id}/"
            "submission.yaml"
        )
        with self._exclusive(worktree):
            for item in bundle.files:
                if item.path == submission_path:
                    self._replace_expected(
                        worktree,
                        item.path,
                        expected_open_submission,
                        item.content,
                    )
                else:
                    self._write_once(worktree, item.path, item.content)
            return self.git.commit_acceptance(
                worktree=worktree,
                branch=branch,
                submission_id=bundle.submission.submission_id,
                decision_id=bundle.decision.decision_id,
                report_id=bundle.report.report_id,
                report_revision=bundle.report.revision,
                operation_id=operation_id,
                expected_parent=expected_head,
                paths=tuple(item.path for item in bundle.files),
            )

    @contextmanager
    def _exclusive(self, worktree: Path) -> Iterator[None]:
        descriptor = os.open(
            worktree,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    @staticmethod
    def _path(worktree: Path, relative: str) -> Path:
        candidate = worktree
        for part in Path(relative).parts:
            candidate = candidate / part
            if candidate.is_symlink():
                raise RCPError(
                    code="submission_path_unsafe",
                    message="Generated Submission path traverses a symbolic link.",
                )
        try:
            candidate.resolve().relative_to(worktree.resolve())
        except ValueError as error:
            raise RCPError(
                code="submission_path_unsafe",
                message="Generated Submission path escapes its worktree.",
            ) from error
        return candidate

    def _write_once(self, worktree: Path, relative: str, content: bytes) -> None:
        path = self._path(worktree, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if path.is_file() and not path.is_symlink() and path.read_bytes() == content:
                return
            raise RCPError(
                code="submission_record_conflict",
                message="Submission identity already names different content.",
                context={"path": relative},
            )
        self._atomic_write(path, content, replace=False)

    def _replace_expected(
        self,
        worktree: Path,
        relative: str,
        expected: bytes,
        replacement: bytes,
    ) -> None:
        path = self._path(worktree, relative)
        if not path.is_file() or path.is_symlink():
            raise RCPError(
                code="submission_record_conflict",
                message="Expected open Submission record is missing or unsafe.",
            )
        observed = path.read_bytes()
        if observed == replacement:
            return
        if observed != expected:
            raise RCPError(
                code="submission_record_conflict",
                message="Submission changed after manager review.",
                context={"path": relative},
            )
        self._atomic_write(path, replacement, replace=True)

    @staticmethod
    def _atomic_write(path: Path, content: bytes, *, replace: bool) -> None:
        descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(name)
        try:
            os.fchmod(descriptor, 0o644)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            if replace:
                os.replace(temporary, path)
            else:
                os.link(temporary, path, follow_symlinks=False)
        except FileExistsError as error:
            raise RCPError(
                code="submission_record_conflict",
                message="Submission record was created concurrently.",
            ) from error
        finally:
            temporary.unlink(missing_ok=True)
