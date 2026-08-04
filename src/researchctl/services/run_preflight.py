from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Callable

from researchctl.domain.models import InputIdentity, RunSpec, TaskRecord
from researchctl.domain.types import utc_now
from researchctl.errors import RCPError
from researchctl.serialization import canonical_digest


_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def declared_input_identities(spec: RunSpec) -> tuple[InputIdentity, ...]:
    values = [spec.environment]
    if spec.config is not None:
        values.append(spec.config)
    values.extend(spec.inputs)
    return tuple(values)


def validate_task_required_inputs(spec: RunSpec, task: TaskRecord) -> None:
    validate_task_required_input_identities(
        declared_input_identities(spec),
        task,
    )


def validate_task_required_input_identities(
    identities: tuple[InputIdentity, ...],
    task: TaskRecord,
) -> None:
    declared = {
        (identity.kind, identity.logical_id): identity
        for identity in identities
    }
    for required in task.required_inputs:
        key = (required.kind, required.logical_id)
        selected = declared.get(key)
        context = {
            "kind": required.kind.value,
            "logical_id": required.logical_id,
            "required": required.model_dump(
                mode="json",
                exclude_none=True,
            ),
        }
        if selected is None:
            raise RCPError(
                code="run_required_input_missing",
                message="RunSpec does not declare a required Task input.",
                remediation=(
                    "Declare the required input in the RunSpec environment, "
                    "config, or inputs before launching."
                ),
                context=context,
            )

        mismatches = [
            field
            for field in ("version", "digest", "uri")
            if getattr(required, field) is not None
            and getattr(required, field) != getattr(selected, field)
        ]
        if mismatches:
            raise RCPError(
                code="run_required_input_mismatch",
                message="RunSpec declaration does not satisfy a required Task input.",
                remediation="Declare the exact Task-required input identity before launching.",
                context={
                    **context,
                    "mismatches": mismatches,
                    "declared": selected.model_dump(
                        mode="json",
                        exclude_none=True,
                    ),
                },
            )


@dataclass(frozen=True, slots=True)
class IdentityObservation:
    kind: str
    logical_id: str
    version: str | None = None
    digest: str | None = None
    uri: str | None = None

    def __post_init__(self) -> None:
        if not self.kind or not self.logical_id:
            raise ValueError("identity observation requires kind and logical_id")
        if self.version is None and self.digest is None:
            raise ValueError("identity observation requires version or digest")
        if self.digest is not None and not _DIGEST.fullmatch(self.digest):
            raise ValueError("identity observation digest is not canonical")

    def as_dict(self) -> dict[str, str]:
        return {
            key: value
            for key, value in {
                "kind": self.kind,
                "logical_id": self.logical_id,
                "version": self.version,
                "digest": self.digest,
                "uri": self.uri,
            }.items()
            if value is not None
        }


class StaticIdentityResolver:
    """Resolves declared inputs from trusted host-profile observations."""

    def __init__(self, observations: tuple[IdentityObservation, ...]) -> None:
        indexed: dict[tuple[str, str], IdentityObservation] = {}
        for observation in observations:
            key = (observation.kind, observation.logical_id)
            if key in indexed:
                raise ValueError("identity observations must be unique")
            indexed[key] = observation
        self._observations = indexed

    def resolve(self, expected: InputIdentity) -> IdentityObservation:
        key = (expected.kind.value, expected.logical_id)
        observed = self._observations.get(key)
        if observed is None:
            raise RCPError(
                code="run_input_unresolved",
                message="Run input is not present in the trusted host profile.",
                context={"kind": key[0], "logical_id": key[1]},
            )
        mismatches: list[str] = []
        if expected.version is not None and expected.version != observed.version:
            mismatches.append("version")
        if expected.digest is not None and expected.digest != observed.digest:
            mismatches.append("digest")
        if expected.uri is not None and expected.uri != observed.uri:
            mismatches.append("uri")
        if mismatches:
            raise RCPError(
                code="run_input_mismatch",
                message="Run input does not match the trusted host observation.",
                context={
                    "kind": key[0],
                    "logical_id": key[1],
                    "mismatches": mismatches,
                    "expected": expected.model_dump(mode="json", exclude_none=True),
                    "observed": observed.as_dict(),
                },
            )
        return observed


@dataclass(frozen=True, slots=True)
class GPUObservation:
    gpu_uuid: str
    gpu_type: str
    memory_gb: int
    available: bool
    observed_at: datetime

    def __post_init__(self) -> None:
        if (
            not self.gpu_uuid
            or not self.gpu_type
            or self.memory_gb < 0
            or self.observed_at.tzinfo is None
            or self.observed_at.utcoffset() is None
        ):
            raise ValueError("GPU observation is invalid")


@dataclass(frozen=True, slots=True)
class RunPreflightReceipt:
    run_id: str
    spec_digest: str
    host: str
    working_directory: str
    executable: str
    identities: tuple[IdentityObservation, ...]
    gpu_uuids: tuple[str, ...]
    gpu_inventory_observed_at: tuple[str, ...]
    artifact_paths: tuple[str, ...]
    free_bytes: int
    allocation_backend: str
    global_exclusivity: bool
    receipt_digest: str

    def as_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "spec_digest": self.spec_digest,
            "host": self.host,
            "working_directory": self.working_directory,
            "executable": self.executable,
            "identities": [item.as_dict() for item in self.identities],
            "gpu_uuids": list(self.gpu_uuids),
            "gpu_inventory_observed_at": list(self.gpu_inventory_observed_at),
            "artifact_paths": list(self.artifact_paths),
            "free_bytes": self.free_bytes,
            "allocation_backend": self.allocation_backend,
            "global_exclusivity": self.global_exclusivity,
            "receipt_digest": self.receipt_digest,
        }


class LocalRunPreflight:
    """Deterministic preflight for the explicit-host local/static backend."""

    def __init__(
        self,
        *,
        local_host: str,
        identities: StaticIdentityResolver,
        gpu_inventory: tuple[GPUObservation, ...] = (),
        minimum_free_bytes: int = 1024 * 1024,
        path_environment: str | None = None,
        inventory_max_age_seconds: float = 30.0,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if not local_host or any(character in local_host for character in "\x00\r\n"):
            raise ValueError("local_host is invalid")
        if minimum_free_bytes < 0:
            raise ValueError("minimum_free_bytes cannot be negative")
        if inventory_max_age_seconds <= 0:
            raise ValueError("inventory_max_age_seconds must be positive")
        indexed = {item.gpu_uuid: item for item in gpu_inventory}
        if len(indexed) != len(gpu_inventory):
            raise ValueError("GPU observations must use unique UUIDs")
        self.local_host = local_host
        self.identities = identities
        self.gpu_inventory = indexed
        self.minimum_free_bytes = minimum_free_bytes
        self.path_environment = os.defpath if path_environment is None else path_environment
        self.inventory_max_age_seconds = inventory_max_age_seconds
        self._clock = clock

    def check(
        self,
        *,
        spec: RunSpec,
        task: TaskRecord,
        execution_worktree: Path,
        assigned_gpu_uuids: tuple[str, ...] = (),
    ) -> RunPreflightReceipt:
        if spec.task_id != task.task_id:
            raise RCPError(
                code="run_task_mismatch",
                message="RunSpec does not belong to the supplied Task.",
            )
        if spec.requested_host is None:
            raise RCPError(
                code="run_host_required",
                message="The local/static backend requires an explicit requested_host.",
                remediation="Set requested_host; automatic host selection requires the controller.",
            )
        if spec.requested_host != self.local_host:
            raise RCPError(
                code="run_host_mismatch",
                message="RunSpec targets a different host.",
                context={
                    "requested_host": spec.requested_host,
                    "local_host": self.local_host,
                },
            )
        validate_task_required_inputs(spec, task)

        root = self._worktree_root(execution_worktree)
        working_directory = self._safe_existing_directory(
            root,
            spec.working_directory,
            code="run_working_directory_invalid",
        )
        executable = self._resolve_executable(working_directory, spec.argv[0])
        resolved_identities = tuple(
            self.identities.resolve(identity)
            for identity in declared_input_identities(spec)
        )
        checked_at = self._clock()
        if checked_at.tzinfo is None or checked_at.utcoffset() is None:
            raise ValueError("preflight clock must return a timezone-aware datetime")
        gpu_uuids = self._check_gpus(spec, assigned_gpu_uuids, checked_at=checked_at)
        inventory_times = tuple(
            self._timestamp(self.gpu_inventory[gpu_uuid].observed_at)
            for gpu_uuid in gpu_uuids
        )
        artifact_paths = self._check_artifacts(root, task, spec)
        free_bytes = shutil.disk_usage(working_directory).free
        if free_bytes < self.minimum_free_bytes:
            raise RCPError(
                code="run_disk_insufficient",
                message="Run target does not have the required free disk space.",
                context={
                    "required_free_bytes": self.minimum_free_bytes,
                    "observed_free_bytes": free_bytes,
                },
            )

        material = {
            "run_id": spec.run_id,
            "spec_digest": spec.spec_digest,
            "host": self.local_host,
            "working_directory": spec.working_directory,
            "executable": str(executable),
            "identities": [item.as_dict() for item in resolved_identities],
            "gpu_uuids": list(gpu_uuids),
            "gpu_inventory_observed_at": list(inventory_times),
            "artifact_paths": list(artifact_paths),
            "free_bytes": free_bytes,
            "allocation_backend": "local_static",
            "global_exclusivity": False,
        }
        return RunPreflightReceipt(
            run_id=spec.run_id,
            spec_digest=spec.spec_digest,
            host=self.local_host,
            working_directory=spec.working_directory,
            executable=str(executable),
            identities=resolved_identities,
            gpu_uuids=gpu_uuids,
            gpu_inventory_observed_at=inventory_times,
            artifact_paths=artifact_paths,
            free_bytes=free_bytes,
            allocation_backend="local_static",
            global_exclusivity=False,
            receipt_digest=canonical_digest(material),
        )

    @staticmethod
    def _timestamp(value: datetime) -> str:
        timespec = "microseconds" if value.microsecond else "seconds"
        return value.isoformat(timespec=timespec).replace("+00:00", "Z")

    @staticmethod
    def _worktree_root(value: Path) -> Path:
        candidate = value.expanduser()
        if candidate.is_symlink():
            LocalRunPreflight._raise_path("run_worktree_invalid", candidate)
        root = candidate.resolve()
        if not root.is_dir():
            LocalRunPreflight._raise_path("run_worktree_invalid", root)
        return root

    @classmethod
    def _safe_existing_directory(
        cls,
        root: Path,
        relative: str,
        *,
        code: str,
    ) -> Path:
        path = cls._safe_path(root, relative, code=code)
        if not path.is_dir():
            cls._raise_path(code, path)
        return path

    @classmethod
    def _safe_path(cls, root: Path, relative: str, *, code: str) -> Path:
        parts = PurePosixPath(relative).parts
        current = root
        for part in parts:
            current = current / part
            if current.is_symlink():
                cls._raise_path(code, current)
        resolved = current.resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            cls._raise_path(code, resolved)
        return resolved

    @staticmethod
    def _raise_path(code: str, path: Path) -> None:
        raise RCPError(
            code=code,
            message="Run path is missing, unsafe, or escapes the execution worktree.",
            context={"path": str(path)},
        )

    def _resolve_executable(self, cwd: Path, value: str) -> Path:
        if any(character in value for character in "\x00\r\n"):
            self._raise_executable(value)
        if "/" in value or "\\" in value:
            path = PurePosixPath(value)
            if path.is_absolute() or ".." in path.parts or "\\" in value:
                self._raise_executable(value)
            candidate = self._safe_path(cwd, path.as_posix(), code="run_executable_invalid")
        else:
            discovered = shutil.which(value, path=self.path_environment)
            if discovered is None:
                self._raise_executable(value)
            candidate = Path(discovered).resolve()
        if candidate.is_symlink() or not candidate.is_file() or not os.access(candidate, os.X_OK):
            self._raise_executable(value)
        return candidate

    @staticmethod
    def _raise_executable(value: str) -> None:
        raise RCPError(
            code="run_executable_invalid",
            message="Run executable is missing, unsafe, or not executable.",
            context={"executable": value},
        )

    def _check_gpus(
        self,
        spec: RunSpec,
        assigned: tuple[str, ...],
        *,
        checked_at: datetime,
    ) -> tuple[str, ...]:
        if len(assigned) != len(set(assigned)):
            raise RCPError(
                code="run_gpu_assignment_invalid",
                message="Static GPU assignment contains duplicate UUIDs.",
            )
        if len(assigned) != spec.resources.gpu_count:
            raise RCPError(
                code="run_gpu_assignment_invalid",
                message="Static GPU assignment count does not match RunSpec.",
                context={
                    "requested_count": spec.resources.gpu_count,
                    "assigned_count": len(assigned),
                },
            )
        for gpu_uuid in assigned:
            observed = self.gpu_inventory.get(gpu_uuid)
            if observed is None or not observed.available:
                raise RCPError(
                    code="run_gpu_unavailable",
                    message="Assigned GPU is missing or unavailable in fresh local inventory.",
                    context={"gpu_uuid": gpu_uuid},
                )
            age_seconds = (checked_at - observed.observed_at).total_seconds()
            if age_seconds < 0 or age_seconds > self.inventory_max_age_seconds:
                raise RCPError(
                    code="run_gpu_inventory_stale",
                    message="Assigned GPU inventory is not fresh enough for launch.",
                    remediation="Refresh local GPU inventory before retrying preflight.",
                    context={
                        "gpu_uuid": gpu_uuid,
                        "age_seconds": age_seconds,
                        "maximum_age_seconds": self.inventory_max_age_seconds,
                    },
                )
            if spec.resources.gpu_type is not None and (
                observed.gpu_type != spec.resources.gpu_type
            ):
                raise RCPError(
                    code="run_gpu_mismatch",
                    message="Assigned GPU type does not match RunSpec.",
                    context={"gpu_uuid": gpu_uuid},
                )
            minimum_memory = spec.resources.min_gpu_memory_gb
            if minimum_memory is not None and observed.memory_gb < minimum_memory:
                raise RCPError(
                    code="run_gpu_mismatch",
                    message="Assigned GPU memory does not meet RunSpec.",
                    context={"gpu_uuid": gpu_uuid},
                )
        return assigned

    @classmethod
    def _check_artifacts(
        cls,
        root: Path,
        task: TaskRecord,
        spec: RunSpec,
    ) -> tuple[str, ...]:
        observed: list[str] = []
        for declaration in spec.artifact_declarations:
            if not task.permits_write_path(declaration.path):
                raise RCPError(
                    code="run_artifact_scope_violation",
                    message="Declared artifact path is outside the Task write scope.",
                    context={
                        "path": declaration.path,
                        "allowed_write_paths": list(task.allowed_write_paths),
                    },
                )
            candidate = cls._safe_path(
                root,
                declaration.path,
                code="run_artifact_path_invalid",
            )
            if candidate.exists() and candidate.is_symlink():
                cls._raise_path("run_artifact_path_invalid", candidate)
            observed.append(declaration.path)
        return tuple(observed)
