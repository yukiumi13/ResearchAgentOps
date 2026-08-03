from __future__ import annotations

import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from researchctl.adapters._subprocess import (
    CommandResult,
    CommandRunner,
    SubprocessCommandRunner,
)
from researchctl.config import ProjectConfig, load_project_config
from researchctl.constants import PROJECT_CONFIG_NAME, PROJECT_DIR_NAME, PROTOCOL_VERSION
from researchctl.domain.models import ProjectPolicy, ProjectRecord
from researchctl.errors import (
    ProtocolCompatibilityError,
    ProtocolLockError,
    RCPError,
    RepositoryNotFoundError,
)
from researchctl.repository import safe_repository_path
from researchctl.schema import generate_schema_files, schema_manifest_digest
from researchctl.serialization import load_model


_GIT_CONTEXT_ENVIRONMENT = {
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CEILING_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_DIR",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    "GIT_INDEX_FILE",
    "GIT_NAMESPACE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_PREFIX",
    "GIT_WORK_TREE",
}
_MAX_CONFIG_BYTES = 64 * 1024
_MAX_GIT_PATH_OUTPUT_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class ProjectRuntimePaths:
    git_common_dir: Path
    state_directory: Path
    database_path: Path
    worktrees_directory: Path


@dataclass(frozen=True, slots=True)
class ManagedProject:
    repository_root: Path
    config: ProjectConfig
    project: ProjectRecord
    policy: ProjectPolicy
    runtime: ProjectRuntimePaths

    @property
    def project_id(self) -> str:
        return self.project.project_id


class ProjectRuntimeService:
    """Discover accepted project identity and its host-local runtime location."""

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

    def discover(
        self,
        path: Path,
        *,
        expected_project_id: str | None = None,
    ) -> ManagedProject:
        requested = path.expanduser().resolve()
        candidate = requested.parent if requested.is_file() else requested
        repository_root = self._discover_repository_root(candidate)
        git_common_dir = self._discover_git_common_dir(repository_root)

        config = self._load_config(repository_root)
        project = self._load_project(repository_root, config)
        policy = self._load_policy(repository_root)
        self._verify_project_identity(
            config,
            project,
            expected_project_id=expected_project_id,
        )

        state_directory = git_common_dir / "researchctl"
        runtime = ProjectRuntimePaths(
            git_common_dir=git_common_dir,
            state_directory=state_directory,
            database_path=state_directory / "runtime-v1.sqlite3",
            worktrees_directory=state_directory / "worktrees",
        )
        return ManagedProject(
            repository_root=repository_root,
            config=config,
            project=project,
            policy=policy,
            runtime=runtime,
        )

    def ensure_runtime_directories(
        self,
        project_or_paths: ManagedProject | ProjectRuntimePaths,
    ) -> ProjectRuntimePaths:
        paths = (
            project_or_paths.runtime
            if isinstance(project_or_paths, ManagedProject)
            else project_or_paths
        )
        _validate_runtime_layout(paths)
        _require_directory(
            paths.git_common_dir,
            code="git_common_dir_invalid",
            label="Git common directory",
        )

        _check_optional_runtime_directory(paths.state_directory)
        if paths.state_directory.exists():
            _check_optional_runtime_directory(paths.worktrees_directory)
        _check_optional_database(paths.database_path)

        _create_runtime_directory(paths.state_directory)
        _create_runtime_directory(paths.worktrees_directory)
        _check_optional_database(paths.database_path)
        return paths

    def _discover_repository_root(self, candidate: Path) -> Path:
        result = self._git(candidate, "rev-parse", "--show-toplevel")
        if result.returncode != 0:
            raise RepositoryNotFoundError(str(candidate))
        root = _parse_absolute_git_path(result.stdout, kind="repository_root")
        _require_directory(
            root,
            code="git_repository_path_invalid",
            label="Git repository root",
        )
        if candidate != root and root not in candidate.parents:
            raise RCPError(
                code="git_repository_mismatch",
                message="Git returned a worktree that does not contain the requested path.",
                context={"path": str(candidate), "root": str(root)},
            )
        return root

    def _discover_git_common_dir(self, repository_root: Path) -> Path:
        result = self._git(
            repository_root,
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        )
        if result.returncode != 0:
            raise RCPError(
                code="git_common_dir_failed",
                message="Git could not resolve its common directory.",
                context={"returncode": result.returncode},
            )
        common_dir = _parse_absolute_git_path(result.stdout, kind="git_common_dir")
        _require_directory(
            common_dir,
            code="git_common_dir_invalid",
            label="Git common directory",
        )
        return common_dir

    def _git(self, path: Path, *args: str) -> CommandResult:
        argv = (
            "git",
            "-c",
            "core.fsmonitor=false",
            "-C",
            str(path),
            *args,
        )
        try:
            return self._runner.run(
                argv,
                cwd=None,
                env=_git_environment(),
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
                message=f"Git command timed out after {self._timeout_seconds:g} seconds.",
                context={"path": str(path)},
            ) from exc
        except OSError as exc:
            raise RCPError(
                code="git_execution_failed",
                message="Git could not be executed.",
                context={"error_type": type(exc).__name__},
            ) from exc

    @staticmethod
    def _load_config(repository_root: Path) -> ProjectConfig:
        path = safe_repository_path(repository_root, PROJECT_CONFIG_NAME)
        _require_protocol_file(
            path,
            code="project_config_invalid",
            label="Project config",
            max_bytes=_MAX_CONFIG_BYTES,
        )
        try:
            config = load_project_config(path)
        except (OSError, UnicodeError, ValueError) as exc:
            raise RCPError(
                code="project_config_invalid",
                message=f"{PROJECT_CONFIG_NAME} is not a valid project config.",
                context={"error_type": type(exc).__name__},
            ) from exc
        if config.protocol_version != PROTOCOL_VERSION:
            raise ProtocolCompatibilityError(config.protocol_version, PROTOCOL_VERSION)
        expected_digest = schema_manifest_digest(generate_schema_files())
        if config.schema_manifest_digest != expected_digest:
            raise ProtocolLockError(
                "schema manifest",
                found=config.schema_manifest_digest,
                expected=expected_digest,
            )
        return config

    @staticmethod
    def _load_project(repository_root: Path, config: ProjectConfig) -> ProjectRecord:
        path = safe_repository_path(
            repository_root,
            config.project_file,
            managed_only=True,
        )
        _require_protocol_file(
            path,
            code="project_record_invalid",
            label="Project record",
        )
        try:
            return load_model(path, ProjectRecord)
        except (OSError, UnicodeError, ValueError) as exc:
            raise RCPError(
                code="project_record_invalid",
                message="The managed ProjectRecord is invalid.",
                context={"error_type": type(exc).__name__},
            ) from exc

    @staticmethod
    def _load_policy(repository_root: Path) -> ProjectPolicy:
        relative = f"{PROJECT_DIR_NAME}/policies/default.yaml"
        path = safe_repository_path(repository_root, relative, managed_only=True)
        _require_protocol_file(
            path,
            code="project_policy_invalid",
            label="Default project policy",
        )
        try:
            return load_model(path, ProjectPolicy)
        except (OSError, UnicodeError, ValueError) as exc:
            raise RCPError(
                code="project_policy_invalid",
                message="The default ProjectPolicy is invalid.",
                context={"error_type": type(exc).__name__},
            ) from exc

    @staticmethod
    def _verify_project_identity(
        config: ProjectConfig,
        project: ProjectRecord,
        *,
        expected_project_id: str | None,
    ) -> None:
        if config.project_id != project.project_id:
            raise RCPError(
                code="project_id_mismatch",
                message="Project IDs differ between config and ProjectRecord.",
                context={
                    "config_project_id": config.project_id,
                    "record_project_id": project.project_id,
                },
            )
        if expected_project_id is not None and project.project_id != expected_project_id:
            raise RCPError(
                code="project_id_mismatch",
                message="The discovered project does not match the expected project ID.",
                context={
                    "expected_project_id": expected_project_id,
                    "actual_project_id": project.project_id,
                },
            )


def discover_managed_project(
    path: Path,
    *,
    expected_project_id: str | None = None,
    runner: CommandRunner | None = None,
    timeout_seconds: float = 10.0,
) -> ManagedProject:
    return ProjectRuntimeService(
        runner=runner,
        timeout_seconds=timeout_seconds,
    ).discover(path, expected_project_id=expected_project_id)


def ensure_runtime_directories(paths: ProjectRuntimePaths) -> ProjectRuntimePaths:
    return ProjectRuntimeService().ensure_runtime_directories(paths)


def _git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for key in tuple(environment):
        if key in _GIT_CONTEXT_ENVIRONMENT or key.startswith("GIT_CONFIG_"):
            environment.pop(key, None)
    environment.pop("GIT_CONFIG_PARAMETERS", None)
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    return environment


def _parse_absolute_git_path(output: str, *, kind: str) -> Path:
    if len(output.encode("utf-8")) > _MAX_GIT_PATH_OUTPUT_BYTES:
        raise RCPError(
            code="git_output_invalid",
            message=f"Git returned oversized {kind} output.",
        )
    lines = output.splitlines()
    if len(lines) != 1 or not lines[0] or "\x00" in lines[0]:
        raise RCPError(
            code="git_output_invalid",
            message=f"Git returned malformed {kind} output.",
        )
    lexical = Path(lines[0])
    if not lexical.is_absolute():
        raise RCPError(
            code="git_output_invalid",
            message=f"Git returned a non-absolute {kind} path.",
        )
    try:
        return lexical.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RCPError(
            code="git_output_invalid",
            message=f"Git returned an unusable {kind} path.",
            context={"error_type": type(exc).__name__},
        ) from exc


def _require_protocol_file(
    path: Path,
    *,
    code: str,
    label: str,
    max_bytes: int | None = None,
) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise RCPError(
            code=code,
            message=f"{label} is missing.",
            context={"path": str(path)},
        ) from exc
    except OSError as exc:
        raise RCPError(
            code=code,
            message=f"{label} could not be inspected.",
            context={"error_type": type(exc).__name__},
        ) from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise RCPError(
            code=code,
            message=f"{label} must be a regular, non-symlink file.",
            context={"path": str(path)},
        )
    if max_bytes is not None and metadata.st_size > max_bytes:
        raise RCPError(
            code=code,
            message=f"{label} exceeds its size limit.",
            context={"max_bytes": max_bytes},
        )


def _validate_runtime_layout(paths: ProjectRuntimePaths) -> None:
    common_dir = paths.git_common_dir
    expected_state = common_dir / "researchctl"
    if (
        not common_dir.is_absolute()
        or paths.state_directory != expected_state
        or paths.database_path != expected_state / "runtime-v1.sqlite3"
        or paths.worktrees_directory != expected_state / "worktrees"
    ):
        raise RCPError(
            code="runtime_layout_invalid",
            message="Runtime paths do not match the managed Git-common-directory layout.",
        )


def _require_directory(path: Path, *, code: str, label: str) -> None:
    try:
        metadata = path.lstat()
    except (FileNotFoundError, OSError) as exc:
        raise RCPError(
            code=code,
            message=f"{label} is missing or inaccessible.",
            context={"path": str(path), "error_type": type(exc).__name__},
        ) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise RCPError(
            code=code,
            message=f"{label} must be a non-symlink directory.",
            context={"path": str(path)},
        )


def _check_optional_runtime_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise RCPError(
            code="runtime_directory_invalid",
            message="A runtime directory could not be inspected.",
            context={"path": str(path), "error_type": type(exc).__name__},
        ) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise RCPError(
            code="runtime_directory_invalid",
            message="Runtime paths must be non-symlink directories.",
            context={"path": str(path)},
        )


def _check_optional_database(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise RCPError(
            code="runtime_database_path_invalid",
            message="The runtime database path could not be inspected.",
            context={"error_type": type(exc).__name__},
        ) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise RCPError(
            code="runtime_database_path_invalid",
            message="The runtime database path must be a regular, non-symlink file.",
            context={"path": str(path)},
        )


def _create_runtime_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        raise RCPError(
            code="runtime_directory_create_failed",
            message="A host-local runtime directory could not be created.",
            context={"path": str(path), "error_type": type(exc).__name__},
        ) from exc
    _check_optional_runtime_directory(path)
