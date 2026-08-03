from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from pydantic import TypeAdapter, ValidationError

from researchctl.adapters.reconcile import (
    LocalReconcileObservation,
    LocalReconcileObserver,
    ManagedBranchObservation,
    ObservationFailure,
    RunMarkerObservation,
    RunRefObservation,
    WorktreeInventoryEntry,
    parse_managed_branch,
    parse_managed_branch_ref,
    parse_tmux_session,
)
from researchctl.domain.enums import RunAttemptState, RunOutcome, SessionState
from researchctl.domain.models import RunAttempt, RunResult, RunSpec
from researchctl.domain.types import (
    HumanKey,
    OperationId,
    RunAttemptId,
    RunId,
    Sha256Digest,
    UtcDateTime,
    utc_now,
)
from researchctl.runtime.models import RuntimeSession
from researchctl.serialization import (
    canonical_json_bytes,
    dump_yaml,
    load_yaml,
)
from researchctl.services.local_run import ProcessTerminalObservation


_CAPABILITY_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_RUN_ID = TypeAdapter(RunId)
_ATTEMPT_ID = TypeAdapter(RunAttemptId)
_OPERATION_ID = TypeAdapter(OperationId)
_SHA256_DIGEST = TypeAdapter(Sha256Digest)
_HUMAN_KEY = TypeAdapter(HumanKey)
_UTC_DATETIME = TypeAdapter(UtcDateTime)
_RUN_MARKER_PHASES = frozenset({"claimed", "launch_intent", "running", "terminal"})


class ReconcileClassification(StrEnum):
    CLEAN = "clean"
    RECOVERABLE = "recoverable"
    UNCERTAIN = "uncertain"
    LOST = "lost"


class ReconcileOutcome(StrEnum):
    CLEAN = "clean"
    PLAN_READY = "plan_ready"
    PARTIAL_OBSERVATION = "partial_observation"


class RuntimeObservationState(StrEnum):
    AVAILABLE = "available"
    MISSING = "missing"
    ERROR = "error"


class RunReconcileState(StrEnum):
    FROZEN = "frozen"
    COLLECTED = "collected"
    COLLECT_CANDIDATE = "collect_candidate"
    EXECUTION_UNCERTAIN = "execution_uncertain"
    INCONSISTENT = "inconsistent"


@dataclass(frozen=True, slots=True)
class RuntimeRecoveryLimit:
    code: str
    description: str


RUNTIME_RECOVERY_LIMITS = (
    RuntimeRecoveryLimit(
        code="session_capabilities_not_reconstructible",
        description=(
            "Session capabilities and their persisted digests cannot be reconstructed "
            "from Git; reconcile never creates a capability or takeover token."
        ),
    ),
    RuntimeRecoveryLimit(
        code="operation_journal_not_reconstructible",
        description="The operation journal cannot be reconstructed from Git observations.",
    ),
    RuntimeRecoveryLimit(
        code="undelivered_status_not_reconstructible",
        description=(
            "Status updates and outbox deliveries that existed only in the runtime "
            "database cannot be reconstructed from Git."
        ),
    ),
    RuntimeRecoveryLimit(
        code="attention_actions_not_reconstructible",
        description=(
            "Inbox acknowledgements, snoozes, and resolutions cannot be reconstructed "
            "from Git."
        ),
    ),
)


class RuntimeSessionReader(Protocol):
    def list_sessions(self, project_id: str | None = None) -> tuple[RuntimeSession, ...]: ...


@dataclass(frozen=True, slots=True)
class ReconcilePlanItem:
    session_id: str
    classification: ReconcileClassification
    task_id: str | None
    task_key: str | None
    runtime_state: SessionState | None
    branch: str | None
    branch_commit: str | None
    worktree_path: str
    worktree_head: str | None
    tmux_session: str
    tmux_present: bool
    native_session_id: str | None
    capability_digest_present: bool
    continued_from: str | None
    reasons: tuple[str, ...]
    proposed_actions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RunMarkerPlanItem:
    observation_key: str
    path: str
    attempt_id: str | None
    phase: str | None
    pid: int | None
    terminal: bool
    valid: bool


@dataclass(frozen=True, slots=True)
class RunReconcilePlanItem:
    observation_key: str
    run_id: str | None
    classification: ReconcileClassification
    state: RunReconcileState
    branch: str | None
    branch_commit: str | None
    tag: str | None
    tag_commit: str | None
    spec_digest: str | None
    result_id: str | None
    markers: tuple[RunMarkerPlanItem, ...]
    reasons: tuple[str, ...]
    proposed_actions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReconcilePlan:
    outcome: ReconcileOutcome
    observed_at: datetime
    runtime_observation: RuntimeObservationState
    items: tuple[ReconcilePlanItem, ...]
    run_items: tuple[RunReconcilePlanItem, ...]
    observation_failures: tuple[ObservationFailure, ...]
    runtime_recovery_limits: tuple[RuntimeRecoveryLimit, ...]
    takeover_token_created: bool
    plan_digest: str

    def as_dict(self) -> dict[str, object]:
        payload = LocalReconcileService._plan_payload(
            outcome=self.outcome,
            observed_at=self.observed_at,
            runtime_state=self.runtime_observation,
            items=self.items,
            run_items=self.run_items,
            failures=self.observation_failures,
            recovery_limits=self.runtime_recovery_limits,
        )
        return {**payload, "plan_digest": self.plan_digest}


@dataclass(frozen=True, slots=True)
class _SessionFacts:
    session_id: str
    runtime: RuntimeSession | None
    branches: tuple[ManagedBranchObservation, ...]
    worktrees: tuple[WorktreeInventoryEntry, ...]
    expected_path: Path
    expected_path_exists: bool
    expected_path_safe_directory: bool
    tmux_name: str
    tmux_present: bool
    incomplete_components: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _ParsedRunMarker:
    observation_key: str
    path: Path
    run_id: str | None
    spec_digest: str | None
    attempt_id: str | None
    operation_id: str | None
    phase: str | None
    pid: int | None
    attempt: RunAttempt | None
    result: RunResult | None
    errors: tuple[str, ...]


class LocalReconcileService:
    """Build a deterministic repair plan without changing any observed authority."""

    def __init__(
        self,
        *,
        project_id: str,
        local_host: str,
        worktrees_directory: Path,
        observer: LocalReconcileObserver,
        runtime: RuntimeSessionReader | None,
        clock: Callable[[], datetime] = utc_now,
        max_runtime_sessions: int = 500,
    ) -> None:
        if not isinstance(project_id, str) or not project_id:
            raise ValueError("project_id must be non-empty")
        if not isinstance(local_host, str) or not local_host:
            raise ValueError("local_host must be non-empty")
        if not 1 <= max_runtime_sessions <= 10_000:
            raise ValueError("max_runtime_sessions must be between 1 and 10000")
        self._project_id = project_id
        self._local_host = local_host
        self._worktrees_directory = self._normalize_path(worktrees_directory)
        self._observer = observer
        self._runtime = runtime
        self._clock = clock
        self._max_runtime_sessions = max_runtime_sessions

    def plan(self) -> ReconcilePlan:
        observed_at = self._clock()
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("reconcile clock must return a timezone-aware datetime")
        observed_at = observed_at.astimezone(UTC)

        observation = self._observer.observe(
            run_marker_directory=(
                self._worktrees_directory / ".researchctl-run-markers"
            )
        )
        runtime_state, runtime_sessions, runtime_failure = self._read_runtime()
        failures = list(observation.failures)
        if runtime_failure is not None:
            failures.append(runtime_failure)

        incomplete = self._incomplete_components(observation, runtime_state)
        items = self._build_items(
            observation,
            runtime_sessions,
            runtime_state=runtime_state,
            incomplete_components=incomplete,
        )
        run_items = self._build_run_items(
            observation,
            incomplete_components=self._incomplete_run_components(observation),
        )
        failures.sort(key=lambda item: (item.component, item.code))
        if failures:
            outcome = ReconcileOutcome.PARTIAL_OBSERVATION
        elif runtime_state is RuntimeObservationState.AVAILABLE and all(
            item.classification is ReconcileClassification.CLEAN for item in items
        ) and all(
            item.classification is ReconcileClassification.CLEAN
            for item in run_items
        ):
            outcome = ReconcileOutcome.CLEAN
        else:
            outcome = ReconcileOutcome.PLAN_READY

        recovery_limits = (
            ()
            if runtime_state is RuntimeObservationState.AVAILABLE
            else RUNTIME_RECOVERY_LIMITS
        )
        payload = self._plan_payload(
            outcome=outcome,
            observed_at=observed_at,
            runtime_state=runtime_state,
            items=items,
            run_items=run_items,
            failures=tuple(failures),
            recovery_limits=recovery_limits,
        )
        plan_digest = "sha256:" + hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
        return ReconcilePlan(
            outcome=outcome,
            observed_at=observed_at,
            runtime_observation=runtime_state,
            items=items,
            run_items=run_items,
            observation_failures=tuple(failures),
            runtime_recovery_limits=recovery_limits,
            takeover_token_created=False,
            plan_digest=plan_digest,
        )

    def _read_runtime(
        self,
    ) -> tuple[
        RuntimeObservationState,
        tuple[RuntimeSession, ...],
        ObservationFailure | None,
    ]:
        if self._runtime is None:
            return RuntimeObservationState.MISSING, (), None
        try:
            sessions = tuple(self._runtime.list_sessions(self._project_id))
        except Exception:
            return (
                RuntimeObservationState.ERROR,
                (),
                ObservationFailure(
                    component="runtime",
                    code="runtime_observation_failed",
                    message="The runtime Session index could not be read safely.",
                ),
            )
        if len(sessions) > self._max_runtime_sessions:
            return (
                RuntimeObservationState.ERROR,
                (),
                ObservationFailure(
                    component="runtime",
                    code="runtime_observation_unbounded",
                    message="The runtime Session index exceeded the local record bound.",
                ),
            )
        identifiers = [session.session_id for session in sessions]
        valid = all(
            session.project_id == self._project_id
            and parse_tmux_session(f"research-{session.session_id}")
            == session.session_id
            for session in sessions
        )
        if not valid or len(identifiers) != len(set(identifiers)):
            return (
                RuntimeObservationState.ERROR,
                (),
                ObservationFailure(
                    component="runtime",
                    code="runtime_identity_invalid",
                    message="The runtime Session index contains an invalid identity.",
                ),
            )
        return (
            RuntimeObservationState.AVAILABLE,
            tuple(sorted(sessions, key=lambda item: item.session_id)),
            None,
        )

    @staticmethod
    def _incomplete_components(
        observation: LocalReconcileObservation,
        runtime_state: RuntimeObservationState,
    ) -> tuple[str, ...]:
        incomplete: list[str] = []
        if not observation.git_branches_complete:
            incomplete.append("git_branches")
        if not observation.git_worktrees_complete:
            incomplete.append("git_worktrees")
        if not observation.tmux_complete:
            incomplete.append("tmux")
        if runtime_state is RuntimeObservationState.ERROR:
            incomplete.append("runtime")
        return tuple(incomplete)

    @staticmethod
    def _incomplete_run_components(
        observation: LocalReconcileObservation,
    ) -> tuple[str, ...]:
        incomplete: list[str] = []
        if not observation.run_refs_complete:
            incomplete.append("run_refs")
        if not observation.run_records_complete:
            incomplete.append("run_records")
        if not observation.run_markers_complete:
            incomplete.append("run_markers")
        return tuple(incomplete)

    def _build_items(
        self,
        observation: LocalReconcileObservation,
        runtime_sessions: tuple[RuntimeSession, ...],
        *,
        runtime_state: RuntimeObservationState,
        incomplete_components: tuple[str, ...],
    ) -> tuple[ReconcilePlanItem, ...]:
        branches_by_session: dict[str, list[ManagedBranchObservation]] = defaultdict(list)
        for branch in observation.branches:
            branches_by_session[branch.session_id].append(branch)

        worktrees_by_session: dict[str, list[WorktreeInventoryEntry]] = defaultdict(list)
        for worktree in observation.worktrees:
            parsed_ref = parse_managed_branch_ref(worktree.branch_ref)
            if parsed_ref is not None:
                worktrees_by_session[parsed_ref[1]].append(worktree)
            path_session = self._session_id_from_worktree_path(worktree.path)
            if path_session is not None and worktree not in worktrees_by_session[path_session]:
                worktrees_by_session[path_session].append(worktree)

        runtime_by_session = {session.session_id: session for session in runtime_sessions}
        tmux_ids = {
            session_id
            for name in observation.tmux_sessions
            if (session_id := parse_tmux_session(name)) is not None
        }
        session_ids = (
            set(branches_by_session)
            | set(worktrees_by_session)
            | set(runtime_by_session)
            | tmux_ids
        )

        items: list[ReconcilePlanItem] = []
        for session_id in sorted(session_ids):
            expected_path = self._worktrees_directory / session_id
            facts = _SessionFacts(
                session_id=session_id,
                runtime=runtime_by_session.get(session_id),
                branches=tuple(
                    sorted(branches_by_session[session_id], key=lambda item: item.ref)
                ),
                worktrees=tuple(
                    sorted(worktrees_by_session[session_id], key=lambda item: str(item.path))
                ),
                expected_path=expected_path,
                expected_path_exists=os.path.lexists(expected_path),
                expected_path_safe_directory=(
                    not expected_path.is_symlink() and expected_path.is_dir()
                ),
                tmux_name=f"research-{session_id}",
                tmux_present=session_id in tmux_ids,
                incomplete_components=incomplete_components,
            )
            items.append(self._classify(facts, runtime_state))
        return tuple(items)

    def _build_run_items(
        self,
        observation: LocalReconcileObservation,
        *,
        incomplete_components: tuple[str, ...],
    ) -> tuple[RunReconcilePlanItem, ...]:
        refs_by_run: dict[str, list[RunRefObservation]] = defaultdict(list)
        malformed_refs: list[RunRefObservation] = []
        for ref in observation.run_refs:
            if ref.run_id is None:
                malformed_refs.append(ref)
            else:
                refs_by_run[ref.run_id].append(ref)

        markers_by_run: dict[str, list[_ParsedRunMarker]] = defaultdict(list)
        malformed_markers: list[_ParsedRunMarker] = []
        for marker in observation.run_markers:
            parsed = self._parse_run_marker(marker)
            if parsed.run_id is None:
                malformed_markers.append(parsed)
            else:
                markers_by_run[parsed.run_id].append(parsed)

        run_ids = set(refs_by_run) | set(markers_by_run)
        items = [
            self._classify_run(
                run_id,
                refs=tuple(refs_by_run[run_id]),
                markers=tuple(markers_by_run[run_id]),
                incomplete_components=incomplete_components,
            )
            for run_id in sorted(run_ids)
        ]
        items.extend(self._malformed_ref_item(ref) for ref in malformed_refs)
        items.extend(self._malformed_marker_item(marker) for marker in malformed_markers)
        return tuple(sorted(items, key=lambda item: item.observation_key))

    def _classify_run(
        self,
        run_id: str,
        *,
        refs: tuple[RunRefObservation, ...],
        markers: tuple[_ParsedRunMarker, ...],
        incomplete_components: tuple[str, ...],
    ) -> RunReconcilePlanItem:
        branches = tuple(ref for ref in refs if ref.kind == "branch")
        tags = tuple(ref for ref in refs if ref.kind == "tag")
        branch = branches[0] if len(branches) == 1 else None
        tag = tags[0] if len(tags) == 1 else None
        reasons = [
            f"observation_incomplete:{component}"
            for component in incomplete_components
        ]
        hard_conflict = False

        if not branches:
            reasons.append("run_branch_missing")
            hard_conflict = True
        elif len(branches) > 1:
            reasons.append("multiple_run_branches")
            hard_conflict = True
        if not tags:
            reasons.append("immutable_run_tag_missing")
            hard_conflict = True
        elif len(tags) > 1:
            reasons.append("multiple_immutable_run_tags")
            hard_conflict = True

        for ref in refs:
            if ref.errors:
                reasons.extend(ref.errors)
                hard_conflict = True

        tag_spec, tag_spec_error = self._parse_run_spec(
            tag.spec_text if tag is not None else None,
            location="tag",
        )
        branch_spec, branch_spec_error = self._parse_run_spec(
            branch.spec_text if branch is not None else None,
            location="branch",
        )
        for error in (tag_spec_error, branch_spec_error):
            if error is not None:
                reasons.append(error)
                hard_conflict = True
        if tag_spec is not None and tag_spec.run_id != run_id:
            reasons.append("tag_run_spec_identity_mismatch")
            hard_conflict = True
        if branch_spec is not None and branch_spec.run_id != run_id:
            reasons.append("branch_run_spec_identity_mismatch")
            hard_conflict = True
        if (
            tag_spec is not None
            and branch_spec is not None
            and tag_spec != branch_spec
        ):
            reasons.append("tag_branch_run_spec_mismatch")
            hard_conflict = True
        spec = tag_spec if tag_spec is not None else branch_spec

        if tag is not None and tag.result_text is not None:
            reasons.append("immutable_run_tag_contains_result")
            hard_conflict = True
        if (
            tag is not None
            and tag_spec is not None
            and (
                tag.commit is None
                or tag.parents != (tag_spec.source_commit,)
            )
        ):
            reasons.append("immutable_run_tag_parent_mismatch")
            hard_conflict = True

        result, result_error = self._parse_run_result(
            branch.result_text if branch is not None else None
        )
        if result_error is not None:
            reasons.append(result_error)
            hard_conflict = True
        if result is not None:
            if result.run_id != run_id:
                reasons.append("run_result_identity_mismatch")
                hard_conflict = True
            if spec is not None and result.run_spec_digest != spec.spec_digest:
                reasons.append("run_result_spec_mismatch")
                hard_conflict = True
            if (
                branch is None
                or tag is None
                or branch.commit is None
                or tag.commit is None
                or branch.commit == tag.commit
                or branch.parents != (tag.commit,)
            ):
                reasons.append("run_result_commit_parent_mismatch")
                hard_conflict = True
        elif (
            branch is not None
            and tag is not None
            and branch.commit != tag.commit
        ):
            reasons.append("run_branch_advanced_without_result")
            hard_conflict = True

        invalid_marker_keys = {
            marker.observation_key for marker in markers if marker.errors
        }
        for marker in markers:
            if marker.errors:
                reasons.extend(marker.errors)
                hard_conflict = True
            if (
                spec is not None
                and marker.spec_digest is not None
                and marker.spec_digest != spec.spec_digest
            ):
                reasons.append("run_marker_spec_mismatch")
                invalid_marker_keys.add(marker.observation_key)
                hard_conflict = True

        attempt_ids = [
            marker.attempt_id
            for marker in markers
            if marker.attempt_id is not None
        ]
        if len(attempt_ids) != len(set(attempt_ids)):
            reasons.append("multiple_markers_for_attempt")
            hard_conflict = True

        valid_markers = tuple(
            marker
            for marker in markers
            if marker.observation_key not in invalid_marker_keys
        )
        nonterminal = tuple(
            marker for marker in valid_markers if marker.phase != "terminal"
        )
        terminal = tuple(
            marker for marker in valid_markers if marker.phase == "terminal"
        )

        if result is not None:
            matching = tuple(
                marker for marker in terminal if marker.result == result
            )
            if len(matching) != 1:
                reasons.append("run_result_without_matching_terminal_marker")
                hard_conflict = True
        elif len(terminal) > 1:
            reasons.append("multiple_terminal_collect_candidates")
            hard_conflict = True

        observation_incomplete = bool(incomplete_components)
        if observation_incomplete:
            classification = ReconcileClassification.UNCERTAIN
            state = RunReconcileState.EXECUTION_UNCERTAIN
            actions = ("manual_observation_required",)
        elif hard_conflict:
            classification = ReconcileClassification.UNCERTAIN
            state = RunReconcileState.INCONSISTENT
            actions = ("manual_observation_required",)
        elif nonterminal:
            reasons.append("attempt_claimed_without_terminal_observation")
            classification = ReconcileClassification.UNCERTAIN
            state = RunReconcileState.EXECUTION_UNCERTAIN
            actions = (
                "manual_observation_required",
                "do_not_retry_attempt_blindly",
            )
        elif result is not None:
            classification = ReconcileClassification.CLEAN
            state = RunReconcileState.COLLECTED
            actions = ()
        elif len(terminal) == 1:
            reasons.append("terminal_marker_without_run_result")
            classification = ReconcileClassification.RECOVERABLE
            state = RunReconcileState.COLLECT_CANDIDATE
            actions = ("collect_terminal_attempt_explicitly",)
        else:
            classification = ReconcileClassification.CLEAN
            state = RunReconcileState.FROZEN
            actions = ()

        marker_items = tuple(
            RunMarkerPlanItem(
                observation_key=marker.observation_key,
                path=str(marker.path),
                attempt_id=marker.attempt_id,
                phase=marker.phase,
                pid=marker.pid,
                terminal=marker.phase == "terminal",
                valid=marker.observation_key not in invalid_marker_keys,
            )
            for marker in sorted(markers, key=lambda item: item.observation_key)
        )
        return RunReconcilePlanItem(
            observation_key=run_id,
            run_id=run_id,
            classification=classification,
            state=state,
            branch=(
                branch.ref.removeprefix("refs/heads/")
                if branch is not None
                else None
            ),
            branch_commit=branch.commit if branch is not None else None,
            tag=tag.ref.removeprefix("refs/tags/") if tag is not None else None,
            tag_commit=tag.commit if tag is not None else None,
            spec_digest=spec.spec_digest if spec is not None else None,
            result_id=result.result_id if result is not None else None,
            markers=marker_items,
            reasons=tuple(dict.fromkeys(reasons)),
            proposed_actions=actions,
        )

    @staticmethod
    def _parse_run_spec(
        text: str | None,
        *,
        location: str,
    ) -> tuple[RunSpec | None, str | None]:
        if text is None:
            return None, f"run_spec_missing_from_{location}"
        try:
            spec = RunSpec.model_validate(load_yaml(text))
        except Exception:
            return None, f"run_spec_malformed_on_{location}"
        if dump_yaml(spec) != text:
            return None, f"run_spec_noncanonical_on_{location}"
        return spec, None

    @staticmethod
    def _parse_run_result(
        text: str | None,
    ) -> tuple[RunResult | None, str | None]:
        if text is None:
            return None, None
        try:
            result = RunResult.model_validate(load_yaml(text))
        except Exception:
            return None, "run_result_malformed"
        if dump_yaml(result) != text:
            return None, "run_result_noncanonical"
        return result, None

    @classmethod
    def _parse_run_marker(
        cls,
        observation: RunMarkerObservation,
    ) -> _ParsedRunMarker:
        key = cls._synthetic_observation_key("marker", observation.path.name)
        errors: list[str] = []
        payload: dict[str, object] | None = None
        if observation.error is not None or observation.content is None:
            errors.append(observation.error or "run_marker_file_unreadable")
        else:
            try:
                decoded = observation.content.decode("utf-8")
                loaded = json.loads(decoded)
                if (
                    not isinstance(loaded, dict)
                    or canonical_json_bytes(loaded) != observation.content
                ):
                    raise ValueError("marker is not canonical JSON")
                payload = loaded
            except (UnicodeError, ValueError, TypeError, json.JSONDecodeError):
                errors.append("run_marker_malformed")

        run_id: str | None = None
        spec_digest: str | None = None
        attempt_id: str | None = None
        operation_id: str | None = None
        phase: str | None = None
        pid: int | None = None
        attempt: RunAttempt | None = None
        result: RunResult | None = None
        terminal_observation: ProcessTerminalObservation | None = None
        if payload is not None:
            if payload.get("marker_version") != 1:
                errors.append("run_marker_version_invalid")
            try:
                run_id = _RUN_ID.validate_python(payload.get("run_id"))
                spec_digest = _SHA256_DIGEST.validate_python(
                    payload.get("spec_digest")
                )
                attempt_id = _ATTEMPT_ID.validate_python(payload.get("attempt_id"))
                operation_id = _OPERATION_ID.validate_python(
                    payload.get("operation_id")
                )
                _HUMAN_KEY.validate_python(payload.get("host"))
            except ValidationError:
                errors.append("run_marker_identity_invalid")
            if (
                attempt_id is not None
                and observation.path.name != f"{attempt_id}.json"
            ):
                errors.append("run_marker_filename_mismatch")

            raw_phase = payload.get("phase")
            if isinstance(raw_phase, str) and raw_phase in _RUN_MARKER_PHASES:
                phase = raw_phase
            else:
                errors.append("run_marker_phase_invalid")

            expected_keys = {
                "marker_version",
                "run_id",
                "spec_digest",
                "attempt_id",
                "operation_id",
                "host",
                "phase",
            }
            time_fields: tuple[str, ...] = ()
            if phase == "claimed":
                expected_keys.add("created_at")
                time_fields = ("created_at",)
            elif phase == "launch_intent":
                expected_keys.add("updated_at")
                time_fields = ("updated_at",)
            elif phase == "running":
                expected_keys.update({"pid", "started_at", "updated_at"})
                time_fields = ("started_at", "updated_at")
                raw_pid = payload.get("pid")
                if type(raw_pid) is int and raw_pid > 0:
                    pid = raw_pid
                else:
                    errors.append("run_marker_pid_invalid")
            elif phase == "terminal":
                expected_keys.update(
                    {
                        "attempt",
                        "result",
                        "observation",
                        "stdout_tail",
                        "stderr_tail",
                        "stdout_truncated",
                        "stderr_truncated",
                        "updated_at",
                    }
                )
                time_fields = ("updated_at",)
                try:
                    attempt = RunAttempt.model_validate(payload.get("attempt"))
                    result = RunResult.model_validate(payload.get("result"))
                    terminal_observation = ProcessTerminalObservation.from_dict(
                        payload.get("observation")
                    )
                    if not isinstance(payload.get("stdout_tail"), str):
                        raise ValueError("stdout tail is not text")
                    if not isinstance(payload.get("stderr_tail"), str):
                        raise ValueError("stderr tail is not text")
                    if type(payload.get("stdout_truncated")) is not bool:
                        raise ValueError("stdout truncation flag is invalid")
                    if type(payload.get("stderr_truncated")) is not bool:
                        raise ValueError("stderr truncation flag is invalid")
                except (ValidationError, ValueError, TypeError):
                    errors.append("run_terminal_marker_records_invalid")
                if (
                    attempt is not None
                    and result is not None
                    and (
                        attempt.attempt_id != attempt_id
                        or attempt.run_id != run_id
                        or attempt.operation_id != operation_id
                        or result.run_id != run_id
                        or result.run_spec_digest != spec_digest
                        or result.attempt_ids != (attempt_id,)
                        or not cls._terminal_pair_valid(attempt, result)
                        or terminal_observation is None
                        or not cls._terminal_observation_valid(
                            attempt,
                            result,
                            terminal_observation,
                        )
                    )
                ):
                    errors.append("run_terminal_marker_binding_mismatch")

            if set(payload) != expected_keys:
                errors.append("run_marker_shape_invalid")
            for field in time_fields:
                try:
                    _UTC_DATETIME.validate_python(payload.get(field))
                except ValidationError:
                    errors.append("run_marker_timestamp_invalid")
                    break

        return _ParsedRunMarker(
            observation_key=key,
            path=observation.path,
            run_id=run_id,
            spec_digest=spec_digest,
            attempt_id=attempt_id,
            operation_id=operation_id,
            phase=phase,
            pid=pid,
            attempt=attempt,
            result=result,
            errors=tuple(dict.fromkeys(errors)),
        )

    @staticmethod
    def _terminal_pair_valid(attempt: RunAttempt, result: RunResult) -> bool:
        terminal = attempt.events[-1].state
        return (
            terminal is RunAttemptState.SUCCEEDED
            and result.outcome is RunOutcome.COMPLETE
        ) or (
            terminal is RunAttemptState.FAILED
            and result.outcome is RunOutcome.FAILED
        )

    @staticmethod
    def _terminal_observation_valid(
        attempt: RunAttempt,
        result: RunResult,
        observation: ProcessTerminalObservation,
    ) -> bool:
        event = attempt.events[-1]
        return (
            event.error_code == observation.error_code
            and event.detail == observation.detail
            and result.exit_code == observation.exit_code
            and result.failure_class == observation.failure_class
            and (
                (
                    event.state is RunAttemptState.SUCCEEDED
                    and observation.error_code is None
                )
                or (
                    event.state is RunAttemptState.FAILED
                    and observation.error_code is not None
                )
            )
        )

    @classmethod
    def _malformed_ref_item(
        cls,
        ref: RunRefObservation,
    ) -> RunReconcilePlanItem:
        key = cls._synthetic_observation_key("ref", ref.ref)
        reasons = ref.errors or ("run_ref_identity_invalid",)
        return RunReconcilePlanItem(
            observation_key=key,
            run_id=None,
            classification=ReconcileClassification.UNCERTAIN,
            state=RunReconcileState.INCONSISTENT,
            branch=ref.ref.removeprefix("refs/heads/") if ref.kind == "branch" else None,
            branch_commit=ref.commit if ref.kind == "branch" else None,
            tag=ref.ref.removeprefix("refs/tags/") if ref.kind == "tag" else None,
            tag_commit=ref.commit if ref.kind == "tag" else None,
            spec_digest=None,
            result_id=None,
            markers=(),
            reasons=tuple(dict.fromkeys(reasons)),
            proposed_actions=("manual_observation_required",),
        )

    @staticmethod
    def _malformed_marker_item(
        marker: _ParsedRunMarker,
    ) -> RunReconcilePlanItem:
        marker_item = RunMarkerPlanItem(
            observation_key=marker.observation_key,
            path=str(marker.path),
            attempt_id=marker.attempt_id,
            phase=marker.phase,
            pid=marker.pid,
            terminal=marker.phase == "terminal",
            valid=False,
        )
        return RunReconcilePlanItem(
            observation_key=marker.observation_key,
            run_id=None,
            classification=ReconcileClassification.UNCERTAIN,
            state=RunReconcileState.INCONSISTENT,
            branch=None,
            branch_commit=None,
            tag=None,
            tag_commit=None,
            spec_digest=None,
            result_id=None,
            markers=(marker_item,),
            reasons=marker.errors or ("run_marker_identity_invalid",),
            proposed_actions=("manual_observation_required",),
        )

    @staticmethod
    def _synthetic_observation_key(kind: str, value: str) -> str:
        digest = hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()
        return f"{kind}:sha256:{digest}"

    def _classify(
        self,
        facts: _SessionFacts,
        runtime_observation: RuntimeObservationState,
    ) -> ReconcilePlanItem:
        runtime = facts.runtime
        branch = facts.branches[0] if len(facts.branches) == 1 else None
        expected_path_entries = tuple(
            item
            for item in facts.worktrees
            if self._normalize_path(item.path) == facts.expected_path
        )
        branch_entries = tuple(
            item
            for item in facts.worktrees
            if branch is not None and item.branch_ref == branch.ref
        )
        exact_worktree = (
            expected_path_entries[0]
            if len(expected_path_entries) == 1
            and len(branch_entries) == 1
            and expected_path_entries[0] == branch_entries[0]
            else None
        )

        reasons: list[str] = []
        hard_conflict = False
        if facts.incomplete_components:
            reasons.extend(
                f"observation_incomplete:{component}"
                for component in facts.incomplete_components
            )

        if len(facts.branches) == 0:
            reasons.append("branch_missing")
        elif len(facts.branches) > 1:
            reasons.append("multiple_session_branches")
            hard_conflict = True

        if len(expected_path_entries) > 1 or len(branch_entries) > 1:
            reasons.append("multiple_worktree_claims")
            hard_conflict = True
        elif exact_worktree is None:
            reasons.append("worktree_missing_or_mismatched")
            if facts.expected_path_exists:
                reasons.append("unregistered_worktree_path_exists")
                hard_conflict = True
            if expected_path_entries or branch_entries:
                hard_conflict = True
        elif exact_worktree.prunable or not facts.expected_path_exists:
            reasons.append("worktree_missing_or_prunable")
        elif not facts.expected_path_safe_directory:
            reasons.append("worktree_path_unsafe")
            hard_conflict = True
        elif branch is not None and exact_worktree.head != branch.commit:
            reasons.append("branch_worktree_head_mismatch")
            hard_conflict = True

        if runtime is not None:
            parsed_runtime_branch = parse_managed_branch(runtime.branch)
            if (
                parsed_runtime_branch is None
                or parsed_runtime_branch[1] != facts.session_id
            ):
                reasons.append("runtime_branch_identity_invalid")
                hard_conflict = True
            elif branch is not None and runtime.branch != branch.name:
                reasons.append("runtime_branch_observation_mismatch")
                hard_conflict = True
            if (
                runtime.worktree_path is None
                or self._normalize_path(Path(runtime.worktree_path)) != facts.expected_path
            ):
                reasons.append("runtime_worktree_identity_invalid")
                hard_conflict = True
            if runtime.metadata.get("tmux_session") != facts.tmux_name:
                reasons.append("runtime_tmux_identity_invalid")
                hard_conflict = True
            if runtime.host != self._local_host:
                reasons.append("runtime_host_not_local")
                hard_conflict = True

        native_session_id, native_invalid = self._native_session_identity(runtime)
        if native_invalid:
            reasons.append("native_session_id_invalid")
            hard_conflict = True
        capability_value = runtime.actor_token_digest if runtime is not None else None
        capability_present = (
            isinstance(capability_value, str)
            and _CAPABILITY_DIGEST.fullmatch(capability_value) is not None
        )
        if capability_value is not None and not capability_present:
            reasons.append("capability_digest_invalid")
            hard_conflict = True

        if facts.incomplete_components:
            classification = ReconcileClassification.UNCERTAIN
        elif runtime is not None and runtime.host != self._local_host:
            classification = ReconcileClassification.UNCERTAIN
        elif facts.tmux_present:
            if runtime is None:
                reasons.append("live_tmux_without_runtime_identity")
                classification = ReconcileClassification.UNCERTAIN
            elif native_session_id is None:
                reasons.append("live_tmux_without_native_session_id")
                classification = ReconcileClassification.UNCERTAIN
            elif not capability_present:
                reasons.append("live_tmux_without_capability_digest")
                classification = ReconcileClassification.UNCERTAIN
            elif runtime.state not in {
                SessionState.PREPARING,
                SessionState.ACTIVE,
                SessionState.STOPPING,
            }:
                reasons.append("live_tmux_conflicts_with_runtime_state")
                classification = ReconcileClassification.UNCERTAIN
            elif (
                hard_conflict
                or branch is None
                or exact_worktree is None
                or exact_worktree.prunable
                or not facts.expected_path_exists
                or not facts.expected_path_safe_directory
            ):
                classification = ReconcileClassification.UNCERTAIN
            else:
                classification = ReconcileClassification.CLEAN
        elif runtime is not None and runtime.state in {
            SessionState.ACTIVE,
            SessionState.LOST,
        }:
            reasons.append(
                "runtime_lost_is_terminal"
                if runtime.state is SessionState.LOST
                else "active_runtime_tmux_missing"
            )
            classification = ReconcileClassification.LOST
        elif runtime is not None and runtime.state in {
            SessionState.PREPARING,
            SessionState.STOPPING,
        }:
            reasons.append("incomplete_operation_is_reobservable")
            if hard_conflict or (
                runtime.state is SessionState.STOPPING and branch is None
            ):
                classification = ReconcileClassification.UNCERTAIN
            else:
                classification = ReconcileClassification.RECOVERABLE
        elif runtime is not None:
            if hard_conflict or branch is None:
                classification = ReconcileClassification.UNCERTAIN
            elif (
                exact_worktree is None
                or exact_worktree.prunable
                or not facts.expected_path_exists
                or not facts.expected_path_safe_directory
                or native_session_id is None
                or not capability_present
            ):
                if native_session_id is None:
                    reasons.append("resumable_runtime_without_native_session_id")
                if not capability_present:
                    reasons.append("resumable_runtime_without_capability_digest")
                classification = ReconcileClassification.RECOVERABLE
            else:
                classification = ReconcileClassification.CLEAN
        else:
            reasons.append(
                "runtime_database_missing"
                if runtime_observation is RuntimeObservationState.MISSING
                else "runtime_identity_unavailable"
            )
            classification = (
                ReconcileClassification.RECOVERABLE
                if not hard_conflict and branch is not None
                else ReconcileClassification.UNCERTAIN
            )

        action_worktree = (
            exact_worktree
            if exact_worktree is not None
            and facts.expected_path_exists
            and facts.expected_path_safe_directory
            and not exact_worktree.prunable
            else None
        )
        actions = self._actions(
            classification,
            runtime=runtime,
            exact_worktree=action_worktree,
            runtime_observation=runtime_observation,
            native_session_id=native_session_id,
            capability_present=capability_present,
        )
        return ReconcilePlanItem(
            session_id=facts.session_id,
            classification=classification,
            task_id=runtime.task_id if runtime is not None else None,
            task_key=branch.task_key if branch is not None else None,
            runtime_state=runtime.state if runtime is not None else None,
            branch=branch.name if branch is not None else None,
            branch_commit=branch.commit if branch is not None else None,
            worktree_path=str(facts.expected_path),
            worktree_head=exact_worktree.head if exact_worktree is not None else None,
            tmux_session=facts.tmux_name,
            tmux_present=facts.tmux_present,
            native_session_id=native_session_id,
            capability_digest_present=capability_present,
            continued_from=runtime.continued_from if runtime is not None else None,
            reasons=tuple(dict.fromkeys(reasons)),
            proposed_actions=actions,
        )

    @staticmethod
    def _actions(
        classification: ReconcileClassification,
        *,
        runtime: RuntimeSession | None,
        exact_worktree: WorktreeInventoryEntry | None,
        runtime_observation: RuntimeObservationState,
        native_session_id: str | None,
        capability_present: bool,
    ) -> tuple[str, ...]:
        if classification is ReconcileClassification.CLEAN:
            return ()
        if classification is ReconcileClassification.UNCERTAIN:
            return ("manual_observation_required",)
        if classification is ReconcileClassification.LOST:
            return ("continue_with_new_session_id",)
        if runtime is None or runtime_observation is not RuntimeObservationState.AVAILABLE:
            return (
                "restore_runtime_backup_if_available",
                "continue_with_new_session_id",
            )
        if runtime.state in {SessionState.PREPARING, SessionState.STOPPING}:
            return ("retry_original_operation_after_reobservation",)
        if native_session_id is None or not capability_present:
            return ("continue_with_new_session_id",)
        if exact_worktree is None or exact_worktree.prunable:
            return ("recreate_worktree_after_reobservation",)
        return ("manual_repair_plan_required",)

    def _session_id_from_worktree_path(self, path: Path) -> str | None:
        normalized = self._normalize_path(path)
        if normalized.parent != self._worktrees_directory:
            return None
        return parse_tmux_session(f"research-{normalized.name}")

    @staticmethod
    def _native_session_identity(
        runtime: RuntimeSession | None,
    ) -> tuple[str | None, bool]:
        if runtime is None:
            return None, False
        value = runtime.metadata.get("native_session_id")
        if value is None:
            return None, False
        if not isinstance(value, str):
            return None, True
        try:
            parsed = uuid.UUID(value)
        except ValueError:
            return None, True
        canonical = str(parsed)
        return (canonical, False) if value == canonical else (None, True)

    @staticmethod
    def _normalize_path(path: Path) -> Path:
        return Path(os.path.abspath(os.fspath(path)))

    @staticmethod
    def _plan_payload(
        *,
        outcome: ReconcileOutcome,
        observed_at: datetime,
        runtime_state: RuntimeObservationState,
        items: tuple[ReconcilePlanItem, ...],
        run_items: tuple[RunReconcilePlanItem, ...],
        failures: tuple[ObservationFailure, ...],
        recovery_limits: tuple[RuntimeRecoveryLimit, ...],
    ) -> dict[str, object]:
        return {
            "version": 2,
            "outcome": outcome.value,
            "observed_at": observed_at.isoformat(timespec="microseconds").replace(
                "+00:00", "Z"
            ),
            "runtime_observation": runtime_state.value,
            "takeover_token_created": False,
            "items": [
                {
                    "session_id": item.session_id,
                    "classification": item.classification.value,
                    "task_id": item.task_id,
                    "task_key": item.task_key,
                    "runtime_state": (
                        item.runtime_state.value if item.runtime_state is not None else None
                    ),
                    "branch": item.branch,
                    "branch_commit": item.branch_commit,
                    "worktree_path": item.worktree_path,
                    "worktree_head": item.worktree_head,
                    "tmux_session": item.tmux_session,
                    "tmux_present": item.tmux_present,
                    "native_session_id": item.native_session_id,
                    "capability_digest_present": item.capability_digest_present,
                    "continued_from": item.continued_from,
                    "reasons": list(item.reasons),
                    "proposed_actions": list(item.proposed_actions),
                }
                for item in items
            ],
            "runs": [
                {
                    "observation_key": item.observation_key,
                    "run_id": item.run_id,
                    "classification": item.classification.value,
                    "state": item.state.value,
                    "branch": item.branch,
                    "branch_commit": item.branch_commit,
                    "tag": item.tag,
                    "tag_commit": item.tag_commit,
                    "spec_digest": item.spec_digest,
                    "result_id": item.result_id,
                    "markers": [
                        {
                            "observation_key": marker.observation_key,
                            "path": marker.path,
                            "attempt_id": marker.attempt_id,
                            "phase": marker.phase,
                            "pid": marker.pid,
                            "terminal": marker.terminal,
                            "valid": marker.valid,
                        }
                        for marker in item.markers
                    ],
                    "reasons": list(item.reasons),
                    "proposed_actions": list(item.proposed_actions),
                }
                for item in run_items
            ],
            "observation_failures": [
                {
                    "component": failure.component,
                    "code": failure.code,
                    "message": failure.message,
                    "returncode": failure.returncode,
                }
                for failure in failures
            ],
            "runtime_recovery_limits": [
                {"code": limit.code, "description": limit.description}
                for limit in recovery_limits
            ],
        }
