from __future__ import annotations

import errno
import os
import re
import stat
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    StrictBool,
    StrictInt,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from researchctl.domain.types import NonEmptyStr, Sha256Digest, ShortText, utc_now
from researchctl.errors import RCPError
from researchctl.serialization import SerializationError, load_yaml
from researchctl.services.run_preflight import (
    GPUObservation,
    IdentityObservation,
    LocalRunPreflight,
    StaticIdentityResolver,
)


_MAX_PROFILE_BYTES = 64 * 1024
_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_PROFILE_REMEDIATION = (
    "Create a reviewed version 1 host profile at the explicit path before "
    "starting a local run."
)
_CANONICAL_REMEDIATION = (
    "Render canonical safe YAML that matches the version 1 host-profile schema."
)


def _canonical_host(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("host must be a string")
    if not value or len(value) > 253 or value != value.lower():
        raise ValueError("host is not canonical")
    labels = value.split(".")
    if any(_HOST_LABEL.fullmatch(label) is None for label in labels):
        raise ValueError("host is not canonical")
    return value


def _parse_observed_at(value: object) -> datetime:
    if isinstance(value, datetime):
        observed = value
    elif type(value) is str:
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            observed = datetime.fromisoformat(normalized)
        except ValueError as error:
            raise ValueError("observed_at must be an RFC 3339 timestamp") from error
    else:
        raise ValueError("observed_at must be an RFC 3339 timestamp")
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise ValueError("observed_at must include a timezone")
    return observed


def _to_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


def _serialize_utc(value: datetime) -> str:
    value = value.astimezone(UTC)
    timespec = "microseconds" if value.microsecond else "seconds"
    return value.isoformat(timespec=timespec).replace("+00:00", "Z")


def _strict_sequence(value: object) -> object:
    if isinstance(value, list):
        return tuple(value)
    if isinstance(value, tuple):
        return value
    raise ValueError("observation collection must be a YAML sequence")


ObservedAt = Annotated[
    datetime,
    BeforeValidator(_parse_observed_at),
    AfterValidator(_to_utc),
    PlainSerializer(_serialize_utc, return_type=str),
]
ControlledPath = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=8192),
]


class _ProfileModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        str_strip_whitespace=False,
        validate_default=True,
    )


class ProfileIdentityObservation(_ProfileModel):
    kind: Literal["config", "dataset", "checkpoint", "environment", "other"]
    logical_id: ShortText
    version: NonEmptyStr | None = None
    digest: Sha256Digest | None = None
    uri: NonEmptyStr | None = None

    @model_validator(mode="after")
    def require_version_or_digest(self) -> ProfileIdentityObservation:
        if self.version is None and self.digest is None:
            raise ValueError("identity observation requires version or digest")
        return self


class ProfileGPUObservation(_ProfileModel):
    gpu_uuid: ShortText
    gpu_type: ShortText
    memory_gb: Annotated[StrictInt, Field(ge=0)]
    available: StrictBool
    observed_at: ObservedAt


IdentityObservations = Annotated[
    tuple[ProfileIdentityObservation, ...],
    BeforeValidator(_strict_sequence),
]
GPUObservations = Annotated[
    tuple[ProfileGPUObservation, ...],
    BeforeValidator(_strict_sequence),
]


class LocalRunProfile(_ProfileModel):
    """Credential-free, host-bound observations for the local run backend."""

    version: Literal[1]
    host: str
    identity_observations: IdentityObservations
    gpu_observations: GPUObservations
    minimum_free_bytes: Annotated[StrictInt, Field(ge=0)]
    controlled_path: ControlledPath = os.defpath

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        return _canonical_host(value)

    @field_validator("controlled_path")
    @classmethod
    def validate_controlled_path(cls, value: str) -> str:
        if any(character in value for character in "\x00\r\n=$"):
            raise ValueError("controlled_path contains forbidden characters")
        entries = value.split(os.pathsep)
        if any(not entry for entry in entries) or len(entries) != len(set(entries)):
            raise ValueError("controlled_path entries must be non-empty and unique")
        for entry in entries:
            path = PurePosixPath(entry)
            if not path.is_absolute() or path.as_posix() != entry:
                raise ValueError("controlled_path entries must be canonical absolute paths")
        return value

    @model_validator(mode="after")
    def require_unique_observations(self) -> LocalRunProfile:
        identities = [
            (observation.kind, observation.logical_id)
            for observation in self.identity_observations
        ]
        if len(identities) != len(set(identities)):
            raise ValueError("identity observations must be unique")
        gpu_uuids = [observation.gpu_uuid for observation in self.gpu_observations]
        if len(gpu_uuids) != len(set(gpu_uuids)):
            raise ValueError("GPU observations must use unique UUIDs")
        return self

    @classmethod
    def load(cls, path: Path, expected_host: str) -> Self:
        profile_path = Path(path)
        content = _read_profile(profile_path)
        try:
            payload = load_yaml(content.decode("utf-8"))
            profile = cls.model_validate(payload)
        except (UnicodeError, SerializationError, ValidationError) as error:
            raise RCPError(
                code="run_host_profile_invalid",
                message="The local run host profile is malformed or unsupported.",
                remediation=_CANONICAL_REMEDIATION,
                context={
                    "path": str(profile_path),
                    "error_type": type(error).__name__,
                },
            ) from error

        try:
            canonical_expected_host = _canonical_host(expected_host)
        except ValueError as error:
            raise RCPError(
                code="run_host_identity_invalid",
                message="The observed local hostname is not canonical.",
                remediation="Pass the canonical local hostname explicitly.",
            ) from error
        if profile.host != canonical_expected_host:
            raise RCPError(
                code="run_host_profile_host_mismatch",
                message="The local run host profile belongs to a different host.",
                remediation="Use the reviewed profile for this exact host.",
                context={
                    "profile_host": profile.host,
                    "expected_host": canonical_expected_host,
                },
            )
        return profile

    def build_identity_resolver(self) -> StaticIdentityResolver:
        return StaticIdentityResolver(
            tuple(
                IdentityObservation(
                    kind=observation.kind,
                    logical_id=observation.logical_id,
                    version=observation.version,
                    digest=observation.digest,
                    uri=observation.uri,
                )
                for observation in self.identity_observations
            )
        )

    def build_gpu_inventory(self) -> tuple[GPUObservation, ...]:
        return tuple(
            GPUObservation(
                gpu_uuid=observation.gpu_uuid,
                gpu_type=observation.gpu_type,
                memory_gb=observation.memory_gb,
                available=observation.available,
                observed_at=observation.observed_at,
            )
            for observation in self.gpu_observations
        )

    def build_preflight(
        self,
        *,
        inventory_max_age_seconds: float = 30.0,
        clock: Callable[[], datetime] = utc_now,
    ) -> LocalRunPreflight:
        return LocalRunPreflight(
            local_host=self.host,
            identities=self.build_identity_resolver(),
            gpu_inventory=self.build_gpu_inventory(),
            minimum_free_bytes=self.minimum_free_bytes,
            path_environment=self.controlled_path,
            inventory_max_age_seconds=inventory_max_age_seconds,
            clock=clock,
        )


def _read_profile(path: Path) -> bytes:
    try:
        observed = os.lstat(path)
    except FileNotFoundError as error:
        raise RCPError(
            code="run_host_profile_missing",
            message="The explicit local run host profile does not exist.",
            remediation=_PROFILE_REMEDIATION,
            context={"path": str(path)},
        ) from error
    except (OSError, ValueError) as error:
        raise _unreadable(path, error) from error

    if stat.S_ISLNK(observed.st_mode) or not stat.S_ISREG(observed.st_mode):
        raise RCPError(
            code="run_host_profile_unsafe",
            message="The local run host profile must be a regular non-symlink file.",
            remediation=_PROFILE_REMEDIATION,
            context={"path": str(path)},
        )
    if observed.st_size > _MAX_PROFILE_BYTES:
        raise _too_large(path)

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as error:
        raise RCPError(
            code="run_host_profile_missing",
            message="The explicit local run host profile does not exist.",
            remediation=_PROFILE_REMEDIATION,
            context={"path": str(path)},
        ) from error
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise RCPError(
                code="run_host_profile_unsafe",
                message="The local run host profile must be a regular non-symlink file.",
                remediation=_PROFILE_REMEDIATION,
                context={"path": str(path)},
            ) from error
        raise _unreadable(path, error) from error

    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (observed.st_dev, observed.st_ino)
        ):
            raise RCPError(
                code="run_host_profile_unsafe",
                message="The local run host profile changed while it was opened.",
                remediation=_PROFILE_REMEDIATION,
                context={"path": str(path)},
            )
        content = bytearray()
        while len(content) <= _MAX_PROFILE_BYTES:
            chunk = os.read(descriptor, _MAX_PROFILE_BYTES + 1 - len(content))
            if not chunk:
                break
            content.extend(chunk)
        if len(content) > _MAX_PROFILE_BYTES:
            raise _too_large(path)
        return bytes(content)
    except RCPError:
        raise
    except OSError as error:
        raise _unreadable(path, error) from error
    finally:
        os.close(descriptor)


def _too_large(path: Path) -> RCPError:
    return RCPError(
        code="run_host_profile_too_large",
        message=f"The local run host profile exceeds {_MAX_PROFILE_BYTES} bytes.",
        remediation="Remove non-protocol data from the host profile.",
        context={"path": str(path), "maximum_bytes": _MAX_PROFILE_BYTES},
    )


def _unreadable(path: Path, error: BaseException) -> RCPError:
    return RCPError(
        code="run_host_profile_unreadable",
        message="The local run host profile could not be read safely.",
        remediation=_PROFILE_REMEDIATION,
        context={"path": str(path), "error_type": type(error).__name__},
    )
