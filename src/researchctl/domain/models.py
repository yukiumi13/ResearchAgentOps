from __future__ import annotations

from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictBool,
    StrictInt,
    TypeAdapter,
    model_validator,
)

from researchctl.constants import PROTOCOL_VERSION
from researchctl.domain.enums import (
    ArtifactVerification,
    ClaimScope,
    CodeDisposition,
    EvidenceStatus,
    FailureClass,
    InputKind,
    Priority,
    ProjectState,
    ReportApplicability,
    RetentionClass,
    ReviewDisposition,
    RunAttemptState,
    RunOutcome,
    StatusKind,
    SubmissionCategory,
    SubmissionState,
    TaskState,
)
from researchctl.domain.types import (
    DecisionId,
    GitObjectId,
    HumanKey,
    AttestationId,
    LinearUuid,
    NonEmptyStr,
    OperationId,
    ProjectId,
    ReportId,
    RepositoryPath,
    RunAttemptId,
    RunId,
    RunResultId,
    Sha256Digest,
    ShortText,
    StatusUpdateId,
    SubmissionId,
    SessionId,
    TaskId,
    UtcDateTime,
)


_REPOSITORY_PATH_ADAPTER = TypeAdapter(RepositoryPath)
_PROTECTED_WRITE_PREFIX = PurePosixPath(".research")
_MAX_REVIEW_BUNDLE_BYTES = 10 * 1024 * 1024


def _path_is_within_prefix(path: str, prefix: str) -> bool:
    if prefix == ".":
        return True
    path_parts = PurePosixPath(path).parts
    prefix_parts = PurePosixPath(prefix).parts
    return path_parts[: len(prefix_parts)] == prefix_parts


def _is_protected_write_path(path: str) -> bool:
    return _path_is_within_prefix(path, _PROTECTED_WRITE_PREFIX.as_posix())


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_default=True,
    )


class ProtocolRecord(StrictModel):
    schema_version: Literal["0.1"] = PROTOCOL_VERSION


class RepositoryIdentity(StrictModel):
    default_branch: ShortText
    remote_url: NonEmptyStr | None = None


class ProjectRecord(ProtocolRecord):
    project_id: ProjectId
    key: HumanKey
    name: ShortText
    state: ProjectState = ProjectState.BOOTSTRAPPING
    repository: RepositoryIdentity
    created_at: UtcDateTime


class AgentPolicy(StrictModel):
    accepted_paths_denied: Annotated[
        tuple[NonEmptyStr, ...],
        Field(min_length=1),
    ]
    dangerous_skip_permissions: Literal[False] = False

    @model_validator(mode="after")
    def require_accepted_state_protection(self) -> AgentPolicy:
        required = {
            ".research/decisions/**",
            ".research/policies/**",
            ".research/project.yaml",
            ".research/reports/**",
            ".research/tasks/**",
        }
        if not required.issubset(self.accepted_paths_denied):
            raise ValueError("agent policy must deny all accepted-state paths")
        return self


class ImpactPolicy(StrictModel):
    auto_advance_validity: Literal[False] = False


class SecurityPolicy(StrictModel):
    same_user_agent_isolation: Literal["best_effort"] = "best_effort"


class ExecutionDomainPolicy(StrictModel):
    execution_domain: HumanKey
    host_pools: Annotated[tuple[HumanKey, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def require_unique_host_pools(self) -> ExecutionDomainPolicy:
        if len(self.host_pools) != len(set(self.host_pools)):
            raise ValueError("execution domain host_pools must be unique")
        return self


class ProjectPolicy(ProtocolRecord):
    agent: AgentPolicy
    impact: ImpactPolicy = Field(default_factory=ImpactPolicy)
    security: SecurityPolicy = Field(default_factory=SecurityPolicy)
    execution_domains: tuple[ExecutionDomainPolicy, ...] = ()

    @model_validator(mode="after")
    def require_unique_execution_domains(self) -> ProjectPolicy:
        domains = [item.execution_domain for item in self.execution_domains]
        if len(domains) != len(set(domains)):
            raise ValueError("execution domain names must be unique")
        return self


class InputIdentity(StrictModel):
    model_config = ConfigDict(
        json_schema_extra={
            "anyOf": [
                {
                    "required": ["version"],
                    "properties": {"version": {"type": "string"}},
                },
                {
                    "required": ["digest"],
                    "properties": {"digest": {"type": "string"}},
                },
            ]
        }
    )

    kind: InputKind
    logical_id: ShortText
    version: NonEmptyStr | None = None
    digest: Sha256Digest | None = None
    uri: NonEmptyStr | None = None
    resolver: ShortText | None = None
    waiver_allowed: StrictBool = False

    @model_validator(mode="after")
    def require_version_or_digest(self) -> InputIdentity:
        if self.version is None and self.digest is None:
            raise ValueError("input identity requires version or digest")
        return self


class ExecutionPreferences(StrictModel):
    preferred_hosts: tuple[HumanKey, ...] = ()
    preferred_pools: tuple[HumanKey, ...] = ()
    gpu_count: Annotated[StrictInt, Field(ge=0, le=1024)] = 0
    gpu_type: ShortText | None = None
    min_gpu_memory_gb: Annotated[StrictInt, Field(ge=0)] | None = None


class TaskRecord(ProtocolRecord):
    task_id: TaskId
    key: HumanKey
    title: ShortText
    state: TaskState = TaskState.PLANNED
    priority: Priority = Priority.MEDIUM
    goal: NonEmptyStr
    done_when: Annotated[tuple[NonEmptyStr, ...], Field(min_length=1)]
    execution_domain: HumanKey
    allowed_write_paths: Annotated[
        tuple[RepositoryPath, ...],
        Field(min_length=1, description="Repository-relative path prefixes."),
    ]
    deliverables: Annotated[tuple[NonEmptyStr, ...], Field(min_length=1)]
    parent_task_id: TaskId | None = None
    milestone: ShortText | None = None
    constraints: tuple[NonEmptyStr, ...] = ()
    required_inputs: tuple[InputIdentity, ...] = ()
    execution: ExecutionPreferences = Field(default_factory=ExecutionPreferences)
    waiting_on: ShortText | None = None
    next_decision: NonEmptyStr | None = None
    created_at: UtcDateTime
    updated_at: UtcDateTime
    linear_issue_id: LinearUuid | None = None

    @model_validator(mode="after")
    def require_valid_timestamps(self) -> TaskRecord:
        if self.updated_at < self.created_at:
            raise ValueError("task updated_at cannot precede created_at")
        if len(self.allowed_write_paths) != len(set(self.allowed_write_paths)):
            raise ValueError("task allowed_write_paths must be unique")
        if any(
            any(character in prefix for character in "*?[]")
            for prefix in self.allowed_write_paths
        ):
            raise ValueError("task allowed_write_paths cannot contain glob syntax")
        if any(
            _is_protected_write_path(path) for path in self.allowed_write_paths
        ):
            raise ValueError("task cannot declare .research as an allowed write prefix")
        if self.parent_task_id == self.task_id:
            raise ValueError("task cannot be its own parent")
        return self

    def permits_write_path(self, path: str) -> bool:
        normalized = _REPOSITORY_PATH_ADAPTER.validate_python(path)
        if _is_protected_write_path(normalized):
            return False
        return any(
            _path_is_within_prefix(normalized, prefix)
            for prefix in self.allowed_write_paths
        )


class ResourceRequirement(StrictModel):
    gpu_count: Annotated[StrictInt, Field(ge=0, le=1024)] = 0
    gpu_type: ShortText | None = None
    min_gpu_memory_gb: Annotated[StrictInt, Field(ge=0)] | None = None
    preferred_hosts: tuple[HumanKey, ...] = ()
    preferred_pools: tuple[HumanKey, ...] = ()


class ArtifactDeclaration(StrictModel):
    name: HumanKey
    path: RepositoryPath
    media_type: ShortText
    required: StrictBool = True


class ArtifactRef(StrictModel):
    name: HumanKey
    uri: NonEmptyStr
    digest: Sha256Digest
    size_bytes: Annotated[StrictInt, Field(ge=0)]
    media_type: ShortText
    producer_host: HumanKey | None = None
    retention: RetentionClass = RetentionClass.TASK
    verification: ArtifactVerification = ArtifactVerification.DECLARED


class RunSpec(ProtocolRecord):
    run_id: RunId
    task_id: TaskId
    session_id: SessionId
    operation_id: OperationId
    source_commit: GitObjectId
    source_tree: GitObjectId
    baseline_commit: GitObjectId | None = None
    argv: Annotated[tuple[NonEmptyStr, ...], Field(min_length=1)]
    working_directory: RepositoryPath = "."
    environment: InputIdentity
    config: InputIdentity | None = None
    inputs: tuple[InputIdentity, ...] = ()
    resources: ResourceRequirement = Field(default_factory=ResourceRequirement)
    requested_host: HumanKey | None = None
    artifact_declarations: tuple[ArtifactDeclaration, ...] = ()
    created_at: UtcDateTime
    spec_digest: Sha256Digest

    @model_validator(mode="after")
    def require_unique_declarations(self) -> RunSpec:
        identities = [self.environment]
        if self.config is not None:
            identities.append(self.config)
        identities.extend(self.inputs)
        identity_keys = [
            (identity.kind, identity.logical_id) for identity in identities
        ]
        if len(identity_keys) != len(set(identity_keys)):
            raise ValueError("run identity keys must be unique")

        artifact_names = [item.name for item in self.artifact_declarations]
        if len(artifact_names) != len(set(artifact_names)):
            raise ValueError("run artifact names must be unique")
        artifact_paths = [item.path for item in self.artifact_declarations]
        if len(artifact_paths) != len(set(artifact_paths)):
            raise ValueError("run artifact paths must be unique")
        return self

    @model_validator(mode="after")
    def require_canonical_spec_digest(self) -> RunSpec:
        from researchctl.serialization import canonical_digest

        content = self.model_dump(
            mode="json",
            exclude={"spec_digest"},
            exclude_none=True,
        )
        if self.spec_digest != canonical_digest(content):
            raise ValueError("run spec_digest does not match canonical RunSpec content")
        return self


class RunAttemptEvent(StrictModel):
    operation_id: OperationId
    sequence: Annotated[StrictInt, Field(ge=0)]
    state: RunAttemptState
    observed_at: UtcDateTime
    idempotency_key: NonEmptyStr
    host: HumanKey | None = None
    external_ids: dict[str, NonEmptyStr] = Field(default_factory=dict)
    error_code: ShortText | None = None
    detail: NonEmptyStr | None = None


class RunAttempt(ProtocolRecord):
    attempt_id: RunAttemptId
    run_id: RunId
    operation_id: OperationId
    retry_of: RunAttemptId | None = None
    events: Annotated[tuple[RunAttemptEvent, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def require_monotonic_sequences(self) -> RunAttempt:
        sequences = [event.sequence for event in self.events]
        if sequences != sorted(sequences) or len(sequences) != len(set(sequences)):
            raise ValueError("attempt event sequences must be strictly increasing")
        if any(event.operation_id != self.operation_id for event in self.events):
            raise ValueError("attempt event operation_id must match its parent attempt")
        return self


class RunResult(ProtocolRecord):
    result_id: RunResultId
    run_id: RunId
    run_spec_digest: Sha256Digest
    attempt_ids: Annotated[tuple[RunAttemptId, ...], Field(min_length=1)]
    outcome: RunOutcome
    started_at: UtcDateTime | None = None
    finished_at: UtcDateTime
    host: HumanKey | None = None
    gpu_uuids: tuple[ShortText, ...] = ()
    exit_code: StrictInt | None = None
    failure_class: FailureClass | None = None
    metrics: dict[str, JsonValue] = Field(default_factory=dict)
    artifacts: tuple[ArtifactRef, ...] = ()
    log_summary: NonEmptyStr | None = None

    @model_validator(mode="after")
    def validate_outcome_fields(self) -> RunResult:
        if len(self.attempt_ids) != len(set(self.attempt_ids)):
            raise ValueError("run result attempt_ids must be unique")
        if self.started_at is not None and self.finished_at < self.started_at:
            raise ValueError("run result finished_at cannot precede started_at")
        if self.outcome == RunOutcome.COMPLETE and self.started_at is None:
            raise ValueError("complete result requires started_at")
        if self.outcome == RunOutcome.COMPLETE and self.exit_code != 0:
            raise ValueError("complete result requires exit_code 0")
        if (
            self.outcome in {RunOutcome.FAILED, RunOutcome.LOST}
            and self.failure_class is None
        ):
            raise ValueError("failed or lost result requires failure_class")
        return self


class DependencySet(StrictModel):
    paths: tuple[RepositoryPath, ...] = ()
    resources: tuple[ShortText, ...] = ()
    environments: tuple[ShortText, ...] = ()


class DecisionRequest(StrictModel):
    question: NonEmptyStr
    options: Annotated[tuple[ShortText, ...], Field(min_length=1)]


class ResearchSubmission(ProtocolRecord):
    submission_id: SubmissionId
    task_id: TaskId
    session_id: SessionId
    state: SubmissionState = SubmissionState.DRAFT
    category: SubmissionCategory
    claim: NonEmptyStr
    run_result_ids: Annotated[tuple[RunResultId, ...], Field(min_length=1)]
    metrics: dict[str, JsonValue] = Field(default_factory=dict)
    dependencies: DependencySet = Field(default_factory=DependencySet)
    limitations: tuple[NonEmptyStr, ...] = ()
    decision_needed: tuple[DecisionRequest, ...] = ()
    review_bundle: tuple[ArtifactRef, ...] = ()
    created_at: UtcDateTime

    @model_validator(mode="after")
    def require_bounded_review_bundle(self) -> ResearchSubmission:
        if len(self.run_result_ids) != len(set(self.run_result_ids)):
            raise ValueError("research submission run_result_ids must be unique")
        total_size = sum(artifact.size_bytes for artifact in self.review_bundle)
        if total_size > _MAX_REVIEW_BUNDLE_BYTES:
            raise ValueError("research submission review_bundle exceeds 10 MiB")
        return self


class ReportProposal(ProtocolRecord):
    submission_id: SubmissionId
    report_id: ReportId
    expected_report_revision: Annotated[StrictInt, Field(ge=0)]
    title: ShortText
    evidence_tree: GitObjectId
    supersedes: ReportId | None = None

    @model_validator(mode="after")
    def reject_self_supersession(self) -> ReportProposal:
        if self.supersedes == self.report_id:
            raise ValueError("report proposal cannot supersede itself")
        return self


class ReviewDecision(ProtocolRecord):
    decision_id: DecisionId
    submission_id: SubmissionId
    disposition: Literal[
        ReviewDisposition.ACCEPTED,
        ReviewDisposition.ACCEPTED_WITH_CONDITIONS,
    ]
    reviewer_actor: ShortText
    decided_at: UtcDateTime
    conditions: tuple[NonEmptyStr, ...] = ()
    claim_scope: ClaimScope
    code_disposition: CodeDisposition
    report_id: ReportId
    expected_report_revision: Annotated[StrictInt, Field(ge=0)]
    accepted_submission_digest: Sha256Digest

    @model_validator(mode="after")
    def require_disposition_conditions(self) -> ReviewDecision:
        if (
            self.disposition == ReviewDisposition.ACCEPTED_WITH_CONDITIONS
            and not self.conditions
        ):
            raise ValueError("accepted_with_conditions requires conditions")
        if self.disposition == ReviewDisposition.ACCEPTED and self.conditions:
            raise ValueError("accepted decision cannot include conditions")
        return self


class ValidationBasis(StrictModel):
    main_tree: GitObjectId
    assessed_at: UtcDateTime


class ReportRecord(ProtocolRecord):
    report_id: ReportId
    revision: Annotated[StrictInt, Field(ge=1)]
    title: ShortText
    claim: NonEmptyStr
    claim_scope: ClaimScope
    evidence_status: EvidenceStatus
    applicability: ReportApplicability
    submission_id: SubmissionId
    run_result_ids: Annotated[tuple[RunResultId, ...], Field(min_length=1)]
    evidence_tree: GitObjectId
    accepted_at_main_tree: GitObjectId
    validation_basis: ValidationBasis | None = None
    dependencies: DependencySet = Field(default_factory=DependencySet)
    supersedes: ReportId | None = None

    @model_validator(mode="after")
    def require_scope_consistency(self) -> ReportRecord:
        if self.claim_scope == ClaimScope.BASELINE and self.validation_basis is None:
            raise ValueError("baseline report requires validation_basis")
        if (
            self.claim_scope == ClaimScope.SNAPSHOT
            and self.applicability != ReportApplicability.SNAPSHOT_ONLY
        ):
            raise ValueError("snapshot report applicability must be snapshot_only")
        return self


class LinearProjectionPolicy(ProtocolRecord):
    workspace_id: LinearUuid
    team_id: LinearUuid
    project_id: LinearUuid | None = None
    notification_author_ids: tuple[LinearUuid, ...] = ()
    renderer_id: Literal["linear.accepted-result.v1"] = "linear.accepted-result.v1"
    renderer_version: Literal[1] = 1

    @model_validator(mode="after")
    def require_unique_notification_authors(self) -> LinearProjectionPolicy:
        if len(set(self.notification_author_ids)) != len(
            self.notification_author_ids
        ):
            raise ValueError("notification_author_ids must not contain duplicates")
        return self


class LinearProjectionDisabled(StrictModel):
    state: Literal["disabled"] = "disabled"
    reason: Literal[
        "integration_not_configured",
        "report_not_acceptance_prepared",
    ]


class LinearProjectionConfigured(StrictModel):
    state: Literal["configured"] = "configured"
    workspace_id: LinearUuid
    team_id: LinearUuid
    project_id: LinearUuid | None = None
    issue_id: LinearUuid
    renderer_id: Literal["linear.accepted-result.v1"]
    renderer_version: Literal[1]
    payload_digest: Sha256Digest


LinearProjectionPreview = Annotated[
    LinearProjectionDisabled | LinearProjectionConfigured,
    Field(discriminator="state"),
]


class CIValidationCheck(StrictModel):
    name: HumanKey
    status: Literal["passed", "failed"]
    evidence_digest: Sha256Digest | None = None


class GeneratedOutputDigest(StrictModel):
    path: RepositoryPath
    digest: Sha256Digest
    size_bytes: Annotated[StrictInt, Field(ge=0)]


class CIValidationAttestation(ProtocolRecord):
    attestation_id: AttestationId
    project_id: ProjectId
    task_id: TaskId
    submission_id: SubmissionId
    repository: ShortText
    pull_request_number: Annotated[StrictInt, Field(ge=1)]
    subject_head: GitObjectId
    subject_tree: GitObjectId
    base_commit: GitObjectId
    validator_id: Literal["researchctl.ci.v1"] = "researchctl.ci.v1"
    validator_version: ShortText
    schema_manifest_digest: Sha256Digest
    workflow_id: ShortText
    check_identity: ShortText
    checks: Annotated[tuple[CIValidationCheck, ...], Field(min_length=1)]
    generated_outputs: Annotated[
        tuple[GeneratedOutputDigest, ...],
        Field(min_length=1),
    ]
    submission_digest: Sha256Digest
    report_proposal_digest: Sha256Digest
    decision_digest: Sha256Digest | None = None
    report_id: ReportId | None = None
    report_revision: Annotated[StrictInt, Field(ge=1)] | None = None
    report_digest: Sha256Digest | None = None
    report_preview_digest: Sha256Digest
    report_renderer_id: Literal["research-report.v1"] = "research-report.v1"
    report_renderer_version: Literal[1] = 1
    projection: LinearProjectionPreview
    generated_at: UtcDateTime
    artifact_digest: Sha256Digest
    overall_result: Literal["passed", "failed"]

    @model_validator(mode="after")
    def require_closed_exact_head_attestation(self) -> CIValidationAttestation:
        check_names = [item.name for item in self.checks]
        if check_names != sorted(check_names) or len(check_names) != len(
            set(check_names)
        ):
            raise ValueError("CI checks must be unique and sorted by name")
        output_paths = [item.path for item in self.generated_outputs]
        if output_paths != sorted(output_paths) or len(output_paths) != len(
            set(output_paths)
        ):
            raise ValueError("generated outputs must be unique and sorted by path")
        report_fields = (
            self.decision_digest,
            self.report_id,
            self.report_revision,
            self.report_digest,
        )
        if any(value is not None for value in report_fields) and not all(
            value is not None for value in report_fields
        ):
            raise ValueError("accepted Report attestation fields are all-or-none")
        expected_result = (
            "passed"
            if all(item.status == "passed" for item in self.checks)
            else "failed"
        )
        if self.overall_result != expected_result:
            raise ValueError("overall_result must match named CI checks")
        from researchctl.serialization import canonical_digest

        expected_artifact_digest = canonical_digest(
            {
                "generated_outputs": [
                    item.model_dump(mode="json") for item in self.generated_outputs
                ]
            }
        )
        if self.artifact_digest != expected_artifact_digest:
            raise ValueError("artifact_digest does not match generated output manifest")
        return self


class StatusEvidence(StrictModel):
    kind: HumanKey
    value: NonEmptyStr


class SessionNotificationSourceMarker(StrictModel):
    """Structured binding recovered from an RCP-owned hidden transport marker."""

    agent_id: ShortText
    task_id: TaskId
    session_id: SessionId
    report_id: ReportId | None = None
    marker_digest: Sha256Digest


class SessionNotificationOrigin(StrictModel):
    """Address needed to return one reply to its originating Linear thread."""

    transport: Literal["linear"] = "linear"
    workspace_id: LinearUuid
    issue_id: LinearUuid
    thread_id: LinearUuid
    comment_id: LinearUuid
    source_marker: SessionNotificationSourceMarker | None = None


class StatusUpdate(ProtocolRecord):
    update_id: StatusUpdateId
    task_id: TaskId
    session_id: SessionId
    status: StatusKind
    summary: NonEmptyStr
    observed_at: UtcDateTime
    evidence: tuple[StatusEvidence, ...] = ()
    blocker_category: ShortText | None = None
    blocker_detail: NonEmptyStr | None = None
    decision_needed: DecisionRequest | None = None
    suggested_next_action: NonEmptyStr | None = None

    @model_validator(mode="after")
    def require_structured_attention_details(self) -> StatusUpdate:
        has_category = self.blocker_category is not None
        has_detail = self.blocker_detail is not None
        if has_category != has_detail:
            raise ValueError("blocker_category and blocker_detail must be supplied together")
        if self.status == StatusKind.BLOCKED and not has_category:
            raise ValueError("blocked status requires blocker_category and blocker_detail")
        if self.status != StatusKind.BLOCKED and has_category:
            raise ValueError("blocker fields are only valid for blocked status")
        if self.status == StatusKind.NEEDS_INPUT and self.decision_needed is None:
            raise ValueError("needs_input status requires decision_needed")
        return self


ProtocolModel = (
    ProjectRecord
    | TaskRecord
    | RunSpec
    | RunAttempt
    | RunResult
    | ResearchSubmission
    | ReportProposal
    | ReviewDecision
    | ProjectPolicy
    | ReportRecord
    | LinearProjectionPolicy
    | CIValidationAttestation
    | StatusUpdate
)
