from __future__ import annotations

import fcntl
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from researchctl.adapters import GitWorktreeAdapter, WorktreeSpec
from researchctl.adapters.git_run import GitRunAdapter
from researchctl.domain.models import RunResult, RunSpec
from researchctl.errors import RCPError
from researchctl.repository import safe_repository_path
from researchctl.serialization import dump_yaml, load_model


@dataclass(frozen=True, slots=True)
class FrozenRunReceipt:
    run_id: str
    branch: str
    tag: str
    metadata_worktree: Path
    execution_worktree: Path
    source_commit: str
    spec_commit: str
    spec_digest: str
    changed: bool
    effect_applied: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "branch": self.branch,
            "tag": self.tag,
            "metadata_worktree": str(self.metadata_worktree),
            "execution_worktree": str(self.execution_worktree),
            "source_commit": self.source_commit,
            "spec_commit": self.spec_commit,
            "spec_digest": self.spec_digest,
            "changed": self.changed,
            "effect_applied": self.effect_applied,
            "delivery": "local_immutable_run",
        }


@dataclass(frozen=True, slots=True)
class CollectedRunReceipt:
    run_id: str
    result_id: str
    branch: str
    spec_commit: str
    result_commit: str
    changed: bool
    effect_applied: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "result_id": self.result_id,
            "branch": self.branch,
            "spec_commit": self.spec_commit,
            "result_commit": self.result_commit,
            "changed": self.changed,
            "effect_applied": self.effect_applied,
            "delivery": "local_run_result",
        }


class GitRunRecordRepository:
    """Persists one immutable RunSpec and at most one RunResult on a run ref."""

    def __init__(
        self,
        *,
        repository_root: Path,
        worktrees_directory: Path,
        spec: RunSpec,
        git: GitWorktreeAdapter | None = None,
        run_git: GitRunAdapter | None = None,
    ) -> None:
        self.repository_root = Path(os.path.abspath(os.fspath(repository_root)))
        self.worktrees_directory = Path(
            os.path.abspath(os.fspath(worktrees_directory))
        )
        if self.repository_root.is_symlink() or not self.repository_root.is_dir():
            self._raise_directory("run_repository_invalid", self.repository_root)
        if (
            self.worktrees_directory.is_symlink()
            or not self.worktrees_directory.is_dir()
        ):
            self._raise_directory(
                "run_worktrees_directory_invalid",
                self.worktrees_directory,
            )
        self.spec = spec
        self.branch = f"research/run/{spec.run_id}"
        self.tag = f"research-run/{spec.run_id}"
        self.metadata_worktree = self.worktrees_directory / f"run-{spec.run_id}"
        self.execution_worktree = self.worktrees_directory / f"run-exec-{spec.run_id}"
        self.git = git or GitWorktreeAdapter()
        self.run_git = run_git or GitRunAdapter()
        self._prepared = False

    @staticmethod
    def _raise_directory(code: str, path: Path) -> None:
        raise RCPError(
            code=code,
            message="Run repository directory is missing, unsafe, or a symbolic link.",
            context={"path": str(path)},
        )

    def _prepare(self) -> None:
        if self._prepared:
            return
        self.run_git.validate_source(
            self.repository_root,
            source_commit=self.spec.source_commit,
            source_tree=self.spec.source_tree,
        )
        branch_commit = self._resolve_branch()
        tag_commit = self.run_git.resolve_tag(
            self.repository_root,
            self.spec.run_id,
        )
        base = branch_commit or tag_commit or self.spec.source_commit
        self.git.create_or_observe(
            WorktreeSpec(
                root=self.repository_root,
                base_commit=base,
                branch=self.branch,
                worktree=self.metadata_worktree,
            )
        )
        self.run_git.create_or_observe_execution_worktree(
            repository_root=self.repository_root,
            execution_worktree=self.execution_worktree,
            source_commit=self.spec.source_commit,
        )
        runs = safe_repository_path(
            self.metadata_worktree,
            ".research/runs",
            managed_only=True,
        )
        if not runs.is_dir() or runs.is_symlink():
            self._raise_directory("run_store_missing", runs)
        directory = runs / self.spec.run_id
        try:
            directory.mkdir(mode=0o755)
            self._fsync(runs)
        except FileExistsError:
            pass
        if not directory.is_dir() or directory.is_symlink():
            self._raise_directory("run_record_directory_invalid", directory)
        self._prepared = True

    def require_started(self) -> None:
        """Require an existing exact Run branch or immutable tag without mutation."""

        branch_commit = self._resolve_branch()
        tag_commit = self.run_git.resolve_tag(
            self.repository_root,
            self.spec.run_id,
        )
        if branch_commit is None and tag_commit is None:
            raise RCPError(
                code="run_not_started",
                message="Run collection requires an existing immutable Run identity.",
                context={
                    "run_id": self.spec.run_id,
                    "branch_ref": f"refs/heads/{self.branch}",
                    "tag_ref": f"refs/tags/{self.tag}",
                },
            )

    def _resolve_branch(self) -> str | None:
        try:
            return self.git.resolve_commit(
                self.repository_root,
                f"refs/heads/{self.branch}",
            )
        except RCPError as error:
            if error.code != "git_revision_not_found":
                raise
            return None

    @property
    def directory(self) -> Path:
        self._prepare()
        return self.metadata_worktree / ".research" / "runs" / self.spec.run_id

    @property
    def spec_path(self) -> Path:
        return self.directory / "spec.yaml"

    @property
    def result_path(self) -> Path:
        return self.directory / "result.yaml"

    def freeze(self) -> FrozenRunReceipt:
        self._prepare()
        content = dump_yaml(self.spec).encode("utf-8")
        with self._exclusive():
            self._write_once(
                self.spec_path,
                content,
                conflict_code="run_spec_conflict",
            )
            committed = self.run_git.commit_spec_or_observe(
                worktree=self.metadata_worktree,
                branch=self.branch,
                record_path=self.spec_path,
                run_id=self.spec.run_id,
                spec_digest=self.spec.spec_digest,
                source_commit=self.spec.source_commit,
            )
        return FrozenRunReceipt(
            run_id=self.spec.run_id,
            branch=self.branch,
            tag=self.tag,
            metadata_worktree=self.metadata_worktree,
            execution_worktree=self.execution_worktree,
            source_commit=self.spec.source_commit,
            spec_commit=committed.commit,
            spec_digest=self.spec.spec_digest,
            changed=committed.changed,
            effect_applied=committed.effect_applied,
        )

    def load_spec(self) -> RunSpec:
        self._prepare()
        if not self.spec_path.is_file():
            raise RCPError(
                code="run_spec_not_found",
                message="Frozen RunSpec was not found on the run branch.",
            )
        try:
            observed = load_model(self.spec_path, RunSpec)
        except Exception as error:
            raise RCPError(
                code="run_spec_invalid",
                message="Frozen RunSpec is malformed.",
            ) from error
        if observed != self.spec:
            raise RCPError(
                code="run_spec_conflict",
                message="Run branch contains a different RunSpec.",
            )
        return observed

    def collect(self, result: RunResult) -> CollectedRunReceipt:
        frozen = self.freeze()
        if result.run_id != self.spec.run_id:
            raise RCPError(
                code="run_result_identity_mismatch",
                message="RunResult does not belong to this RunSpec.",
            )
        if result.run_spec_digest != self.spec.spec_digest:
            raise RCPError(
                code="run_result_spec_mismatch",
                message="RunResult does not bind the frozen RunSpec digest.",
            )
        content = dump_yaml(result).encode("utf-8")
        with self._exclusive():
            self._write_once(
                self.result_path,
                content,
                conflict_code="run_result_conflict",
            )
            committed = self.run_git.commit_result_or_observe(
                worktree=self.metadata_worktree,
                branch=self.branch,
                record_path=self.result_path,
                run_id=self.spec.run_id,
                result_id=result.result_id,
                spec_commit=frozen.spec_commit,
            )
        return CollectedRunReceipt(
            run_id=self.spec.run_id,
            result_id=result.result_id,
            branch=self.branch,
            spec_commit=frozen.spec_commit,
            result_commit=committed.commit,
            changed=committed.changed,
            effect_applied=committed.effect_applied,
        )

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        descriptor = os.open(
            self.metadata_worktree,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _write_once(self, path: Path, content: bytes, *, conflict_code: str) -> None:
        if path.exists():
            if path.is_file() and not path.is_symlink() and path.read_bytes() == content:
                return
            raise RCPError(
                code=conflict_code,
                message="Run identity already names different record content.",
                context={"path": path.relative_to(self.metadata_worktree).as_posix()},
            )
        descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(name)
        try:
            os.fchmod(descriptor, 0o644)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, path, follow_symlinks=False)
            except FileExistsError as error:
                raise RCPError(
                    code=conflict_code,
                    message="Run record was created concurrently with different content.",
                ) from error
            self._fsync(path.parent)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _fsync(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
