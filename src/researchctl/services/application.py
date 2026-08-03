from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from functools import wraps
from typing import Any, Callable, Protocol

from researchctl.constants import LINEAR_PROJECTION_POLICY_PATH
from researchctl.domain.enums import (
    NotificationRoute,
    NotificationState,
    SessionState,
    TaskState,
)
from researchctl.domain.models import (
    CIValidationAttestation,
    LinearProjectionPolicy,
    ProjectPolicy,
    RunAttemptEvent,
    RunSpec,
    TaskRecord,
)
from researchctl.domain.types import utc_now
from researchctl.errors import RCPError
from researchctl.runtime.models import (
    AttentionItem,
    OperationRecord,
    RuntimeSession,
    SessionNotification,
)
from researchctl.runtime.store import RuntimeStore, attention_dedupe_key
from researchctl.serialization import canonical_digest
from researchctl.services.actor import ActorContext, ActorRole
from researchctl.services.control_linear_policy import LinearPolicyWriteResult
from researchctl.services.requests import (
    BootstrapAcceptRequest,
    BootstrapProposalRequest,
    InboxAckRequest,
    InboxListRequest,
    InboxResolveRequest,
    InboxSnoozeRequest,
    LinearConfigureRequest,
    MutationRequest,
    NotificationAckRequest,
    NotificationListRequest,
    NotificationReplyRequest,
    NotificationSendRequest,
    ReviewAcceptRequest,
    RunCollectRequest,
    RunRetryRequest,
    RunStartRequest,
    SubmissionCreateRequest,
    SessionAddressRequest,
    SessionAttachRequest,
    SessionContinueRequest,
    SessionListRequest,
    SessionPauseRequest,
    SessionShowRequest,
    SessionStartRequest,
    StatusPublishRequest,
    TaskCancelRequest,
    TaskCreateRequest,
    TaskUpdateRequest,
    linear_notification_request_digest,
)
from researchctl.services.post_merge import PostMergeRequest
from researchctl.services.task_policy import task_transition_allowed
from researchctl.services.task_records import TaskRecordRepository, TaskWriteResult


_UNRECORDED_OPERATION_ERRORS = frozenset(
    {
        "git_timeout",
        "idempotency_conflict",
        "operation_id_conflict",
        "runtime_store_busy",
        "run_execution_uncertain",
        "session_start_pending",
        "session_transition_pending",
        "tmux_timeout",
    }
)


def _journaled_mutation(command: str):
    def decorate(method):
        @wraps(method)
        def wrapped(self, request, actor):
            try:
                return method(self, request, actor)
            except RCPError as error:
                self._record_operation_error(
                    command,
                    request.operation_id,
                    error,
                )
                raise

        return wrapped

    return decorate


@dataclass(frozen=True, slots=True)
class ServiceResult:
    command: str
    operation_id: str
    terminal_result: str
    data: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "terminal_result": self.terminal_result,
            **self.data,
        }


class SessionHarness(Protocol):
    def start_or_observe(
        self,
        request: SessionStartRequest,
        task: TaskRecord,
        *,
        continued_from: str | None = None,
    ) -> RuntimeSession: ...

    def pause_or_observe(self, session_id: str, mode: str) -> RuntimeSession: ...

    def attach_argv(self, session_id: str) -> tuple[str, ...]: ...

    def continue_or_observe(
        self,
        request: SessionContinueRequest,
        source: RuntimeSession,
        task: TaskRecord,
    ) -> RuntimeSession: ...


class SessionCommitVerifier(Protocol):
    def require_reachable(self, commit_sha: str, branch: str) -> None: ...


class BootstrapAcceptanceReceipt(Protocol):
    def as_dict(self) -> dict[str, object]: ...


class BootstrapAcceptance(Protocol):
    def prepare(self) -> BootstrapAcceptanceReceipt: ...


class BootstrapProposal(Protocol):
    def prepare(self) -> BootstrapAcceptanceReceipt: ...


class RunExecutionResult(Protocol):
    @property
    def terminal_result(self) -> str: ...

    def as_dict(self) -> dict[str, object]: ...


class RunCoordinator(Protocol):
    def execute(
        self,
        *,
        spec: RunSpec,
        task: TaskRecord,
        attempt_id: str,
        operation_id: str,
        assigned_gpu_uuids: tuple[str, ...],
        event_callback: Callable[[RunAttemptEvent], None],
        retry_of: str | None = None,
    ) -> RunExecutionResult: ...

    def collect(
        self,
        *,
        spec: RunSpec,
        task: TaskRecord,
        attempt_id: str,
        operation_id: str,
    ) -> RunExecutionResult: ...


class SubmissionWorkflowResult(Protocol):
    @property
    def terminal_result(self) -> str: ...

    def as_dict(self) -> dict[str, object]: ...


class SubmissionWorkflow(Protocol):
    def propose(
        self,
        request: SubmissionCreateRequest,
        task: TaskRecord,
    ) -> SubmissionWorkflowResult: ...

    def prepare_acceptance(
        self,
        request: ReviewAcceptRequest,
        task: TaskRecord,
        *,
        reviewer_actor: str,
        decided_at: datetime,
    ) -> SubmissionWorkflowResult: ...


class LinearAutomationResult(Protocol):
    state: str

    def as_dict(self) -> dict[str, object]: ...


class LinearAutomation(Protocol):
    runtime: RuntimeStore

    def enqueue_accepted(
        self,
        *,
        actor: ActorContext,
        project_id: str,
        merge_commit: str,
        ci: CIValidationAttestation,
    ) -> str | None: ...

    def run_once(
        self,
        *,
        actor: ActorContext,
        project_id: str,
        claim_id: str,
    ) -> LinearAutomationResult: ...


class LinearPolicyControl(Protocol):
    def configure(
        self,
        policy: LinearProjectionPolicy,
    ) -> LinearPolicyWriteResult: ...


class PostMergeAutomationResult(Protocol):
    state: str

    def as_dict(self) -> dict[str, object]: ...


class PostMergeAutomation(Protocol):
    runtime: RuntimeStore

    def process(
        self,
        *,
        request: PostMergeRequest,
        dispatch_artifact: bytes,
        actor: ActorContext,
    ) -> PostMergeAutomationResult: ...


class ApplicationService:
    """Shared authorization and mutation boundary for human and machine callers."""

    def __init__(
        self,
        *,
        project_id: str,
        policy: ProjectPolicy,
        tasks: TaskRecordRepository,
        runtime: RuntimeStore,
        sessions: SessionHarness | None = None,
        bootstrap_acceptance: BootstrapAcceptance | None = None,
        bootstrap_proposal: BootstrapProposal | None = None,
        runs: RunCoordinator | None = None,
        submission_workflow: SubmissionWorkflow | None = None,
        notification_commits: SessionCommitVerifier | None = None,
        linear_policy_control: LinearPolicyControl | None = None,
        linear_worker: LinearAutomation | None = None,
        post_merge: PostMergeAutomation | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.project_id = project_id
        self.policy = policy
        self.tasks = tasks
        self.runtime = runtime
        self.sessions = sessions
        self.bootstrap_acceptance = bootstrap_acceptance
        self.bootstrap_proposal = bootstrap_proposal
        self.runs = runs
        self.submission_workflow = submission_workflow
        self.notification_commits = notification_commits
        self.linear_policy_control = linear_policy_control
        self._linear_worker = linear_worker
        self._post_merge = post_merge
        self._clock = clock

    def _bind_linear_worker(self, worker: LinearAutomation) -> None:
        """One-time composition hook used only by the trusted deployment factory."""

        if worker.runtime is not self.runtime:
            raise ValueError("Linear worker must share the ApplicationService RuntimeStore")
        if self._linear_worker is not None and self._linear_worker is not worker:
            raise ValueError("Linear worker is already configured")
        self._linear_worker = worker

    def _require_linear_worker(self) -> LinearAutomation:
        if self._linear_worker is None:
            raise RCPError(
                code="linear_worker_not_configured",
                message="Trusted Linear delivery worker is not configured.",
            )
        return self._linear_worker

    def _bind_post_merge(self, service: PostMergeAutomation) -> None:
        if service.runtime is not self.runtime:
            raise ValueError("Post-merge service must share the RuntimeStore")
        if self._post_merge is not None and self._post_merge is not service:
            raise ValueError("Post-merge service is already configured")
        self._post_merge = service

    def _require_post_merge(self) -> PostMergeAutomation:
        if self._post_merge is None:
            raise RCPError(
                code="post_merge_not_configured",
                message="Trusted post-merge processing is not configured.",
            )
        return self._post_merge

    def post_merge_process(
        self,
        *,
        request: PostMergeRequest,
        dispatch_artifact: bytes,
        actor: ActorContext,
    ) -> PostMergeAutomationResult:
        actor.require_role("post-merge.process", ActorRole.TRUSTED_AUTOMATION)
        return self._require_post_merge().process(
            request=request,
            dispatch_artifact=dispatch_artifact,
            actor=actor,
        )

    def linear_enqueue_accepted(
        self,
        *,
        merge_commit: str,
        ci: CIValidationAttestation,
        actor: ActorContext,
    ) -> str | None:
        actor.require_role("linear.enqueue", ActorRole.TRUSTED_AUTOMATION)
        return self._require_linear_worker().enqueue_accepted(
            actor=actor,
            project_id=self.project_id,
            merge_commit=merge_commit,
            ci=ci,
        )

    def linear_delivery_run_once(
        self,
        *,
        claim_id: str,
        actor: ActorContext,
    ) -> LinearAutomationResult:
        actor.require_role("linear.deliver", ActorRole.TRUSTED_AUTOMATION)
        return self._require_linear_worker().run_once(
            actor=actor,
            project_id=self.project_id,
            claim_id=claim_id,
        )

    def _claim(
        self,
        command: str,
        request: MutationRequest,
        actor: ActorContext,
    ) -> OperationRecord:
        request_digest = canonical_digest(
            {
                "request": request.model_dump(mode="json", exclude_none=True),
                "actor": actor.model_dump(mode="json", exclude_none=True),
            }
        )
        return self.runtime.begin_operation(
            self.project_id,
            command,
            request.idempotency_key,
            request_digest,
            request.operation_id,
            self._clock(),
        )

    def _authorize(
        self,
        operation: OperationRecord,
        actor: ActorContext,
        *allowed: ActorRole,
        session_id: str | None = None,
        manager_allowed: bool = True,
    ) -> None:
        try:
            actor.require_role(operation.command, *allowed)
            if session_id is not None:
                actor.require_session_scope(
                    session_id,
                    command=operation.command,
                    manager_allowed=manager_allowed,
                )
        except RCPError as error:
            if operation.state != "terminal":
                self.runtime.append_operation_event(
                    operation.operation_id,
                    "authorization_denied",
                    self._clock(),
                    {
                        "actor_id": actor.actor_id,
                        "actor_role": actor.role.value,
                        "error_code": error.code,
                    },
                )
                self.runtime.finish_operation(
                    operation.operation_id,
                    "denied",
                    self._clock(),
                    {"success": False, "error": _error_data(error)},
                )
            raise
        if not any(event.kind == "actor_authorized" for event in operation.events):
            self.runtime.append_operation_event(
                operation.operation_id,
                "actor_authorized",
                self._clock(),
                {
                    "actor_id": actor.actor_id,
                    "actor_role": actor.role.value,
                    "credential_kind": actor.credential_kind.value,
                    "bound_session_id": actor.bound_session_id,
                },
            )

    @staticmethod
    def _replay(operation: OperationRecord) -> ServiceResult:
        if operation.state != "terminal" or operation.terminal_result is None:
            raise ValueError("operation is not terminal")
        payload = operation.result or {}
        if payload.get("success") is False:
            stored = payload.get("error")
            if not isinstance(stored, dict):
                raise RCPError(
                    code="runtime_store_corrupt",
                    message="Terminal operation is missing its stored error.",
                )
            raise RCPError(
                code=str(stored.get("code", "operation_failed")),
                message=str(stored.get("message", "Operation failed.")),
                remediation=(
                    str(stored["remediation"])
                    if stored.get("remediation") is not None
                    else None
                ),
                context=(stored.get("context") if isinstance(stored.get("context"), dict) else {}),
            )
        data = payload.get("data")
        if payload.get("success") is not True or not isinstance(data, dict):
            raise RCPError(
                code="runtime_store_corrupt",
                message="Terminal operation is missing its stored service result.",
            )
        return ServiceResult(
            command=operation.command,
            operation_id=operation.operation_id,
            terminal_result=operation.terminal_result,
            data=data,
        )

    def _finish(
        self,
        operation: OperationRecord,
        terminal_result: str,
        data: dict[str, Any],
    ) -> ServiceResult:
        finished = self.runtime.finish_operation(
            operation.operation_id,
            terminal_result,
            self._clock(),
            {"success": True, "data": data},
        )
        return self._replay(finished)

    def _validate_task_policy(self, task: TaskRecord) -> None:
        configured_domains = {
            item.execution_domain for item in self.policy.execution_domains
        }
        if task.execution_domain not in configured_domains:
            raise RCPError(
                code="execution_domain_not_configured",
                message="Task execution domain is not present in accepted Project policy.",
                context={"execution_domain": task.execution_domain},
            )
        if task.parent_task_id is not None:
            self.tasks.load(task.parent_task_id)

    def _record_operation_error(
        self,
        command: str,
        operation_id: str,
        error: RCPError,
    ) -> None:
        if error.code in _UNRECORDED_OPERATION_ERRORS:
            return
        operation = self.runtime.get_operation(operation_id)
        if (
            operation is None
            or operation.project_id != self.project_id
            or operation.command != command
            or operation.state == "terminal"
        ):
            return
        self.runtime.append_operation_event(
            operation_id,
            "operation_failed",
            self._clock(),
            {"error_code": error.code},
        )
        self.runtime.finish_operation(
            operation_id,
            "failed",
            self._clock(),
            {"success": False, "error": _error_data(error)},
        )

    def _task_result(self, result: TaskWriteResult, root: Any) -> dict[str, Any]:
        data: dict[str, Any] = {
            "task": result.record.model_dump(mode="json", exclude_none=True),
            "task_digest": result.digest,
            "path": result.path.relative_to(root).as_posix(),
            "changed": result.changed,
        }
        receipt = getattr(self.tasks, "proposal_receipt", None)
        if receipt is not None:
            data["proposal"] = receipt.as_dict()
            data["changed"] = receipt.effect_applied
        return data

    def _finish_task(
        self,
        operation: OperationRecord,
        result: TaskWriteResult,
        *,
        changed_result: str,
    ) -> ServiceResult:
        data = self._task_result(result, self.tasks.root)
        receipt = getattr(self.tasks, "proposal_receipt", None)
        if receipt is not None and receipt.effect_applied:
            terminal_result = "proposal_prepared"
        else:
            terminal_result = changed_result if result.changed else "no_change"
        return self._finish(operation, terminal_result, data)

    @_journaled_mutation("bootstrap.accept")
    def bootstrap_accept(
        self,
        request: BootstrapAcceptRequest,
        actor: ActorContext,
    ) -> ServiceResult:
        command = "bootstrap.accept"
        operation = self._claim(command, request, actor)
        if operation.state == "terminal":
            return self._replay(operation)
        self._authorize(operation, actor, ActorRole.MANAGER)
        if self.bootstrap_acceptance is None:
            raise RCPError(
                code="bootstrap_acceptance_not_configured",
                message="Bootstrap acceptance preparation is not configured.",
            )
        receipt = self.bootstrap_acceptance.prepare()
        proposal = receipt.as_dict()
        if proposal.get("proposal_commit") != request.proposal_commit:
            raise RCPError(
                code="bootstrap_proposal_commit_mismatch",
                message="Bootstrap receipt does not bind the requested proposal head.",
            )
        self.runtime.append_operation_event(
            operation.operation_id,
            "bootstrap_acceptance_prepared",
            self._clock(),
            {
                "bootstrap_id": request.bootstrap_id,
                "proposal_commit": request.proposal_commit,
                "acceptance_commit": proposal.get("commit"),
                "manifest_digest": proposal.get("manifest_digest"),
            },
        )
        return self._finish(
            operation,
            "proposal_prepared",
            {
                "bootstrap_id": request.bootstrap_id,
                "project_state": "bootstrapping",
                "proposal": proposal,
            },
        )

    @_journaled_mutation("bootstrap.propose")
    def bootstrap_propose(
        self,
        request: BootstrapProposalRequest,
        actor: ActorContext,
    ) -> ServiceResult:
        command = "bootstrap.propose"
        operation = self._claim(command, request, actor)
        if operation.state == "terminal":
            return self._replay(operation)
        self._authorize(operation, actor, ActorRole.MANAGER, ActorRole.AGENT)
        if self.bootstrap_proposal is None:
            raise RCPError(
                code="bootstrap_proposal_not_configured",
                message="Bootstrap proposal preparation is not configured.",
            )
        receipt = self.bootstrap_proposal.prepare()
        proposal = receipt.as_dict()
        mismatches = [
            field
            for field, expected in {
                "operation_id": request.operation_id,
                "bootstrap_id": request.bootstrap_id,
                "base_commit": request.expected_default_head,
            }.items()
            if proposal.get(field) != expected
        ]
        if mismatches:
            raise RCPError(
                code="bootstrap_proposal_receipt_mismatch",
                message="Bootstrap proposal receipt does not bind the request.",
                context={"mismatches": mismatches},
            )
        self.runtime.append_operation_event(
            operation.operation_id,
            "bootstrap_proposal_prepared",
            self._clock(),
            {
                "bootstrap_id": request.bootstrap_id,
                "base_commit": request.expected_default_head,
                "proposal_commit": proposal.get("commit"),
                "manifest_digest": proposal.get("manifest_digest"),
            },
        )
        return self._finish(
            operation,
            "proposal_prepared",
            {
                "bootstrap_id": request.bootstrap_id,
                "project_state": "bootstrapping",
                "proposal": proposal,
            },
        )

    @_journaled_mutation("linear.configure")
    def linear_configure(
        self,
        request: LinearConfigureRequest,
        actor: ActorContext,
    ) -> ServiceResult:
        command = "linear.configure"
        operation = self._claim(command, request, actor)
        if operation.state == "terminal":
            return self._replay(operation)
        self._authorize(operation, actor, ActorRole.MANAGER)
        if self.linear_policy_control is None:
            raise RCPError(
                code="linear_policy_control_not_configured",
                message="Linear policy proposal preparation is not configured.",
            )
        written = self.linear_policy_control.configure(request.policy)
        proposal = written.proposal.as_dict()
        try:
            relative_path = written.path.relative_to(
                written.proposal.worktree
            ).as_posix()
        except ValueError as error:
            raise RCPError(
                code="linear_policy_control_receipt_mismatch",
                message="Linear policy receipt path is outside its worktree.",
            ) from error
        expected_digest = canonical_digest(request.policy)
        expected_branch = f"research/control/{request.operation_id}"
        if (
            written.policy != request.policy
            or written.base_commit != request.expected_default_head
            or written.digest != expected_digest
            or written.proposal.branch != expected_branch
            or relative_path != LINEAR_PROJECTION_POLICY_PATH
        ):
            raise RCPError(
                code="linear_policy_control_receipt_mismatch",
                message="Linear policy receipt does not bind the configure request.",
            )
        self.runtime.append_operation_event(
            operation.operation_id,
            "linear_policy_observed",
            self._clock(),
            {
                "base_commit": written.base_commit,
                "previous_digest": written.previous_digest,
                "policy_digest": written.digest,
                "proposal_commit": written.proposal.commit,
            },
        )
        data = {
            "linear_policy": written.policy.model_dump(
                mode="json",
                exclude_none=True,
            ),
            "previous_policy_digest": written.previous_digest,
            "policy_digest": written.digest,
            "path": relative_path,
            "changed": written.proposal.effect_applied,
            "proposal": proposal,
        }
        return self._finish(
            operation,
            (
                "proposal_prepared"
                if written.proposal.effect_applied
                else "no_change"
            ),
            data,
        )

    @_journaled_mutation("task.create")
    def task_create(
        self,
        request: TaskCreateRequest,
        actor: ActorContext,
    ) -> ServiceResult:
        command = "task.create"
        operation = self._claim(command, request, actor)
        if operation.state == "terminal":
            return self._replay(operation)
        self._authorize(operation, actor, ActorRole.MANAGER)
        if request.task.state is not TaskState.PLANNED:
            raise RCPError(
                code="invalid_task_initial_state",
                message="A new Task must begin in the planned state.",
            )
        self._validate_task_policy(request.task)
        written = self.tasks.create(request.task)
        self.runtime.append_operation_event(
            operation.operation_id,
            "task_record_observed",
            self._clock(),
            {"task_id": request.task.task_id, "digest": written.digest},
        )
        return self._finish_task(
            operation,
            written,
            changed_result="created",
        )

    @_journaled_mutation("task.update")
    def task_update(
        self,
        request: TaskUpdateRequest,
        actor: ActorContext,
    ) -> ServiceResult:
        command = "task.update"
        operation = self._claim(command, request, actor)
        if operation.state == "terminal":
            return self._replay(operation)
        self._authorize(operation, actor, ActorRole.MANAGER)
        current = self.tasks.load(request.task_id)
        replacement = request.replacement
        if replacement.task_id != current.task_id or replacement.key != current.key:
            raise RCPError(
                code="immutable_task_identity",
                message="Task update cannot change its canonical ID or key.",
            )
        if replacement.created_at != current.created_at:
            raise RCPError(
                code="immutable_task_creation_time",
                message="Task update cannot change created_at.",
            )
        if not task_transition_allowed(current.state, replacement.state):
            raise RCPError(
                code="invalid_task_transition",
                message=f"Task cannot transition from {current.state} to {replacement.state}.",
                context={"from": current.state.value, "to": replacement.state.value},
            )
        self._validate_task_policy(replacement)
        replacement_digest = canonical_digest(replacement)
        if canonical_digest(current) == replacement_digest:
            written = TaskWriteResult(
                record=current,
                digest=replacement_digest,
                path=self.tasks.path_for(request.task_id),
                changed=False,
            )
        else:
            if replacement.updated_at <= current.updated_at:
                raise RCPError(
                    code="invalid_task_update_time",
                    message="A changed Task must use an updated_at later than the current record.",
                )
            written = self.tasks.replace(
                request.task_id,
                request.expected_digest,
                replacement,
            )
        self.runtime.append_operation_event(
            operation.operation_id,
            "task_record_observed",
            self._clock(),
            {"task_id": request.task_id, "digest": written.digest},
        )
        return self._finish_task(
            operation,
            written,
            changed_result="updated",
        )

    @_journaled_mutation("task.cancel")
    def task_cancel(
        self,
        request: TaskCancelRequest,
        actor: ActorContext,
    ) -> ServiceResult:
        command = "task.cancel"
        operation = self._claim(command, request, actor)
        if operation.state == "terminal":
            return self._replay(operation)
        self._authorize(operation, actor, ActorRole.MANAGER)
        current = self.tasks.load(request.task_id)
        if current.state is TaskState.DONE:
            raise RCPError(
                code="task_terminal",
                message="A completed Task cannot be canceled.",
            )
        written = self.tasks.cancel(
            request.task_id,
            request.expected_digest,
            updated_at=request.updated_at,
        )
        self.runtime.append_operation_event(
            operation.operation_id,
            "task_cancellation_observed",
            self._clock(),
            {
                "task_id": request.task_id,
                "digest": written.digest,
                "reason": request.reason,
            },
        )
        return self._finish_task(
            operation,
            written,
            changed_result="canceled",
        )

    def _session_harness(self) -> SessionHarness:
        if self.sessions is None:
            raise RCPError(
                code="session_harness_not_configured",
                message="Local Session execution is not configured for this project.",
            )
        return self.sessions

    def _read_session(
        self,
        session_id: str,
        actor: ActorContext,
        *,
        command: str,
    ) -> RuntimeSession:
        actor.require_role(command, ActorRole.MANAGER, ActorRole.AGENT)
        actor.require_session_scope(session_id, command=command)
        session = self.runtime.get_session(session_id)
        if session is None or session.project_id != self.project_id:
            raise RCPError(
                code="session_not_found",
                message="Session was not found in this project.",
                context={"session_id": session_id},
            )
        return session

    def _session_task(self, task_id: str) -> TaskRecord:
        task = self.tasks.load(task_id)
        if task.state not in {TaskState.READY, TaskState.ACTIVE, TaskState.BLOCKED}:
            raise RCPError(
                code="task_not_runnable",
                message="Session start requires a ready, active, or blocked Task.",
                context={"task_id": task.task_id, "task_state": task.state.value},
            )
        self._validate_task_policy(task)
        return task

    def _run_context(
        self,
        *,
        spec: RunSpec,
        operation: OperationRecord,
        actor: ActorContext,
        require_runnable: bool = True,
    ) -> TaskRecord:
        self._authorize(
            operation,
            actor,
            ActorRole.MANAGER,
            ActorRole.AGENT,
            ActorRole.RUNNER,
            session_id=spec.session_id,
        )
        if require_runnable:
            task = self._session_task(spec.task_id)
        else:
            task = self.tasks.load(spec.task_id)
            self._validate_task_policy(task)
        session = self.runtime.get_session(spec.session_id)
        if session is None or session.project_id != self.project_id:
            raise RCPError(
                code="session_not_found",
                message="RunSpec Session was not found in this project.",
                context={"session_id": spec.session_id},
            )
        if session.task_id != spec.task_id:
            raise RCPError(
                code="run_session_task_mismatch",
                message="RunSpec Task does not match its bound Session.",
            )
        if require_runnable and session.state not in {
            SessionState.ACTIVE,
            SessionState.IDLE,
        }:
            raise RCPError(
                code="run_session_not_runnable",
                message="Run requires an active or idle bound Session.",
                context={
                    "session_id": session.session_id,
                    "session_state": session.state.value,
                },
            )
        if self.runs is None:
            raise RCPError(
                code="run_coordinator_not_configured",
                message="Local immutable Run execution is not configured.",
            )
        return task

    def _run_event_callback(
        self,
        operation_id: str,
    ) -> Callable[[RunAttemptEvent], None]:
        def append(event: RunAttemptEvent) -> None:
            if event.operation_id != operation_id:
                raise RCPError(
                    code="run_attempt_operation_mismatch",
                    message="RunAttempt event belongs to another Operation.",
                )
            self.runtime.append_operation_event(
                operation_id,
                f"run_attempt.{event.state.value}",
                event.observed_at,
                event.model_dump(mode="json", exclude_none=True),
            )

        return append

    def _execute_run(
        self,
        *,
        command: str,
        request: RunStartRequest | RunRetryRequest,
        actor: ActorContext,
    ) -> ServiceResult:
        operation = self._claim(command, request, actor)
        if operation.state == "terminal":
            return self._replay(operation)
        task = self._run_context(spec=request.spec, operation=operation, actor=actor)
        self.runtime.append_operation_event(
            operation.operation_id,
            "run_request_validated",
            self._clock(),
            {
                "run_id": request.spec.run_id,
                "attempt_id": request.attempt_id,
                "retry_of": (
                    request.retry_of if isinstance(request, RunRetryRequest) else None
                ),
                "spec_digest": request.spec.spec_digest,
            },
        )
        assert self.runs is not None
        executed = self.runs.execute(
            spec=request.spec,
            task=task,
            attempt_id=request.attempt_id,
            operation_id=request.operation_id,
            assigned_gpu_uuids=request.assigned_gpu_uuids,
            event_callback=self._run_event_callback(operation.operation_id),
            retry_of=(
                request.retry_of if isinstance(request, RunRetryRequest) else None
            ),
        )
        return self._finish(
            operation,
            executed.terminal_result,
            {"run": executed.as_dict()},
        )

    @_journaled_mutation("run.start")
    def run_start(
        self,
        request: RunStartRequest,
        actor: ActorContext,
    ) -> ServiceResult:
        return self._execute_run(command="run.start", request=request, actor=actor)

    @_journaled_mutation("run.retry")
    def run_retry(
        self,
        request: RunRetryRequest,
        actor: ActorContext,
    ) -> ServiceResult:
        return self._execute_run(command="run.retry", request=request, actor=actor)

    @_journaled_mutation("run.collect")
    def run_collect(
        self,
        request: RunCollectRequest,
        actor: ActorContext,
    ) -> ServiceResult:
        command = "run.collect"
        operation = self._claim(command, request, actor)
        if operation.state == "terminal":
            return self._replay(operation)
        task = self._run_context(
            spec=request.spec,
            operation=operation,
            actor=actor,
            require_runnable=False,
        )
        self.runtime.append_operation_event(
            operation.operation_id,
            "run_collection_validated",
            self._clock(),
            {
                "run_id": request.spec.run_id,
                "attempt_id": request.attempt_id,
                "spec_digest": request.spec.spec_digest,
            },
        )
        assert self.runs is not None
        collected = self.runs.collect(
            spec=request.spec,
            task=task,
            attempt_id=request.attempt_id,
            operation_id=request.operation_id,
        )
        return self._finish(
            operation,
            collected.terminal_result,
            {"run": collected.as_dict()},
        )

    def _submission_task(
        self,
        *,
        task_id: str,
        session_id: str,
        operation: OperationRecord,
        actor: ActorContext,
    ) -> TaskRecord:
        self._authorize(
            operation,
            actor,
            ActorRole.MANAGER,
            ActorRole.AGENT,
            session_id=session_id,
        )
        task = self.tasks.load(task_id)
        self._validate_task_policy(task)
        session = self.runtime.get_session(session_id)
        if session is None or session.project_id != self.project_id:
            raise RCPError(
                code="session_not_found",
                message="ResearchSubmission Session was not found in this Project.",
                context={"session_id": session_id},
            )
        if session.task_id != task_id:
            raise RCPError(
                code="submission_session_task_mismatch",
                message="ResearchSubmission Task does not match its bound Session.",
            )
        if self.submission_workflow is None:
            raise RCPError(
                code="submission_workflow_not_configured",
                message="ResearchSubmission workflow is not configured.",
            )
        return task

    @_journaled_mutation("submission.create")
    def submission_create(
        self,
        request: SubmissionCreateRequest,
        actor: ActorContext,
    ) -> ServiceResult:
        command = "submission.create"
        operation = self._claim(command, request, actor)
        if operation.state == "terminal":
            return self._replay(operation)
        task = self._submission_task(
            task_id=request.submission.task_id,
            session_id=request.submission.session_id,
            operation=operation,
            actor=actor,
        )
        assert self.submission_workflow is not None
        proposed = self.submission_workflow.propose(request, task)
        data = proposed.as_dict()
        self.runtime.append_operation_event(
            operation.operation_id,
            "submission_proposal_prepared",
            self._clock(),
            {
                "submission_id": request.submission.submission_id,
                "base_commit": request.base_commit,
                "proposal_commit": data.get("proposal", {}).get("commit")
                if isinstance(data.get("proposal"), dict)
                else None,
            },
        )
        return self._finish(
            operation,
            proposed.terminal_result,
            {"submission": data},
        )

    @_journaled_mutation("review.accept")
    def review_accept(
        self,
        request: ReviewAcceptRequest,
        actor: ActorContext,
    ) -> ServiceResult:
        command = "review.accept"
        operation = self._claim(command, request, actor)
        if operation.state == "terminal":
            return self._replay(operation)
        self._authorize(operation, actor, ActorRole.MANAGER)
        task = self.tasks.load(request.task_id)
        self._validate_task_policy(task)
        if self.submission_workflow is None:
            raise RCPError(
                code="submission_workflow_not_configured",
                message="ResearchSubmission workflow is not configured.",
            )
        prepared = self.submission_workflow.prepare_acceptance(
            request,
            task,
            reviewer_actor=actor.actor_id,
            decided_at=self._clock(),
        )
        data = prepared.as_dict()
        self.runtime.append_operation_event(
            operation.operation_id,
            "review_acceptance_prepared",
            self._clock(),
            {
                "submission_id": request.submission_id,
                "expected_head": request.expected_head,
                "acceptance_commit": data.get("proposal", {}).get("commit")
                if isinstance(data.get("proposal"), dict)
                else None,
            },
        )
        return self._finish(
            operation,
            prepared.terminal_result,
            {"review": data},
        )

    def _finish_session(
        self,
        operation: OperationRecord,
        session: RuntimeSession,
    ) -> ServiceResult:
        self.runtime.append_operation_event(
            operation.operation_id,
            "session_observed",
            self._clock(),
            {
                "session_id": session.session_id,
                "state": session.state.value,
                "host": session.host,
                "branch": session.branch,
                "worktree_path": session.worktree_path,
                "tmux_session": session.metadata.get("tmux_session"),
                "native_session_id": session.metadata.get("native_session_id"),
            },
        )
        if session.state in {SessionState.PREPARING, SessionState.STOPPING}:
            raise RCPError(
                code="session_transition_pending",
                message="Session transition has not reached an observable stable state yet.",
                remediation="Retry with the same idempotency key.",
                context={
                    "operation_id": operation.operation_id,
                    "session_id": session.session_id,
                },
            )
        return self._finish(
            operation,
            session.state.value,
            {"session": _session_data(session)},
        )

    @_journaled_mutation("session.start")
    def session_start(
        self,
        request: SessionStartRequest,
        actor: ActorContext,
    ) -> ServiceResult:
        command = "session.start"
        operation = self._claim(command, request, actor)
        if operation.state == "terminal":
            return self._replay(operation)
        self._authorize(
            operation,
            actor,
            ActorRole.MANAGER,
            ActorRole.AGENT,
            session_id=request.session_id,
        )
        task = self._session_task(request.task_id)
        session = self._session_harness().start_or_observe(request, task)
        return self._finish_session(operation, session)

    @_journaled_mutation("session.pause")
    def session_pause(
        self,
        request: SessionPauseRequest,
        actor: ActorContext,
    ) -> ServiceResult:
        command = "session.pause"
        operation = self._claim(command, request, actor)
        if operation.state == "terminal":
            return self._replay(operation)
        self._authorize(
            operation,
            actor,
            ActorRole.MANAGER,
            ActorRole.AGENT,
            session_id=request.session_id,
        )
        current = self.runtime.get_session(request.session_id)
        if current is None or current.project_id != self.project_id:
            raise RCPError(
                code="session_not_found",
                message="Session was not found in this project.",
                context={"session_id": request.session_id},
            )
        session = self._session_harness().pause_or_observe(
            request.session_id,
            request.mode,
        )
        return self._finish_session(operation, session)

    def session_attach(
        self,
        request: SessionAttachRequest,
        actor: ActorContext,
    ) -> dict[str, Any]:
        actor.require_role("session.attach", ActorRole.MANAGER, ActorRole.AGENT)
        actor.require_session_scope(request.session_id, command="session.attach")
        session = self.runtime.get_session(request.session_id)
        if session is None or session.project_id != self.project_id:
            raise RCPError(
                code="session_not_found",
                message="Session was not found in this project.",
                context={"session_id": request.session_id},
            )
        argv = self._session_harness().attach_argv(request.session_id)
        return {"session": _session_data(session), "attach_argv": list(argv)}

    def session_list(
        self,
        request: SessionListRequest,
        actor: ActorContext,
    ) -> dict[str, Any]:
        actor.require_role("session.list", ActorRole.MANAGER, ActorRole.AGENT)
        if actor.role is ActorRole.MANAGER:
            sessions = self.runtime.list_sessions(
                self.project_id,
                task_id=request.task_id,
                state=request.state,
            )
        else:
            bound_session_id = actor.bound_session_id
            assert bound_session_id is not None
            bound = self.runtime.get_session(bound_session_id)
            visible = (
                bound is not None
                and bound.project_id == self.project_id
                and (request.task_id is None or bound.task_id == request.task_id)
                and (request.state is None or bound.state is request.state)
            )
            sessions = (bound,) if visible and bound is not None else ()
        return {"items": [_session_data(item) for item in sessions[: request.limit]]}

    def session_show(
        self,
        request: SessionShowRequest,
        actor: ActorContext,
    ) -> dict[str, Any]:
        session = self._read_session(
            request.session_id,
            actor,
            command="session.show",
        )
        return {"session": _session_data(session)}

    def session_address(
        self,
        request: SessionAddressRequest,
        actor: ActorContext,
    ) -> dict[str, Any]:
        session = self._read_session(
            request.session_id,
            actor,
            command="session.address",
        )
        if not session.branch:
            raise RCPError(
                code="notification_session_branch_missing",
                message="Notification Session has no recorded Git branch.",
                context={"session_id": session.session_id},
            )
        if self.notification_commits is None:
            raise RCPError(
                code="notification_commit_verifier_not_configured",
                message="Session commit verification is not configured.",
            )
        self.notification_commits.require_reachable(
            request.commit_sha,
            session.branch,
        )
        command_header = (
            f"@{request.app} notify session:{session.session_id} "
            f"commit:{request.commit_sha}"
        )
        return {
            "command_header": command_header,
            "message_required": True,
            "session": _session_data(session),
        }

    @_journaled_mutation("session.continue")
    def session_continue_new(
        self,
        request: SessionContinueRequest,
        actor: ActorContext,
    ) -> ServiceResult:
        command = "session.continue"
        operation = self._claim(command, request, actor)
        if operation.state == "terminal":
            return self._replay(operation)
        self._authorize(
            operation,
            actor,
            ActorRole.MANAGER,
            ActorRole.AGENT,
            session_id=request.source_session_id,
        )
        source = self.runtime.get_session(request.source_session_id)
        if source is None or source.project_id != self.project_id:
            raise RCPError(
                code="session_not_found",
                message="Source Session was not found in this project.",
                context={"session_id": request.source_session_id},
            )
        if request.new_session_id == source.session_id:
            raise RCPError(
                code="continued_session_identity_conflict",
                message="Continuation requires a new Session ID.",
            )
        task = self._session_task(source.task_id)
        session = self._session_harness().continue_or_observe(
            request,
            source,
            task,
        )
        if session.continued_from != source.session_id:
            raise RCPError(
                code="continued_session_link_missing",
                message="New Session does not preserve its continued-from identity.",
            )
        return self._finish_session(operation, session)

    @_journaled_mutation("notification.send")
    def notification_send(
        self,
        request: NotificationSendRequest,
        actor: ActorContext,
    ) -> ServiceResult:
        """Persist a pre-parsed notification from a trusted human/app boundary."""
        command = "notification.send"
        operation = self._claim(command, request, actor)
        if operation.state == "terminal":
            return self._replay(operation)
        self._authorize(
            operation,
            actor,
            ActorRole.MANAGER,
            ActorRole.TRUSTED_AUTOMATION,
        )
        ingress_receipt = self.runtime.require_verified_notification_origin(
            project_id=self.project_id,
            workspace_id=request.origin.workspace_id,
            issue_id=request.origin.issue_id,
            thread_id=request.origin.thread_id,
            comment_id=request.origin.comment_id,
            task_id=request.task_id,
        )
        source_marker = request.origin.source_marker
        if ingress_receipt.source_marker != source_marker:
            raise RCPError(
                code="linear_notification_source_receipt_mismatch",
                message=(
                    "Notification source marker does not match the verified "
                    "accepted-result thread binding."
                ),
            )
        if ingress_receipt.command_digest != linear_notification_request_digest(
            request
        ):
            raise RCPError(
                code="linear_notification_request_receipt_mismatch",
                message=(
                    "Notification content does not match its verified Linear "
                    "ingress receipt."
                ),
            )
        task = self.tasks.load(request.task_id)
        if task.linear_issue_id is None:
            raise RCPError(
                code="notification_task_linear_unbound",
                message="Task has no accepted Linear issue binding.",
                remediation="Bind the exact Linear issue UUID through a reviewed Task update.",
                context={"task_id": task.task_id},
            )
        if task.linear_issue_id != request.origin.issue_id:
            raise RCPError(
                code="notification_linear_issue_mismatch",
                message="Notification source issue does not match the Task binding.",
                context={
                    "task_id": task.task_id,
                    "expected_issue_id": task.linear_issue_id,
                    "observed_issue_id": request.origin.issue_id,
                },
            )
        session = self.runtime.get_session(request.session_id)
        if session is None or session.project_id != self.project_id:
            raise RCPError(
                code="notification_session_not_found",
                message="Notification Session was not found in this project.",
                context={"session_id": request.session_id},
            )
        if session.task_id != task.task_id:
            raise RCPError(
                code="notification_session_task_mismatch",
                message="Notification Session is bound to another Task.",
                context={
                    "session_id": session.session_id,
                    "task_id": task.task_id,
                },
            )
        if not session.branch:
            raise RCPError(
                code="notification_session_branch_missing",
                message="Notification Session has no recorded Git branch.",
                context={"session_id": session.session_id},
            )
        if self.notification_commits is None:
            raise RCPError(
                code="notification_commit_verifier_not_configured",
                message="Session commit verification is not configured.",
            )
        self.notification_commits.require_reachable(
            request.commit_sha,
            session.branch,
        )
        notification = self.runtime.create_notification(
            notification_id=request.notification_id,
            project_id=self.project_id,
            task_id=task.task_id,
            session_id=session.session_id,
            commit_sha=request.commit_sha,
            message=request.message,
            origin=request.origin,
            created_at=self._clock(),
        )
        if not any(
            event.kind == "session_notification_persisted"
            for event in operation.events
        ):
            self.runtime.append_operation_event(
                operation.operation_id,
                "session_notification_persisted",
                self._clock(),
                {
                    "notification_id": notification.notification_id,
                    "task_id": notification.task_id,
                    "session_id": notification.session_id,
                    "commit_sha": notification.commit_sha,
                    "route": notification.route.value,
                    "source_thread_id": notification.origin.thread_id,
                    "source_comment_id": notification.origin.comment_id,
                },
            )
        terminal_result = (
            "routed_to_manager_exception"
            if notification.route is NotificationRoute.MANAGER_EXCEPTION
            else "routed_to_session"
        )
        return self._finish(
            operation,
            terminal_result,
            {"notification": _notification_data(notification)},
        )

    def notification_list(
        self,
        request: NotificationListRequest,
        actor: ActorContext,
    ) -> tuple[SessionNotification, ...]:
        actor.require_role(
            "notification.list",
            ActorRole.MANAGER,
            ActorRole.AGENT,
        )
        route: NotificationRoute | None = None
        session_id = request.session_id
        if actor.role is ActorRole.AGENT:
            if request.manager_exceptions_only:
                raise RCPError(
                    code="authorization_denied",
                    message="Session actors cannot read the manager exception inbox.",
                    context={
                        "actor_id": actor.actor_id,
                        "command": "notification.list",
                    },
                )
            session_id = session_id or actor.bound_session_id
            assert session_id is not None
            actor.require_session_scope(
                session_id,
                command="notification.list",
                manager_allowed=False,
            )
            route = NotificationRoute.SESSION
        elif request.manager_exceptions_only:
            route = NotificationRoute.MANAGER_EXCEPTION
        return self.runtime.list_notifications(
            self.project_id,
            session_id=session_id,
            route=route,
            include_closed=request.include_closed,
            limit=request.limit,
        )

    def _notification_for_action(
        self,
        operation: OperationRecord,
        notification_id: str,
        actor: ActorContext,
    ) -> SessionNotification:
        notification = self.runtime.get_notification(notification_id)
        if notification is None or notification.project_id != self.project_id:
            raise RCPError(
                code="notification_not_found",
                message="Session notification was not found in this project.",
                context={"notification_id": notification_id},
            )
        self._authorize(
            operation,
            actor,
            ActorRole.MANAGER,
            ActorRole.AGENT,
            session_id=notification.session_id,
        )
        if (
            notification.route is NotificationRoute.MANAGER_EXCEPTION
            and actor.role is not ActorRole.MANAGER
        ):
            raise RCPError(
                code="notification_manager_exception",
                message="Terminal Session notification has fallen back to the manager inbox.",
                remediation="A manager must resolve or reply to this notification.",
                context={
                    "notification_id": notification.notification_id,
                    "session_id": notification.session_id,
                    "fallback_reason": notification.fallback_reason,
                },
            )
        return notification

    @_journaled_mutation("notification.ack")
    def notification_ack(
        self,
        request: NotificationAckRequest,
        actor: ActorContext,
    ) -> ServiceResult:
        command = "notification.ack"
        operation = self._claim(command, request, actor)
        if operation.state == "terminal":
            return self._replay(operation)
        current = self._notification_for_action(
            operation,
            request.notification_id,
            actor,
        )
        notification = self.runtime.ack_notification(
            request.notification_id,
            actor.actor_id,
            self._clock(),
            expected_revision=request.expected_revision,
            operation_id=operation.operation_id,
        )
        if not any(
            event.kind == "session_notification_acknowledged"
            for event in operation.events
        ):
            self.runtime.append_operation_event(
                operation.operation_id,
                "session_notification_acknowledged",
                self._clock(),
                {
                    "notification_id": notification.notification_id,
                    "revision": notification.revision,
                },
            )
        terminal_result = (
            "already_acknowledged"
            if current.state is NotificationState.ACKNOWLEDGED
            else "acknowledged"
        )
        return self._finish(
            operation,
            terminal_result,
            {"notification": _notification_data(notification)},
        )

    @_journaled_mutation("notification.reply")
    def notification_reply(
        self,
        request: NotificationReplyRequest,
        actor: ActorContext,
    ) -> ServiceResult:
        command = "notification.reply"
        operation = self._claim(command, request, actor)
        if operation.state == "terminal":
            return self._replay(operation)
        self._notification_for_action(
            operation,
            request.notification_id,
            actor,
        )
        published = self.runtime.reply_notification(
            notification_id=request.notification_id,
            reply_id=request.reply_id,
            actor_id=actor.actor_id,
            body=request.body,
            observed_at=self._clock(),
            expected_revision=request.expected_revision,
            operation_id=operation.operation_id,
        )
        if not any(
            event.kind == "session_notification_reply_persisted"
            for event in operation.events
        ):
            self.runtime.append_operation_event(
                operation.operation_id,
                "session_notification_reply_persisted",
                self._clock(),
                {
                    "notification_id": published.notification.notification_id,
                    "reply_id": published.reply.reply_id,
                    "outbox_id": published.outbox.outbox_id,
                    "source_thread_id": published.notification.origin.thread_id,
                },
            )
        return self._finish(
            operation,
            "reply_queued",
            {
                "notification": _notification_data(published.notification),
                "reply": {
                    "reply_id": published.reply.reply_id,
                    "notification_id": published.reply.notification_id,
                    "actor_id": published.reply.actor_id,
                    "body": published.reply.body,
                    "payload_digest": published.reply.payload_digest,
                    "created_at": published.reply.created_at.isoformat().replace(
                        "+00:00", "Z"
                    ),
                },
                "outbox": {
                    "outbox_id": published.outbox.outbox_id,
                    "topic": published.outbox.topic,
                    "state": published.outbox.state,
                },
            },
        )

    @_journaled_mutation("status.publish")
    def status_publish(
        self,
        request: StatusPublishRequest,
        actor: ActorContext,
    ) -> ServiceResult:
        command = "status.publish"
        operation = self._claim(command, request, actor)
        if operation.state == "terminal":
            return self._replay(operation)
        self._authorize(
            operation,
            actor,
            ActorRole.AGENT,
            ActorRole.RUNNER,
            session_id=request.update.session_id,
            manager_allowed=False,
        )
        published = self.runtime.publish_status_update(self.project_id, request.update)
        self.runtime.append_operation_event(
            operation.operation_id,
            "status_update_persisted",
            self._clock(),
            {
                "update_id": published.update.update_id,
                "outbox_id": published.outbox.outbox_id,
                "attention_key": published.attention.dedupe_key,
            },
        )
        data = {
            "update": published.update.model_dump(mode="json", exclude_none=True),
            "outbox_id": published.outbox.outbox_id,
            "attention": _attention_data(published.attention),
        }
        return self._finish(operation, "persisted", data)

    def inbox_list(
        self,
        request: InboxListRequest,
        actor: ActorContext,
    ) -> tuple[AttentionItem, ...]:
        actor.require_role("inbox.list", ActorRole.MANAGER)
        items = self.runtime.list_inbox(
            self.project_id,
            as_of=request.now,
            include_resolved=request.include_resolved,
        )
        return items[: request.limit]

    def _attention_for_action(self, update_id: str, expected_generation: int) -> AttentionItem:
        update = self.runtime.get_status_update(update_id)
        if update is None:
            raise RCPError(
                code="status_update_not_found",
                message="StatusUpdate was not found.",
                context={"update_id": update_id},
            )
        item = self.runtime.get_attention(attention_dedupe_key(self.project_id, update))
        generation = getattr(item, "generation", None) if item is not None else None
        if (
            item is None
            or item.current_update.update_id != update_id
            or generation != expected_generation
        ):
            raise RCPError(
                code="stale_attention",
                message="Attention item changed after the manager observed it.",
                context={"update_id": update_id},
            )
        return item

    @_journaled_mutation("inbox.ack")
    def inbox_ack(
        self,
        request: InboxAckRequest,
        actor: ActorContext,
    ) -> ServiceResult:
        return self._inbox_action("inbox.ack", request, actor)

    @_journaled_mutation("inbox.snooze")
    def inbox_snooze(
        self,
        request: InboxSnoozeRequest,
        actor: ActorContext,
    ) -> ServiceResult:
        return self._inbox_action("inbox.snooze", request, actor)

    @_journaled_mutation("inbox.resolve")
    def inbox_resolve(
        self,
        request: InboxResolveRequest,
        actor: ActorContext,
    ) -> ServiceResult:
        return self._inbox_action("inbox.resolve", request, actor)

    def _inbox_action(
        self,
        command: str,
        request: InboxAckRequest | InboxSnoozeRequest | InboxResolveRequest,
        actor: ActorContext,
    ) -> ServiceResult:
        operation = self._claim(command, request, actor)
        if operation.state == "terminal":
            return self._replay(operation)
        self._authorize(operation, actor, ActorRole.MANAGER)
        item = self._attention_for_action(request.update_id, request.expected_generation)
        if isinstance(request, InboxAckRequest):
            changed = self.runtime.ack_attention(
                item.dedupe_key,
                actor.actor_id,
                self._clock(),
                expected_generation=request.expected_generation,
                operation_id=operation.operation_id,
            )
            result = "acknowledged"
        elif isinstance(request, InboxSnoozeRequest):
            changed = self.runtime.snooze_attention(
                item.dedupe_key,
                actor.actor_id,
                request.until,
                self._clock(),
                expected_generation=request.expected_generation,
                operation_id=operation.operation_id,
            )
            result = "snoozed"
        else:
            changed = self.runtime.resolve_attention(
                item.dedupe_key,
                actor.actor_id,
                request.reason,
                self._clock(),
                expected_generation=request.expected_generation,
                operation_id=operation.operation_id,
            )
            result = "resolved"
        self.runtime.append_operation_event(
            operation.operation_id,
            "attention_action_persisted",
            self._clock(),
            {
                "update_id": request.update_id,
                "attention_key": item.dedupe_key,
                "action": command.removeprefix("inbox."),
            },
        )
        return self._finish(operation, result, {"attention": _attention_data(changed)})


def _error_data(error: RCPError) -> dict[str, Any]:
    return {
        "code": error.code,
        "message": error.message,
        "remediation": error.remediation,
        "context": error.context,
    }


def _attention_data(item: AttentionItem) -> dict[str, Any]:
    return {
        "attention_key": item.dedupe_key,
        "generation": getattr(item, "generation", None),
        "state": item.state,
        "kind": item.kind,
        "task_id": item.task_id,
        "session_id": item.session_id,
        "current_update_id": item.current_update.update_id,
        "last_seen_at": item.last_seen_at.isoformat().replace("+00:00", "Z"),
    }


def _session_data(session: RuntimeSession) -> dict[str, Any]:
    return {
        "session_id": session.session_id,
        "task_id": session.task_id,
        "state": session.state.value,
        "host": session.host,
        "branch": session.branch,
        "worktree_path": session.worktree_path,
        "continued_from": session.continued_from,
        "tmux_session": session.metadata.get("tmux_session"),
        "agent": session.metadata.get("agent"),
        "native_session_id": session.metadata.get("native_session_id"),
        "last_observed_at": session.updated_at.isoformat().replace("+00:00", "Z"),
    }


def _notification_data(notification: SessionNotification) -> dict[str, Any]:
    return {
        "notification_id": notification.notification_id,
        "task_id": notification.task_id,
        "session_id": notification.session_id,
        "commit_sha": notification.commit_sha,
        "message": notification.message,
        "origin": notification.origin.model_dump(mode="json", exclude_none=True),
        "route": notification.route.value,
        "state": notification.state.value,
        "revision": notification.revision,
        "created_at": notification.created_at.isoformat().replace("+00:00", "Z"),
        "routed_at": notification.routed_at.isoformat().replace("+00:00", "Z"),
        "fallback_reason": notification.fallback_reason,
        "acknowledged_by": notification.acknowledged_by,
        "acknowledged_at": (
            notification.acknowledged_at.isoformat().replace("+00:00", "Z")
            if notification.acknowledged_at is not None
            else None
        ),
        "reply_id": notification.reply_id,
        "replied_by": notification.replied_by,
        "replied_at": (
            notification.replied_at.isoformat().replace("+00:00", "Z")
            if notification.replied_at is not None
            else None
        ),
    }
