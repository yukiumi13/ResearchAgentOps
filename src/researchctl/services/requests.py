from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, StrictBool, StrictInt, field_validator, model_validator

from researchctl.domain.enums import (
    ClaimScope,
    CodeDisposition,
    ImpactDisposition,
    ReviewDisposition,
    SessionState,
)
from researchctl.domain.models import (
    DependencySet,
    LinearProjectionPolicy,
    ReportProposal,
    ResearchSubmission,
    RunSpec,
    SessionNotificationOrigin,
    StatusUpdate,
    StrictModel,
    TaskRecord,
)
from researchctl.domain.types import (
    BootstrapId,
    DecisionId,
    GitObjectId,
    HumanKey,
    ImpactId,
    NonEmptyStr,
    NotificationId,
    NotificationReplyId,
    OperationId,
    ReportId,
    RunAttemptId,
    RunId,
    SubmissionId,
    SessionId,
    Sha256Digest,
    ShortText,
    StatusUpdateId,
    TaskId,
    UtcDateTime,
)
from researchctl.serialization import canonical_digest


class AgentKind(StrEnum):
    CODEX = "codex"
    CLAUDE = "claude"


class MutationRequest(StrictModel):
    operation_id: OperationId
    idempotency_key: NonEmptyStr


class BootstrapAcceptRequest(MutationRequest):
    bootstrap_id: BootstrapId
    proposal_commit: GitObjectId


class BootstrapProposalRequest(MutationRequest):
    bootstrap_id: BootstrapId
    expected_default_head: GitObjectId


class LinearConfigureRequest(MutationRequest):
    expected_default_head: GitObjectId
    policy: LinearProjectionPolicy


LinearDeliveryTopic = Literal[
    "linear.accepted-result.v1",
    "linear.session-reply.v1",
]
LinearDeliveryState = Literal["pending", "delivered", "dead_letter"]


class LinearDeliveryListRequest(StrictModel):
    topic: LinearDeliveryTopic | None = None
    state: LinearDeliveryState | None = None
    limit: Annotated[StrictInt, Field(ge=1, le=1000)] = 100


class LinearDeliveryShowRequest(StrictModel):
    topic: LinearDeliveryTopic
    outbox_id: NonEmptyStr


class TaskCreateRequest(MutationRequest):
    task: TaskRecord


class TaskUpdateRequest(MutationRequest):
    task_id: TaskId
    expected_digest: Sha256Digest
    replacement: TaskRecord


class TaskCancelRequest(MutationRequest):
    task_id: TaskId
    expected_digest: Sha256Digest
    reason: NonEmptyStr
    updated_at: UtcDateTime


class SessionStartRequest(MutationRequest):
    session_id: SessionId
    task_id: TaskId
    base_commit: GitObjectId
    host: HumanKey
    agent: AgentKind
    prompt: NonEmptyStr


class SessionPauseRequest(MutationRequest):
    session_id: SessionId
    mode: Literal["idle", "stop"] = "idle"


class SessionAttachRequest(StrictModel):
    session_id: SessionId


class SessionListRequest(StrictModel):
    task_id: TaskId | None = None
    state: SessionState | None = None
    limit: Annotated[StrictInt, Field(ge=1, le=1000)] = 100


class SessionShowRequest(StrictModel):
    session_id: SessionId


class SessionAddressRequest(StrictModel):
    session_id: SessionId
    commit_sha: GitObjectId
    app: HumanKey = "researchctl-app"


class SessionContinueRequest(MutationRequest):
    source_session_id: SessionId
    new_session_id: SessionId
    target_host: HumanKey
    prompt: NonEmptyStr


class NotificationSendRequest(MutationRequest):
    notification_id: NotificationId
    directive_kind: Literal["notify", "reply"] = "notify"
    task_id: TaskId
    session_id: SessionId
    commit_sha: GitObjectId
    message: NonEmptyStr
    origin: SessionNotificationOrigin

    @field_validator("message", mode="before")
    @classmethod
    def normalize_message_newlines(cls, value: object) -> object:
        if isinstance(value, str):
            return value.replace("\r\n", "\n").replace("\r", "\n")
        return value

    @model_validator(mode="after")
    def bind_source_marker(self) -> NotificationSendRequest:
        marker = self.origin.source_marker
        if (self.directive_kind == "reply") != (marker is not None):
            raise ValueError(
                "reply directives require a source marker and notify directives "
                "forbid one"
            )
        if marker is not None and (
            marker.task_id != self.task_id or marker.session_id != self.session_id
        ):
            raise ValueError("source marker must bind the requested Task and Session")
        return self


def linear_notification_request_digest(request: NotificationSendRequest) -> str:
    """Bind one verified Linear comment to one canonical notification request."""

    marker = request.origin.source_marker
    return canonical_digest(
        {
            "kind": "linear.notification-request.v1",
            "directive_kind": request.directive_kind,
            "notification_id": request.notification_id,
            "task_id": request.task_id,
            "session_id": request.session_id,
            "commit_sha": request.commit_sha,
            "message": request.message,
            "origin": {
                "transport": request.origin.transport,
                "workspace_id": request.origin.workspace_id,
                "issue_id": request.origin.issue_id,
                "thread_id": request.origin.thread_id,
                "comment_id": request.origin.comment_id,
            },
            "source_marker": (
                marker.model_dump(mode="json", exclude_none=True)
                if marker is not None
                else None
            ),
        }
    )


class NotificationListRequest(StrictModel):
    session_id: SessionId | None = None
    manager_exceptions_only: StrictBool = False
    include_closed: StrictBool = False
    limit: Annotated[StrictInt, Field(ge=1, le=1000)] = 100


class NotificationActionRequest(MutationRequest):
    notification_id: NotificationId
    expected_revision: Annotated[StrictInt, Field(ge=1)]


class NotificationAckRequest(NotificationActionRequest):
    pass


class NotificationReplyRequest(NotificationActionRequest):
    reply_id: NotificationReplyId
    body: NonEmptyStr


class RunStartRequest(MutationRequest):
    spec: RunSpec
    attempt_id: RunAttemptId
    assigned_gpu_uuids: tuple[ShortText, ...] = ()

    @model_validator(mode="after")
    def bind_start_operation_and_gpus(self) -> RunStartRequest:
        if self.spec.operation_id != self.operation_id:
            raise ValueError("run start operation_id must match RunSpec operation_id")
        if len(self.assigned_gpu_uuids) != len(set(self.assigned_gpu_uuids)):
            raise ValueError("assigned_gpu_uuids must be unique")
        return self


class RunRetryRequest(MutationRequest):
    spec: RunSpec
    attempt_id: RunAttemptId
    retry_of: RunAttemptId
    assigned_gpu_uuids: tuple[ShortText, ...] = ()

    @model_validator(mode="after")
    def bind_retry_identity_and_gpus(self) -> RunRetryRequest:
        if self.attempt_id == self.retry_of:
            raise ValueError("run retry requires a new Attempt ID")
        if self.operation_id == self.spec.operation_id:
            raise ValueError("run retry requires a new Operation ID")
        if len(self.assigned_gpu_uuids) != len(set(self.assigned_gpu_uuids)):
            raise ValueError("assigned_gpu_uuids must be unique")
        return self


class RunCollectRequest(MutationRequest):
    spec: RunSpec
    attempt_id: RunAttemptId

    @model_validator(mode="after")
    def require_new_collection_operation(self) -> RunCollectRequest:
        if self.operation_id == self.spec.operation_id:
            raise ValueError("run collect requires a new Operation ID")
        return self


class SubmissionCreateRequest(MutationRequest):
    base_commit: GitObjectId
    submission: ResearchSubmission
    report_proposal: ReportProposal
    run_ids: Annotated[tuple[RunId, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def bind_submission_proposal_and_runs(self) -> SubmissionCreateRequest:
        if self.report_proposal.submission_id != self.submission.submission_id:
            raise ValueError("ReportProposal must bind the ResearchSubmission")
        if len(self.run_ids) != len(set(self.run_ids)):
            raise ValueError("Submission run_ids must be unique")
        if len(self.run_ids) != len(self.submission.run_result_ids):
            raise ValueError("Submission run_ids and run_result_ids must have equal length")
        return self


class ImpactCreateRequest(MutationRequest):
    impact_id: ImpactId
    report_id: ReportId
    expected_report_revision: Annotated[StrictInt, Field(ge=1)]
    target_commit: GitObjectId


class ImpactBatchCreateRequest(MutationRequest):
    impact_id: ImpactId
    before_commit: GitObjectId
    target_commit: GitObjectId
    generated_at: UtcDateTime

    @model_validator(mode="after")
    def require_nonempty_trigger_range(self) -> ImpactBatchCreateRequest:
        if self.before_commit == self.target_commit:
            raise ValueError("Impact batch before and target commits must differ")
        return self


class ReportStatusRequest(StrictModel):
    report_id: ReportId
    target_commit: GitObjectId | None = None


class ImpactDecisionCreateRequest(MutationRequest):
    decision_id: DecisionId
    impact_id: ImpactId
    report_id: ReportId
    expected_report_revision: Annotated[StrictInt, Field(ge=1)]
    expected_impact_digest: Sha256Digest
    target_commit: GitObjectId
    disposition: ImpactDisposition
    reason: NonEmptyStr
    rerun_task_id: TaskId | None = None
    replacement_dependencies: DependencySet | None = None

    @model_validator(mode="after")
    def require_disposition_inputs(self) -> ImpactDecisionCreateRequest:
        if (self.disposition is ImpactDisposition.RERUN) != (
            self.rerun_task_id is not None
        ):
            raise ValueError("rerun disposition requires only rerun_task_id")
        if (self.disposition is ImpactDisposition.DEPENDENCY_FIX) != (
            self.replacement_dependencies is not None
        ):
            raise ValueError(
                "dependency_fix disposition requires replacement_dependencies"
            )
        return self


class ReviewAcceptRequest(MutationRequest):
    submission_id: SubmissionId
    task_id: TaskId
    expected_head: GitObjectId
    decision_id: DecisionId
    expected_report_revision: Annotated[StrictInt, Field(ge=0)]
    disposition: Literal[
        ReviewDisposition.ACCEPTED,
        ReviewDisposition.ACCEPTED_WITH_CONDITIONS,
    ]
    conditions: tuple[NonEmptyStr, ...] = ()
    claim_scope: ClaimScope
    code_disposition: CodeDisposition

    @model_validator(mode="after")
    def require_disposition_conditions(self) -> ReviewAcceptRequest:
        if (
            self.disposition is ReviewDisposition.ACCEPTED_WITH_CONDITIONS
            and not self.conditions
        ):
            raise ValueError("accepted_with_conditions requires conditions")
        if self.disposition is ReviewDisposition.ACCEPTED and self.conditions:
            raise ValueError("accepted review cannot include conditions")
        return self


class StatusPublishRequest(MutationRequest):
    update: StatusUpdate


class InboxListRequest(StrictModel):
    include_resolved: StrictBool = False
    limit: Annotated[StrictInt, Field(ge=1, le=1000)] = 100
    now: UtcDateTime | None = None


class InboxActionRequest(MutationRequest):
    update_id: StatusUpdateId
    expected_generation: Annotated[StrictInt, Field(ge=1)]


class InboxAckRequest(InboxActionRequest):
    pass


class InboxSnoozeRequest(InboxActionRequest):
    until: UtcDateTime


class InboxResolveRequest(InboxActionRequest):
    reason: NonEmptyStr
