from __future__ import annotations

import errno
import hashlib
import json
import os
import signal
import stat
import subprocess
import threading
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol

from pydantic import TypeAdapter, ValidationError

from researchctl.domain.enums import (
    ArtifactVerification,
    FailureClass,
    RetentionClass,
    RunAttemptState,
    RunOutcome,
)
from researchctl.domain.models import (
    ArtifactRef,
    RunAttempt,
    RunAttemptEvent,
    RunResult,
    RunSpec,
)
from researchctl.domain.types import (
    HumanKey,
    OperationId,
    RunAttemptId,
    utc_now,
)
from researchctl.errors import RCPError
from researchctl.serialization import canonical_digest, canonical_json_bytes
from researchctl.services.run_preflight import RunPreflightReceipt

_ATTEMPT_ID = TypeAdapter(RunAttemptId)
_OPERATION_ID = TypeAdapter(OperationId)
_HUMAN_KEY = TypeAdapter(HumanKey)
_MARKER_VERSION = 1
_MAX_MARKER_BYTES = 256 * 1024
_MAX_LOG_SUMMARY_CHARS = 4096
_DEFAULT_ALLOWED_ENVIRONMENT = frozenset(
    {
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "PATH",
        "TMPDIR",
        "TZ",
    }
)
_SECRET_ENVIRONMENT_PARTS = (
    "API_KEY",
    "CREDENTIAL",
    "PASSWORD",
    "SECRET",
    "TOKEN",
)
_SECRET_ENVIRONMENT_PREFIXES = (
    "AWS_",
    "AZURE_",
    "GITHUB_",
    "GITLAB_",
    "GOOGLE_",
    "LINEAR_",
    "OPENAI_",
    "SSH_",
)


class RunEventCallback(Protocol):
    """Durably appends an event before returning to the executor."""

    def __call__(self, event: RunAttemptEvent) -> None: ...


TerminalKind = Literal[
    "exited",
    "signaled",
    "timed_out",
    "launch_failed",
    "artifact_failed",
]


@dataclass(frozen=True, slots=True)
class ProcessTerminalObservation:
    kind: TerminalKind
    error_code: str | None
    failure_class: FailureClass | None
    exit_code: int | None
    signal_number: int | None = None
    detail: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            key: value
            for key, value in {
                "kind": self.kind,
                "error_code": self.error_code,
                "failure_class": (
                    self.failure_class.value
                    if self.failure_class is not None
                    else None
                ),
                "exit_code": self.exit_code,
                "signal_number": self.signal_number,
                "detail": self.detail,
            }.items()
            if value is not None
        }

    @classmethod
    def from_dict(cls, value: object) -> ProcessTerminalObservation:
        if not isinstance(value, dict):
            raise ValueError("terminal observation must be an object")
        allowed = {
            "kind",
            "error_code",
            "failure_class",
            "exit_code",
            "signal_number",
            "detail",
        }
        if set(value) - allowed:
            raise ValueError("terminal observation contains unknown fields")
        kind = value.get("kind")
        if kind not in {
            "exited",
            "signaled",
            "timed_out",
            "launch_failed",
            "artifact_failed",
        }:
            raise ValueError("terminal observation kind is invalid")
        failure_value = value.get("failure_class")
        failure_class = (
            FailureClass(failure_value) if failure_value is not None else None
        )
        exit_code = value.get("exit_code")
        signal_number = value.get("signal_number")
        if exit_code is not None and type(exit_code) is not int:
            raise ValueError("terminal exit_code is invalid")
        if signal_number is not None and type(signal_number) is not int:
            raise ValueError("terminal signal_number is invalid")
        error_code = value.get("error_code")
        detail = value.get("detail")
        if error_code is not None and not isinstance(error_code, str):
            raise ValueError("terminal error_code is invalid")
        if detail is not None and not isinstance(detail, str):
            raise ValueError("terminal detail is invalid")
        return cls(
            kind=kind,
            error_code=error_code,
            failure_class=failure_class,
            exit_code=exit_code,
            signal_number=signal_number,
            detail=detail,
        )


@dataclass(frozen=True, slots=True)
class LocalRunExecution:
    attempt: RunAttempt
    result: RunResult
    observation: ProcessTerminalObservation
    stdout_tail: str
    stderr_tail: str
    stdout_truncated: bool
    stderr_truncated: bool
    marker_path: Path
    launched: bool
    observed_existing: bool


@dataclass(frozen=True, slots=True)
class _ArtifactIssue:
    code: str
    path: str
    detail: str


class _BoundedTail:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.buffer = bytearray()
        self.total_bytes = 0
        self.read_error: str | None = None

    def consume(self, stream: object) -> None:
        try:
            while True:
                chunk = stream.read(64 * 1024)  # type: ignore[attr-defined]
                if not chunk:
                    return
                self.total_bytes += len(chunk)
                if len(chunk) >= self.limit:
                    self.buffer[:] = chunk[-self.limit :]
                else:
                    self.buffer.extend(chunk)
                    overflow = len(self.buffer) - self.limit
                    if overflow > 0:
                        del self.buffer[:overflow]
        except OSError as error:
            self.read_error = type(error).__name__
        finally:
            with suppress(OSError):
                stream.close()  # type: ignore[attr-defined]

    @property
    def truncated(self) -> bool:
        return self.total_bytes > len(self.buffer)

    def text(self) -> str:
        return bytes(self.buffer).decode("utf-8", errors="replace")


class LocalRunExecutor:
    """Runs one frozen local attempt and collects bounded terminal evidence.

    The caller owns Git freezing and the durable RunAttempt journal. The event
    callback must return only after its event is durable. A private marker claims
    the attempt before launch; a non-terminal marker is treated as uncertain and
    is never relaunched under the same attempt identity.
    """

    def __init__(
        self,
        *,
        local_host: str,
        timeout_seconds: float = 24 * 60 * 60,
        terminate_grace_seconds: float = 1.0,
        max_tail_bytes: int = 1536,
        base_environment: Mapping[str, str] | None = None,
        environment_allowlist: frozenset[str] = _DEFAULT_ALLOWED_ENVIRONMENT,
        marker_directory: Path | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.local_host = _HUMAN_KEY.validate_python(local_host)
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if terminate_grace_seconds < 0:
            raise ValueError("terminate_grace_seconds cannot be negative")
        if not 1 <= max_tail_bytes <= 64 * 1024:
            raise ValueError("max_tail_bytes must be between 1 and 65536")
        if any(not self._valid_environment_name(name) for name in environment_allowlist):
            raise ValueError("environment allowlist contains an invalid name")
        self.timeout_seconds = timeout_seconds
        self.terminate_grace_seconds = terminate_grace_seconds
        self.max_tail_bytes = max_tail_bytes
        self.base_environment = dict(
            os.environ if base_environment is None else base_environment
        )
        self.environment_allowlist = environment_allowlist
        self.marker_directory = marker_directory
        self.clock = clock

    def execute(
        self,
        *,
        spec: RunSpec,
        execution_worktree: Path,
        preflight: RunPreflightReceipt,
        attempt_id: str,
        operation_id: str,
        event_callback: RunEventCallback,
        retry_of: str | None = None,
    ) -> LocalRunExecution:
        validated_attempt_id = _ATTEMPT_ID.validate_python(attempt_id)
        validated_operation_id = _OPERATION_ID.validate_python(operation_id)
        validated_retry_of = (
            _ATTEMPT_ID.validate_python(retry_of) if retry_of is not None else None
        )
        root = self._worktree_root(execution_worktree)
        cwd = self._validate_preflight(spec, preflight, root)
        process_environment = self._sanitized_environment(preflight.gpu_uuids)
        marker_path = self.marker_path_for(root, validated_attempt_id)
        identity = self._marker_identity(
            spec=spec,
            attempt_id=validated_attempt_id,
            operation_id=validated_operation_id,
        )
        existing = self._claim_or_observe(marker_path, identity)
        if existing is not None:
            return self._observed_execution(existing, marker_path, identity)

        events: list[RunAttemptEvent] = []

        def emit(
            state: RunAttemptState,
            *,
            error_code: str | None = None,
            detail: str | None = None,
            external_ids: dict[str, str] | None = None,
        ) -> None:
            sequence = len(events)
            event = RunAttemptEvent(
                operation_id=validated_operation_id,
                sequence=sequence,
                state=state,
                observed_at=self.clock(),
                idempotency_key=(
                    f"local-run:{validated_attempt_id}:{sequence}:{state.value}"
                ),
                host=self.local_host,
                external_ids=external_ids or {},
                error_code=error_code,
                detail=detail,
            )
            event_callback(event)
            events.append(event)

        emit(RunAttemptState.PREPARING)
        emit(RunAttemptState.SNAPSHOTTED)
        emit(RunAttemptState.PREFLIGHTED)
        emit(RunAttemptState.ALLOCATED)
        emit(RunAttemptState.LAUNCHING)
        self._replace_marker(
            marker_path,
            {**identity, "phase": "launch_intent", "updated_at": self._now_json()},
        )

        stdout = _BoundedTail(self.max_tail_bytes)
        stderr = _BoundedTail(self.max_tail_bytes)
        started_at: datetime | None = None
        timed_out = False
        return_code: int | None = None
        launch_error: OSError | ValueError | None = None
        process: subprocess.Popen[bytes] | None = None
        readers: tuple[threading.Thread, threading.Thread] | None = None

        try:
            # Execute the exact binary resolved by preflight, independent of ambient PATH.
            process = subprocess.Popen(
                [preflight.executable, *spec.argv[1:]],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(cwd),
                env=process_environment,
                shell=False,
                close_fds=True,
                start_new_session=True,
            )
            started_at = self.clock()
            self._replace_marker(
                marker_path,
                {
                    **identity,
                    "phase": "running",
                    "pid": process.pid,
                    "started_at": self._date_json(started_at),
                    "updated_at": self._now_json(),
                },
            )
            emit(
                RunAttemptState.RUNNING,
                external_ids={"local_pid": str(process.pid)},
            )
            assert process.stdout is not None
            assert process.stderr is not None
            readers = (
                threading.Thread(target=stdout.consume, args=(process.stdout,), daemon=True),
                threading.Thread(target=stderr.consume, args=(process.stderr,), daemon=True),
            )
            for reader in readers:
                reader.start()
            try:
                return_code = process.wait(timeout=self.timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                self._terminate_process(process)
                return_code = process.returncode
        except (OSError, ValueError) as error:
            launch_error = error
        finally:
            if process is not None and process.poll() is None:
                self._terminate_process(process)
            if readers is not None:
                for reader in readers:
                    reader.join(timeout=max(self.terminate_grace_seconds, 1.0))

        artifacts: tuple[ArtifactRef, ...] = ()
        artifact_issues: tuple[_ArtifactIssue, ...] = ()
        if launch_error is None:
            emit(RunAttemptState.COLLECTING)
            artifacts, artifact_issues = self._collect_artifacts(
                root=root,
                spec=spec,
                producer_host=preflight.host,
            )

        observation = self._terminal_observation(
            return_code=return_code,
            timed_out=timed_out,
            launch_error=launch_error,
            artifact_issues=artifact_issues,
        )
        terminal_state = (
            RunAttemptState.SUCCEEDED
            if observation.error_code is None
            else RunAttemptState.FAILED
        )
        emit(
            terminal_state,
            error_code=observation.error_code,
            detail=observation.detail,
        )
        attempt = RunAttempt(
            attempt_id=validated_attempt_id,
            run_id=spec.run_id,
            operation_id=validated_operation_id,
            retry_of=validated_retry_of,
            events=tuple(events),
        )
        finished_at = self.clock()
        stdout_tail = stdout.text()
        stderr_tail = stderr.text()
        result = RunResult(
            result_id=self._result_id(validated_attempt_id),
            run_id=spec.run_id,
            run_spec_digest=spec.spec_digest,
            attempt_ids=(validated_attempt_id,),
            outcome=(
                RunOutcome.COMPLETE
                if observation.error_code is None
                else RunOutcome.FAILED
            ),
            started_at=started_at,
            finished_at=finished_at,
            host=preflight.host,
            gpu_uuids=preflight.gpu_uuids,
            exit_code=observation.exit_code,
            failure_class=observation.failure_class,
            artifacts=artifacts,
            log_summary=self._log_summary(
                observation=observation,
                artifact_issues=artifact_issues,
                stdout_tail=stdout_tail,
                stderr_tail=stderr_tail,
            ),
        )
        execution = LocalRunExecution(
            attempt=attempt,
            result=result,
            observation=observation,
            stdout_tail=stdout_tail,
            stderr_tail=stderr_tail,
            stdout_truncated=stdout.truncated,
            stderr_truncated=stderr.truncated,
            marker_path=marker_path,
            launched=process is not None,
            observed_existing=False,
        )
        self._write_terminal_marker(marker_path, identity, execution)
        return execution

    def marker_path_for(self, execution_worktree: Path, attempt_id: str) -> Path:
        validated_attempt_id = _ATTEMPT_ID.validate_python(attempt_id)
        root = self._worktree_root(execution_worktree)
        directory = (
            self.marker_directory
            if self.marker_directory is not None
            else root.parent / ".researchctl-run-markers"
        )
        safe_directory = self._marker_directory(Path(directory))
        return safe_directory / f"{validated_attempt_id}.json"

    def _validate_preflight(
        self,
        spec: RunSpec,
        preflight: RunPreflightReceipt,
        root: Path,
    ) -> Path:
        receipt = preflight.as_dict()
        receipt.pop("receipt_digest")
        expected_artifacts = tuple(
            declaration.path for declaration in spec.artifact_declarations
        )
        mismatches: list[str] = []
        if preflight.receipt_digest != canonical_digest(receipt):
            mismatches.append("receipt_digest")
        if preflight.run_id != spec.run_id:
            mismatches.append("run_id")
        if preflight.spec_digest != spec.spec_digest:
            mismatches.append("spec_digest")
        if preflight.host != self.local_host or preflight.host != spec.requested_host:
            mismatches.append("host")
        if preflight.working_directory != spec.working_directory:
            mismatches.append("working_directory")
        if preflight.artifact_paths != expected_artifacts:
            mismatches.append("artifact_paths")
        if preflight.allocation_backend != "local_static":
            mismatches.append("allocation_backend")
        if preflight.global_exclusivity is not False:
            mismatches.append("global_exclusivity")
        if len(preflight.gpu_inventory_observed_at) != len(preflight.gpu_uuids):
            mismatches.append("gpu_inventory_observed_at")
        if mismatches:
            raise RCPError(
                code="run_preflight_receipt_invalid",
                message="Preflight receipt does not bind the frozen RunSpec.",
                context={"mismatches": sorted(set(mismatches))},
            )
        try:
            _HUMAN_KEY.validate_python(preflight.host)
        except ValidationError as error:
            raise RCPError(
                code="run_preflight_receipt_invalid",
                message="Preflight receipt contains an invalid host identity.",
            ) from error
        cwd = self._safe_directory(root, spec.working_directory)
        return cwd

    @staticmethod
    def _worktree_root(value: Path) -> Path:
        candidate = Path(os.path.abspath(os.fspath(value)))
        if candidate.is_symlink() or not candidate.is_dir():
            raise RCPError(
                code="run_execution_worktree_invalid",
                message="Execution worktree is missing, unsafe, or a symbolic link.",
                context={"path": str(candidate)},
            )
        return candidate.resolve()

    @classmethod
    def _safe_directory(cls, root: Path, relative: str) -> Path:
        current = root
        for part in PurePosixPath(relative).parts:
            current = current / part
            if current.is_symlink():
                cls._raise_artifact_path("run_working_directory_invalid", relative)
        resolved = current.resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            cls._raise_artifact_path("run_working_directory_invalid", relative)
        if not resolved.is_dir():
            cls._raise_artifact_path("run_working_directory_invalid", relative)
        return resolved

    def _sanitized_environment(self, gpu_uuids: tuple[str, ...]) -> dict[str, str]:
        if any(
            not value or any(character in value for character in ",\x00\r\n")
            for value in gpu_uuids
        ):
            raise RCPError(
                code="run_environment_invalid",
                message="GPU UUIDs cannot be represented safely in the process environment.",
            )
        environment: dict[str, str] = {}
        for name in sorted(self.environment_allowlist):
            if self._secret_environment_name(name):
                continue
            value = self.base_environment.get(name)
            if value is None:
                continue
            if "\x00" in value:
                raise RCPError(
                    code="run_environment_invalid",
                    message="Allowed process environment contains a NUL byte.",
                    context={"name": name},
                )
            environment[name] = value
        if gpu_uuids:
            environment["CUDA_VISIBLE_DEVICES"] = ",".join(gpu_uuids)
        return environment

    @staticmethod
    def _valid_environment_name(value: str) -> bool:
        return (
            bool(value)
            and value.isascii()
            and (value[0].isalpha() or value[0] == "_")
            and all(character.isalnum() or character == "_" for character in value)
        )

    @staticmethod
    def _secret_environment_name(value: str) -> bool:
        upper = value.upper()
        return upper.startswith(_SECRET_ENVIRONMENT_PREFIXES) or any(
            part in upper for part in _SECRET_ENVIRONMENT_PARTS
        )

    def _collect_artifacts(
        self,
        *,
        root: Path,
        spec: RunSpec,
        producer_host: str,
    ) -> tuple[tuple[ArtifactRef, ...], tuple[_ArtifactIssue, ...]]:
        artifacts: list[ArtifactRef] = []
        issues: list[_ArtifactIssue] = []
        for declaration in spec.artifact_declarations:
            try:
                digest, size = self._digest_regular_file(root, declaration.path)
            except FileNotFoundError:
                if declaration.required:
                    issues.append(
                        _ArtifactIssue(
                            code="run_artifact_missing",
                            path=declaration.path,
                            detail="Required artifact is missing.",
                        )
                    )
                continue
            except RCPError as error:
                issues.append(
                    _ArtifactIssue(
                        code=error.code,
                        path=declaration.path,
                        detail=error.message,
                    )
                )
                continue
            candidate = root / PurePosixPath(declaration.path)
            artifacts.append(
                ArtifactRef(
                    name=declaration.name,
                    uri=candidate.absolute().as_uri(),
                    digest=digest,
                    size_bytes=size,
                    media_type=declaration.media_type,
                    producer_host=producer_host,
                    retention=RetentionClass.TASK,
                    verification=ArtifactVerification.PRODUCER_VERIFIED,
                )
            )
        return tuple(artifacts), tuple(issues)

    @classmethod
    def _digest_regular_file(cls, root: Path, relative: str) -> tuple[str, int]:
        parts = PurePosixPath(relative).parts
        if not parts:
            cls._raise_artifact_path("run_artifact_invalid", relative)
        directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        opened_directories: list[int] = [directory_fd]
        file_fd: int | None = None
        try:
            for part in parts[:-1]:
                next_fd = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=directory_fd,
                )
                opened_directories.append(next_fd)
                directory_fd = next_fd
            try:
                file_fd = os.open(
                    parts[-1],
                    os.O_RDONLY | os.O_NOFOLLOW,
                    dir_fd=directory_fd,
                )
            except OSError as error:
                if error.errno == errno.ENOENT:
                    raise FileNotFoundError(relative) from error
                cls._raise_artifact_path("run_artifact_invalid", relative)
            before = os.fstat(file_fd)
            if not stat.S_ISREG(before.st_mode):
                cls._raise_artifact_path("run_artifact_invalid", relative)
            digest = hashlib.sha256()
            size = 0
            while True:
                chunk = os.read(file_fd, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
            after = os.fstat(file_fd)
            if (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ) or size != after.st_size:
                cls._raise_artifact_path("run_artifact_changed", relative)
            return f"sha256:{digest.hexdigest()}", size
        except FileNotFoundError:
            raise
        except OSError as error:
            if error.errno == errno.ENOENT:
                raise FileNotFoundError(relative) from error
            cls._raise_artifact_path("run_artifact_invalid", relative)
        finally:
            if file_fd is not None:
                os.close(file_fd)
            for value in reversed(opened_directories):
                os.close(value)

    @staticmethod
    def _raise_artifact_path(code: str, path: str) -> None:
        raise RCPError(
            code=code,
            message="Artifact path is missing, unsafe, or is not a stable regular file.",
            context={"path": path},
        )

    @staticmethod
    def _terminal_observation(
        *,
        return_code: int | None,
        timed_out: bool,
        launch_error: OSError | ValueError | None,
        artifact_issues: tuple[_ArtifactIssue, ...],
    ) -> ProcessTerminalObservation:
        if launch_error is not None:
            return ProcessTerminalObservation(
                kind="launch_failed",
                error_code="run_process_launch_failed",
                failure_class=FailureClass.ENVIRONMENT,
                exit_code=None,
                detail=f"Process launch failed ({type(launch_error).__name__}).",
            )
        if timed_out:
            return ProcessTerminalObservation(
                kind="timed_out",
                error_code="run_process_timeout",
                failure_class=FailureClass.COMMAND,
                exit_code=None,
                detail="Process exceeded its configured execution timeout.",
            )
        if return_code is None:
            return ProcessTerminalObservation(
                kind="launch_failed",
                error_code="run_process_observation_failed",
                failure_class=FailureClass.INFRASTRUCTURE,
                exit_code=None,
                detail="Process terminal status could not be observed.",
            )
        if return_code < 0:
            number = -return_code
            return ProcessTerminalObservation(
                kind="signaled",
                error_code="run_process_signaled",
                failure_class=FailureClass.COMMAND,
                exit_code=return_code,
                signal_number=number,
                detail=f"Process terminated by signal {number}.",
            )
        if return_code != 0:
            return ProcessTerminalObservation(
                kind="exited",
                error_code="run_process_nonzero_exit",
                failure_class=FailureClass.COMMAND,
                exit_code=return_code,
                detail=f"Process exited with status {return_code}.",
            )
        if artifact_issues:
            first = artifact_issues[0]
            return ProcessTerminalObservation(
                kind="artifact_failed",
                error_code=first.code,
                failure_class=FailureClass.COMMAND,
                exit_code=0,
                detail=f"Artifact collection failed for {first.path}: {first.detail}",
            )
        return ProcessTerminalObservation(
            kind="exited",
            error_code=None,
            failure_class=None,
            exit_code=0,
            detail=None,
        )

    @staticmethod
    def _log_summary(
        *,
        observation: ProcessTerminalObservation,
        artifact_issues: tuple[_ArtifactIssue, ...],
        stdout_tail: str,
        stderr_tail: str,
    ) -> str | None:
        parts: list[str] = []
        if observation.detail:
            parts.append(observation.detail)
        for issue in artifact_issues:
            line = f"{issue.code}: {issue.path}: {issue.detail}"
            if line not in parts:
                parts.append(line)
        if stdout_tail:
            parts.append(f"stdout tail:\n{stdout_tail}")
        if stderr_tail:
            parts.append(f"stderr tail:\n{stderr_tail}")
        summary = "\n".join(parts).strip()
        if not summary:
            return None
        return summary[:_MAX_LOG_SUMMARY_CHARS]

    def _terminate_process(self, process: subprocess.Popen[bytes]) -> None:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=self.terminate_grace_seconds)
            return
        except subprocess.TimeoutExpired:
            pass
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()

    @staticmethod
    def _result_id(attempt_id: str) -> str:
        return "result_" + attempt_id.removeprefix("attempt_")

    def _marker_identity(
        self,
        *,
        spec: RunSpec,
        attempt_id: str,
        operation_id: str,
    ) -> dict[str, object]:
        return {
            "marker_version": _MARKER_VERSION,
            "run_id": spec.run_id,
            "spec_digest": spec.spec_digest,
            "attempt_id": attempt_id,
            "operation_id": operation_id,
            "host": self.local_host,
        }

    def _claim_or_observe(
        self,
        marker_path: Path,
        identity: dict[str, object],
    ) -> dict[str, object] | None:
        payload = {
            **identity,
            "phase": "claimed",
            "created_at": self._now_json(),
        }
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        try:
            descriptor = os.open(marker_path, flags, 0o600)
        except FileExistsError:
            return self._read_marker(marker_path)
        try:
            self._write_descriptor(descriptor, canonical_json_bytes(payload))
        finally:
            os.close(descriptor)
        self._fsync_directory(marker_path.parent)
        return None

    def _observed_execution(
        self,
        marker: dict[str, object],
        marker_path: Path,
        identity: dict[str, object],
    ) -> LocalRunExecution:
        mismatches = [
            key for key, value in identity.items() if marker.get(key) != value
        ]
        if mismatches:
            raise RCPError(
                code="run_execution_marker_conflict",
                message="Attempt marker belongs to different immutable run inputs.",
                context={"mismatches": mismatches, "path": str(marker_path)},
            )
        phase = marker.get("phase")
        if phase != "terminal":
            raise RCPError(
                code="run_execution_uncertain",
                message="Attempt was already claimed without a durable terminal result.",
                remediation="Observe/reconcile the process, then use a new attempt for any retry.",
                context={
                    "phase": phase,
                    "pid": marker.get("pid"),
                    "path": str(marker_path),
                },
            )
        try:
            attempt = RunAttempt.model_validate(marker.get("attempt"))
            result = RunResult.model_validate(marker.get("result"))
            observation = ProcessTerminalObservation.from_dict(
                marker.get("observation")
            )
            stdout_tail = marker.get("stdout_tail", "")
            stderr_tail = marker.get("stderr_tail", "")
            stdout_truncated = marker.get("stdout_truncated", False)
            stderr_truncated = marker.get("stderr_truncated", False)
            if not isinstance(stdout_tail, str) or not isinstance(stderr_tail, str):
                raise ValueError("marker tails must be strings")
            if type(stdout_truncated) is not bool or type(stderr_truncated) is not bool:
                raise ValueError("marker truncation flags must be booleans")
            if (
                attempt.attempt_id != identity["attempt_id"]
                or attempt.run_id != identity["run_id"]
                or attempt.operation_id != identity["operation_id"]
                or result.run_id != identity["run_id"]
                or result.run_spec_digest != identity["spec_digest"]
                or result.attempt_ids != (identity["attempt_id"],)
            ):
                raise ValueError("terminal records do not match marker identity")
        except (ValidationError, ValueError, TypeError) as error:
            raise RCPError(
                code="run_execution_marker_invalid",
                message="Terminal attempt marker is malformed or inconsistent.",
                context={"path": str(marker_path)},
            ) from error
        return LocalRunExecution(
            attempt=attempt,
            result=result,
            observation=observation,
            stdout_tail=stdout_tail,
            stderr_tail=stderr_tail,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            marker_path=marker_path,
            launched=False,
            observed_existing=True,
        )

    def _write_terminal_marker(
        self,
        marker_path: Path,
        identity: dict[str, object],
        execution: LocalRunExecution,
    ) -> None:
        self._replace_marker(
            marker_path,
            {
                **identity,
                "phase": "terminal",
                "attempt": execution.attempt.model_dump(mode="json", exclude_none=True),
                "result": execution.result.model_dump(mode="json", exclude_none=True),
                "observation": execution.observation.as_dict(),
                "stdout_tail": execution.stdout_tail,
                "stderr_tail": execution.stderr_tail,
                "stdout_truncated": execution.stdout_truncated,
                "stderr_truncated": execution.stderr_truncated,
                "updated_at": self._now_json(),
            },
        )

    def _replace_marker(self, marker_path: Path, payload: dict[str, object]) -> None:
        try:
            existing = marker_path.lstat()
        except OSError as error:
            raise RCPError(
                code="run_execution_marker_invalid",
                message="Attempt marker disappeared during execution.",
                context={"path": str(marker_path)},
            ) from error
        if not stat.S_ISREG(existing.st_mode):
            raise RCPError(
                code="run_execution_marker_invalid",
                message="Attempt marker is not a regular file.",
                context={"path": str(marker_path)},
            )
        temporary = marker_path.with_name(
            f".{marker_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, 0o600)
        try:
            self._write_descriptor(descriptor, canonical_json_bytes(payload))
        finally:
            os.close(descriptor)
        try:
            os.replace(temporary, marker_path)
            self._fsync_directory(marker_path.parent)
        finally:
            with suppress(FileNotFoundError):
                temporary.unlink()

    @staticmethod
    def _write_descriptor(descriptor: int, content: bytes) -> None:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)

    @staticmethod
    def _read_marker(path: Path) -> dict[str, object]:
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_MARKER_BYTES:
                raise ValueError("marker is not a bounded regular file")
            raw = path.read_bytes()
            value = json.loads(raw)
            if not isinstance(value, dict) or canonical_json_bytes(value) != raw:
                raise ValueError("marker is not canonical JSON")
            return value
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
            raise RCPError(
                code="run_execution_marker_invalid",
                message="Attempt marker is malformed, unsafe, or unreadable.",
                context={"path": str(path)},
            ) from error

    @staticmethod
    def _marker_directory(value: Path) -> Path:
        candidate = Path(os.path.abspath(os.fspath(value)))
        if candidate.exists():
            try:
                info = candidate.lstat()
            except OSError as error:
                raise RCPError(
                    code="run_execution_marker_directory_invalid",
                    message="Attempt marker directory cannot be inspected.",
                    context={"path": str(candidate)},
                ) from error
            if (
                not stat.S_ISDIR(info.st_mode)
                or stat.S_IMODE(info.st_mode) & 0o077
                or info.st_uid != os.getuid()
            ):
                raise RCPError(
                    code="run_execution_marker_directory_invalid",
                    message="Attempt marker directory must be private and owned locally.",
                    context={"path": str(candidate)},
                )
        else:
            try:
                candidate.mkdir(mode=0o700)
            except OSError as error:
                raise RCPError(
                    code="run_execution_marker_directory_invalid",
                    message="Attempt marker directory cannot be created safely.",
                    context={"path": str(candidate)},
                ) from error
        return candidate

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _now_json(self) -> str:
        return self._date_json(self.clock())

    @staticmethod
    def _date_json(value: datetime) -> str:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("local run clock must return a timezone-aware datetime")
        return value.astimezone(UTC).isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z")
