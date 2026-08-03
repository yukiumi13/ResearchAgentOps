from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Iterator

from researchctl.adapters import GitWorktreeAdapter, WorktreeSpec
from researchctl.adapters.git_bootstrap_proposal import (
    BootstrapProposalReceipt,
    GitBootstrapProposalAdapter,
)
from researchctl.constants import PROJECT_DIR_NAME
from researchctl.errors import RCPError
from researchctl.repository import safe_repository_path
from researchctl.services.control_bootstrap import (
    capture_managed_init_manifest,
)


class BootstrapProposalService:
    """Copies an uncommitted init manifest into one isolated proposal commit."""

    def __init__(
        self,
        *,
        repository_root: Path,
        worktrees_directory: Path,
        default_branch: str,
        expected_default_head: str,
        operation_id: str,
        bootstrap_id: str,
        git: GitWorktreeAdapter | None = None,
        commits: GitBootstrapProposalAdapter | None = None,
    ) -> None:
        branch = f"research/bootstrap/{bootstrap_id}"
        GitBootstrapProposalAdapter.validate_identity(
            operation_id=operation_id,
            bootstrap_id=bootstrap_id,
            branch=branch,
        )
        GitBootstrapProposalAdapter.validate_commit(expected_default_head)
        root = Path(os.path.abspath(os.fspath(repository_root)))
        worktrees = Path(os.path.abspath(os.fspath(worktrees_directory)))
        if root.is_symlink() or not root.is_dir():
            raise RCPError(
                code="bootstrap_proposal_repository_invalid",
                message="Bootstrap proposal source must be a non-symlink directory.",
            )
        if worktrees.is_symlink() or not worktrees.is_dir():
            raise RCPError(
                code="bootstrap_proposal_worktrees_invalid",
                message="Bootstrap proposal worktree parent must be a non-symlink directory.",
            )
        if not isinstance(default_branch, str) or not default_branch:
            raise RCPError(
                code="bootstrap_proposal_default_branch_invalid",
                message="Bootstrap proposal requires a default branch.",
            )

        self.repository_root = root
        self.worktrees_directory = worktrees
        self.default_branch = default_branch
        self.expected_default_head = expected_default_head
        self.operation_id = operation_id
        self.bootstrap_id = bootstrap_id
        self.branch = branch
        self.worktree = worktrees / f"bootstrap-{bootstrap_id}"
        self.git = git or GitWorktreeAdapter()
        self.commits = commits or GitBootstrapProposalAdapter()

    def prepare(self) -> BootstrapProposalReceipt:
        with self._exclusive():
            manifest = capture_managed_init_manifest(
                self.repository_root,
                default_branch=self.default_branch,
            )
            resolved_base = self.git.resolve_commit(
                self.repository_root,
                self.expected_default_head,
            )
            if resolved_base != self.expected_default_head:
                raise RCPError(
                    code="bootstrap_proposal_base_invalid",
                    message="Bootstrap proposal base did not resolve exactly.",
                )
            if self.commits.controlled_tree_paths(
                root=self.repository_root,
                commit=resolved_base,
            ):
                raise RCPError(
                    code="bootstrap_proposal_base_not_empty",
                    message="Bootstrap proposal base already has managed init content.",
                )

            branch_head = self._branch_head_or_none()
            if branch_head is None:
                self._require_current_default()
                branch_head = resolved_base
            self._prepare_worktree(branch_head)

            files = manifest.file_map()
            observed = self.commits.observe(
                worktree=self.worktree,
                branch=self.branch,
                base_commit=resolved_base,
                operation_id=self.operation_id,
                bootstrap_id=self.bootstrap_id,
                manifest_digest=manifest.digest,
                desired_files=files,
            )
            if observed is not None:
                return observed

            self._require_current_default()
            _validate_partial_worktree(self.worktree, frozenset(files))
            self._write_files(files)
            repeated = capture_managed_init_manifest(
                self.repository_root,
                default_branch=self.default_branch,
            )
            if repeated.digest != manifest.digest:
                raise RCPError(
                    code="bootstrap_proposal_source_changed",
                    message="Init manifest changed while bootstrap proposal was prepared.",
                )
            self._require_current_default()
            return self.commits.commit_or_observe(
                worktree=self.worktree,
                branch=self.branch,
                base_commit=resolved_base,
                operation_id=self.operation_id,
                bootstrap_id=self.bootstrap_id,
                manifest_digest=manifest.digest,
                desired_files=files,
            )

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        lock = self.worktrees_directory / (
            f".bootstrap-proposal-{self.bootstrap_id}.lock"
        )
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(lock, flags, 0o600)
        except OSError as error:
            raise RCPError(
                code="bootstrap_proposal_lock_invalid",
                message="Bootstrap proposal lock could not be opened safely.",
            ) from error
        try:
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _require_current_default(self) -> None:
        observed = self.git.resolve_commit(
            self.repository_root,
            f"refs/heads/{self.default_branch}",
        )
        if observed != self.expected_default_head:
            raise RCPError(
                code="bootstrap_proposal_default_head_changed",
                message="Default branch no longer matches the proposal base.",
                context={
                    "expected_head": self.expected_default_head,
                    "observed_head": observed,
                },
            )

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
                and error.context.get("branch") == self.branch
                and error.context.get("worktree") == str(self.worktree)
                and error.context.get("reason")
                == "requested branch and worktree do not match the declared identity"
                and not os.path.lexists(self.worktree)
            )
            if not branch_only:
                raise
            self.commits.attach_existing_branch(
                repository_root=self.repository_root,
                worktree=self.worktree,
                branch=self.branch,
                expected_head=branch_head,
                operation_id=self.operation_id,
                bootstrap_id=self.bootstrap_id,
            )
            self.git.create_or_observe(spec)

    def _write_files(self, files: dict[str, bytes]) -> None:
        for index, (relative, content) in enumerate(sorted(files.items())):
            destination = _prepare_destination(self.worktree, relative)
            if destination.exists():
                try:
                    if destination.read_bytes() == content:
                        continue
                except OSError as error:
                    raise RCPError(
                        code="bootstrap_proposal_worktree_invalid",
                        message="Bootstrap proposal file could not be read.",
                        context={"path": relative},
                    ) from error

            temporary = self.worktrees_directory / (
                f".bootstrap-proposal-{self.bootstrap_id}-{index}.tmp"
            )
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(
                os,
                "O_NOFOLLOW",
                0,
            )
            try:
                descriptor = os.open(temporary, flags, 0o600)
                try:
                    os.fchmod(descriptor, 0o600)
                    handle = os.fdopen(descriptor, "wb")
                    descriptor = -1
                    with handle:
                        handle.write(content)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temporary, destination)
                    _fsync_directory(destination.parent)
                finally:
                    if descriptor >= 0:
                        os.close(descriptor)
            except OSError as error:
                raise RCPError(
                    code="bootstrap_proposal_write_failed",
                    message="Bootstrap proposal file could not be written atomically.",
                    context={"path": relative},
                ) from error


def _validate_partial_worktree(root: Path, expected_files: frozenset[str]) -> None:
    expected_directories = _directory_prefixes(expected_files)
    managed = safe_repository_path(root, PROJECT_DIR_NAME, managed_only=True)
    if os.path.lexists(managed):
        if managed.is_symlink() or not managed.is_dir():
            raise RCPError(
                code="bootstrap_proposal_worktree_invalid",
                message="Bootstrap proposal managed path is not a directory.",
            )
        try:
            discovered = sorted(managed.rglob("*"), key=lambda path: path.as_posix())
        except OSError as error:
            raise RCPError(
                code="bootstrap_proposal_worktree_invalid",
                message="Bootstrap proposal worktree could not be inspected.",
            ) from error
        for path in discovered:
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                raise RCPError(
                    code="bootstrap_proposal_worktree_symlink",
                    message="Bootstrap proposal worktree contains a symbolic link.",
                    context={"path": relative},
                )
            allowed = (
                relative in expected_directories
                if path.is_dir()
                else path.is_file() and relative in expected_files
            )
            if not allowed:
                raise RCPError(
                    code="bootstrap_proposal_worktree_unexpected",
                    message="Bootstrap proposal worktree contains an unexpected entry.",
                    context={"path": relative},
                )

    config = safe_repository_path(root, ".researchctl.toml")
    if os.path.lexists(config) and (config.is_symlink() or not config.is_file()):
        raise RCPError(
            code="bootstrap_proposal_worktree_invalid",
            message="Bootstrap proposal config path is not a regular file.",
        )


def _prepare_destination(root: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    current = root
    for part in pure.parent.parts:
        if part == ".":
            continue
        candidate = current / part
        if os.path.lexists(candidate):
            if candidate.is_symlink() or not candidate.is_dir():
                raise RCPError(
                    code="bootstrap_proposal_worktree_invalid",
                    message="Bootstrap proposal path parent is unsafe.",
                    context={"path": relative},
                )
        else:
            try:
                candidate.mkdir(mode=0o700)
                _fsync_directory(current)
            except OSError as error:
                raise RCPError(
                    code="bootstrap_proposal_write_failed",
                    message="Bootstrap proposal directory could not be created.",
                    context={"path": relative},
                ) from error
        current = candidate

    destination = safe_repository_path(
        root,
        relative,
        managed_only=relative.startswith(f"{PROJECT_DIR_NAME}/"),
    )
    if os.path.lexists(destination) and (
        destination.is_symlink() or not destination.is_file()
    ):
        raise RCPError(
            code="bootstrap_proposal_worktree_invalid",
            message="Bootstrap proposal destination is not a regular file.",
            context={"path": relative},
        )
    return destination


def _directory_prefixes(files: frozenset[str]) -> frozenset[str]:
    directories: set[str] = set()
    for relative in files:
        if not relative.startswith(f"{PROJECT_DIR_NAME}/"):
            continue
        parent = PurePosixPath(relative).parent
        while parent.as_posix() != ".":
            directories.add(parent.as_posix())
            if parent.as_posix() == PROJECT_DIR_NAME:
                break
            parent = parent.parent
    return frozenset(directories)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
