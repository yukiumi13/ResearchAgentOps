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
    ImpactDisposition,
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
    DependencyReceiptId,
    DecisionId,
    GitObjectId,
    HumanKey,
    ImpactId,
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
            ".research/impacts/**",
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

    @model_validator(mode="after")
    def require_canonical_dependencies(self) -> DependencySet:
        for label, values in (
            ("paths", self.paths),
            ("resources", self.resources),
            ("environments", self.environments),
        ):
            if tuple(sorted(values)) != values or len(values) != len(set(values)):
                raise ValueError(
                    f"dependency {label} must be unique and sorted"
                )
        for path in self.paths:
            has_pattern_syntax = any(character in path for character in "*?[]")
            if has_pattern_syntax and not (
                path.endswith("/**")
                and "*" not in path[:-3]
                and "?" not in path
                and "[" not in path
                and "]" not in path
            ):
                raise ValueError(
                    "dependency paths support only exact paths or a trailing /**"
                )
            if path == ".research" or path.startswith(".research/"):
                raise ValueError(
                    "dependency paths cannot target research control records"
                )
        return self


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


class ObservedDependencyIdentity(StrictModel):
    version: NonEmptyStr | None = None
    digest: Sha256Digest | None = None

    @model_validator(mode="after")
    def require_version_or_digest(self) -> ObservedDependencyIdentity:
        if self.version is None and self.digest is None:
            raise ValueError(
                "observed dependency identity requires version or digest"
            )
        return self


class DependencyChangeObservation(StrictModel):
    kind: Literal["resource", "environment"]
    dependency: ShortText
    state: Literal["changed", "unchanged", "unknown"]
    basis_identity: ObservedDependencyIdentity | None = None
    target_identity: ObservedDependencyIdentity | None = None
    evidence_digest: Sha256Digest | None = None
    reason: NonEmptyStr | None = None

    @model_validator(mode="after")
    def require_state_evidence(self) -> DependencyChangeObservation:
        if self.state == "unknown":
            if self.reason is None:
                raise ValueError("unknown dependency observation requires reason")
            return self
        if self.reason is not None:
            raise ValueError("known dependency observation cannot include reason")
        if (
            self.basis_identity is None
            or self.target_identity is None
            or self.evidence_digest is None
        ):
            raise ValueError(
                "known dependency observation requires both identities and evidence"
            )
        identities_match = self.basis_identity == self.target_identity
        if (self.state == "unchanged") != identities_match:
            raise ValueError(
                "dependency observation state must match its identities"
            )
        return self


class DependencyChangeReceipt(ProtocolRecord):
    receipt_id: DependencyReceiptId
    provider_id: HumanKey
    provider_version: ShortText
    basis_tree: GitObjectId
    target_commit: GitObjectId
    target_tree: GitObjectId
    observations: Annotated[
        tuple[DependencyChangeObservation, ...],
        Field(min_length=1, max_length=4096),
    ]
    provider_query_digest: Sha256Digest
    query_digest: Sha256Digest
    observed_at: UtcDateTime
    receipt_digest: Sha256Digest

    @staticmethod
    def calculate_query_digest(
        *,
        provider_id: str,
        provider_version: str,
        basis_tree: str,
        target_commit: str,
        target_tree: str,
        provider_query_digest: str,
        dependencies: tuple[tuple[str, str], ...],
    ) -> str:
        from researchctl.serialization import canonical_digest

        return canonical_digest(
            {
                "schema_version": PROTOCOL_VERSION,
                "provider_id": provider_id,
                "provider_version": provider_version,
                "basis_tree": basis_tree,
                "target_commit": target_commit,
                "target_tree": target_tree,
                "provider_query_digest": provider_query_digest,
                "dependencies": [
                    {"kind": kind, "dependency": dependency}
                    for kind, dependency in dependencies
                ],
            }
        )

    @model_validator(mode="after")
    def require_canonical_receipt(self) -> DependencyChangeReceipt:
        dependency_keys = tuple(
            (item.kind, item.dependency) for item in self.observations
        )
        if dependency_keys != tuple(sorted(dependency_keys)) or len(
            dependency_keys
        ) != len(set(dependency_keys)):
            raise ValueError(
                "dependency receipt observations must be unique and sorted"
            )
        expected_query_digest = self.calculate_query_digest(
            provider_id=self.provider_id,
            provider_version=self.provider_version,
            basis_tree=self.basis_tree,
            target_commit=self.target_commit,
            target_tree=self.target_tree,
            provider_query_digest=self.provider_query_digest,
            dependencies=dependency_keys,
        )
        if self.query_digest != expected_query_digest:
            raise ValueError("dependency receipt query_digest does not match query")
        from researchctl.serialization import canonical_digest

        content = self.model_dump(
            mode="json",
            exclude={"receipt_digest"},
            exclude_none=True,
        )
        if self.receipt_digest != canonical_digest(content):
            raise ValueError(
                "receipt_digest does not match canonical dependency receipt"
            )
        return self


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


class ReportImpact(ProtocolRecord):
    impact_id: ImpactId
    report_id: ReportId
    expected_report_revision: Annotated[StrictInt, Field(ge=1)]
    change_provider_id: HumanKey
    dependency_evaluator_id: HumanKey
    basis_tree: GitObjectId
    target_commit: GitObjectId
    target_tree: GitObjectId
    changed_paths: tuple[RepositoryPath, ...] = ()
    dependency_receipts: Annotated[
        tuple[DependencyChangeReceipt, ...],
        Field(max_length=64),
    ] = ()
    matched_path_dependencies: tuple[RepositoryPath, ...] = ()
    matched_resource_dependencies: tuple[ShortText, ...] = ()
    matched_environment_dependencies: tuple[ShortText, ...] = ()
    unresolved_resource_dependencies: tuple[ShortText, ...] = ()
    unresolved_environment_dependencies: tuple[ShortText, ...] = ()
    outcome: Literal["overlap", "no_overlap"]
    proposed_applicability: Literal[
        ReportApplicability.CURRENT,
        ReportApplicability.STALE,
    ]
    proposed_report_digest: Sha256Digest
    generated_at: UtcDateTime
    impact_digest: Sha256Digest

    @model_validator(mode="after")
    def require_canonical_impact(self) -> ReportImpact:
        for label, values in (
            ("changed_paths", self.changed_paths),
            ("matched_path_dependencies", self.matched_path_dependencies),
            ("matched_resource_dependencies", self.matched_resource_dependencies),
            (
                "matched_environment_dependencies",
                self.matched_environment_dependencies,
            ),
            (
                "unresolved_resource_dependencies",
                self.unresolved_resource_dependencies,
            ),
            (
                "unresolved_environment_dependencies",
                self.unresolved_environment_dependencies,
            ),
        ):
            if tuple(sorted(values)) != values or len(values) != len(set(values)):
                raise ValueError(f"impact {label} must be unique and sorted")
        receipt_ids = tuple(item.receipt_id for item in self.dependency_receipts)
        if receipt_ids != tuple(sorted(receipt_ids)) or len(receipt_ids) != len(
            set(receipt_ids)
        ):
            raise ValueError("dependency receipts must be unique and sorted by ID")
        for receipt in self.dependency_receipts:
            if (
                receipt.basis_tree != self.basis_tree
                or receipt.target_commit != self.target_commit
                or receipt.target_tree != self.target_tree
            ):
                raise ValueError(
                    "dependency receipt target identity does not match Impact"
                )
        observed_states = [
            ((observation.kind, observation.dependency), observation.state)
            for receipt in self.dependency_receipts
            for observation in receipt.observations
        ]
        observed_keys = [key for key, _ in observed_states]
        if len(observed_keys) != len(set(observed_keys)):
            raise ValueError(
                "dependency receipts contain duplicate observations"
            )
        observed_changed = {
            key for key, state in observed_states if state == "changed"
        }
        expected_changed = {
            ("resource", value)
            for value in self.matched_resource_dependencies
        } | {
            ("environment", value)
            for value in self.matched_environment_dependencies
        }
        if observed_changed != expected_changed:
            raise ValueError(
                "matched external dependencies must equal changed receipt evidence"
            )
        if set(self.matched_resource_dependencies) & set(
            self.unresolved_resource_dependencies
        ) or set(self.matched_environment_dependencies) & set(
            self.unresolved_environment_dependencies
        ):
            raise ValueError("matched and unresolved dependencies must be disjoint")
        unresolved_external = {
            ("resource", value)
            for value in self.unresolved_resource_dependencies
        } | {
            ("environment", value)
            for value in self.unresolved_environment_dependencies
        }
        for key, state in observed_states:
            if (state == "unknown") != (key in unresolved_external):
                raise ValueError(
                    "receipt observation state must match unresolved dependencies"
                )
        has_overlap = bool(
            self.matched_path_dependencies
            or self.matched_resource_dependencies
            or self.matched_environment_dependencies
        )
        if (self.outcome == "overlap") != has_overlap:
            raise ValueError("impact outcome must match dependency overlap")
        if not has_overlap and (
            self.unresolved_resource_dependencies
            or self.unresolved_environment_dependencies
        ):
            raise ValueError("no-overlap Impact cannot contain unresolved dependencies")
        expected_applicability = (
            ReportApplicability.STALE
            if has_overlap
            else ReportApplicability.CURRENT
        )
        if self.proposed_applicability is not expected_applicability:
            raise ValueError(
                "proposed applicability must match dependency overlap"
            )
        from researchctl.serialization import canonical_digest

        content = self.model_dump(
            mode="json",
            exclude={"impact_digest"},
            exclude_none=True,
        )
        if self.impact_digest != canonical_digest(content):
            raise ValueError(
                "impact_digest does not match canonical ReportImpact content"
            )
        return self


class ReportImpactBatch(ProtocolRecord):
    impact_id: ImpactId
    before_commit: GitObjectId
    target_commit: GitObjectId
    target_tree: GitObjectId
    impacts: Annotated[tuple[ReportImpact, ...], Field(min_length=1)]
    snapshot_report_ids: tuple[ReportId, ...] = ()
    ineligible_report_ids: tuple[ReportId, ...] = ()
    up_to_date_report_ids: tuple[ReportId, ...] = ()
    no_code_change_report_ids: tuple[ReportId, ...] = ()
    unresolved_report_ids: tuple[ReportId, ...] = ()
    generated_at: UtcDateTime
    batch_digest: Sha256Digest

    @model_validator(mode="after")
    def require_canonical_batch(self) -> ReportImpactBatch:
        report_ids = tuple(item.report_id for item in self.impacts)
        if report_ids != tuple(sorted(report_ids)) or len(report_ids) != len(
            set(report_ids)
        ):
            raise ValueError("batch impacts must be unique and sorted by Report ID")
        skipped_groups = (
            self.snapshot_report_ids,
            self.ineligible_report_ids,
            self.up_to_date_report_ids,
            self.no_code_change_report_ids,
            self.unresolved_report_ids,
        )
        for values in skipped_groups:
            if values != tuple(sorted(values)) or len(values) != len(set(values)):
                raise ValueError("batch skipped Report IDs must be unique and sorted")
        all_report_ids = [*report_ids]
        for values in skipped_groups:
            all_report_ids.extend(values)
        if len(all_report_ids) != len(set(all_report_ids)):
            raise ValueError("a Report may appear only once in an Impact batch")
        for impact in self.impacts:
            if (
                impact.impact_id != self.impact_id
                or impact.target_commit != self.target_commit
                or impact.target_tree != self.target_tree
                or impact.generated_at != self.generated_at
            ):
                raise ValueError("batch child Impact identity does not match the batch")
        from researchctl.serialization import canonical_digest

        content = self.model_dump(
            mode="json",
            exclude={"batch_digest"},
            exclude_none=True,
        )
        if self.batch_digest != canonical_digest(content):
            raise ValueError(
                "batch_digest does not match canonical ReportImpactBatch content"
            )
        return self


class ImpactDecision(ProtocolRecord):
    decision_id: DecisionId
    impact_id: ImpactId
    report_id: ReportId
    expected_report_revision: Annotated[StrictInt, Field(ge=1)]
    expected_impact_digest: Sha256Digest
    impact_target_commit: GitObjectId
    impact_target_tree: GitObjectId
    decision_base_commit: GitObjectId
    decision_base_tree: GitObjectId
    disposition: ImpactDisposition
    reviewer_actor: ShortText
    reason: NonEmptyStr
    rerun_task_id: TaskId | None = None
    replacement_dependencies: DependencySet | None = None
    decided_at: UtcDateTime
    decision_digest: Sha256Digest

    @model_validator(mode="after")
    def require_canonical_impact_decision(self) -> ImpactDecision:
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
        from researchctl.serialization import canonical_digest

        content = self.model_dump(
            mode="json",
            exclude={"decision_digest"},
            exclude_none=True,
        )
        if self.decision_digest != canonical_digest(content):
            raise ValueError(
                "decision_digest does not match canonical ImpactDecision content"
            )
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
    | DependencyChangeReceipt
    | ReportImpact
    | ReportImpactBatch
    | ImpactDecision
    | LinearProjectionPolicy
    | CIValidationAttestation
    | StatusUpdate
)
