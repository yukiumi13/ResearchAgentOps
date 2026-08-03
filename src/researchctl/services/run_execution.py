from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import TypeAdapter, ValidationError

from researchctl.adapters.git_scope import GitWriteScopeValidator
from researchctl.domain.enums import RunAttemptState, RunOutcome, TaskState
from researchctl.domain.models import RunAttempt, RunResult, RunSpec, TaskRecord
from researchctl.domain.types import OperationId, RunAttemptId
from researchctl.errors import RCPError
from researchctl.serialization import canonical_digest, canonical_json_bytes, load_model
from researchctl.services.local_run import (
    LocalRunExecution,
    LocalRunExecutor,
    RunEventCallback,
)
from researchctl.services.run_preflight import LocalRunPreflight, RunPreflightReceipt
from researchctl.services.run_records import (
    CollectedRunReceipt,
    FrozenRunReceipt,
    GitRunRecordRepository,
)


_ATTEMPT_ID = TypeAdapter(RunAttemptId)
_OPERATION_ID = TypeAdapter(OperationId)
_MAX_MARKER_BYTES = 256 * 1024


@dataclass(frozen=True, slots=True)
class LocalRunCoordinatorReceipt:
    frozen: FrozenRunReceipt
    preflight: RunPreflightReceipt
    attempt: RunAttempt
    result: RunResult
    collection: CollectedRunReceipt | None
    marker_path: Path
    process_launched: bool
    observed_existing: bool
    stdout_tail_present: bool
    stderr_tail_present: bool
    stdout_truncated: bool
    stderr_truncated: bool

    @property
    def collected(self) -> bool:
        return self.collection is not None

    @property
    def terminal_result(self) -> Literal["collected", "attempt_failed"]:
        return "collected" if self.collected else "attempt_failed"

    def as_dict(self) -> dict[str, object]:
        return {
            "run_id": self.result.run_id,
            "attempt_id": self.attempt.attempt_id,
            "terminal_result": self.terminal_result,
            "frozen": self.frozen.as_dict(),
            "preflight": self.preflight.as_dict(),
            "attempt": self.attempt.model_dump(mode="json", exclude_none=True),
            "result": self.result.model_dump(mode="json", exclude_none=True),
            "collection": (
                self.collection.as_dict() if self.collection is not None else None
            ),
            "execution": {
                "marker_path": str(self.marker_path),
                "process_launched": self.process_launched,
                "observed_existing": self.observed_existing,
                "stdout_tail_present": self.stdout_tail_present,
                "stderr_tail_present": self.stderr_tail_present,
                "stdout_truncated": self.stdout_truncated,
                "stderr_truncated": self.stderr_truncated,
            },
            "delivery": "local_run_execution",
        }


@dataclass(frozen=True, slots=True)
class LocalRunCollectionReceipt:
    frozen: FrozenRunReceipt
    attempt: RunAttempt
    result: RunResult
    collection: CollectedRunReceipt
    marker_path: Path

    @property
    def terminal_result(self) -> Literal["collected", "already_collected"]:
        return "collected" if self.collection.changed else "already_collected"

    def as_dict(self) -> dict[str, object]:
        return {
            "run_id": self.result.run_id,
            "attempt_id": self.attempt.attempt_id,
            "terminal_result": self.terminal_result,
            "frozen": self.frozen.as_dict(),
            "attempt": self.attempt.model_dump(mode="json", exclude_none=True),
            "result": self.result.model_dump(mode="json", exclude_none=True),
            "collection": self.collection.as_dict(),
            "execution": {
                "marker_path": str(self.marker_path),
                "process_launched": False,
                "observed_existing": True,
            },
            "delivery": "local_run_collection",
        }


class LocalRunCoordinator:
    """Composes immutable Git freeze, local preflight, execution, and collect."""

    def __init__(
        self,
        *,
        repository_root: Path,
        worktrees_directory: Path,
        default_branch: str,
        preflight: LocalRunPreflight,
        executor: LocalRunExecutor,
        write_scope: GitWriteScopeValidator | None = None,
    ) -> None:
        self.repository_root = Path(
            os.path.abspath(os.fspath(repository_root))
        )
        self.worktrees_directory = Path(
            os.path.abspath(os.fspath(worktrees_directory))
        )
        self.default_branch = default_branch
        self.preflight = preflight
        self.executor = executor
        self.write_scope = write_scope or GitWriteScopeValidator()

    def execute(
        self,
        *,
        spec: RunSpec,
        task: TaskRecord,
        attempt_id: str,
        operation_id: str,
        assigned_gpu_uuids: tuple[str, ...],
        event_callback: RunEventCallback,
        retry_of: str | None = None,
    ) -> LocalRunCoordinatorReceipt:
        current_attempt, operation, prior_attempt = self._validate_identity(
            spec=spec,
            task=task,
            attempt_id=attempt_id,
            operation_id=operation_id,
            retry_of=retry_of,
        )
        trusted_base = self.write_scope.resolve_branch_head(
            repository_root=self.repository_root,
            branch=self.default_branch,
        )
        protected_task = self.write_scope.load_protected_task(
            repository_root=self.repository_root,
            protected_commit=trusted_base,
            task_id=spec.task_id,
        )
        if protected_task.state not in {
            TaskState.READY,
            TaskState.ACTIVE,
            TaskState.BLOCKED,
        }:
            raise RCPError(
                code="task_not_runnable",
                message="Run requires a runnable Task on the protected default branch.",
                context={
                    "task_id": protected_task.task_id,
                    "task_state": protected_task.state.value,
                },
            )
        self.write_scope.validate_source(
            task=protected_task,
            repository_root=self.repository_root,
            trusted_base_commit=trusted_base,
            baseline_commit=spec.baseline_commit,
            source_commit=spec.source_commit,
        )
        records = GitRunRecordRepository(
            repository_root=self.repository_root,
            worktrees_directory=self.worktrees_directory,
            spec=spec,
        )
        frozen = records.freeze()
        checked = self.preflight.check(
            spec=spec,
            task=protected_task,
            execution_worktree=frozen.execution_worktree,
            assigned_gpu_uuids=assigned_gpu_uuids,
        )

        marker_path = self.executor.marker_path_for(
            frozen.execution_worktree,
            current_attempt,
        )
        existing_result = self._existing_result(records, spec)
        if existing_result is not None:
            if existing_result.attempt_ids != (current_attempt,):
                raise RCPError(
                    code="run_already_finalized",
                    message="Run already has a final result from another Attempt.",
                    context={
                        "result_id": existing_result.result_id,
                        "attempt_ids": list(existing_result.attempt_ids),
                    },
                )
            if not marker_path.exists():
                raise RCPError(
                    code="run_execution_uncertain",
                    message="Final RunResult exists without its local execution marker.",
                    remediation="Reconcile the Run metadata before any process launch.",
                    context={"marker_path": str(marker_path)},
                )

        if prior_attempt is not None:
            if existing_result is not None:
                raise RCPError(
                    code="run_already_finalized",
                    message="A finalized Run cannot create another Attempt.",
                    context={"result_id": existing_result.result_id},
                )
            prior = self._retry_origin(
                execution_worktree=frozen.execution_worktree,
                spec=spec,
                retry_of=prior_attempt,
            )
            if prior.operation_id == operation:
                raise RCPError(
                    code="run_retry_identity_invalid",
                    message="A retry requires a new Operation ID.",
                )

        executed = self.executor.execute(
            spec=spec,
            execution_worktree=frozen.execution_worktree,
            preflight=checked,
            attempt_id=current_attempt,
            operation_id=operation,
            event_callback=event_callback,
            retry_of=prior_attempt,
        )
        collection = self._collect_success(records, executed)
        return LocalRunCoordinatorReceipt(
            frozen=frozen,
            preflight=checked,
            attempt=executed.attempt,
            result=executed.result,
            collection=collection,
            marker_path=executed.marker_path,
            process_launched=executed.launched,
            observed_existing=executed.observed_existing,
            stdout_tail_present=bool(executed.stdout_tail),
            stderr_tail_present=bool(executed.stderr_tail),
            stdout_truncated=executed.stdout_truncated,
            stderr_truncated=executed.stderr_truncated,
        )

    def collect(
        self,
        *,
        spec: RunSpec,
        task: TaskRecord,
        attempt_id: str,
        operation_id: str,
    ) -> LocalRunCollectionReceipt:
        attempt_identity, operation = self._validate_collection_identity(
            spec=spec,
            task=task,
            attempt_id=attempt_id,
            operation_id=operation_id,
        )
        records = GitRunRecordRepository(
            repository_root=self.repository_root,
            worktrees_directory=self.worktrees_directory,
            spec=spec,
        )
        records.require_started()
        frozen = records.freeze()
        marker_path = self.executor.marker_path_for(
            frozen.execution_worktree,
            attempt_identity,
        )
        attempt, result = self._terminal_marker(
            marker_path=marker_path,
            spec=spec,
            attempt_id=attempt_identity,
            missing_code="run_collection_marker_not_found",
            invalid_code="run_collection_marker_invalid",
        )
        if attempt.operation_id == operation:
            raise RCPError(
                code="run_collection_operation_reused",
                message="Collection requires a new Operation ID.",
            )
        self._validate_terminal_pair(attempt, result)
        existing = self._existing_result(records, spec)
        if existing is not None and canonical_digest(existing) != canonical_digest(result):
            raise RCPError(
                code="run_already_finalized",
                message="Run already has a different final RunResult.",
                context={
                    "result_id": existing.result_id,
                    "attempt_ids": list(existing.attempt_ids),
                },
            )
        collection = records.collect(result)
        return LocalRunCollectionReceipt(
            frozen=frozen,
            attempt=attempt,
            result=result,
            collection=collection,
            marker_path=marker_path,
        )

    @staticmethod
    def _validate_identity(
        *,
        spec: RunSpec,
        task: TaskRecord,
        attempt_id: str,
        operation_id: str,
        retry_of: str | None,
    ) -> tuple[str, str, str | None]:
        try:
            attempt = _ATTEMPT_ID.validate_python(attempt_id)
            operation = _OPERATION_ID.validate_python(operation_id)
            prior = (
                _ATTEMPT_ID.validate_python(retry_of)
                if retry_of is not None
                else None
            )
        except ValidationError as error:
            raise RCPError(
                code="run_execution_identity_invalid",
                message="Run execution requires canonical Attempt and Operation IDs.",
            ) from error
        if spec.task_id != task.task_id:
            raise RCPError(
                code="run_task_mismatch",
                message="RunSpec does not belong to the supplied Task.",
            )
        if prior is None and operation != spec.operation_id:
            raise RCPError(
                code="run_operation_mismatch",
                message="Initial Attempt operation_id must match its frozen RunSpec.",
            )
        if prior is not None and (
            prior == attempt or operation == spec.operation_id
        ):
            raise RCPError(
                code="run_retry_identity_invalid",
                message="Retry requires a new Attempt and Operation identity.",
            )
        return attempt, operation, prior

    @staticmethod
    def _validate_collection_identity(
        *,
        spec: RunSpec,
        task: TaskRecord,
        attempt_id: str,
        operation_id: str,
    ) -> tuple[str, str]:
        try:
            attempt = _ATTEMPT_ID.validate_python(attempt_id)
            operation = _OPERATION_ID.validate_python(operation_id)
        except ValidationError as error:
            raise RCPError(
                code="run_collection_identity_invalid",
                message="Run collection requires canonical Attempt and Operation IDs.",
            ) from error
        if spec.task_id != task.task_id:
            raise RCPError(
                code="run_task_mismatch",
                message="RunSpec does not belong to the supplied Task.",
            )
        return attempt, operation

    @staticmethod
    def _existing_result(
        records: GitRunRecordRepository,
        spec: RunSpec,
    ) -> RunResult | None:
        path = records.result_path
        if not path.exists():
            return None
        if path.is_symlink() or not path.is_file():
            raise RCPError(
                code="run_result_invalid",
                message="Existing RunResult path is unsafe or not a regular file.",
            )
        try:
            result = load_model(path, RunResult)
        except Exception as error:
            raise RCPError(
                code="run_result_invalid",
                message="Existing RunResult is malformed.",
            ) from error
        if result.run_id != spec.run_id or result.run_spec_digest != spec.spec_digest:
            raise RCPError(
                code="run_result_identity_mismatch",
                message="Existing RunResult does not bind the frozen RunSpec.",
            )
        return result

    def _retry_origin(
        self,
        *,
        execution_worktree: Path,
        spec: RunSpec,
        retry_of: str,
    ) -> RunAttempt:
        path = self.executor.marker_path_for(execution_worktree, retry_of)
        attempt, result = self._terminal_marker(
            marker_path=path,
            spec=spec,
            attempt_id=retry_of,
            missing_code="run_retry_origin_not_found",
            invalid_code="run_retry_origin_invalid",
        )
        if (
            attempt.events[-1].state != RunAttemptState.FAILED
            or result.outcome != RunOutcome.FAILED
        ):
            raise RCPError(
                code="run_retry_origin_not_failed",
                message="Only a terminal failed Attempt can be retried.",
                context={"retry_of": retry_of},
            )
        return attempt

    @staticmethod
    def _terminal_marker(
        *,
        marker_path: Path,
        spec: RunSpec,
        attempt_id: str,
        missing_code: str,
        invalid_code: str,
    ) -> tuple[RunAttempt, RunResult]:
        try:
            info = marker_path.lstat()
        except FileNotFoundError as error:
            raise RCPError(
                code=missing_code,
                message="Attempt terminal marker was not found on this host.",
                context={"attempt_id": attempt_id},
            ) from error
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_MARKER_BYTES:
            raise RCPError(
                code=invalid_code,
                message="Attempt terminal marker is unsafe or too large.",
                context={"attempt_id": attempt_id},
            )
        try:
            raw = marker_path.read_bytes()
            marker = json.loads(raw)
            if not isinstance(marker, dict) or canonical_json_bytes(marker) != raw:
                raise ValueError("marker is not canonical JSON")
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
            raise RCPError(
                code=invalid_code,
                message="Attempt terminal marker is malformed or unreadable.",
                context={"attempt_id": attempt_id},
            ) from error
        if marker.get("phase") != "terminal":
            raise RCPError(
                code="run_execution_uncertain",
                message="Retry origin has no durable terminal observation.",
                remediation="Reconcile the original Attempt before retrying.",
                context={
                    "attempt_id": attempt_id,
                    "phase": marker.get("phase"),
                    "pid": marker.get("pid"),
                },
            )
        try:
            attempt = RunAttempt.model_validate(marker.get("attempt"))
            result = RunResult.model_validate(marker.get("result"))
        except ValidationError as error:
            raise RCPError(
                code=invalid_code,
                message="Attempt marker contains invalid terminal records.",
                context={"attempt_id": attempt_id},
            ) from error
        if (
            attempt.attempt_id != attempt_id
            or attempt.run_id != spec.run_id
            or result.run_id != spec.run_id
            or result.run_spec_digest != spec.spec_digest
            or result.attempt_ids != (attempt_id,)
        ):
            raise RCPError(
                code=invalid_code,
                message="Attempt marker does not belong to the frozen Run.",
                context={"attempt_id": attempt_id},
            )
        return attempt, result

    @staticmethod
    def _validate_terminal_pair(attempt: RunAttempt, result: RunResult) -> None:
        terminal = attempt.events[-1].state
        valid = (
            terminal == RunAttemptState.SUCCEEDED
            and result.outcome == RunOutcome.COMPLETE
        ) or (
            terminal == RunAttemptState.FAILED
            and result.outcome == RunOutcome.FAILED
        )
        if not valid:
            raise RCPError(
                code="run_terminal_observation_invalid",
                message="Attempt marker contains inconsistent terminal records.",
            )

    @staticmethod
    def _collect_success(
        records: GitRunRecordRepository,
        executed: LocalRunExecution,
    ) -> CollectedRunReceipt | None:
        terminal = executed.attempt.events[-1].state
        if terminal == RunAttemptState.SUCCEEDED:
            if executed.result.outcome != RunOutcome.COMPLETE:
                raise RCPError(
                    code="run_terminal_observation_invalid",
                    message="Succeeded Attempt does not carry a complete RunResult.",
                )
            return records.collect(executed.result)
        if terminal != RunAttemptState.FAILED or executed.result.outcome != RunOutcome.FAILED:
            raise RCPError(
                code="run_terminal_observation_invalid",
                message="Local executor returned inconsistent terminal records.",
            )
        return None
