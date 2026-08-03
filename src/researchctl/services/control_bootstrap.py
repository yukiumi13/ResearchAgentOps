from __future__ import annotations

import fcntl
import hashlib
import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterator

from researchctl.adapters import GitWorktreeAdapter, WorktreeSpec
from researchctl.adapters.git_bootstrap import (
    BootstrapAcceptanceReceipt,
    GitBootstrapCommitAdapter,
)
from researchctl.config import dump_project_config, load_project_config
from researchctl.constants import PROJECT_CONFIG_NAME, PROJECT_DIR_NAME, PROTOCOL_VERSION
from researchctl.domain.enums import ProjectState
from researchctl.domain.models import ProjectPolicy, ProjectRecord
from researchctl.domain.types import Sha256Digest
from researchctl.errors import RCPError
from researchctl.repository import safe_repository_path
from researchctl.schema import generate_schema_files, schema_manifest_digest
from researchctl.serialization import (
    SerializationError,
    canonical_digest,
    dump_yaml,
    load_model,
    load_yaml,
)


_MANAGED_DIRECTORIES = (
    "bootstrap",
    "tasks",
    "runs",
    "submissions",
    "decisions",
    "reports",
)
_PROJECT_RECORD_PATH = ".research/project.yaml"
_POLICY_PATH = ".research/policies/default.yaml"


@dataclass(frozen=True, slots=True)
class ManagedInitFile:
    path: str
    content: bytes

    @property
    def digest(self) -> str:
        return f"sha256:{hashlib.sha256(self.content).hexdigest()}"


@dataclass(frozen=True, slots=True)
class ManagedInitManifest:
    project: ProjectRecord
    files: tuple[ManagedInitFile, ...]
    digest: Sha256Digest

    def file_map(self) -> dict[str, bytes]:
        return {item.path: item.content for item in self.files}

    def accepted_file_map(self) -> dict[str, bytes]:
        payload = self.project.model_dump(mode="python")
        payload["state"] = ProjectState.MANAGED
        accepted_project = ProjectRecord.model_validate(payload)
        files = self.file_map()
        files[_PROJECT_RECORD_PATH] = dump_yaml(accepted_project).encode("utf-8")
        return files

    def as_dict(self) -> dict[str, object]:
        return {
            "project_id": self.project.project_id,
            "source_state": self.project.state.value,
            "digest": self.digest,
            "files": [
                {"path": item.path, "digest": item.digest} for item in self.files
            ],
        }


def capture_managed_init_manifest(
    repository_root: Path,
    *,
    default_branch: str,
) -> ManagedInitManifest:
    """Validate and snapshot the exact deterministic output of ``init``."""
    root = Path(os.path.abspath(os.fspath(repository_root)))
    if root.is_symlink() or not root.is_dir():
        raise RCPError(
            code="bootstrap_repository_invalid",
            message="Bootstrap source must be an existing non-symlink directory.",
        )
    if not isinstance(default_branch, str) or not default_branch:
        raise RCPError(
            code="bootstrap_default_branch_invalid",
            message="Bootstrap source requires a non-empty default branch.",
        )

    schema_files = generate_schema_files()
    expected_files = {
        PROJECT_CONFIG_NAME,
        _PROJECT_RECORD_PATH,
        _POLICY_PATH,
        *(f"{PROJECT_DIR_NAME}/schemas/{name}" for name in schema_files),
        *(
            f"{PROJECT_DIR_NAME}/{directory}/.gitkeep"
            for directory in _MANAGED_DIRECTORIES
        ),
    }
    expected_directories = _directory_prefixes(expected_files)
    _validate_managed_tree(
        root,
        expected_files=expected_files,
        expected_directories=expected_directories,
    )

    config_path = _regular_path(root, PROJECT_CONFIG_NAME)
    project_path = _regular_path(root, _PROJECT_RECORD_PATH)
    policy_path = _regular_path(root, _POLICY_PATH)
    try:
        config = load_project_config(config_path)
        project = load_model(project_path, ProjectRecord)
        policy = load_model(policy_path, ProjectPolicy)
    except (OSError, UnicodeError, ValueError, SerializationError) as error:
        raise RCPError(
            code="bootstrap_init_manifest_invalid",
            message="Bootstrap initialization records are malformed.",
            context={"error_type": type(error).__name__},
        ) from error

    expected_schema_digest = schema_manifest_digest(schema_files)
    if (
        config.protocol_version != PROTOCOL_VERSION
        or config.schema_manifest_digest != expected_schema_digest
    ):
        raise RCPError(
            code="bootstrap_protocol_lock_mismatch",
            message="Bootstrap initialization does not match this protocol build.",
        )
    if project.project_id != config.project_id:
        raise RCPError(
            code="bootstrap_project_identity_mismatch",
            message="Bootstrap Project IDs differ between config and ProjectRecord.",
        )
    if project.repository.default_branch != default_branch:
        raise RCPError(
            code="bootstrap_default_branch_mismatch",
            message="Bootstrap ProjectRecord names a different default branch.",
            context={"recorded_default_branch": project.repository.default_branch},
        )
    if project.state is not ProjectState.BOOTSTRAPPING:
        raise RCPError(
            code="bootstrap_project_state_invalid",
            message="Only a bootstrapping Project can prepare bootstrap acceptance.",
            context={"observed_state": project.state.value},
        )

    canonical: dict[str, bytes] = {
        PROJECT_CONFIG_NAME: dump_project_config(config),
        _PROJECT_RECORD_PATH: dump_yaml(project).encode("utf-8"),
        _POLICY_PATH: dump_yaml(policy).encode("utf-8"),
    }
    canonical.update(
        {
            f"{PROJECT_DIR_NAME}/schemas/{name}": content
            for name, content in schema_files.items()
        }
    )
    canonical.update(
        {
            f"{PROJECT_DIR_NAME}/{directory}/.gitkeep": b""
            for directory in _MANAGED_DIRECTORIES
        }
    )
    for relative, expected in canonical.items():
        path = _regular_path(root, relative)
        try:
            observed = path.read_bytes()
        except OSError as error:
            raise RCPError(
                code="bootstrap_init_manifest_invalid",
                message="Bootstrap initialization file could not be read.",
                context={"path": relative},
            ) from error
        if observed != expected:
            raise RCPError(
                code="bootstrap_init_manifest_noncanonical",
                message="Bootstrap initialization file is not canonical.",
                context={"path": relative},
            )

    files = tuple(
        ManagedInitFile(path=path, content=content)
        for path, content in sorted(canonical.items())
    )
    digest = canonical_digest(
        {
            "files": [
                {"path": item.path, "digest": item.digest} for item in files
            ]
        }
    )
    return ManagedInitManifest(project=project, files=files, digest=digest)


class ControlBootstrapAcceptance:
    """Prepares, but never merges, bootstrapping-to-managed acceptance."""

    def __init__(
        self,
        *,
        repository_root: Path,
        worktrees_directory: Path,
        default_branch: str,
        operation_id: str,
        proposal_commit: str | None = None,
        git: GitWorktreeAdapter | None = None,
        commits: GitBootstrapCommitAdapter | None = None,
    ) -> None:
        branch = f"research/control/{operation_id}"
        GitBootstrapCommitAdapter.validate_identity(
            operation_id=operation_id,
            branch=branch,
        )
        if proposal_commit is not None:
            GitBootstrapCommitAdapter.validate_commit(proposal_commit)

        self.repository_root = Path(
            os.path.abspath(os.fspath(repository_root))
        )
        self.worktrees_directory = Path(
            os.path.abspath(os.fspath(worktrees_directory))
        )
        if self.repository_root.is_symlink() or not self.repository_root.is_dir():
            raise RCPError(
                code="bootstrap_repository_invalid",
                message="Bootstrap repository must be an existing non-symlink directory.",
            )
        if (
            self.worktrees_directory.is_symlink()
            or not self.worktrees_directory.is_dir()
        ):
            raise RCPError(
                code="bootstrap_worktrees_directory_invalid",
                message="Bootstrap worktree parent must be a non-symlink directory.",
            )
        if not isinstance(default_branch, str) or not default_branch:
            raise RCPError(
                code="bootstrap_default_branch_invalid",
                message="Bootstrap acceptance requires a default branch.",
            )

        self.default_branch = default_branch
        self.operation_id = operation_id
        self.proposal_commit = proposal_commit
        self.branch = branch
        self.worktree = self.worktrees_directory / f"control-{operation_id}"
        self.git = git or GitWorktreeAdapter()
        self.commits = commits or GitBootstrapCommitAdapter()

    def prepare(self) -> BootstrapAcceptanceReceipt:
        with self._exclusive():
            manifest = capture_managed_init_manifest(
                self.repository_root,
                default_branch=self.default_branch,
            )
            default_head = self.git.resolve_commit(
                self.repository_root,
                f"refs/heads/{self.default_branch}",
            )
            proposal = self.proposal_commit or default_head
            resolved_proposal = self.git.resolve_commit(
                self.repository_root,
                proposal,
            )
            if resolved_proposal != proposal:
                raise RCPError(
                    code="bootstrap_proposal_commit_invalid",
                    message="Bootstrap proposal must bind an exact commit object ID.",
                )
            self._verify_authoritative_inputs(
                manifest=manifest,
                default_head=default_head,
                proposal_commit=proposal,
            )
            self._prepare_worktree(proposal)

            accepted_files = manifest.accepted_file_map()
            observed = self.commits.observe_prepared(
                worktree=self.worktree,
                branch=self.branch,
                proposal_commit=proposal,
                operation_id=self.operation_id,
                manifest_digest=manifest.digest,
                desired_files=accepted_files,
            )
            if observed is not None:
                return observed

            _validate_control_worktree(self.worktree, frozenset(accepted_files))
            self._write_files(accepted_files)
            repeated = capture_managed_init_manifest(
                self.repository_root,
                default_branch=self.default_branch,
            )
            if repeated.digest != manifest.digest:
                raise RCPError(
                    code="bootstrap_source_changed",
                    message="Bootstrap initialization changed during acceptance preparation.",
                )
            repeated_default = self.git.resolve_commit(
                self.repository_root,
                f"refs/heads/{self.default_branch}",
            )
            if repeated_default != default_head:
                raise RCPError(
                    code="bootstrap_default_head_changed",
                    message="Default branch changed during acceptance preparation.",
                )
            return self.commits.commit_or_observe(
                worktree=self.worktree,
                branch=self.branch,
                proposal_commit=proposal,
                operation_id=self.operation_id,
                manifest_digest=manifest.digest,
                desired_files=accepted_files,
            )

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        lock = self.worktrees_directory / f".bootstrap-{self.operation_id}.lock"
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(lock, flags, 0o600)
        except OSError as error:
            raise RCPError(
                code="bootstrap_operation_lock_invalid",
                message="Bootstrap operation lock could not be opened safely.",
            ) from error
        try:
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _verify_authoritative_inputs(
        self,
        *,
        manifest: ManagedInitManifest,
        default_head: str,
        proposal_commit: str,
    ) -> None:
        default_paths = self.commits.controlled_tree_paths(
            root=self.repository_root,
            commit=default_head,
        )
        if default_paths:
            self._verify_commit_manifest(
                manifest=manifest,
                commit=default_head,
                label="default branch",
            )
        if not self.commits.is_ancestor(
            root=self.repository_root,
            ancestor=default_head,
            descendant=proposal_commit,
        ):
            raise RCPError(
                code="bootstrap_proposal_base_invalid",
                message="Bootstrap proposal does not descend from the current default branch.",
            )
        self._verify_commit_manifest(
            manifest=manifest,
            commit=proposal_commit,
            label="proposal",
        )

    def _verify_commit_manifest(
        self,
        *,
        manifest: ManagedInitManifest,
        commit: str,
        label: str,
    ) -> None:
        expected = manifest.file_map()
        observed_paths = self.commits.controlled_tree_paths(
            root=self.repository_root,
            commit=commit,
        )
        if observed_paths != tuple(sorted(expected)):
            raise RCPError(
                code="bootstrap_unexpected_managed_content",
                message=f"Bootstrap {label} does not contain exactly the init manifest.",
            )
        project_bytes = self.commits.read_tree_file(
            root=self.repository_root,
            commit=commit,
            relative=_PROJECT_RECORD_PATH,
        )
        project = _project_from_bytes(project_bytes, label=label)
        if project.state is not ProjectState.BOOTSTRAPPING:
            raise RCPError(
                code="bootstrap_project_state_invalid",
                message=f"Bootstrap {label} Project state is not bootstrapping.",
                context={"observed_state": project.state.value},
            )
        if project != manifest.project:
            raise RCPError(
                code="bootstrap_project_identity_mismatch",
                message=f"Bootstrap {label} ProjectRecord differs from the init manifest.",
            )
        for relative, expected_content in expected.items():
            observed_content = self.commits.read_tree_file(
                root=self.repository_root,
                commit=commit,
                relative=relative,
            )
            if observed_content != expected_content:
                raise RCPError(
                    code="bootstrap_init_manifest_mismatch",
                    message=f"Bootstrap {label} differs from the init manifest.",
                    context={"path": relative},
                )

    def _prepare_worktree(self, proposal_commit: str) -> None:
        try:
            branch_head = self.git.resolve_commit(
                self.repository_root,
                f"refs/heads/{self.branch}",
            )
        except RCPError as error:
            if error.code != "git_revision_not_found":
                raise
            branch_head = proposal_commit
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
            )
            self.git.create_or_observe(spec)

    def _write_files(self, files: dict[str, bytes]) -> None:
        for index, (relative, content) in enumerate(sorted(files.items())):
            destination = _regular_path(self.worktree, relative)
            try:
                if destination.read_bytes() == content:
                    continue
            except OSError as error:
                raise RCPError(
                    code="bootstrap_worktree_invalid",
                    message="Bootstrap worktree manifest file could not be read.",
                    context={"path": relative},
                ) from error

            temporary = self.worktrees_directory / (
                f".bootstrap-{self.operation_id}-{index}.tmp"
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
                    code="bootstrap_worktree_write_failed",
                    message="Bootstrap acceptance file could not be written atomically.",
                    context={"path": relative},
                ) from error


def _directory_prefixes(files: set[str]) -> frozenset[str]:
    directories: set[str] = set()
    for relative in files:
        pure = PurePosixPath(relative)
        if not relative.startswith(f"{PROJECT_DIR_NAME}/"):
            continue
        parent = pure.parent
        while parent.as_posix() != ".":
            directories.add(parent.as_posix())
            if parent.as_posix() == PROJECT_DIR_NAME:
                break
            parent = parent.parent
    return frozenset(directories)


def _validate_managed_tree(
    root: Path,
    *,
    expected_files: set[str],
    expected_directories: frozenset[str],
) -> None:
    managed = safe_repository_path(root, PROJECT_DIR_NAME, managed_only=True)
    if managed.is_symlink() or not managed.is_dir():
        raise RCPError(
            code="bootstrap_init_manifest_invalid",
            message="Bootstrap managed directory is missing or unsafe.",
        )
    try:
        discovered = sorted(managed.rglob("*"), key=lambda path: path.as_posix())
    except OSError as error:
        raise RCPError(
            code="bootstrap_init_manifest_invalid",
            message="Bootstrap managed directory could not be inspected.",
        ) from error
    for path in discovered:
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise RCPError(
                code="bootstrap_init_manifest_symlink",
                message="Bootstrap init manifest must not contain symbolic links.",
                context={"path": relative},
            )
        if path.is_dir():
            allowed = relative in expected_directories
        elif path.is_file():
            allowed = relative in expected_files
        else:
            allowed = False
        if not allowed:
            raise RCPError(
                code="bootstrap_unexpected_managed_content",
                message="Bootstrap init manifest contains an unexpected managed entry.",
                context={"path": relative},
            )


def _validate_control_worktree(root: Path, expected_files: frozenset[str]) -> None:
    expected_directories = _directory_prefixes(set(expected_files))
    _validate_managed_tree(
        root,
        expected_files=set(expected_files),
        expected_directories=expected_directories,
    )
    _regular_path(root, PROJECT_CONFIG_NAME)


def _regular_path(root: Path, relative: str) -> Path:
    path = safe_repository_path(
        root,
        relative,
        managed_only=relative.startswith(f"{PROJECT_DIR_NAME}/"),
    )
    if path.is_symlink() or not path.is_file():
        raise RCPError(
            code="bootstrap_init_manifest_invalid",
            message="Bootstrap init manifest path must be a regular file.",
            context={"path": relative},
        )
    return path


def _project_from_bytes(content: bytes | None, *, label: str) -> ProjectRecord:
    if content is None:
        raise RCPError(
            code="bootstrap_project_record_missing",
            message=f"Bootstrap {label} is missing its ProjectRecord.",
        )
    try:
        payload = load_yaml(content.decode("utf-8"))
        return ProjectRecord.model_validate(payload)
    except (UnicodeError, ValueError, SerializationError) as error:
        raise RCPError(
            code="bootstrap_project_record_invalid",
            message=f"Bootstrap {label} ProjectRecord is malformed.",
        ) from error


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
