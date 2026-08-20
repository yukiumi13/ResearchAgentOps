from __future__ import annotations

import math
from datetime import date
from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictBool,
    StrictFloat,
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
    PlanFindingKind,
    PlanMetricDirection,
    PlanReviewOutcome,
    PlanValueSourceKind,
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
    AttestationId,
    DecisionId,
    DependencyReceiptId,
    DocumentId,
    DocumentLabel,
    DocumentSlug,
    GitBranchName,
    GitHubAccountLogin,
    GitHubRepository,
    GitHubTeamSlug,
    GitObjectId,
    HumanKey,
    ImpactId,
    LinearUuid,
    NonEmptyStr,
    OperationId,
    PlanId,
    PlanReviewId,
    ProjectId,
    ReportId,
    RepositoryPath,
    RunAttemptId,
    RunId,
    RunResultId,
    SessionId,
    Sha256Digest,
    ShortText,
    StatusUpdateId,
    SubmissionId,
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


class PlanReviewPolicy(StrictModel):
    provider: Literal["codex", "claude"]
    model: ShortText
    policy_version: ShortText
    timeout_seconds: Annotated[StrictInt, Field(ge=1, le=3600)]


DocumentKind = Literal["design_document", "project_status_summary"]
DocumentSchema = Literal[
    "design-document",
    "project-status-summary",
    "analysis-brief",
    "markdown-frontmatter",
]
DocumentRelationKind = Literal["supersedes", "derived_from", "see_also"]
AgentGuideFormat = Literal["claude", "agents"]


class GeneratedMarkdownFrontmatterPolicy(StrictModel):
    required_fields: Annotated[
        tuple[HumanKey, ...],
        Field(
            min_length=1,
            max_length=32,
            description=(
                "Project-owned YAML frontmatter keys preserved around a generated "
                "Markdown body. The project remains responsible for field semantics."
            ),
        ),
    ]

    @model_validator(mode="after")
    def require_unique_fields(self) -> GeneratedMarkdownFrontmatterPolicy:
        if len(self.required_fields) != len(set(self.required_fields)):
            raise ValueError("generated Markdown frontmatter fields must be unique")
        return self


class DocumentRoute(StrictModel):
    classification: DocumentLabel
    document_type: DocumentSlug
    directory: RepositoryPath
    contract: DocumentSchema
    rationale: ShortText
    required_relations: tuple[DocumentRelationKind, ...] = ()
    generated_markdown_frontmatter: GeneratedMarkdownFrontmatterPolicy | None = None

    @model_validator(mode="after")
    def constrain_generated_frontmatter(self) -> DocumentRoute:
        if (
            self.contract == "markdown-frontmatter"
            and self.generated_markdown_frontmatter is not None
        ):
            raise ValueError(
                "manual markdown-frontmatter routes cannot add a generated body envelope"
            )
        return self


class LegacyDocumentPath(StrictModel):
    path: RepositoryPath
    classification: DocumentLabel
    migration_target: RepositoryPath
    reason: ShortText


class AgentGuideTarget(StrictModel):
    path: RepositoryPath
    format: AgentGuideFormat


class ClassificationDepthPolicy(StrictModel):
    minimum: Annotated[StrictInt, Field(ge=1, le=8)] = 2
    maximum: Annotated[StrictInt, Field(ge=1, le=8)] = 4

    @model_validator(mode="after")
    def require_ordered_bounds(self) -> ClassificationDepthPolicy:
        if self.minimum > self.maximum:
            raise ValueError("classification depth minimum cannot exceed maximum")
        return self


class MachineArtifactRoot(StrictModel):
    directory: RepositoryPath
    allowed_extensions: Annotated[tuple[str, ...], Field(min_length=1)] = (
        ".csv",
        ".json",
        ".jsonl",
        ".xlsx",
        ".py",
    )

    @model_validator(mode="after")
    def require_machine_only_extensions(self) -> MachineArtifactRoot:
        if self.directory == ".":
            raise ValueError("machine artifact root must identify a repository directory")
        if len(self.allowed_extensions) != len(set(self.allowed_extensions)):
            raise ValueError("machine artifact extensions must be unique")
        for extension in self.allowed_extensions:
            if (
                not extension.startswith(".")
                or extension != extension.lower()
                or len(extension) < 2
                or not extension[1:].replace("-", "").isalnum()
            ):
                raise ValueError(
                    "machine artifact extensions must be lowercase dot-prefixed suffixes"
                )
        if {".md", ".markdown"} & set(self.allowed_extensions):
            raise ValueError("machine artifact roots cannot allow Markdown")
        return self


def _default_document_routes() -> tuple[DocumentRoute, ...]:
    return (
        DocumentRoute(
            classification="decision/architecture:adr",
            document_type="adr",
            directory="docs/adr",
            contract="markdown-frontmatter",
            rationale="Architecture decisions use the established docs/adr history.",
        ),
        DocumentRoute(
            classification="design/architecture:proposal",
            document_type="design",
            directory="docs/design",
            contract="design-document",
            rationale="Design proposals require structured alternatives and validation.",
        ),
        DocumentRoute(
            classification="operations/project:runbook",
            document_type="runbook",
            directory="docs/runbooks",
            contract="markdown-frontmatter",
            rationale="Operational procedures need a stable executable location.",
        ),
        DocumentRoute(
            classification="reference/project:document",
            document_type="reference",
            directory="docs/reference",
            contract="markdown-frontmatter",
            rationale="Long-lived references are separate from current project status.",
        ),
        DocumentRoute(
            classification="status/project:snapshot",
            document_type="status",
            directory="docs/status",
            contract="project-status-summary",
            rationale="Current project snapshots require traceable structured evidence.",
        ),
    )


class DocumentLayoutPolicy(StrictModel):
    root: RepositoryPath = "docs"
    classification_depth: ClassificationDepthPolicy = Field(
        default_factory=ClassificationDepthPolicy
    )
    routes: Annotated[tuple[DocumentRoute, ...], Field(min_length=1)] = Field(
        default_factory=_default_document_routes
    )
    root_files: tuple[RepositoryPath, ...] = ("docs/README.md",)
    generated_index: RepositoryPath | None = None
    agent_guides: tuple[AgentGuideTarget, ...] = ()
    legacy_files: tuple[LegacyDocumentPath, ...] = ()
    machine_artifact_roots: tuple[MachineArtifactRoot, ...] = ()
    max_depth: Annotated[StrictInt, Field(ge=1, le=8)] = 4

    @model_validator(mode="after")
    def require_closed_unambiguous_layout(self) -> DocumentLayoutPolicy:
        root_parts = PurePosixPath(self.root).parts

        def within_root(path: str) -> bool:
            parts = PurePosixPath(path).parts
            return parts[: len(root_parts)] == root_parts and len(parts) > len(root_parts)

        labels = tuple(route.classification for route in self.routes)
        types = tuple(route.document_type for route in self.routes)
        directories = tuple(route.directory for route in self.routes)
        if len(labels) != len(set(labels)):
            raise ValueError("document route classifications must be unique")
        if len(types) != len(set(types)):
            raise ValueError("document route types must be unique")
        for label in labels:
            namespace, _separator, _category = label.partition(":")
            depth = len(namespace.split("/"))
            if not self.classification_depth.minimum <= depth <= self.classification_depth.maximum:
                raise ValueError(
                    "document route classification depth must satisfy classification_depth"
                )
        if any(not within_root(directory) for directory in directories):
            raise ValueError("document route directories must be below the document root")
        directory_parts = [PurePosixPath(directory).parts for directory in directories]
        for index, parts in enumerate(directory_parts):
            for other_index, other in enumerate(directory_parts):
                if index != other_index and parts[: len(other)] == other:
                    raise ValueError("document route directories must not overlap")

        root_files = tuple(self.root_files)
        if len(root_files) != len(set(root_files)) or any(
            not within_root(path) for path in root_files
        ):
            raise ValueError("document root_files must be unique files below the root")
        if self.generated_index is not None and self.generated_index not in root_files:
            raise ValueError("generated document index must be declared in root_files")

        guide_paths = tuple(item.path for item in self.agent_guides)
        if len(guide_paths) != len(set(guide_paths)):
            raise ValueError("agent guide paths must be unique")
        for guide_path in guide_paths:
            guide_parts = PurePosixPath(guide_path).parts
            if guide_parts[: len(root_parts)] == root_parts:
                raise ValueError("agent guides must live outside the document root")
            if PurePosixPath(guide_path).suffix.lower() != ".md":
                raise ValueError("agent guides must be Markdown files")

        legacy_paths = tuple(item.path for item in self.legacy_files)
        migration_targets = tuple(item.migration_target for item in self.legacy_files)
        if len(legacy_paths) != len(set(legacy_paths)):
            raise ValueError("legacy document paths must be unique")
        if any(
            not within_root(path)
            for path in (*legacy_paths, *migration_targets)
        ):
            raise ValueError("legacy paths and migration targets must be below the root")
        if set(legacy_paths) & set(root_files):
            raise ValueError("legacy documents cannot also be root files")
        route_labels = set(labels)
        if any(item.classification not in route_labels for item in self.legacy_files):
            raise ValueError("legacy documents must use an accepted route classification")

        artifact_directories = tuple(
            item.directory for item in self.machine_artifact_roots
        )
        if len(artifact_directories) != len(set(artifact_directories)):
            raise ValueError("machine artifact roots must be unique")
        document_parts = PurePosixPath(self.root).parts
        artifact_path_parts = [
            PurePosixPath(directory).parts for directory in artifact_directories
        ]
        for index, parts in enumerate(artifact_path_parts):
            if parts[: len(document_parts)] == document_parts or document_parts[
                : len(parts)
            ] == parts:
                raise ValueError("machine artifact roots must not overlap the document root")
            for other_index, other in enumerate(artifact_path_parts):
                if index != other_index and (
                    parts[: len(other)] == other or other[: len(parts)] == parts
                ):
                    raise ValueError("machine artifact roots must not overlap")
        for guide_path in guide_paths:
            guide_parts = PurePosixPath(guide_path).parts
            if any(
                guide_parts[: len(parts)] == parts
                for parts in artifact_path_parts
            ):
                raise ValueError("agent guides cannot live in machine artifact roots")
        return self

    def route_for_label(self, label: str) -> DocumentRoute | None:
        return next(
            (route for route in self.routes if route.classification == label),
            None,
        )


StructuredDocumentSchema = Literal[
    "design-document",
    "project-status-summary",
    "analysis-brief",
]
SimpleDocumentStatus = Literal["draft", "active", "deprecated", "archived"]


#: Structured contracts whose envelope carries a required ``a/b:c`` label.
CLASSIFIED_STRUCTURED_SCHEMAS: tuple[str, ...] = (
    "design-document",
    "project-status-summary",
)


class SimpleSectionStructure(StrictModel):
    """Opt-in canonical YAML contract for one section.

    ``classification`` exists only so a structured envelope that already
    requires an ``a/b:c`` label keeps a policy-level counterpart to compare
    against. It is never a taxonomy for ordinary Markdown, and a contract that
    has no such field cannot declare one.
    """

    contract: StructuredDocumentSchema
    classification: DocumentLabel | None = None

    @model_validator(mode="after")
    def bind_classification_to_its_contract(self) -> SimpleSectionStructure:
        classified = self.contract in CLASSIFIED_STRUCTURED_SCHEMAS
        if classified and self.classification is None:
            raise ValueError(
                f"{self.contract} envelopes require a policy-level classification"
            )
        if not classified and self.classification is not None:
            raise ValueError(
                f"{self.contract} has no classification field to compare against"
            )
        return self


class SimpleDocumentSection(StrictModel):
    """One direct child directory of the document root.

    The directory name is the document type. There is no separate label,
    ``document_type``, or ``directory`` field to keep in sync.
    """

    path: DocumentSlug
    structured: SimpleSectionStructure | None = None


class SimpleDocumentOwnershipPolicy(StrictModel):
    source: Literal["codeowners"] = "codeowners"
    required: StrictBool = False


class SimpleDocumentLayoutPolicy(StrictModel):
    """Directory-first standalone document policy.

    ``version`` is an explicit discriminator. A policy file without it, or with
    ``version: 1``, remains the strict v1 :class:`DocumentLayoutPolicy`.
    """

    version: Literal[2]
    root: RepositoryPath = "docs"
    sections: Annotated[tuple[SimpleDocumentSection, ...], Field(min_length=1)]
    root_pages: tuple[RepositoryPath, ...] = ()
    max_depth: Annotated[StrictInt, Field(ge=1, le=8)] = 3
    ownership: SimpleDocumentOwnershipPolicy | None = None
    agent_guides: tuple[AgentGuideTarget, ...] = ()

    @model_validator(mode="after")
    def require_closed_unambiguous_layout(self) -> SimpleDocumentLayoutPolicy:
        if self.root == ".":
            raise ValueError("simple document root must identify a repository directory")
        root_parts = PurePosixPath(self.root).parts

        section_paths = tuple(section.path for section in self.sections)
        if len(section_paths) != len(set(section_paths)):
            raise ValueError("simple document section paths must be unique")

        page_paths = tuple(self.root_pages)
        if len(page_paths) != len(set(page_paths)):
            raise ValueError("simple document root_pages must be unique")
        for page in page_paths:
            if page == "." or PurePosixPath(page).suffix.lower() != ".md":
                raise ValueError("simple document root_pages must be Markdown files")
            if len(PurePosixPath(page).parts) != 1:
                raise ValueError(
                    "simple document root_pages must be direct children of the root"
                )

        guide_paths = tuple(item.path for item in self.agent_guides)
        if len(guide_paths) != len(set(guide_paths)):
            raise ValueError("agent guide paths must be unique")
        for guide_path in guide_paths:
            guide_parts = PurePosixPath(guide_path).parts
            if guide_parts[: len(root_parts)] == root_parts:
                raise ValueError("agent guides must live outside the document root")
            if PurePosixPath(guide_path).suffix.lower() != ".md":
                raise ValueError("agent guides must be Markdown files")
        return self

    def section_directory(self, section: SimpleDocumentSection) -> str:
        return f"{self.root}/{section.path}"

    def section_for_path(self, relative: str) -> SimpleDocumentSection | None:
        """Return the section owning a repository-relative path, if any."""

        root_parts = PurePosixPath(self.root).parts
        parts = PurePosixPath(relative).parts
        if parts[: len(root_parts)] != root_parts or len(parts) <= len(root_parts) + 1:
            return None
        candidate = parts[len(root_parts)]
        return next(
            (section for section in self.sections if section.path == candidate),
            None,
        )

    def root_page_paths(self) -> tuple[str, ...]:
        return tuple(f"{self.root}/{page}" for page in self.root_pages)


class GitHubAgentAppPrincipal(StrictModel):
    app_id: Annotated[StrictInt, Field(ge=1)]
    installation_id: Annotated[StrictInt, Field(ge=1)]
    login: GitHubAccountLogin

    @model_validator(mode="after")
    def require_bot_login(self) -> GitHubAgentAppPrincipal:
        if not self.login.endswith("[bot]"):
            raise ValueError("GitHub Agent App login must use the canonical [bot] account")
        return self


class GitHubUserManagerPrincipal(StrictModel):
    kind: Literal["user"] = "user"
    login: GitHubAccountLogin

    @model_validator(mode="after")
    def forbid_bot_manager(self) -> GitHubUserManagerPrincipal:
        if self.login.endswith("[bot]"):
            raise ValueError("GitHub Manager user must be a human account")
        return self


class GitHubTeamManagerPrincipal(StrictModel):
    kind: Literal["team"] = "team"
    organization: GitHubAccountLogin
    slug: GitHubTeamSlug

    @model_validator(mode="after")
    def forbid_bot_organization(self) -> GitHubTeamManagerPrincipal:
        if self.organization.endswith("[bot]"):
            raise ValueError("GitHub Manager team must belong to an organization")
        return self


GitHubManagerPrincipal = Annotated[
    GitHubUserManagerPrincipal | GitHubTeamManagerPrincipal,
    Field(discriminator="kind"),
]


class GitHubBypassActorPolicy(StrictModel):
    actor_type: Literal["Integration", "RepositoryRole", "Team"]
    actor_id: Annotated[StrictInt, Field(ge=1)]
    bypass_mode: Literal["always", "pull_request"]
    rationale: NonEmptyStr


class GitHubGovernancePolicy(StrictModel):
    repository: GitHubRepository
    default_branch: GitBranchName
    agent_app: GitHubAgentAppPrincipal
    managers: Annotated[tuple[GitHubManagerPrincipal, ...], Field(min_length=1)]
    required_status_checks: tuple[
        Literal["researchctl/exact-head", "researchctl/source-tests"], ...
    ] = ("researchctl/exact-head", "researchctl/source-tests")
    required_approvals: Annotated[StrictInt, Field(ge=1, le=6)] = 1
    require_code_owner_review: StrictBool = True
    dismiss_stale_reviews: StrictBool = True
    require_last_push_approval: StrictBool = True
    strict_status_checks: StrictBool = True
    block_force_pushes: StrictBool = True
    block_deletions: StrictBool = True
    bypass_actors: tuple[GitHubBypassActorPolicy, ...] = ()

    @model_validator(mode="after")
    def require_protected_acceptance(self) -> GitHubGovernancePolicy:
        expected_checks = (
            "researchctl/exact-head",
            "researchctl/source-tests",
        )
        if self.required_status_checks != expected_checks:
            raise ValueError(
                "GitHub governance requires the two fixed status checks in canonical order"
            )
        required_flags = (
            self.require_code_owner_review,
            self.dismiss_stale_reviews,
            self.require_last_push_approval,
            self.strict_status_checks,
            self.block_force_pushes,
            self.block_deletions,
        )
        if not all(required_flags):
            raise ValueError("GitHub governance cannot disable a protected merge gate")
        manager_keys = tuple(
            ("user", item.login)
            if isinstance(item, GitHubUserManagerPrincipal)
            else ("team", f"{item.organization}/{item.slug}")
            for item in self.managers
        )
        if manager_keys != tuple(sorted(manager_keys)):
            raise ValueError("GitHub Manager principals must be canonically sorted")
        if len(manager_keys) != len(set(manager_keys)):
            raise ValueError("GitHub Manager principals must be unique")
        if ("user", self.agent_app.login) in manager_keys:
            raise ValueError("GitHub Agent App cannot also be an accepting Manager")
        bypass_keys = tuple(
            (item.actor_type, item.actor_id, item.bypass_mode)
            for item in self.bypass_actors
        )
        if bypass_keys != tuple(sorted(bypass_keys)):
            raise ValueError("GitHub bypass actors must be canonically sorted")
        if len(bypass_keys) != len(set(bypass_keys)):
            raise ValueError("GitHub bypass actors must be unique")
        return self


class ProjectPolicy(ProtocolRecord):
    agent: AgentPolicy
    impact: ImpactPolicy = Field(default_factory=ImpactPolicy)
    security: SecurityPolicy = Field(default_factory=SecurityPolicy)
    execution_domains: tuple[ExecutionDomainPolicy, ...] = ()
    plan_choices: dict[ShortText, JsonValue] = Field(default_factory=dict)
    plan_review: PlanReviewPolicy | None = None
    document_layout: DocumentLayoutPolicy = Field(default_factory=DocumentLayoutPolicy)
    github: GitHubGovernancePolicy | None = None

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
    plan_choices: dict[ShortText, JsonValue] = Field(default_factory=dict)
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


class PlanMetric(StrictModel):
    name: HumanKey
    direction: PlanMetricDirection
    target: JsonValue | None = None

    @model_validator(mode="after")
    def require_target_consistency(self) -> PlanMetric:
        if (self.direction is PlanMetricDirection.TARGET) != (self.target is not None):
            raise ValueError("target metrics require a target and other directions forbid it")
        return self


class PlanValueSource(StrictModel):
    field_path: ShortText
    source_kind: PlanValueSourceKind
    source_digest: Sha256Digest


_PLAN_DECISION_FIELDS = frozenset(
    {
        "argv",
        "artifact_declarations",
        "comparison",
        "config",
        "environment",
        "failure_conditions",
        "hypothesis",
        "inputs",
        "metrics",
        "repetitions",
        "requested_host",
        "resources",
        "seeds",
        "stop_conditions",
        "working_directory",
    }
)


class ExperimentPlan(ProtocolRecord):
    plan_id: PlanId
    task_id: TaskId
    session_id: SessionId
    draft_invocation_id: ShortText
    task_digest: Sha256Digest
    policy_digest: Sha256Digest
    run_id: RunId
    operation_id: OperationId
    source_commit: GitObjectId
    source_tree: GitObjectId
    baseline_commit: GitObjectId | None
    hypothesis: NonEmptyStr
    comparison: NonEmptyStr
    argv: Annotated[tuple[NonEmptyStr, ...], Field(min_length=1)]
    working_directory: RepositoryPath
    environment: InputIdentity
    config: InputIdentity | None
    inputs: tuple[InputIdentity, ...]
    metrics: Annotated[tuple[PlanMetric, ...], Field(min_length=1)]
    repetitions: Annotated[StrictInt, Field(ge=1, le=1000000)] | None
    seeds: tuple[Annotated[StrictInt, Field(ge=0)], ...]
    resources: ResourceRequirement
    requested_host: HumanKey | None
    stop_conditions: Annotated[tuple[NonEmptyStr, ...], Field(min_length=1)]
    failure_conditions: Annotated[tuple[NonEmptyStr, ...], Field(min_length=1)]
    artifact_declarations: tuple[ArtifactDeclaration, ...]
    value_sources: Annotated[tuple[PlanValueSource, ...], Field(min_length=1)]
    created_at: UtcDateTime
    plan_digest: Sha256Digest

    @model_validator(mode="after")
    def require_canonical_plan(self) -> ExperimentPlan:
        if (self.repetitions is not None) == bool(self.seeds):
            raise ValueError("plan requires exactly one of repetitions or non-empty seeds")
        if self.seeds != tuple(sorted(self.seeds)) or len(self.seeds) != len(
            set(self.seeds)
        ):
            raise ValueError("plan seeds must be unique and sorted")
        metric_names = tuple(item.name for item in self.metrics)
        if len(metric_names) != len(set(metric_names)):
            raise ValueError("plan metric names must be unique")
        identities = [self.environment]
        if self.config is not None:
            identities.append(self.config)
        identities.extend(self.inputs)
        identity_keys = [(item.kind, item.logical_id) for item in identities]
        if len(identity_keys) != len(set(identity_keys)):
            raise ValueError("plan input identity keys must be unique")
        artifact_names = tuple(item.name for item in self.artifact_declarations)
        artifact_paths = tuple(item.path for item in self.artifact_declarations)
        if len(artifact_names) != len(set(artifact_names)):
            raise ValueError("plan artifact names must be unique")
        if len(artifact_paths) != len(set(artifact_paths)):
            raise ValueError("plan artifact paths must be unique")
        source_paths = tuple(item.field_path for item in self.value_sources)
        if source_paths != tuple(sorted(source_paths)):
            raise ValueError("plan value_sources must be sorted by field_path")
        if len(source_paths) != len(set(source_paths)):
            raise ValueError("plan value_sources must be unique by field_path")
        if set(source_paths) != _PLAN_DECISION_FIELDS:
            raise ValueError(
                "plan value_sources must cover every decision-bearing field exactly"
            )
        from researchctl.serialization import canonical_digest

        content = self.model_dump(
            mode="json",
            exclude={"plan_digest"},
            exclude_none=False,
        )
        if self.plan_digest != canonical_digest(content):
            raise ValueError("plan_digest does not match canonical ExperimentPlan content")
        return self


class PlanReviewFinding(StrictModel):
    kind: PlanFindingKind
    code: HumanKey
    field_path: ShortText | None = None
    message: NonEmptyStr


class PlanReview(ProtocolRecord):
    review_id: PlanReviewId
    review_operation_id: OperationId
    plan_id: PlanId
    plan_digest: Sha256Digest
    task_id: TaskId
    task_digest: Sha256Digest
    policy_digest: Sha256Digest
    review_policy_version: ShortText
    drafter_invocation_id: ShortText
    reviewer_invocation_id: ShortText
    reviewer_provider: HumanKey
    reviewer_model: ShortText
    outcome: PlanReviewOutcome
    findings: tuple[PlanReviewFinding, ...] = ()
    reviewer_output_digest: Sha256Digest
    completed_at: UtcDateTime
    completion_receipt_digest: Sha256Digest
    review_digest: Sha256Digest

    @staticmethod
    def calculate_completion_receipt(
        *,
        review_operation_id: str,
        plan_digest: str,
        task_digest: str,
        policy_digest: str,
        review_policy_version: str,
        reviewer_invocation_id: str,
        reviewer_provider: str,
        reviewer_model: str,
        outcome: str,
        findings: tuple[PlanReviewFinding, ...],
        reviewer_output_digest: str,
    ) -> str:
        from researchctl.serialization import canonical_digest

        return canonical_digest(
            {
                "review_operation_id": review_operation_id,
                "plan_digest": plan_digest,
                "task_digest": task_digest,
                "policy_digest": policy_digest,
                "review_policy_version": review_policy_version,
                "reviewer_invocation_id": reviewer_invocation_id,
                "reviewer_provider": reviewer_provider,
                "reviewer_model": reviewer_model,
                "outcome": outcome,
                "findings": [
                    item.model_dump(mode="json", exclude_none=True) for item in findings
                ],
                "reviewer_output_digest": reviewer_output_digest,
            }
        )

    @model_validator(mode="after")
    def require_canonical_review(self) -> PlanReview:
        if self.reviewer_invocation_id == self.drafter_invocation_id:
            raise ValueError("PlanReview invocation must differ from the Plan drafter")
        finding_keys = tuple(
            (item.field_path or "", item.code, item.kind.value, item.message)
            for item in self.findings
        )
        if finding_keys != tuple(sorted(finding_keys)) or len(finding_keys) != len(
            set(finding_keys)
        ):
            raise ValueError("PlanReview findings must be unique and sorted")
        kinds = {item.kind for item in self.findings}
        if self.outcome is PlanReviewOutcome.PASSED and kinds & {
            PlanFindingKind.NEEDS_INPUT,
            PlanFindingKind.INVALID,
        }:
            raise ValueError("passed PlanReview cannot contain blocking findings")
        if self.outcome is PlanReviewOutcome.NEEDS_INPUT and (
            PlanFindingKind.NEEDS_INPUT not in kinds
            or PlanFindingKind.INVALID in kinds
        ):
            raise ValueError("needs_input PlanReview requires only needs-input blockers")
        if (
            self.outcome is PlanReviewOutcome.INVALID
            and PlanFindingKind.INVALID not in kinds
        ):
            raise ValueError("invalid PlanReview requires an invalid finding")
        expected_receipt = self.calculate_completion_receipt(
            review_operation_id=self.review_operation_id,
            plan_digest=self.plan_digest,
            task_digest=self.task_digest,
            policy_digest=self.policy_digest,
            review_policy_version=self.review_policy_version,
            reviewer_invocation_id=self.reviewer_invocation_id,
            reviewer_provider=self.reviewer_provider,
            reviewer_model=self.reviewer_model,
            outcome=self.outcome.value,
            findings=self.findings,
            reviewer_output_digest=self.reviewer_output_digest,
        )
        if self.completion_receipt_digest != expected_receipt:
            raise ValueError("PlanReview completion receipt does not match review evidence")
        from researchctl.serialization import canonical_digest

        content = self.model_dump(
            mode="json",
            exclude={"review_digest"},
            exclude_none=True,
        )
        if self.review_digest != canonical_digest(content):
            raise ValueError("review_digest does not match canonical PlanReview content")
        return self


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
    experiment_plan: ExperimentPlan | None = None
    plan_review: PlanReview | None = None
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
    def require_plan_consistency(self) -> RunSpec:
        if (self.experiment_plan is None) != (self.plan_review is None):
            raise ValueError("RunSpec requires both ExperimentPlan and PlanReview or neither")
        plan = self.experiment_plan
        review = self.plan_review
        if plan is None or review is None:
            return self
        expected = (
            plan.run_id,
            plan.task_id,
            plan.session_id,
            plan.operation_id,
            plan.source_commit,
            plan.source_tree,
            plan.baseline_commit,
            plan.argv,
            plan.working_directory,
            plan.environment,
            plan.config,
            plan.inputs,
            plan.resources,
            plan.requested_host,
            plan.artifact_declarations,
            plan.created_at,
        )
        observed = (
            self.run_id,
            self.task_id,
            self.session_id,
            self.operation_id,
            self.source_commit,
            self.source_tree,
            self.baseline_commit,
            self.argv,
            self.working_directory,
            self.environment,
            self.config,
            self.inputs,
            self.resources,
            self.requested_host,
            self.artifact_declarations,
            self.created_at,
        )
        if observed != expected:
            raise ValueError("RunSpec execution fields drift from its ExperimentPlan")
        if (
            review.outcome is not PlanReviewOutcome.PASSED
            or review.plan_id != plan.plan_id
            or review.plan_digest != plan.plan_digest
            or review.task_id != plan.task_id
            or review.task_digest != plan.task_digest
            or review.policy_digest != plan.policy_digest
            or review.drafter_invocation_id != plan.draft_invocation_id
        ):
            raise ValueError("RunSpec PlanReview does not pass and bind its ExperimentPlan")
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


BriefValue = ShortText | StrictBool | StrictInt | StrictFloat


class BriefMetric(StrictModel):
    key: HumanKey
    label: ShortText


class BriefSource(StrictModel):
    key: HumanKey
    location: NonEmptyStr


class BriefEvidence(StrictModel):
    setting: ShortText
    values: Annotated[
        dict[HumanKey, BriefValue],
        Field(
            min_length=1,
            max_length=5,
            description=(
                "Display values keyed by declared metric. Quote measured decimal "
                "values such as '0.20' because YAML numeric parsing does not retain "
                "significant trailing zeros."
            ),
        ),
    ]
    source_keys: Annotated[tuple[HumanKey, ...], Field(min_length=1, max_length=4)]

    @model_validator(mode="after")
    def require_finite_values_and_unique_sources(self) -> BriefEvidence:
        if len(self.source_keys) != len(set(self.source_keys)):
            raise ValueError("brief evidence source_keys must be unique")
        for value in self.values.values():
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("brief evidence values must be finite")
        return self


DocumentLifecycle = Literal[
    "draft",
    "proposed",
    "accepted",
    "superseded",
    "deprecated",
]
DocumentActorRole = Literal["manager", "agent", "trusted_automation", "external_agent"]


class DocumentAuthor(StrictModel):
    role: DocumentActorRole
    actor_id: ShortText
    session_id: SessionId | None = None

    @model_validator(mode="after")
    def bind_governed_agent_session(self) -> DocumentAuthor:
        if (self.role == "agent") != (self.session_id is not None):
            raise ValueError(
                "governed Agent document authors require exactly one canonical session_id"
            )
        return self


class DocumentAcceptance(StrictModel):
    accepted_by: ShortText
    accepted_at: UtcDateTime
    accepted_commit: GitObjectId


class DocumentReference(StrictModel):
    key: HumanKey
    kind: Literal[
        "repository_path",
        "git_commit",
        "task",
        "run",
        "report",
        "artifact",
        "external",
    ]
    location: NonEmptyStr
    digest: Sha256Digest | None = None


class MarkdownDocumentReference(StrictModel):
    kind: Literal[
        "repository_path",
        "git_commit",
        "task",
        "run",
        "report",
        "artifact",
        "external",
    ]
    location: NonEmptyStr
    digest: Sha256Digest | None = None


class MarkdownClaimProvenance(StrictModel):
    key: HumanKey
    value: Annotated[
        ShortText,
        Field(
            description=(
                "Exact display text that must occur verbatim in the Markdown body. "
                "Always quote numeric-looking YAML values, for example '11677' or "
                "'91.20', so formatting and significant digits remain stable."
            )
        ),
    ]
    basis: Literal["measured", "estimated", "derived", "external"]
    source_keys: Annotated[tuple[HumanKey, ...], Field(min_length=1, max_length=8)]
    method: ShortText | None = None

    @model_validator(mode="after")
    def require_traceable_claim(self) -> MarkdownClaimProvenance:
        if len(self.source_keys) != len(set(self.source_keys)):
            raise ValueError("Markdown provenance source_keys must be unique")
        if self.basis in {"estimated", "derived"} and self.method is None:
            raise ValueError("estimated and derived Markdown provenance requires method")
        return self


RepositoryRelationTargets = Annotated[
    tuple[RepositoryPath, ...],
    Field(
        description=(
            "Repository-root-relative document paths, for example "
            "docs/runbooks/evaluation.md."
        )
    ),
]


class DocumentRelations(StrictModel):
    supersedes: RepositoryRelationTargets = ()
    derived_from: RepositoryRelationTargets = ()
    see_also: RepositoryRelationTargets = ()

    @model_validator(mode="after")
    def require_unique_relations(self) -> DocumentRelations:
        for kind, values in (
            ("supersedes", self.supersedes),
            ("derived_from", self.derived_from),
            ("see_also", self.see_also),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"document relation {kind} must be unique")
        return self


class MarkdownFrontmatter(StrictModel):
    type: DocumentSlug
    title: ShortText
    owner: Annotated[
        str,
        Field(
            min_length=3,
            max_length=160,
            pattern=(
                r"^(?:session:session_\d{8}T\d{6}Z_[0-9a-f]{24}|"
                r"(?:person|team):[A-Za-z0-9][A-Za-z0-9._-]*)$"
            ),
        ),
    ]
    last_updated: date
    validity: Literal["valid", "invalid", "frozen"]
    invalid_reason: ShortText | None = None
    tags: tuple[HumanKey, ...] = ()
    references: tuple[MarkdownDocumentReference, ...] = ()
    sources: tuple[DocumentReference, ...] = ()
    provenance: tuple[MarkdownClaimProvenance, ...] = ()
    relations: DocumentRelations = Field(default_factory=DocumentRelations)

    @model_validator(mode="after")
    def require_frontmatter_semantics(self) -> MarkdownFrontmatter:
        if (self.validity == "invalid") != (self.invalid_reason is not None):
            raise ValueError(
                "invalid documents require invalid_reason and other validity states forbid it"
            )
        if len(self.tags) != len(set(self.tags)):
            raise ValueError("document frontmatter tags must be unique")
        source_keys = tuple(source.key for source in self.sources)
        if len(source_keys) != len(set(source_keys)):
            raise ValueError("document frontmatter source keys must be unique")
        provenance_keys = tuple(item.key for item in self.provenance)
        if len(provenance_keys) != len(set(provenance_keys)):
            raise ValueError("document frontmatter provenance keys must be unique")
        used_source_keys = {
            source_key
            for item in self.provenance
            for source_key in item.source_keys
        }
        declared_source_keys = set(source_keys)
        if used_source_keys != declared_source_keys:
            unused = sorted(declared_source_keys - used_source_keys)
            undeclared = sorted(used_source_keys - declared_source_keys)
            differences: list[str] = []
            if unused:
                differences.append("unused declared sources: " + ", ".join(unused))
            if undeclared:
                differences.append("undeclared source keys: " + ", ".join(undeclared))
            raise ValueError("Markdown provenance source mismatch; " + "; ".join(differences))
        return self


#: The contract an ordinary version 2 document satisfies. Its JSON Schema is
#: registered under the same name, so what `doc check` reports, what the managed
#: Agent guide names, and what `doc schema --contract` prints are one string.
SIMPLE_MARKDOWN_CONTRACT = "simple-markdown-frontmatter"

SIMPLE_MARKDOWN_FRONTMATTER_FIELDS: tuple[str, ...] = (
    "status",
    "tags",
    "reviewed_on",
    "locked",
    "depends_on",
    "superseded_by",
)


class SimpleMarkdownFrontmatter(StrictModel):
    """Optional metadata block for an ordinary Markdown document.

    Every field has a default, so a document with no frontmatter at all is a
    valid active, untagged, unreviewed, unlocked document. The title is not a
    field here: the first level-one heading is the only title.
    """

    status: SimpleDocumentStatus = "active"
    tags: tuple[HumanKey, ...] = ()
    reviewed_on: date | None = None
    locked: StrictBool = False
    depends_on: tuple[RepositoryPath, ...] = ()
    superseded_by: RepositoryPath | None = None

    @model_validator(mode="after")
    def require_unique_metadata(self) -> SimpleMarkdownFrontmatter:
        if len(self.tags) != len(set(self.tags)):
            raise ValueError("document tags must be unique")
        if len(self.depends_on) != len(set(self.depends_on)):
            raise ValueError("document depends_on entries must be unique")
        return self


DocumentSitePageKind = Literal["root", "manual", "structured", "legacy"]
DocumentSiteHistoryKind = Literal["archive", "legacy"]


class DocumentSitePage(StrictModel):
    path: RepositoryPath
    source_path: RepositoryPath | None = None
    kind: DocumentSitePageKind
    title: ShortText
    document_type: DocumentSlug | None = None
    classification: DocumentLabel | None = None
    contract: DocumentSchema | None = None
    validity: Literal["valid", "invalid", "frozen"] | None = None
    lifecycle: DocumentLifecycle | None = None
    generated: StrictBool = False
    route_order: Annotated[StrictInt, Field(ge=0)] | None = None
    relations: DocumentRelations = Field(default_factory=DocumentRelations)
    content_digest: Sha256Digest
    source_digest: Sha256Digest | None = None
    history_kind: DocumentSiteHistoryKind | None = None

    @model_validator(mode="after")
    def require_kind_specific_metadata(self) -> DocumentSitePage:
        route_fields = (
            self.document_type,
            self.classification,
            self.contract,
            self.route_order,
        )
        if self.kind == "root":
            if any(value is not None for value in route_fields):
                raise ValueError("root site pages cannot carry route metadata")
            if (
                self.source_path is not None
                or self.validity is not None
                or self.lifecycle is not None
                or self.generated
                or self.source_digest is not None
                or self.history_kind is not None
                or self.relations != DocumentRelations()
            ):
                raise ValueError("root site pages must be direct Markdown content")
            return self
        if self.kind == "legacy":
            if (
                self.document_type is None
                or self.classification is None
                or self.route_order is None
            ):
                raise ValueError("legacy site pages require their migration route metadata")
            if self.contract is not None:
                raise ValueError("legacy site pages cannot claim a validated content contract")
            if (
                self.source_path is not None
                or self.validity is not None
                or self.lifecycle is not None
                or self.generated
                or self.source_digest is not None
                or self.history_kind != "legacy"
                or self.relations != DocumentRelations()
            ):
                raise ValueError("legacy site pages must remain explicit legacy content")
            return self
        if any(value is None for value in route_fields):
            raise ValueError("routed site pages require complete route metadata")
        if self.kind == "manual":
            if self.contract != "markdown-frontmatter" or self.validity is None:
                raise ValueError("manual site pages require validated Markdown frontmatter")
            if (
                self.source_path is not None
                or self.lifecycle is not None
                or self.generated
                or self.source_digest is not None
            ):
                raise ValueError("manual site pages cannot claim a generated source pair")
            expected_history = (
                "archive"
                if self.document_type == "archive" or self.validity == "invalid"
                else None
            )
            if self.history_kind != expected_history:
                raise ValueError("manual site page history metadata is inconsistent")
            return self
        if (
            self.contract == "markdown-frontmatter"
            or self.validity is not None
            or not self.generated
            or self.source_path is None
            or self.source_digest is None
            or self.relations != DocumentRelations()
        ):
            raise ValueError("structured site pages require a canonical source/render pair")
        if self.contract in {"design-document", "project-status-summary"}:
            if self.lifecycle is None:
                raise ValueError("project structured site pages require lifecycle metadata")
        elif self.lifecycle is not None:
            raise ValueError("this structured contract does not define a lifecycle")
        expected_history = (
            "archive"
            if self.document_type == "archive"
            or self.lifecycle in {"superseded", "deprecated"}
            else None
        )
        if self.history_kind != expected_history:
            raise ValueError("structured site page history metadata is inconsistent")
        return self


class DocumentSiteExcludedPath(StrictModel):
    path: RepositoryPath
    reason: Literal[
        "structured_source",
        "legacy_non_markdown",
        "root_non_markdown",
    ]
    page_path: RepositoryPath | None = None

    @model_validator(mode="after")
    def bind_structured_source_to_page(self) -> DocumentSiteExcludedPath:
        if (self.reason == "structured_source") != (self.page_path is not None):
            raise ValueError("only structured source exclusions require page_path")
        return self


class DocumentSiteManifest(ProtocolRecord):
    manifest_kind: Literal["document_site_manifest"] = "document_site_manifest"
    document_root: RepositoryPath
    repository_head: GitObjectId | None = None
    repository_state: Literal["clean", "dirty"]
    repository_remote: NonEmptyStr | None = None
    policy_digest: Sha256Digest
    pages: tuple[DocumentSitePage, ...]
    excluded_paths: tuple[DocumentSiteExcludedPath, ...] = ()
    manifest_digest: Sha256Digest

    @model_validator(mode="after")
    def require_unique_paths_and_valid_digest(self) -> DocumentSiteManifest:
        page_paths = tuple(page.path for page in self.pages)
        excluded_paths = tuple(item.path for item in self.excluded_paths)
        if len(page_paths) != len(set(page_paths)):
            raise ValueError("document site page paths must be unique")
        if len(excluded_paths) != len(set(excluded_paths)):
            raise ValueError("document site excluded paths must be unique")
        if set(page_paths) & set(excluded_paths):
            raise ValueError("document site paths cannot be both published and excluded")
        structured_pairs = {
            (page.source_path, page.path)
            for page in self.pages
            if page.kind == "structured"
        }
        excluded_source_pairs = {
            (item.path, item.page_path)
            for item in self.excluded_paths
            if item.reason == "structured_source"
        }
        if structured_pairs != excluded_source_pairs:
            raise ValueError(
                "structured site pages and source exclusions must have a one-to-one mapping"
            )

        from researchctl.serialization import canonical_digest

        payload = self.model_dump(mode="json", exclude={"manifest_digest"})
        if self.manifest_digest != canonical_digest(payload):
            raise ValueError("document site manifest digest does not match its content")
        return self


#: A version 2 site page is one of three things, and nothing else.
SimpleDocumentSitePageKind = Literal["root", "ordinary", "structured"]

#: Lifecycle values that retire a structured page from the main navigation.
_RETIRED_LIFECYCLES = frozenset({"superseded", "deprecated"})
#: Statuses that retire an ordinary page from the main navigation.
_RETIRED_STATUSES = frozenset({"deprecated", "archived"})


class SimpleDocumentSiteSection(StrictModel):
    """One policy section. Tuple order in the manifest is policy order."""

    path: DocumentSlug


class SimpleDocumentSitePage(StrictModel):
    """One publishable page, described well enough to place it in a navigation.

    ``section`` and ``section_relative_path`` are the whole hierarchy: a site
    generator joins them to build nested navigation, so the manifest never
    carries a second, hand-maintained tree that could disagree with the files.
    """

    path: RepositoryPath
    kind: SimpleDocumentSitePageKind
    section: DocumentSlug | None = None
    section_relative_path: RepositoryPath | None = None
    source_path: RepositoryPath | None = None
    contract: StructuredDocumentSchema | None = None
    classification: DocumentLabel | None = None
    title: ShortText
    status: SimpleDocumentStatus | None = None
    lifecycle: DocumentLifecycle | None = None
    tags: tuple[HumanKey, ...] = ()
    owners: tuple[NonEmptyStr, ...] = ()
    reviewed_on: date | None = None
    locked: StrictBool = False
    depends_on: tuple[RepositoryPath, ...] = ()
    links: tuple[RepositoryPath, ...] = ()
    git_history_present: StrictBool
    last_edited_at: UtcDateTime | None = None
    content_digest: Sha256Digest
    source_digest: Sha256Digest | None = None
    in_history: StrictBool = False

    @model_validator(mode="after")
    def require_consistent_page_metadata(self) -> SimpleDocumentSitePage:
        for name, values in (
            ("tags", self.tags),
            ("owners", self.owners),
            ("depends_on", self.depends_on),
            ("links", self.links),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"site page {name} entries must be unique")

        # Git is the only source of edit time, so the timestamp and the flag
        # that says whether one exists can never disagree.
        if self.git_history_present != (self.last_edited_at is not None):
            raise ValueError("site page Git history flag and timestamp disagree")

        # The tree classifies by lowercased suffix, so README.MD is an ordinary
        # page there. The manifest matches that rule; disagreeing would make a
        # legal tree unbuildable.
        if PurePosixPath(self.path).suffix.lower() != ".md":
            raise ValueError("every site page is Markdown")

        structured_fields = (self.source_path, self.contract, self.source_digest)
        if self.kind == "structured":
            if any(value is None for value in structured_fields):
                raise ValueError("structured site pages require a canonical source pair")
            if self.status is not None:
                raise ValueError("structured site pages carry a lifecycle, not a status")
            # A structured contract states none of these, so a manifest that
            # claimed them would be inventing facts its source cannot support.
            if self.reviewed_on is not None or self.locked or self.depends_on:
                raise ValueError(
                    "structured contracts state no review date, lock, or dependency"
                )
            classified = self.contract in CLASSIFIED_STRUCTURED_SCHEMAS
            if classified and (self.classification is None or self.lifecycle is None):
                raise ValueError("structured envelopes require a classification and lifecycle")
            if not classified and (
                self.classification is not None or self.lifecycle is not None
            ):
                raise ValueError("this structured contract has no classification or lifecycle")
            retired = self.lifecycle in _RETIRED_LIFECYCLES
        else:
            if any(value is not None for value in structured_fields):
                raise ValueError("only structured site pages have a canonical source")
            if self.classification is not None or self.lifecycle is not None:
                raise ValueError("ordinary site pages carry no classification or lifecycle")
            if self.status is None:
                raise ValueError("ordinary site pages require a status")
            retired = self.status in _RETIRED_STATUSES
        if self.in_history != retired:
            raise ValueError("site page history placement does not follow its own status")

        located = (self.section, self.section_relative_path)
        if self.kind == "root":
            if any(value is not None for value in located):
                raise ValueError("root site pages sit directly below the document root")
            return self
        if any(value is None for value in located):
            raise ValueError("section site pages require a section and a relative path")
        if not self.path.endswith(f"/{self.section}/{self.section_relative_path}"):
            raise ValueError("site page path does not end in its section-relative path")
        return self


class SimpleDocumentSiteAsset(StrictModel):
    """One non-Markdown file published exactly as it stands."""

    path: RepositoryPath
    section: DocumentSlug
    section_relative_path: RepositoryPath
    content_digest: Sha256Digest

    @model_validator(mode="after")
    def bind_path_to_its_section(self) -> SimpleDocumentSiteAsset:
        if PurePosixPath(self.path).suffix.lower() == ".md":
            raise ValueError("Markdown is a page, never a static asset")
        if not self.path.endswith(f"/{self.section}/{self.section_relative_path}"):
            raise ValueError("site asset path does not end in its section-relative path")
        return self


class SimpleDocumentSiteExcludedPath(StrictModel):
    """One file inside the document root that is deliberately not published."""

    path: RepositoryPath
    reason: Literal["structured_source", "codeowners"]
    page_path: RepositoryPath | None = None

    @model_validator(mode="after")
    def bind_structured_source_to_page(self) -> SimpleDocumentSiteExcludedPath:
        if (self.reason == "structured_source") != (self.page_path is not None):
            raise ValueError("only structured source exclusions name a page")
        return self


class SimpleDocumentSiteManifest(ProtocolRecord):
    """An engine-neutral projection of a passing version 2 document tree.

    Every field restates something a source of truth already decided: the
    policy fixed the taxonomy, CODEOWNERS fixed review, and Git fixed edit
    time. The manifest is a build artifact of those, never a place to record
    something they do not say.
    """

    manifest_kind: Literal["simple_document_site_manifest"] = "simple_document_site_manifest"
    policy_version: Literal[2] = 2
    document_root: RepositoryPath
    repository_head: GitObjectId | None = None
    repository_state: Literal["clean", "dirty"]
    repository_remote: NonEmptyStr | None = None
    policy_digest: Sha256Digest
    sections: Annotated[tuple[SimpleDocumentSiteSection, ...], Field(min_length=1)]
    pages: tuple[SimpleDocumentSitePage, ...] = ()
    assets: tuple[SimpleDocumentSiteAsset, ...] = ()
    excluded_paths: tuple[SimpleDocumentSiteExcludedPath, ...] = ()
    manifest_digest: Sha256Digest

    @model_validator(mode="after")
    def require_consistent_tree_projection(self) -> SimpleDocumentSiteManifest:
        sections = tuple(section.path for section in self.sections)
        if len(sections) != len(set(sections)):
            raise ValueError("site manifest sections must be unique")

        page_paths = tuple(page.path for page in self.pages)
        asset_paths = tuple(asset.path for asset in self.assets)
        excluded_paths = tuple(item.path for item in self.excluded_paths)
        for name, paths in (
            ("page", page_paths),
            ("asset", asset_paths),
            ("excluded", excluded_paths),
        ):
            if len(paths) != len(set(paths)):
                raise ValueError(f"site manifest {name} paths must be unique")
        if (
            set(page_paths) & set(asset_paths)
            or set(page_paths) & set(excluded_paths)
            or set(asset_paths) & set(excluded_paths)
        ):
            raise ValueError("a site manifest path cannot hold two roles")

        structured_pairs = {
            (page.source_path, page.path)
            for page in self.pages
            if page.kind == "structured"
        }
        excluded_source_pairs = {
            (item.path, item.page_path)
            for item in self.excluded_paths
            if item.reason == "structured_source"
        }
        if structured_pairs != excluded_source_pairs:
            raise ValueError(
                "structured site pages and source exclusions must have a one-to-one mapping"
            )

        codeowners = tuple(
            item.path for item in self.excluded_paths if item.reason == "codeowners"
        )
        if len(codeowners) > 1:
            raise ValueError("only one CODEOWNERS file can govern one repository")

        root_prefix = f"{self.document_root}/"
        for item in self.excluded_paths:
            if not item.path.startswith(root_prefix):
                raise ValueError("a site manifest only excludes paths under its root")
        for page in self.pages:
            if page.kind == "root":
                if page.path.count("/") != root_prefix.count("/") or not page.path.startswith(
                    root_prefix
                ):
                    raise ValueError("root site pages are direct children of the document root")
                continue
            if page.section not in sections:
                raise ValueError("site page names a section the policy does not declare")
            if page.path != f"{root_prefix}{page.section}/{page.section_relative_path}":
                raise ValueError("site page path is not inside its declared section")
            if page.kind != "structured":
                continue
            expected_source = page.path.removesuffix(".md") + ".yaml"
            if page.source_path != expected_source:
                raise ValueError("a structured page and its canonical source must share a stem")
            if not page.source_path.startswith(f"{root_prefix}{page.section}/"):
                raise ValueError("a canonical source sits inside its page's section")
            if page.source_path.count("/") != root_prefix.count("/") + 1:
                raise ValueError("canonical sources are direct children of their section")
        for asset in self.assets:
            if asset.section not in sections:
                raise ValueError("site asset names a section the policy does not declare")
            if asset.path != f"{root_prefix}{asset.section}/{asset.section_relative_path}":
                raise ValueError("site asset path is not inside its declared section")

        from researchctl.serialization import canonical_digest

        payload = self.model_dump(mode="json", exclude={"manifest_digest"})
        if self.manifest_digest != canonical_digest(payload):
            raise ValueError("simple document site manifest digest does not match its content")
        return self


class DocumentEnvelope(ProtocolRecord):
    document_id: DocumentId
    document_kind: DocumentKind
    classification: DocumentLabel
    slug: DocumentSlug
    title: ShortText
    status: DocumentLifecycle = "draft"
    project_id: ProjectId | None = None
    basis_commit: GitObjectId
    revision: Annotated[StrictInt, Field(ge=1)] = 1
    authored_by: DocumentAuthor
    created_at: UtcDateTime
    updated_at: UtcDateTime
    acceptance: DocumentAcceptance | None = None
    supersedes: DocumentId | None = None
    tags: tuple[HumanKey, ...] = ()
    sources: tuple[DocumentReference, ...] = ()

    @model_validator(mode="after")
    def require_document_lineage(self) -> DocumentEnvelope:
        if self.updated_at < self.created_at:
            raise ValueError("document updated_at cannot precede created_at")
        if self.supersedes == self.document_id:
            raise ValueError("document cannot supersede itself")
        if len(self.tags) != len(set(self.tags)):
            raise ValueError("document tags must be unique")
        source_keys = tuple(source.key for source in self.sources)
        if len(source_keys) != len(set(source_keys)):
            raise ValueError("document source keys must be unique")
        if (self.status == "accepted") != (self.acceptance is not None):
            raise ValueError(
                "accepted documents require acceptance and other states forbid it"
            )
        return self


class DesignOption(StrictModel):
    key: HumanKey
    summary: NonEmptyStr
    benefits: Annotated[tuple[NonEmptyStr, ...], Field(min_length=1)]
    drawbacks: Annotated[tuple[NonEmptyStr, ...], Field(min_length=1)]
    disposition: Literal["selected", "rejected", "deferred"]
    rationale: NonEmptyStr


class DesignComponent(StrictModel):
    key: HumanKey
    responsibility: NonEmptyStr
    interfaces: tuple[NonEmptyStr, ...] = ()


class DesignWorkflow(StrictModel):
    name: ShortText
    steps: Annotated[tuple[NonEmptyStr, ...], Field(min_length=2)]


class DesignFailureMode(StrictModel):
    condition: NonEmptyStr
    behavior: NonEmptyStr
    recovery: NonEmptyStr


class DesignValidationCase(StrictModel):
    case: NonEmptyStr
    expected: NonEmptyStr
    evidence: NonEmptyStr


class DesignDocument(DocumentEnvelope):
    document_kind: Literal["design_document"] = "design_document"
    problem: NonEmptyStr
    context: NonEmptyStr
    goals: Annotated[tuple[NonEmptyStr, ...], Field(min_length=1)]
    non_goals: Annotated[tuple[NonEmptyStr, ...], Field(min_length=1)]
    constraints: Annotated[tuple[NonEmptyStr, ...], Field(min_length=1)]
    options: Annotated[tuple[DesignOption, ...], Field(min_length=2)]
    components: Annotated[tuple[DesignComponent, ...], Field(min_length=1)]
    workflows: Annotated[tuple[DesignWorkflow, ...], Field(min_length=1)]
    security_considerations: Annotated[
        tuple[NonEmptyStr, ...], Field(min_length=1)
    ]
    failure_modes: Annotated[tuple[DesignFailureMode, ...], Field(min_length=1)]
    migration_steps: Annotated[tuple[NonEmptyStr, ...], Field(min_length=1)]
    validation: Annotated[tuple[DesignValidationCase, ...], Field(min_length=1)]
    open_questions: tuple[NonEmptyStr, ...] = ()
    decision_requests: tuple[DecisionRequest, ...] = ()

    @model_validator(mode="after")
    def require_complete_design(self) -> DesignDocument:
        option_keys = tuple(option.key for option in self.options)
        component_keys = tuple(component.key for component in self.components)
        workflow_names = tuple(workflow.name for workflow in self.workflows)
        if len(option_keys) != len(set(option_keys)):
            raise ValueError("design option keys must be unique")
        if sum(option.disposition == "selected" for option in self.options) != 1:
            raise ValueError("design documents require exactly one selected option")
        if len(component_keys) != len(set(component_keys)):
            raise ValueError("design component keys must be unique")
        if len(workflow_names) != len(set(workflow_names)):
            raise ValueError("design workflow names must be unique")
        if self.status == "accepted" and self.open_questions:
            raise ValueError("accepted design documents cannot retain open questions")
        return self


CapabilityStatus = Literal[
    "verified_local",
    "partial",
    "deployment_pending",
    "designed",
    "blocked",
    "deprecated",
]


class StatusCapability(StrictModel):
    key: HumanKey
    title: ShortText
    status: CapabilityStatus
    summary: NonEmptyStr
    evidence_keys: Annotated[tuple[HumanKey, ...], Field(min_length=1)]
    missing: tuple[NonEmptyStr, ...] = ()

    @model_validator(mode="after")
    def require_status_evidence(self) -> StatusCapability:
        if len(self.evidence_keys) != len(set(self.evidence_keys)):
            raise ValueError("capability evidence_keys must be unique")
        incomplete = {"partial", "deployment_pending", "designed", "blocked"}
        if self.status in incomplete and not self.missing:
            raise ValueError(f"{self.status} capabilities must declare missing work")
        if self.status == "verified_local" and self.missing:
            raise ValueError("verified_local capabilities cannot declare missing work")
        return self


class StatusWorkItem(StrictModel):
    key: HumanKey
    summary: NonEmptyStr
    state: Literal["active", "blocked", "ready_for_review", "queued"]
    owner: ShortText
    next_action: NonEmptyStr


class StatusRisk(StrictModel):
    key: HumanKey
    severity: Literal["low", "medium", "high", "critical"]
    risk: NonEmptyStr
    mitigation: NonEmptyStr


class ProjectStatusSummary(DocumentEnvelope):
    document_kind: Literal["project_status_summary"] = "project_status_summary"
    as_of: UtcDateTime
    executive_summary: NonEmptyStr
    capabilities: Annotated[tuple[StatusCapability, ...], Field(min_length=1)]
    active_work: tuple[StatusWorkItem, ...] = ()
    risks: tuple[StatusRisk, ...] = ()
    decisions_needed: tuple[DecisionRequest, ...] = ()
    next_steps: Annotated[tuple[NonEmptyStr, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def require_traceable_current_state(self) -> ProjectStatusSummary:
        capability_keys = tuple(item.key for item in self.capabilities)
        work_keys = tuple(item.key for item in self.active_work)
        risk_keys = tuple(item.key for item in self.risks)
        if len(capability_keys) != len(set(capability_keys)):
            raise ValueError("status capability keys must be unique")
        if len(work_keys) != len(set(work_keys)):
            raise ValueError("status work item keys must be unique")
        if len(risk_keys) != len(set(risk_keys)):
            raise ValueError("status risk keys must be unique")
        declared_sources = {source.key for source in self.sources}
        used_sources = {
            key for capability in self.capabilities for key in capability.evidence_keys
        }
        if used_sources != declared_sources:
            raise ValueError(
                "status capability evidence must use every declared source exactly by key"
            )
        if self.as_of < self.updated_at:
            raise ValueError("status as_of cannot precede document updated_at")
        return self


ANALYSIS_BRIEF_QUESTION_PROSE_LIMITS = {
    "max_sentences": 2,
    "max_english_words": 40,
    "max_cjk_characters": 100,
}
ANALYSIS_BRIEF_ANSWER_PROSE_LIMITS = {
    "max_sentences": 2,
    "max_english_words": 60,
    "max_cjk_characters": 140,
}
ANALYSIS_BRIEF_LIST_ITEM_PROSE_LIMITS = {
    "max_sentences": 2,
    "max_english_words": 45,
    "max_cjk_characters": 120,
}
ANALYSIS_BRIEF_DOCUMENT_PROSE_LIMITS = {
    "max_english_words": 350,
    "max_cjk_characters": 700,
}


class AnalysisBrief(ProtocolRecord):
    model_config = ConfigDict(
        json_schema_extra={
            "x-researchctl-prose": {
                "scope": "document",
                **ANALYSIS_BRIEF_DOCUMENT_PROSE_LIMITS,
            }
        }
    )

    question: Annotated[
        ShortText,
        Field(
            description="Research question under the declared prose budget.",
            json_schema_extra={
                "x-researchctl-prose": {
                    "scope": "field",
                    **ANALYSIS_BRIEF_QUESTION_PROSE_LIMITS,
                }
            },
        ),
    ]
    answer: Annotated[
        ShortText,
        Field(
            validation_alias=AliasChoices("answer", "conclusion"),
            description=(
                "Evidence-supported answer; at most 2 sentences, 60 English words, "
                "or 140 CJK characters under researchctl lint. Legacy input may use "
                "conclusion, but canonical output uses answer."
            ),
            json_schema_extra={
                "x-researchctl-prose": {
                    "scope": "field",
                    **ANALYSIS_BRIEF_ANSWER_PROSE_LIMITS,
                }
            },
        ),
    ]
    protocol: ShortText
    metrics: Annotated[tuple[BriefMetric, ...], Field(min_length=1, max_length=5)]
    evidence: Annotated[tuple[BriefEvidence, ...], Field(min_length=1, max_length=8)]
    interpretation: Annotated[
        tuple[ShortText, ...],
        Field(
            max_length=3,
            description="Interpretation bullets; the prose budget applies to each item.",
            json_schema_extra={
                "x-researchctl-prose": {
                    "scope": "each_item",
                    **ANALYSIS_BRIEF_LIST_ITEM_PROSE_LIMITS,
                }
            },
        ),
    ] = ()
    limitations: Annotated[
        tuple[ShortText, ...],
        Field(
            max_length=3,
            description="Limitation bullets; the prose budget applies to each item.",
            json_schema_extra={
                "x-researchctl-prose": {
                    "scope": "each_item",
                    **ANALYSIS_BRIEF_LIST_ITEM_PROSE_LIMITS,
                }
            },
        ),
    ] = ()
    sources: Annotated[tuple[BriefSource, ...], Field(min_length=1, max_length=16)]

    @model_validator(mode="before")
    @classmethod
    def reject_ambiguous_answer_alias(cls, value: object) -> object:
        if isinstance(value, dict) and "answer" in value and "conclusion" in value:
            raise ValueError("analysis brief cannot define both answer and conclusion")
        return value

    @model_validator(mode="after")
    def require_rectangular_traceable_evidence(self) -> AnalysisBrief:
        metric_keys = tuple(metric.key for metric in self.metrics)
        if len(metric_keys) != len(set(metric_keys)):
            raise ValueError("analysis brief metric keys must be unique")
        source_keys = tuple(source.key for source in self.sources)
        if len(source_keys) != len(set(source_keys)):
            raise ValueError("analysis brief source keys must be unique")
        settings = tuple(row.setting for row in self.evidence)
        if len(settings) != len(set(settings)):
            raise ValueError("analysis brief settings must be unique")

        expected_metrics = set(metric_keys)
        declared_sources = set(source_keys)
        used_sources: set[str] = set()
        for row in self.evidence:
            if set(row.values) != expected_metrics:
                raise ValueError(
                    "every analysis brief setting must define every metric exactly once"
                )
            unknown_sources = set(row.source_keys) - declared_sources
            if unknown_sources:
                raise ValueError(
                    "analysis brief evidence references undeclared source keys"
                )
            used_sources.update(row.source_keys)
        if used_sources != declared_sources:
            raise ValueError("analysis brief sources must all be used by evidence")
        return self


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
    renderer_id: Literal["linear.accepted-result-markdown.v2"] = (
        "linear.accepted-result-markdown.v2"
    )
    renderer_version: Literal[2] = 2

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
    renderer_id: Literal["linear.accepted-result-markdown.v2"]
    renderer_version: Literal[2]
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
    report_renderer_id: Literal["research-report.v2"] = "research-report.v2"
    report_renderer_version: Literal[2] = 2
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


class ResearchUpdate(ProtocolRecord):
    event: Literal["started", "completed", "failed", "conclusion_changed"]
    session_id: SessionId
    session_label: HumanKey | None = None
    task_key: HumanKey | None = None
    source_commit: GitObjectId
    observed_at: UtcDateTime
    summary: ShortText
    evidence: Annotated[tuple[StatusEvidence, ...], Field(max_length=2)] = ()
    source: BriefSource
    next_action: ShortText | None = None

    @model_validator(mode="after")
    def require_unique_evidence_kinds(self) -> ResearchUpdate:
        kinds = tuple(item.kind for item in self.evidence)
        if len(kinds) != len(set(kinds)):
            raise ValueError("research update evidence kinds must be unique")
        return self


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
    | AnalysisBrief
    | ResearchUpdate
    | DesignDocument
    | ProjectStatusSummary
)
