from __future__ import annotations

import os
import tempfile
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Literal

from pydantic import ValidationError

from researchctl.config import ProjectConfig, dump_project_config, load_project_config
from researchctl.constants import PROJECT_CONFIG_NAME, PROJECT_DIR_NAME, PROTOCOL_VERSION
from researchctl.domain.enums import ProjectState
from researchctl.domain.ids import new_id
from researchctl.domain.models import (
    AgentPolicy,
    ProjectPolicy,
    ProjectRecord,
    RepositoryIdentity,
)
from researchctl.domain.types import utc_now
from researchctl.errors import (
    ConflictError,
    RCPError,
    ProtocolCompatibilityError,
    ProtocolLockError,
)
from researchctl.repository import (
    GitRepository,
    discover_repository,
    safe_repository_path,
)
from researchctl.schema import generate_schema_files, schema_manifest_digest
from researchctl.serialization import dump_yaml, load_model

ActionKind = Literal["create", "preserve", "unchanged", "conflict"]
_MANAGED_DIRS = (
    "bootstrap",
    "tasks",
    "runs",
    "submissions",
    "decisions",
    "reports",
    "impacts",
)


@dataclass(frozen=True, slots=True)
class FileAction:
    path: str
    action: ActionKind
    content: bytes | None = None
    reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "action": self.action,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class InitPlan:
    repository: GitRepository
    project: ProjectRecord
    actions: tuple[FileAction, ...]
    warnings: tuple[str, ...] = ()

    @property
    def conflicts(self) -> tuple[FileAction, ...]:
        return tuple(action for action in self.actions if action.action == "conflict")

    @property
    def creates(self) -> tuple[FileAction, ...]:
        return tuple(action for action in self.actions if action.action == "create")

    def as_dict(self) -> dict[str, object]:
        return {
            "repository": str(self.repository.root),
            "project_id": self.project.project_id,
            "project_key": self.project.key,
            "actions": [action.as_dict() for action in self.actions],
        }


@dataclass(frozen=True, slots=True)
class InitResult:
    repository: Path
    project_id: str
    created: tuple[str, ...]
    preserved: tuple[str, ...]
    warnings: tuple[str, ...]
    dry_run: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "repository": str(self.repository),
            "project_id": self.project_id,
            "created": list(self.created),
            "preserved": list(self.preserved),
            "warnings": list(self.warnings),
            "dry_run": self.dry_run,
        }


def _project_key(name: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.")
    if not value or not value[0].isalpha():
        value = f"project-{value or 'research'}"
    return value[:64]


def _default_policy() -> bytes:
    policy = ProjectPolicy(
        agent=AgentPolicy(
            accepted_paths_denied=(
                ".research/decisions/**",
                ".research/policies/**",
                ".research/project.yaml",
                ".research/impacts/**",
                ".research/reports/**",
                ".research/tasks/**",
            )
        )
    )
    return dump_yaml(policy).encode("utf-8")


def _existing_project(root: Path, config: ProjectConfig | None) -> ProjectRecord | None:
    project_path = safe_repository_path(
        root,
        config.project_file if config else ".research/project.yaml",
        managed_only=True,
    )
    if not project_path.exists():
        return None
    return load_model(project_path, ProjectRecord)


def _classify_create(root: Path, relative: str, content: bytes) -> FileAction:
    path = safe_repository_path(root, relative, managed_only=True)
    if not path.exists():
        return FileAction(path=relative, action="create", content=content)
    if not path.is_file():
        return FileAction(
            path=relative,
            action="conflict",
            reason="managed path exists but is not a regular file",
        )
    if path.read_bytes() == content:
        return FileAction(path=relative, action="unchanged")
    return FileAction(
        path=relative,
        action="conflict",
        reason="managed generated file differs from this CLI version",
    )


def plan_init(
    path: Path,
    *,
    name: str | None = None,
    key: str | None = None,
    default_branch: str | None = None,
    now: datetime | None = None,
    id_factory: Callable[[str], str] = new_id,
) -> InitPlan:
    repository = discover_repository(path, default_branch=default_branch)
    root = repository.root
    warnings: list[str] = []
    schema_files = generate_schema_files()
    expected_manifest_digest = schema_manifest_digest(schema_files)

    config_path = safe_repository_path(root, PROJECT_CONFIG_NAME)
    config: ProjectConfig | None = None
    if config_path.exists():
        config = load_project_config(config_path)
        if config.protocol_version != PROTOCOL_VERSION:
            raise ProtocolCompatibilityError(config.protocol_version, PROTOCOL_VERSION)
        if config.schema_manifest_digest != expected_manifest_digest:
            raise ProtocolLockError(
                "schema manifest",
                found=config.schema_manifest_digest,
                expected=expected_manifest_digest,
            )

    project = _existing_project(root, config)
    if (
        project is None
        and repository.remote_url is not None
        and repository.default_branch_source not in {"explicit", "origin_head"}
    ):
        raise RCPError(
            code="ambiguous_default_branch",
            message="The repository remote does not declare its default branch.",
            remediation="Repeat init with --default-branch BRANCH.",
            context={"observed_branch": repository.default_branch},
        )

    if (
        project is not None
        and config is not None
        and project.project_id != config.project_id
    ):
        raise ConflictError(
            "Project ID differs between .researchctl.toml and project record.",
            paths=[PROJECT_CONFIG_NAME, config.project_file],
        )

    if project is None:
        project_name = name or root.name
        project_key = key or _project_key(root.name)
        project_id = config.project_id if config else id_factory("project")
        project = ProjectRecord(
            project_id=project_id,
            key=project_key,
            name=project_name,
            state=ProjectState.BOOTSTRAPPING,
            repository=RepositoryIdentity(
                default_branch=repository.default_branch,
                remote_url=repository.remote_url,
            ),
            created_at=now or utc_now(),
        )
    else:
        if name is not None and name != project.name:
            warnings.append("Existing project name was preserved.")
        if key is not None and key != project.key:
            warnings.append("Existing project key was preserved.")

    if config is None:
        config = ProjectConfig(
            project_id=project.project_id,
            schema_manifest_digest=expected_manifest_digest,
        )

    actions: list[FileAction] = []
    if config_path.exists():
        actions.append(FileAction(PROJECT_CONFIG_NAME, "preserve"))
    else:
        actions.append(
            FileAction(
                PROJECT_CONFIG_NAME,
                "create",
                dump_project_config(config),
            )
        )

    project_relative = config.project_file
    project_path = safe_repository_path(root, project_relative, managed_only=True)
    if project_path.exists():
        actions.append(FileAction(project_relative, "preserve"))
    else:
        actions.append(
            FileAction(
                project_relative,
                "create",
                dump_yaml(project).encode("utf-8"),
            )
        )

    policy_relative = f"{PROJECT_DIR_NAME}/policies/default.yaml"
    policy_path = safe_repository_path(root, policy_relative, managed_only=True)
    if policy_path.exists():
        actions.append(FileAction(policy_relative, "preserve"))
    else:
        actions.append(FileAction(policy_relative, "create", _default_policy()))

    for schema_name, schema_content in schema_files.items():
        relative = f"{PROJECT_DIR_NAME}/schemas/{schema_name}"
        actions.append(_classify_create(root, relative, schema_content))

    for directory in _MANAGED_DIRS:
        directory_path = safe_repository_path(
            root,
            f"{PROJECT_DIR_NAME}/{directory}",
            managed_only=True,
        )
        if directory_path.exists() and any(directory_path.iterdir()):
            continue
        keep_path = f"{PROJECT_DIR_NAME}/{directory}/.gitkeep"
        if not safe_repository_path(root, keep_path, managed_only=True).exists():
            actions.append(FileAction(keep_path, "create", b""))

    return InitPlan(
        repository=repository,
        project=project,
        actions=tuple(actions),
        warnings=tuple(warnings),
    )


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish_exclusive(
    destination: Path,
    content: bytes,
    *,
    display_path: str,
) -> tuple[int, int] | None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.researchctl-",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, destination, follow_symlinks=False)
        except FileExistsError as exc:
            if destination.is_file() and destination.read_bytes() == content:
                return None
            raise ConflictError(
                "A managed file changed while initialization was applying.",
                paths=[display_path],
            ) from exc
        published = destination.stat(follow_symlinks=False)
        _fsync_directory(destination.parent)
        return published.st_dev, published.st_ino
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _remove_if_unchanged(destination: Path, identity: tuple[int, int]) -> None:
    try:
        observed = destination.stat(follow_symlinks=False)
    except FileNotFoundError:
        return
    if (observed.st_dev, observed.st_ino) == identity:
        destination.unlink()
        _fsync_directory(destination.parent)


def apply_init(plan: InitPlan, *, dry_run: bool = False) -> InitResult:
    if plan.conflicts:
        raise ConflictError(
            "Initialization found conflicting generated files.",
            paths=[action.path for action in plan.conflicts],
        )

    created: list[str] = []
    published: list[tuple[str, tuple[int, int]]] = []
    concurrently_preserved: list[str] = []
    if not dry_run:
        try:
            for action in plan.creates:
                assert action.content is not None
                destination = safe_repository_path(plan.repository.root, action.path)
                identity = _publish_exclusive(
                    destination,
                    action.content,
                    display_path=action.path,
                )
                if identity is None:
                    concurrently_preserved.append(action.path)
                else:
                    created.append(action.path)
                    published.append((action.path, identity))
        except Exception:
            for relative, identity in reversed(published):
                destination = safe_repository_path(plan.repository.root, relative)
                _remove_if_unchanged(destination, identity)
            raise
    else:
        created = [action.path for action in plan.creates]

    preserved = [
        action.path
        for action in plan.actions
        if action.action in {"preserve", "unchanged"}
    ]
    preserved.extend(concurrently_preserved)
    return InitResult(
        repository=plan.repository.root,
        project_id=plan.project.project_id,
        created=tuple(created),
        preserved=tuple(preserved),
        warnings=plan.warnings,
        dry_run=dry_run,
    )


def initialize_project(
    path: Path,
    *,
    name: str | None = None,
    key: str | None = None,
    default_branch: str | None = None,
    dry_run: bool = False,
) -> InitResult:
    try:
        plan = plan_init(
            path,
            name=name,
            key=key,
            default_branch=default_branch,
        )
    except ValidationError:
        raise
    return apply_init(plan, dry_run=dry_run)
