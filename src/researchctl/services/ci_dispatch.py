from __future__ import annotations

import hashlib
import os
import re
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, Never, TypeVar

from pydantic import BaseModel, Field, StrictInt, ValidationError, model_validator

from researchctl.adapters.git_ci import (
    GitCIObjectReader,
    GitCommitData,
    GitTreeChange,
)
from researchctl.config import ProjectConfig, dump_project_config
from researchctl.constants import (
    LINEAR_PROJECTION_POLICY_PATH,
    PROJECT_CONFIG_NAME,
    PROJECT_POLICY_PATH,
    PROTOCOL_VERSION,
    __version__,
)
from researchctl.domain.enums import ProjectState, TaskState
from researchctl.domain.models import (
    CIValidationAttestation,
    CIValidationCheck,
    ImpactDecision,
    LinearProjectionPolicy,
    ProjectPolicy,
    ProjectRecord,
    ReportImpact,
    ReportImpactBatch,
    StrictModel,
    TaskRecord,
)
from researchctl.domain.types import (
    AttestationId,
    GitObjectId,
    Sha256Digest,
    ShortText,
    UtcDateTime,
)
from researchctl.errors import RCPError
from researchctl.schema import generate_schema_files, schema_manifest_digest
from researchctl.serialization import (
    SerializationError,
    canonical_digest,
    dump_yaml,
    load_yaml,
)
from researchctl.services.ci_validation import (
    CI_CHECK_IDENTITY,
    CI_WORKFLOW_ID,
    CIValidationRequest,
    CIValidationResult,
    ExactHeadCIValidator,
)
from researchctl.services.git_report_impact import GitReportImpactAnalyzer
from researchctl.services.impact_decision import ImpactDecisionBuilder
from researchctl.services.task_policy import task_transition_allowed

CI_DISPATCHER_ID = "researchctl.ci.dispatch.v1"
CI_DISPATCHER_VERSION = __version__
_PROJECT_PATH = ".research/project.yaml"
_POLICY_PATH = ".research/policies/default.yaml"
_SCHEMA_MANIFEST_PATH = ".research/schemas/manifest.json"
_MANAGED_DIRECTORIES = (
    "bootstrap",
    "tasks",
    "runs",
    "submissions",
    "decisions",
    "reports",
    "impacts",
)
_SUBMISSION_RECORD = re.compile(
    r"^\.research/submissions/"
    r"(submission_\d{8}T\d{6}Z_[0-9a-f]{24})/submission\.yaml$"
)
_TASK_RECORD = re.compile(
    r"^\.research/tasks/(task_\d{8}T\d{6}Z_[0-9a-f]{24})\.yaml$"
)
_IMPACT_RECORD = re.compile(
    r"^\.research/impacts/"
    r"(impact_\d{8}T\d{6}Z_[0-9a-f]{24})/impact\.yaml$"
)
_IMPACT_BATCH_RECORD = re.compile(
    r"^\.research/impacts/"
    r"(impact_\d{8}T\d{6}Z_[0-9a-f]{24})/impact-batch\.yaml$"
)
_IMPACT_DECISION_BRANCH = re.compile(
    r"^research/impact-decision/"
    r"(decision_\d{8}T\d{6}Z_[0-9a-f]{24})$"
)
_IMPACT_DECISION_MARKER = re.compile(
    r"^researchctl: impact\.decide "
    r"(decision_\d{8}T\d{6}Z_[0-9a-f]{24}) "
    r"(operation_\d{8}T\d{6}Z_[0-9a-f]{24})$"
)
_IMPACT_MARKER = re.compile(
    r"^researchctl: impact\.(create|batch) "
    r"(impact_\d{8}T\d{6}Z_[0-9a-f]{24}) "
    r"(operation_\d{8}T\d{6}Z_[0-9a-f]{24})\n\n"
    r"manifest-digest: (sha256:[0-9a-f]{64})$"
)
_TASK_MARKER = re.compile(
    r"^researchctl: (task\.(?:create|update|cancel)) "
    r"(operation_\d{8}T\d{6}Z_[0-9a-f]{24})$"
)
_LINEAR_POLICY_MARKER = re.compile(
    r"^researchctl: linear\.configure "
    r"(operation_\d{8}T\d{6}Z_[0-9a-f]{24})$"
)
_PLAN_REVIEW_POLICY_MARKER = re.compile(
    r"^researchctl: plan\.configure-review "
    r"(operation_\d{8}T\d{6}Z_[0-9a-f]{24})$"
)
_DOCUMENT_LAYOUT_POLICY_MARKER = re.compile(
    r"^researchctl: doc\.configure-layout "
    r"(operation_\d{8}T\d{6}Z_[0-9a-f]{24})$"
)
_GITHUB_GOVERNANCE_POLICY_MARKER = re.compile(
    r"^researchctl: github\.configure-governance "
    r"(operation_\d{8}T\d{6}Z_[0-9a-f]{24})$"
)
_BOOTSTRAP_PROPOSAL_MARKER = re.compile(
    r"^researchctl: bootstrap proposal "
    r"(bootstrap_\d{8}T\d{6}Z_[0-9a-f]{24})\n\n"
    r"operation-id: (operation_\d{8}T\d{6}Z_[0-9a-f]{24})\n\n"
    r"manifest-digest: (sha256:[0-9a-f]{64})$"
)
_BOOTSTRAP_ACCEPTANCE_MARKER = re.compile(
    r"^researchctl: bootstrap\.accept "
    r"(operation_\d{8}T\d{6}Z_[0-9a-f]{24})\n\n"
    r"manifest-digest: (sha256:[0-9a-f]{64})$"
)
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,511}$")
_MAX_DISPATCH_ARTIFACT_BYTES = 8 * 1024 * 1024
_ModelT = TypeVar("_ModelT", bound=BaseModel)
PRType = Literal[
    "submission",
    "impact",
    "impact_decision",
    "task_control",
    "linear_policy_control",
    "plan_review_policy_control",
    "document_layout_policy_control",
    "github_governance_policy_control",
    "bootstrap_proposal",
    "bootstrap_acceptance",
    "ordinary_source",
]


class CIPRDispatchRequest(StrictModel):
    attestation_id: AttestationId
    repository: ShortText
    pull_request_number: StrictInt = Field(ge=1)
    subject_head: GitObjectId
    base_commit: GitObjectId
    head_ref: ShortText
    base_ref: ShortText
    generated_at: UtcDateTime


class CIPRDispatchAttestation(StrictModel):
    envelope_version: Literal["1"] = "1"
    attestation_id: AttestationId
    repository: ShortText
    pull_request_number: StrictInt = Field(ge=1)
    subject_head: GitObjectId
    subject_tree: GitObjectId
    base_commit: GitObjectId
    head_ref: ShortText
    base_ref: ShortText
    dispatcher_id: Literal["researchctl.ci.dispatch.v1"] = CI_DISPATCHER_ID
    dispatcher_version: ShortText = CI_DISPATCHER_VERSION
    workflow_id: ShortText = CI_WORKFLOW_ID
    check_identity: ShortText = CI_CHECK_IDENTITY
    pr_type: PRType
    applicability: Literal["validated", "not_applicable"]
    checks: tuple[CIValidationCheck, ...] = Field(min_length=1)
    submission_attestation: CIValidationAttestation | None = None
    generated_at: UtcDateTime
    overall_result: Literal["passed", "failed"] = "passed"

    @model_validator(mode="after")
    def require_consistent_result(self) -> CIPRDispatchAttestation:
        names = [item.name for item in self.checks]
        if names != sorted(names) or len(names) != len(set(names)):
            raise ValueError("dispatch checks must be unique and sorted")
        expected = (
            "passed"
            if all(item.status == "passed" for item in self.checks)
            else "failed"
        )
        if self.overall_result != expected:
            raise ValueError("overall_result must match dispatch checks")
        if (self.pr_type == "submission") != (
            self.submission_attestation is not None
        ):
            raise ValueError("only Submission dispatch embeds an exact-head attestation")
        nested = self.submission_attestation
        if nested is not None:
            bindings = (
                ("attestation_id", self.attestation_id, nested.attestation_id),
                ("repository", self.repository, nested.repository),
                (
                    "pull_request_number",
                    self.pull_request_number,
                    nested.pull_request_number,
                ),
                ("subject_head", self.subject_head, nested.subject_head),
                ("subject_tree", self.subject_tree, nested.subject_tree),
                ("base_commit", self.base_commit, nested.base_commit),
                ("workflow_id", self.workflow_id, nested.workflow_id),
                ("check_identity", self.check_identity, nested.check_identity),
                ("generated_at", self.generated_at, nested.generated_at),
            )
            if any(outer != inner for _, outer, inner in bindings):
                raise ValueError(
                    "embedded Submission attestation identity differs from envelope"
                )
        if (self.pr_type == "ordinary_source") != (
            self.applicability == "not_applicable"
        ):
            raise ValueError("only ordinary source changes are not applicable")
        return self


@dataclass(frozen=True, slots=True)
class CIPRDispatchResult:
    attestation: CIPRDispatchAttestation
    exact_result: CIValidationResult | None = None

    @property
    def attestation_bytes(self) -> bytes:
        return dump_yaml(self.attestation).encode("utf-8")

    def as_dict(self) -> dict[str, object]:
        return {
            "pr_type": self.attestation.pr_type,
            "applicability": self.attestation.applicability,
            "attestation": self.attestation.model_dump(
                mode="json",
                exclude_none=True,
            ),
            "attestation_size_bytes": len(self.attestation_bytes),
            "exact_head_kind": (
                self.exact_result.head_kind
                if self.exact_result is not None
                else None
            ),
        }


@dataclass(frozen=True, slots=True)
class CIPRDispatchArtifactReceipt:
    path: Path
    content_digest: str
    size_bytes: int
    created: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "content_digest": self.content_digest,
            "size_bytes": self.size_bytes,
            "created": self.created,
        }


@dataclass(frozen=True, slots=True)
class _ManagedBase:
    config: ProjectConfig
    project: ProjectRecord
    policy: ProjectPolicy


class ProtectedBasePRDispatcher:
    """Classify and validate an exact PR head using protected-base code only."""

    def __init__(
        self,
        *,
        git: GitCIObjectReader | None = None,
        submissions: ExactHeadCIValidator | None = None,
        impacts: GitReportImpactAnalyzer | None = None,
    ) -> None:
        self.git = git or GitCIObjectReader()
        self.submissions = submissions or ExactHeadCIValidator()
        self.impacts = impacts or GitReportImpactAnalyzer(git=self.git)
        self.impact_decisions = ImpactDecisionBuilder()

    def validate(
        self,
        repository_root: Path,
        request: CIPRDispatchRequest,
    ) -> CIPRDispatchResult:
        root = repository_root
        self._require_ref(request.head_ref, label="head")
        self._require_ref(request.base_ref, label="base")
        base = self.git.read_commit(root, request.base_commit)
        head = self.git.read_commit(root, request.subject_head)
        if not self.git.is_ancestor(
            root,
            ancestor=base.object_id,
            descendant=head.object_id,
        ):
            self._invalid(
                "ci_dispatch_lineage_invalid",
                "The exact PR head does not descend from the exact protected base.",
            )
        changes = self.git.changes(
            root,
            old_commit=base.object_id,
            new_commit=head.object_id,
        )
        self._require_canonical_paths(changes)
        changed_paths = tuple(item.path for item in changes)
        submission_ids = {
            matched.group(1)
            for path in changed_paths
            if (matched := _SUBMISSION_RECORD.fullmatch(path)) is not None
        }
        impact_ids = {
            matched.group(1)
            for path in changed_paths
            if (
                matched := (
                    _IMPACT_RECORD.fullmatch(path)
                    or _IMPACT_BATCH_RECORD.fullmatch(path)
                )
            )
            is not None
        }
        task_paths = tuple(
            path for path in changed_paths if _TASK_RECORD.fullmatch(path)
        )
        linear_policy_paths = tuple(
            path
            for path in changed_paths
            if path == LINEAR_PROJECTION_POLICY_PATH
        )
        project_policy_paths = tuple(
            path for path in changed_paths if path == PROJECT_POLICY_PATH
        )
        protocol_paths = tuple(
            path for path in changed_paths if self._is_protocol_path(path)
        )
        decision_branch = _IMPACT_DECISION_BRANCH.fullmatch(request.head_ref)

        exact_result: CIValidationResult | None = None
        if impact_ids:
            if len(impact_ids) != 1:
                self._unknown_protocol_change(changed_paths)
            impact_id = next(iter(impact_ids))
            self._require_branch(
                request.head_ref,
                f"research/impact/{impact_id}",
                kind="Impact",
            )
            evidence = self._validate_impact(
                root,
                request=request,
                base=base,
                head=head,
                changes=changes,
                impact_id=impact_id,
            )
            pr_type: PRType = "impact"
        elif decision_branch is not None:
            decision_id = decision_branch.group(1)
            evidence = self._validate_impact_decision(
                root,
                request=request,
                base=base,
                head=head,
                changes=changes,
                decision_id=decision_id,
            )
            pr_type = "impact_decision"
        elif submission_ids:
            if len(submission_ids) != 1:
                self._unknown_protocol_change(changed_paths)
            submission_id = next(iter(submission_ids))
            self._require_branch(
                request.head_ref,
                f"research/submission/{submission_id}",
                kind="Submission",
            )
            exact_result = self.submissions.validate(
                root,
                CIValidationRequest(
                    attestation_id=request.attestation_id,
                    repository=request.repository,
                    pull_request_number=request.pull_request_number,
                    subject_head=head.object_id,
                    base_commit=base.object_id,
                    submission_id=submission_id,
                    generated_at=request.generated_at,
                ),
            )
            pr_type: PRType = "submission"
            evidence = {
                "exact_head_validation": canonical_digest(
                    exact_result.attestation
                ),
                "pr_type_dispatch": self._dispatch_digest(
                    changes,
                    pr_type=pr_type,
                    identity=submission_id,
                ),
            }
        elif task_paths:
            evidence = self._validate_task_control(
                root,
                request=request,
                base=base,
                head=head,
                changes=changes,
            )
            pr_type = "task_control"
        elif (
            project_policy_paths
            and _PLAN_REVIEW_POLICY_MARKER.fullmatch(
                head.message.rstrip("\n")
            )
            is not None
        ):
            evidence = self._validate_plan_review_policy_control(
                root,
                request=request,
                base=base,
                head=head,
                changes=changes,
            )
            pr_type = "plan_review_policy_control"
        elif (
            project_policy_paths
            and _DOCUMENT_LAYOUT_POLICY_MARKER.fullmatch(
                head.message.rstrip("\n")
            )
            is not None
        ):
            evidence = self._validate_document_layout_policy_control(
                root,
                request=request,
                base=base,
                head=head,
                changes=changes,
            )
            pr_type = "document_layout_policy_control"
        elif (
            project_policy_paths
            and _GITHUB_GOVERNANCE_POLICY_MARKER.fullmatch(
                head.message.rstrip("\n")
            )
            is not None
        ):
            evidence = self._validate_github_governance_policy_control(
                root,
                request=request,
                base=base,
                head=head,
                changes=changes,
            )
            pr_type = "github_governance_policy_control"
        elif linear_policy_paths:
            evidence = self._validate_linear_policy_control(
                root,
                request=request,
                base=base,
                head=head,
                changes=changes,
            )
            pr_type = "linear_policy_control"
        elif protocol_paths:
            proposal = _BOOTSTRAP_PROPOSAL_MARKER.fullmatch(
                head.message.rstrip("\n")
            )
            acceptance = _BOOTSTRAP_ACCEPTANCE_MARKER.fullmatch(
                head.message.rstrip("\n")
            )
            if proposal is not None:
                evidence = self._validate_bootstrap_proposal(
                    root,
                    request=request,
                    base=base,
                    head=head,
                    changes=changes,
                    marker=proposal,
                )
                pr_type = "bootstrap_proposal"
            elif acceptance is not None:
                evidence = self._validate_bootstrap_acceptance(
                    root,
                    request=request,
                    base=base,
                    head=head,
                    marker=acceptance,
                )
                pr_type = "bootstrap_acceptance"
            else:
                self._unknown_protocol_change(changed_paths)
        else:
            pr_type = "ordinary_source"
            evidence = {
                "protocol_path_absence": self._dispatch_digest(
                    changes,
                    pr_type=pr_type,
                    identity="none",
                )
            }

        checks = self._checks(evidence)
        attestation = CIPRDispatchAttestation(
            attestation_id=request.attestation_id,
            repository=request.repository,
            pull_request_number=request.pull_request_number,
            subject_head=head.object_id,
            subject_tree=head.tree,
            base_commit=base.object_id,
            head_ref=request.head_ref,
            base_ref=request.base_ref,
            pr_type=pr_type,
            applicability=(
                "not_applicable"
                if pr_type == "ordinary_source"
                else "validated"
            ),
            checks=checks,
            submission_attestation=(
                exact_result.attestation if exact_result is not None else None
            ),
            generated_at=request.generated_at,
            overall_result="passed",
        )
        return CIPRDispatchResult(
            attestation=attestation,
            exact_result=exact_result,
        )

    def _validate_impact(
        self,
        root: Path,
        *,
        request: CIPRDispatchRequest,
        base: GitCommitData,
        head: GitCommitData,
        changes: tuple[GitTreeChange, ...],
        impact_id: str,
    ) -> dict[str, Sha256Digest]:
        marker = _IMPACT_MARKER.fullmatch(head.message.rstrip("\n"))
        if (
            marker is None
            or marker.group(2) != impact_id
            or head.parents != (base.object_id,)
        ):
            self._invalid(
                "ci_impact_commit_invalid",
                "Impact proposal must be one marked commit over the exact protected base.",
            )
        impact_path = f".research/impacts/{impact_id}/impact.yaml"
        batch_path = f".research/impacts/{impact_id}/impact-batch.yaml"
        changed_paths = {item.path for item in changes}
        has_single = impact_path in changed_paths
        has_batch = batch_path in changed_paths
        if has_single == has_batch:
            self._invalid(
                "ci_impact_scope_invalid",
                "Impact proposal must contain exactly one canonical Impact record.",
            )
        if has_batch:
            if marker.group(1) != "batch":
                self._invalid(
                    "ci_impact_commit_invalid",
                    "Batched Impact output requires the impact.batch marker.",
                )
            batch = self._record(
                root,
                commit=head.object_id,
                path=batch_path,
                model_type=ReportImpactBatch,
            )
            if batch.impact_id != impact_id:
                self._invalid(
                    "ci_impact_identity_mismatch",
                    "Impact identity does not match its canonical path.",
                )
            if (
                batch.target_commit != base.object_id
                or batch.target_tree != base.tree
            ):
                self._invalid(
                    "ci_impact_target_mismatch",
                    "Impact batch is not bound to the exact protected base commit and tree.",
                )
            analysis = self.impacts.scan(
                root,
                impact_id=batch.impact_id,
                before_commit=batch.before_commit,
                target_commit=base.object_id,
                generated_at=batch.generated_at,
            )
            bundle = analysis.bundle
            if bundle is None:
                self._invalid(
                    "ci_impact_batch_empty",
                    "Impact batch does not regenerate any Report proposal.",
                )
            record_digest = batch.batch_digest
        else:
            if marker.group(1) != "create":
                self._invalid(
                    "ci_impact_commit_invalid",
                    "Single-Report Impact output requires the impact.create marker.",
                )
            impact = self._record(
                root,
                commit=head.object_id,
                path=impact_path,
                model_type=ReportImpact,
            )
            if impact.impact_id != impact_id:
                self._invalid(
                    "ci_impact_identity_mismatch",
                    "Impact identity does not match its canonical path.",
                )
            if (
                impact.target_commit != base.object_id
                or impact.target_tree != base.tree
            ):
                self._invalid(
                    "ci_impact_target_mismatch",
                    "Impact record is not bound to the exact protected base commit and tree.",
                )
            bundle = self.impacts.analyze(
                root,
                impact_id=impact.impact_id,
                report_id=impact.report_id,
                expected_report_revision=impact.expected_report_revision,
                target_commit=base.object_id,
                generated_at=impact.generated_at,
            )
            record_digest = impact.impact_digest
        expected_paths = tuple(item.path for item in bundle.files)
        self._require_exact_changes(
            changes,
            {path: ("000000", "100644", "A") for path in expected_paths},
            code="ci_impact_scope_invalid",
        )
        for rendered in bundle.files:
            if self._blob(
                root,
                commit=head.object_id,
                path=rendered.path,
            ) != rendered.content:
                self._invalid(
                    "ci_impact_generated_output_mismatch",
                    "Impact proposal does not match protected-base regeneration.",
                )
        if marker.group(4) != bundle.manifest_digest:
            self._invalid(
                "ci_impact_manifest_mismatch",
                "Impact commit marker does not match the regenerated manifest.",
            )
        managed = self._managed_base(
            root,
            commit=base.object_id,
            base_ref=request.base_ref,
        )
        return {
            "impact_record": record_digest,
            "impact_regeneration": bundle.manifest_digest,
            "pr_type_dispatch": self._dispatch_digest(
                changes,
                pr_type="impact",
                identity=impact_id,
            ),
            "trusted_base": canonical_digest(
                {
                    "project_id": managed.project.project_id,
                    "schema_manifest_digest": managed.config.schema_manifest_digest,
                }
            ),
        }

    def _validate_impact_decision(
        self,
        root: Path,
        *,
        request: CIPRDispatchRequest,
        base: GitCommitData,
        head: GitCommitData,
        changes: tuple[GitTreeChange, ...],
        decision_id: str,
    ) -> dict[str, Sha256Digest]:
        marker = _IMPACT_DECISION_MARKER.fullmatch(head.message.rstrip("\n"))
        if (
            marker is None
            or marker.group(1) != decision_id
            or head.parents != (base.object_id,)
        ):
            self._invalid(
                "ci_impact_decision_commit_invalid",
                "Impact decision must be one marked commit over protected main.",
            )
        decision_path = f".research/decisions/{decision_id}.yaml"
        decision = self._record(
            root,
            commit=head.object_id,
            path=decision_path,
            model_type=ImpactDecision,
        )
        if decision.decision_id != decision_id:
            self._invalid(
                "ci_impact_decision_identity_mismatch",
                "ImpactDecision identity does not match its canonical path.",
            )
        if (
            decision.decision_base_commit != base.object_id
            or decision.decision_base_tree != base.tree
        ):
            self._invalid(
                "ci_impact_decision_base_mismatch",
                "ImpactDecision does not bind the exact protected base.",
            )
        impact = self.impacts.load_impact(
            root,
            commit=base.object_id,
            impact_id=decision.impact_id,
            report_id=decision.report_id,
        )
        if (
            decision.expected_impact_digest != impact.impact_digest
            or decision.impact_target_commit != impact.target_commit
            or decision.impact_target_tree != impact.target_tree
        ):
            self._invalid(
                "ci_impact_decision_source_mismatch",
                "ImpactDecision does not bind the accepted Impact identity.",
            )
        report = self.impacts.load_latest_report(
            root,
            commit=base.object_id,
            report_id=decision.report_id,
        )
        bundle = self.impact_decisions.build(
            impact=impact,
            report=report,
            decision_id=decision.decision_id,
            expected_report_revision=decision.expected_report_revision,
            decision_base_commit=base.object_id,
            decision_base_tree=base.tree,
            disposition=decision.disposition,
            reviewer_actor=decision.reviewer_actor,
            reason=decision.reason,
            decided_at=decision.decided_at,
            rerun_task_id=decision.rerun_task_id,
            replacement_dependencies=decision.replacement_dependencies,
        )
        expected_paths = tuple(item.path for item in bundle.files)
        self._require_exact_changes(
            changes,
            {path: ("000000", "100644", "A") for path in expected_paths},
            code="ci_impact_decision_scope_invalid",
        )
        for rendered in bundle.files:
            if self._blob(
                root,
                commit=head.object_id,
                path=rendered.path,
            ) != rendered.content:
                self._invalid(
                    "ci_impact_decision_output_mismatch",
                    "ImpactDecision output differs from protected-base regeneration.",
                )
        task_binding: dict[str, object] = {"rerun_task_id": None}
        if decision.rerun_task_id is not None:
            tasks = self._task_records(root, commit=base.object_id)
            task = tasks.get(decision.rerun_task_id)
            if task is None or task.state not in {TaskState.PLANNED, TaskState.READY}:
                self._invalid(
                    "ci_impact_rerun_task_invalid",
                    "Rerun decision must reference a planned or ready accepted Task.",
                )
            task_binding = {
                "rerun_task_id": task.task_id,
                "task_digest": canonical_digest(task),
            }
        managed = self._managed_base(
            root,
            commit=base.object_id,
            base_ref=request.base_ref,
        )
        return {
            "impact_decision_record": decision.decision_digest,
            "impact_decision_regeneration": bundle.manifest_digest,
            "impact_decision_task": canonical_digest(task_binding),
            "pr_type_dispatch": self._dispatch_digest(
                changes,
                pr_type="impact_decision",
                identity=decision_id,
            ),
            "trusted_base": canonical_digest(
                {
                    "project_id": managed.project.project_id,
                    "schema_manifest_digest": managed.config.schema_manifest_digest,
                }
            ),
        }

    def _validate_task_control(
        self,
        root: Path,
        *,
        request: CIPRDispatchRequest,
        base: GitCommitData,
        head: GitCommitData,
        changes: tuple[GitTreeChange, ...],
    ) -> dict[str, Sha256Digest]:
        marker = _TASK_MARKER.fullmatch(head.message.rstrip("\n"))
        if marker is None or len(head.parents) != 1 or head.parents[0] != base.object_id:
            self._invalid(
                "ci_task_control_commit_invalid",
                "Task control must be one marked commit over the exact protected base.",
            )
        command, operation_id = marker.groups()
        self._require_branch(
            request.head_ref,
            f"research/control/{operation_id}",
            kind="Task control",
        )
        if len(changes) != 1:
            self._invalid(
                "ci_task_control_scope_invalid",
                "Task control must change exactly one canonical Task record.",
            )
        change = changes[0]
        path_match = _TASK_RECORD.fullmatch(change.path)
        if path_match is None:
            self._invalid(
                "ci_task_control_scope_invalid",
                "Task control changed a non-Task path.",
            )
        task_id = path_match.group(1)
        managed = self._managed_base(
            root,
            commit=base.object_id,
            base_ref=request.base_ref,
        )
        task = self._record(
            root,
            commit=head.object_id,
            path=change.path,
            model_type=TaskRecord,
        )
        if task.task_id != task_id:
            self._invalid(
                "ci_task_identity_mismatch",
                "Task identity does not match its canonical path.",
            )
        current_content = self.git.read_blob_at(
            root,
            commit=base.object_id,
            path=change.path,
            required=False,
        )
        current = (
            None
            if current_content is None
            else self._parse_canonical(
                current_content,
                model_type=TaskRecord,
                path=change.path,
            )
        )
        if command == "task.create":
            self._require_change_mode(change, ("000000", "100644", "A"))
            if current is not None or task.state is not TaskState.PLANNED:
                self._invalid(
                    "ci_task_create_invalid",
                    "A new Task must be absent from base and begin planned.",
                )
        else:
            self._require_change_mode(change, ("100644", "100644", "M"))
            if current is None:
                self._invalid(
                    "ci_task_update_invalid",
                    "Task update or cancellation requires an accepted base record.",
                )
            if current.task_id != task.task_id or current.key != task.key:
                self._invalid(
                    "ci_task_identity_immutable",
                    "Task ID and key are immutable.",
                )
            if current.created_at != task.created_at:
                self._invalid(
                    "ci_task_creation_time_immutable",
                    "Task created_at is immutable.",
                )
            if command == "task.update":
                if not task_transition_allowed(current.state, task.state):
                    self._invalid(
                        "ci_task_transition_invalid",
                        "Task update contains an invalid state transition.",
                    )
                if task.updated_at <= current.updated_at:
                    self._invalid(
                        "ci_task_update_time_invalid",
                        "A changed Task must advance updated_at.",
                    )
            else:
                self._validate_task_cancellation(current=current, replacement=task)

        records = self._task_records(root, commit=head.object_id)
        if records.get(task.task_id) != task:
            self._invalid(
                "ci_task_store_invalid",
                "The exact head Task store does not contain the changed Task.",
            )
        keys = [item.key for item in records.values()]
        if len(keys) != len(set(keys)):
            self._invalid(
                "ci_task_key_conflict",
                "Accepted Task keys must remain unique.",
            )
        configured_domains = {
            item.execution_domain for item in managed.policy.execution_domains
        }
        if task.execution_domain not in configured_domains:
            self._invalid(
                "ci_task_execution_domain_invalid",
                "Task execution domain is absent from accepted Project policy.",
            )
        if task.parent_task_id is not None and task.parent_task_id not in records:
            self._invalid(
                "ci_task_parent_missing",
                "Task parent does not exist at the exact head.",
            )
        return {
            "pr_type_dispatch": self._dispatch_digest(
                changes,
                pr_type="task_control",
                identity=operation_id,
            ),
            "task_record": canonical_digest(task),
            "task_transition": canonical_digest(
                {
                    "command": command,
                    "from": current.state.value if current is not None else None,
                    "to": task.state.value,
                }
            ),
            "trusted_base": canonical_digest(
                {
                    "project_id": managed.project.project_id,
                    "schema_manifest_digest": managed.config.schema_manifest_digest,
                }
            ),
        }

    def _validate_linear_policy_control(
        self,
        root: Path,
        *,
        request: CIPRDispatchRequest,
        base: GitCommitData,
        head: GitCommitData,
        changes: tuple[GitTreeChange, ...],
    ) -> dict[str, Sha256Digest]:
        marker = _LINEAR_POLICY_MARKER.fullmatch(head.message.rstrip("\n"))
        if marker is None or head.parents != (base.object_id,):
            self._invalid(
                "ci_linear_policy_commit_invalid",
                "Linear policy control must be one marked commit over protected base.",
            )
        operation_id = marker.group(1)
        self._require_branch(
            request.head_ref,
            f"research/control/{operation_id}",
            kind="Linear policy control",
        )
        if len(changes) != 1 or changes[0].path != LINEAR_PROJECTION_POLICY_PATH:
            self._invalid(
                "ci_linear_policy_scope_invalid",
                "Linear policy control must change only its fixed policy path.",
            )

        managed = self._managed_base(
            root,
            commit=base.object_id,
            base_ref=request.base_ref,
        )
        current_content = self.git.read_blob_at(
            root,
            commit=base.object_id,
            path=LINEAR_PROJECTION_POLICY_PATH,
            required=False,
        )
        if current_content is None:
            transition = "create"
            expected_mode = ("000000", "100644", "A")
            previous_digest = None
        else:
            transition = "update"
            expected_mode = ("100644", "100644", "M")
            previous = self._parse_canonical(
                current_content,
                model_type=LinearProjectionPolicy,
                path=LINEAR_PROJECTION_POLICY_PATH,
            )
            previous_digest = canonical_digest(previous)
        self._require_exact_changes(
            changes,
            {LINEAR_PROJECTION_POLICY_PATH: expected_mode},
            code="ci_linear_policy_scope_invalid",
        )
        policy = self._record(
            root,
            commit=head.object_id,
            path=LINEAR_PROJECTION_POLICY_PATH,
            model_type=LinearProjectionPolicy,
        )
        return {
            "linear_policy": canonical_digest(policy),
            "linear_policy_transition": canonical_digest(
                {
                    "transition": transition,
                    "previous_digest": previous_digest,
                    "policy_digest": canonical_digest(policy),
                }
            ),
            "pr_type_dispatch": self._dispatch_digest(
                changes,
                pr_type="linear_policy_control",
                identity=operation_id,
            ),
            "trusted_base": canonical_digest(
                {
                    "project_id": managed.project.project_id,
                    "schema_manifest_digest": managed.config.schema_manifest_digest,
                }
            ),
        }

    def _validate_plan_review_policy_control(
        self,
        root: Path,
        *,
        request: CIPRDispatchRequest,
        base: GitCommitData,
        head: GitCommitData,
        changes: tuple[GitTreeChange, ...],
    ) -> dict[str, Sha256Digest]:
        marker = _PLAN_REVIEW_POLICY_MARKER.fullmatch(
            head.message.rstrip("\n")
        )
        if marker is None or head.parents != (base.object_id,):
            self._invalid(
                "ci_plan_review_policy_commit_invalid",
                "Plan review policy control must be one marked commit over protected base.",
            )
        operation_id = marker.group(1)
        self._require_branch(
            request.head_ref,
            f"research/control/{operation_id}",
            kind="Plan review policy control",
        )
        self._require_exact_changes(
            changes,
            {PROJECT_POLICY_PATH: ("100644", "100644", "M")},
            code="ci_plan_review_policy_scope_invalid",
        )

        managed = self._managed_base(
            root,
            commit=base.object_id,
            base_ref=request.base_ref,
        )
        previous = self._record(
            root,
            commit=base.object_id,
            path=PROJECT_POLICY_PATH,
            model_type=ProjectPolicy,
        )
        replacement = self._record(
            root,
            commit=head.object_id,
            path=PROJECT_POLICY_PATH,
            model_type=ProjectPolicy,
        )
        if replacement.plan_review is None:
            self._invalid(
                "ci_plan_review_policy_missing",
                "Plan review policy control must configure an explicit reviewer.",
            )
        if replacement.model_copy(update={"plan_review": previous.plan_review}) != previous:
            self._invalid(
                "ci_plan_review_policy_scope_invalid",
                "Plan review policy control changed another Project policy field.",
            )
        return {
            "plan_review_policy": canonical_digest(replacement.plan_review),
            "project_policy_transition": canonical_digest(
                {
                    "previous_policy_digest": canonical_digest(previous),
                    "policy_digest": canonical_digest(replacement),
                }
            ),
            "pr_type_dispatch": self._dispatch_digest(
                changes,
                pr_type="plan_review_policy_control",
                identity=operation_id,
            ),
            "trusted_base": canonical_digest(
                {
                    "project_id": managed.project.project_id,
                    "schema_manifest_digest": managed.config.schema_manifest_digest,
                }
            ),
        }

    def _validate_document_layout_policy_control(
        self,
        root: Path,
        *,
        request: CIPRDispatchRequest,
        base: GitCommitData,
        head: GitCommitData,
        changes: tuple[GitTreeChange, ...],
    ) -> dict[str, Sha256Digest]:
        marker = _DOCUMENT_LAYOUT_POLICY_MARKER.fullmatch(
            head.message.rstrip("\n")
        )
        if marker is None or head.parents != (base.object_id,):
            self._invalid(
                "ci_document_layout_policy_commit_invalid",
                "Document layout policy control must be one marked commit over protected base.",
            )
        operation_id = marker.group(1)
        self._require_branch(
            request.head_ref,
            f"research/control/{operation_id}",
            kind="Document layout policy control",
        )
        self._require_exact_changes(
            changes,
            {PROJECT_POLICY_PATH: ("100644", "100644", "M")},
            code="ci_document_layout_policy_scope_invalid",
        )
        managed = self._managed_base(
            root,
            commit=base.object_id,
            base_ref=request.base_ref,
        )
        previous = self._record(
            root,
            commit=base.object_id,
            path=PROJECT_POLICY_PATH,
            model_type=ProjectPolicy,
        )
        replacement = self._record(
            root,
            commit=head.object_id,
            path=PROJECT_POLICY_PATH,
            model_type=ProjectPolicy,
        )
        if (
            replacement.model_copy(
                update={"document_layout": previous.document_layout}
            )
            != previous
        ):
            self._invalid(
                "ci_document_layout_policy_scope_invalid",
                "Document layout control changed another Project policy field.",
            )
        return {
            "document_layout_policy": canonical_digest(replacement.document_layout),
            "project_policy_transition": canonical_digest(
                {
                    "previous_policy_digest": canonical_digest(previous),
                    "policy_digest": canonical_digest(replacement),
                }
            ),
            "pr_type_dispatch": self._dispatch_digest(
                changes,
                pr_type="document_layout_policy_control",
                identity=operation_id,
            ),
            "trusted_base": canonical_digest(
                {
                    "project_id": managed.project.project_id,
                    "schema_manifest_digest": managed.config.schema_manifest_digest,
                }
            ),
        }

    def _validate_github_governance_policy_control(
        self,
        root: Path,
        *,
        request: CIPRDispatchRequest,
        base: GitCommitData,
        head: GitCommitData,
        changes: tuple[GitTreeChange, ...],
    ) -> dict[str, Sha256Digest]:
        marker = _GITHUB_GOVERNANCE_POLICY_MARKER.fullmatch(
            head.message.rstrip("\n")
        )
        if marker is None or head.parents != (base.object_id,):
            self._invalid(
                "ci_github_governance_policy_commit_invalid",
                "GitHub governance control must be one marked commit over protected base.",
            )
        operation_id = marker.group(1)
        self._require_branch(
            request.head_ref,
            f"research/control/{operation_id}",
            kind="GitHub governance policy control",
        )
        self._require_exact_changes(
            changes,
            {PROJECT_POLICY_PATH: ("100644", "100644", "M")},
            code="ci_github_governance_policy_scope_invalid",
        )
        managed = self._managed_base(
            root,
            commit=base.object_id,
            base_ref=request.base_ref,
        )
        previous = self._record(
            root,
            commit=base.object_id,
            path=PROJECT_POLICY_PATH,
            model_type=ProjectPolicy,
        )
        replacement = self._record(
            root,
            commit=head.object_id,
            path=PROJECT_POLICY_PATH,
            model_type=ProjectPolicy,
        )
        if replacement.github is None:
            self._invalid(
                "ci_github_governance_policy_missing",
                "GitHub governance control must configure an explicit policy.",
            )
        if replacement.model_copy(update={"github": previous.github}) != previous:
            self._invalid(
                "ci_github_governance_policy_scope_invalid",
                "GitHub governance control changed another Project policy field.",
            )
        return {
            "github_governance_policy": canonical_digest(replacement.github),
            "project_policy_transition": canonical_digest(
                {
                    "previous_policy_digest": canonical_digest(previous),
                    "policy_digest": canonical_digest(replacement),
                }
            ),
            "pr_type_dispatch": self._dispatch_digest(
                changes,
                pr_type="github_governance_policy_control",
                identity=operation_id,
            ),
            "trusted_base": canonical_digest(
                {
                    "project_id": managed.project.project_id,
                    "schema_manifest_digest": managed.config.schema_manifest_digest,
                }
            ),
        }

    def _validate_bootstrap_proposal(
        self,
        root: Path,
        *,
        request: CIPRDispatchRequest,
        base: GitCommitData,
        head: GitCommitData,
        changes: tuple[GitTreeChange, ...],
        marker: re.Match[str],
    ) -> dict[str, Sha256Digest]:
        bootstrap_id, operation_id, claimed_digest = marker.groups()
        self._require_branch(
            request.head_ref,
            f"research/bootstrap/{bootstrap_id}",
            kind="Bootstrap proposal",
        )
        if head.parents != (base.object_id,):
            self._invalid(
                "ci_bootstrap_proposal_commit_invalid",
                "Bootstrap proposal must be one commit over the exact protected base.",
            )
        if self._controlled_paths(root, commit=base.object_id):
            self._invalid(
                "ci_bootstrap_base_not_empty",
                "Bootstrap proposal base already contains managed protocol state.",
            )
        files, manifest_digest = self._bootstrap_manifest(
            root,
            commit=head.object_id,
            base_ref=request.base_ref,
            expected_state=ProjectState.BOOTSTRAPPING,
        )
        if manifest_digest != claimed_digest:
            self._invalid(
                "ci_bootstrap_manifest_digest_mismatch",
                "Bootstrap proposal marker does not bind its exact manifest.",
            )
        self._require_exact_changes(
            changes,
            {
                path: ("000000", "100644", "A")
                for path in sorted(files)
            },
            code="ci_bootstrap_proposal_scope_invalid",
        )
        return {
            "bootstrap_manifest": manifest_digest,
            "pr_type_dispatch": self._dispatch_digest(
                changes,
                pr_type="bootstrap_proposal",
                identity=f"{bootstrap_id}:{operation_id}",
            ),
        }

    def _validate_bootstrap_acceptance(
        self,
        root: Path,
        *,
        request: CIPRDispatchRequest,
        base: GitCommitData,
        head: GitCommitData,
        marker: re.Match[str],
    ) -> dict[str, Sha256Digest]:
        operation_id, claimed_digest = marker.groups()
        self._require_branch(
            request.head_ref,
            f"research/control/{operation_id}",
            kind="Bootstrap acceptance",
        )
        if len(head.parents) != 1:
            self._invalid(
                "ci_bootstrap_acceptance_commit_invalid",
                "Bootstrap acceptance must have exactly one proposal parent.",
            )
        proposal = self.git.read_commit(root, head.parents[0])
        if proposal.object_id != base.object_id:
            proposal_marker = _BOOTSTRAP_PROPOSAL_MARKER.fullmatch(
                proposal.message.rstrip("\n")
            )
            if proposal.parents != (base.object_id,) or proposal_marker is None:
                self._invalid(
                    "ci_bootstrap_acceptance_parent_invalid",
                    "Bootstrap acceptance parent is not a reviewed proposal over base.",
                )
            proposal_files, proposal_digest = self._bootstrap_manifest(
                root,
                commit=proposal.object_id,
                base_ref=request.base_ref,
                expected_state=ProjectState.BOOTSTRAPPING,
            )
            if proposal_marker.group(3) != proposal_digest:
                self._invalid(
                    "ci_bootstrap_manifest_digest_mismatch",
                    "Bootstrap proposal parent marker does not bind its manifest.",
                )
            proposal_changes = self.git.changes(
                root,
                old_commit=base.object_id,
                new_commit=proposal.object_id,
            )
            self._require_exact_changes(
                proposal_changes,
                {
                    path: ("000000", "100644", "A")
                    for path in sorted(proposal_files)
                },
                code="ci_bootstrap_proposal_scope_invalid",
            )
        else:
            proposal_files, proposal_digest = self._bootstrap_manifest(
                root,
                commit=proposal.object_id,
                base_ref=request.base_ref,
                expected_state=ProjectState.BOOTSTRAPPING,
            )
        if proposal_digest != claimed_digest:
            self._invalid(
                "ci_bootstrap_manifest_digest_mismatch",
                "Bootstrap acceptance marker does not bind the reviewed proposal.",
            )

        accepted_files, _ = self._bootstrap_manifest(
            root,
            commit=head.object_id,
            base_ref=request.base_ref,
            expected_state=ProjectState.MANAGED,
        )
        expected_files = dict(proposal_files)
        project = self._parse_canonical(
            proposal_files[_PROJECT_PATH],
            model_type=ProjectRecord,
            path=_PROJECT_PATH,
        )
        project_payload = project.model_dump(mode="python")
        project_payload["state"] = ProjectState.MANAGED
        expected_files[_PROJECT_PATH] = dump_yaml(
            ProjectRecord.model_validate(project_payload)
        ).encode("utf-8")
        if accepted_files != expected_files:
            self._invalid(
                "ci_bootstrap_acceptance_manifest_invalid",
                "Bootstrap acceptance changed content beyond the Project state transition.",
            )
        acceptance_changes = self.git.changes(
            root,
            old_commit=proposal.object_id,
            new_commit=head.object_id,
        )
        self._require_exact_changes(
            acceptance_changes,
            {_PROJECT_PATH: ("100644", "100644", "M")},
            code="ci_bootstrap_acceptance_scope_invalid",
        )
        aggregate = self.git.changes(
            root,
            old_commit=base.object_id,
            new_commit=head.object_id,
        )
        aggregate_expected = (
            {_PROJECT_PATH: ("100644", "100644", "M")}
            if proposal.object_id == base.object_id
            else {
                path: ("000000", "100644", "A")
                for path in sorted(accepted_files)
            }
        )
        self._require_exact_changes(
            aggregate,
            aggregate_expected,
            code="ci_bootstrap_acceptance_scope_invalid",
        )
        return {
            "bootstrap_manifest": proposal_digest,
            "project_transition": canonical_digest(
                {"from": "bootstrapping", "to": "managed"}
            ),
            "pr_type_dispatch": self._dispatch_digest(
                aggregate,
                pr_type="bootstrap_acceptance",
                identity=operation_id,
            ),
        }

    def _managed_base(
        self,
        root: Path,
        *,
        commit: str,
        base_ref: str,
    ) -> _ManagedBase:
        config = self._config(root, commit=commit)
        manifest = self._blob(root, commit=commit, path=_SCHEMA_MANIFEST_PATH)
        generated_manifest = generate_schema_files()["manifest.json"]
        if (
            manifest != generated_manifest
            or self._sha256(manifest) != config.schema_manifest_digest
            or config.schema_manifest_digest != schema_manifest_digest()
        ):
            self._invalid(
                "ci_schema_manifest_mismatch",
                "Protected validator and protected-base schema manifest do not match.",
            )
        project = self._record(
            root,
            commit=commit,
            path=config.project_file,
            model_type=ProjectRecord,
        )
        policy = self._record(
            root,
            commit=commit,
            path=_POLICY_PATH,
            model_type=ProjectPolicy,
        )
        if project.project_id != config.project_id:
            self._invalid(
                "ci_project_identity_mismatch",
                "Protected-base Project identities do not match.",
            )
        if project.repository.default_branch != base_ref:
            self._invalid(
                "ci_base_ref_mismatch",
                "Protected-base ProjectRecord names a different default branch.",
            )
        if project.state is not ProjectState.MANAGED:
            self._invalid(
                "ci_project_state_invalid",
                "Task control requires a managed protected-base Project.",
            )
        return _ManagedBase(config=config, project=project, policy=policy)

    def _bootstrap_manifest(
        self,
        root: Path,
        *,
        commit: str,
        base_ref: str,
        expected_state: ProjectState,
    ) -> tuple[dict[str, bytes], Sha256Digest]:
        schema_files = generate_schema_files()
        expected_paths = {
            PROJECT_CONFIG_NAME,
            _PROJECT_PATH,
            _POLICY_PATH,
            *(f".research/schemas/{name}" for name in schema_files),
            *(
                f".research/{directory}/.gitkeep"
                for directory in _MANAGED_DIRECTORIES
            ),
        }
        observed_paths = self._controlled_paths(root, commit=commit)
        if observed_paths != tuple(sorted(expected_paths)):
            self._invalid(
                "ci_bootstrap_manifest_scope_invalid",
                "Bootstrap tree does not contain exactly the managed init manifest.",
            )
        files = {
            path: self._blob(root, commit=commit, path=path)
            for path in sorted(expected_paths)
        }
        config = self._config_bytes(files[PROJECT_CONFIG_NAME])
        if (
            config.protocol_version != PROTOCOL_VERSION
            or config.schema_manifest_digest != schema_manifest_digest(schema_files)
        ):
            self._invalid(
                "ci_bootstrap_protocol_lock_mismatch",
                "Bootstrap config does not match the protected protocol build.",
            )
        project = self._parse_canonical(
            files[_PROJECT_PATH],
            model_type=ProjectRecord,
            path=_PROJECT_PATH,
        )
        self._parse_canonical(
            files[_POLICY_PATH],
            model_type=ProjectPolicy,
            path=_POLICY_PATH,
        )
        if project.project_id != config.project_id:
            self._invalid(
                "ci_bootstrap_project_identity_mismatch",
                "Bootstrap Project IDs differ between config and ProjectRecord.",
            )
        if project.repository.default_branch != base_ref:
            self._invalid(
                "ci_bootstrap_default_branch_mismatch",
                "Bootstrap ProjectRecord names a different protected base branch.",
            )
        if project.state is not expected_state:
            self._invalid(
                "ci_bootstrap_project_state_invalid",
                "Bootstrap Project state is invalid for this PR type.",
            )
        for name, expected in schema_files.items():
            if files[f".research/schemas/{name}"] != expected:
                self._invalid(
                    "ci_bootstrap_schema_mismatch",
                    "Bootstrap schemas do not match the protected validator.",
                )
        for directory in _MANAGED_DIRECTORIES:
            if files[f".research/{directory}/.gitkeep"] != b"":
                self._invalid(
                    "ci_bootstrap_gitkeep_invalid",
                    "Bootstrap directory markers must be empty regular files.",
                )
        digest = canonical_digest(
            {
                "files": [
                    {"path": path, "digest": self._sha256(content)}
                    for path, content in sorted(files.items())
                ]
            }
        )
        return files, digest

    def _task_records(self, root: Path, *, commit: str) -> dict[str, TaskRecord]:
        records: dict[str, TaskRecord] = {}
        entries = self.git.list_entries(
            root,
            commit=commit,
            path=".research/tasks",
        )
        for entry in entries:
            if entry.path == ".research/tasks/.gitkeep":
                if (
                    entry.mode != "100644"
                    or entry.object_type != "blob"
                    or self._blob(root, commit=commit, path=entry.path) != b""
                ):
                    self._invalid(
                        "ci_task_store_invalid",
                        "Task store marker is not an empty regular file.",
                    )
                continue
            matched = _TASK_RECORD.fullmatch(entry.path)
            if (
                matched is None
                or entry.mode != "100644"
                or entry.object_type != "blob"
            ):
                self._invalid(
                    "ci_task_store_invalid",
                    "Task store contains an unexpected tree entry.",
                )
            record = self._record(
                root,
                commit=commit,
                path=entry.path,
                model_type=TaskRecord,
            )
            task_id = matched.group(1)
            if record.task_id != task_id or task_id in records:
                self._invalid(
                    "ci_task_store_invalid",
                    "Task record identity does not match its canonical path.",
                )
            records[task_id] = record
        return records

    @staticmethod
    def _validate_task_cancellation(
        *,
        current: TaskRecord,
        replacement: TaskRecord,
    ) -> None:
        if current.state in {TaskState.DONE, TaskState.CANCELED}:
            raise RCPError(
                code="ci_task_cancel_invalid",
                message="A terminal Task cannot produce a cancellation change.",
            )
        payload = current.model_dump(mode="python")
        payload.update(
            state=TaskState.CANCELED,
            updated_at=replacement.updated_at,
        )
        if replacement != TaskRecord.model_validate(payload):
            raise RCPError(
                code="ci_task_cancel_invalid",
                message="Task cancellation may change only state and updated_at.",
            )

    def _controlled_paths(self, root: Path, *, commit: str) -> tuple[str, ...]:
        entries = self.git.list_entries(
            root,
            commit=commit,
            path=".research",
        )
        paths: list[str] = []
        for entry in entries:
            if entry.mode != "100644" or entry.object_type != "blob":
                self._invalid(
                    "ci_protocol_tree_entry_invalid",
                    "Protocol state must contain only regular non-executable files.",
                )
            paths.append(entry.path)
        config = self.git.read_blob_at(
            root,
            commit=commit,
            path=PROJECT_CONFIG_NAME,
            required=False,
        )
        if config is not None:
            paths.append(PROJECT_CONFIG_NAME)
        return tuple(sorted(paths))

    def _config(self, root: Path, *, commit: str) -> ProjectConfig:
        return self._config_bytes(
            self._blob(root, commit=commit, path=PROJECT_CONFIG_NAME)
        )

    @staticmethod
    def _config_bytes(content: bytes) -> ProjectConfig:
        try:
            config = ProjectConfig.model_validate(
                tomllib.loads(content.decode("utf-8"))
            )
        except (UnicodeError, tomllib.TOMLDecodeError, ValidationError) as error:
            raise RCPError(
                code="ci_project_config_invalid",
                message="Project configuration is malformed.",
            ) from error
        if dump_project_config(config) != content:
            raise RCPError(
                code="ci_project_config_invalid",
                message="Project configuration is not canonical.",
            )
        return config

    def _record(
        self,
        root: Path,
        *,
        commit: str,
        path: str,
        model_type: type[_ModelT],
    ) -> _ModelT:
        return self._parse_canonical(
            self._blob(root, commit=commit, path=path),
            model_type=model_type,
            path=path,
        )

    @staticmethod
    def _parse_canonical(
        content: bytes,
        *,
        model_type: type[_ModelT],
        path: str,
    ) -> _ModelT:
        try:
            record = model_type.model_validate(load_yaml(content.decode("utf-8")))
        except (
            UnicodeError,
            SerializationError,
            TypeError,
            ValueError,
            ValidationError,
        ) as error:
            raise RCPError(
                code="ci_record_invalid",
                message="PR dispatch found a malformed protocol record.",
                context={"path": path},
            ) from error
        if dump_yaml(record).encode("utf-8") != content:
            raise RCPError(
                code="ci_record_not_canonical",
                message="Protocol records must use canonical YAML.",
                context={"path": path},
            )
        return record

    def _blob(self, root: Path, *, commit: str, path: str) -> bytes:
        content = self.git.read_blob_at(
            root,
            commit=commit,
            path=path,
        )
        assert content is not None
        return content

    @staticmethod
    def _require_exact_changes(
        changes: tuple[GitTreeChange, ...],
        expected: dict[str, tuple[str, str, str]],
        *,
        code: str,
    ) -> None:
        observed = {item.path: item for item in changes}
        if tuple(sorted(observed)) != tuple(sorted(expected)):
            raise RCPError(
                code=code,
                message="Protocol PR changed paths outside its closed manifest.",
                context={
                    "expected": sorted(expected),
                    "observed": sorted(observed),
                },
            )
        for path, modes in expected.items():
            change = observed[path]
            actual = (change.old_mode, change.new_mode, change.status)
            if actual != modes:
                raise RCPError(
                    code=code,
                    message="Protocol PR contains an unsafe path operation or mode.",
                    context={
                        "path": path,
                        "expected": list(modes),
                        "observed": list(actual),
                    },
                )

    @staticmethod
    def _require_change_mode(
        change: GitTreeChange,
        expected: tuple[str, str, str],
    ) -> None:
        observed = (change.old_mode, change.new_mode, change.status)
        if observed != expected:
            raise RCPError(
                code="ci_task_control_mode_invalid",
                message="Task control contains an unsafe path operation or mode.",
                context={
                    "path": change.path,
                    "expected": list(expected),
                    "observed": list(observed),
                },
            )

    @staticmethod
    def _require_ref(value: str, *, label: str) -> None:
        if (
            _REF.fullmatch(value) is None
            or ".." in value
            or "//" in value
            or "@{" in value
            or value.endswith(("/", "."))
        ):
            raise RCPError(
                code="ci_dispatch_ref_invalid",
                message=f"The {label} ref is not a canonical branch name.",
            )

    @staticmethod
    def _require_branch(observed: str, expected: str, *, kind: str) -> None:
        if observed != expected:
            raise RCPError(
                code="ci_dispatch_branch_mismatch",
                message=f"{kind} content does not match the PR source branch.",
                context={"expected": expected, "observed": observed},
            )

    @staticmethod
    def _require_canonical_paths(changes: tuple[GitTreeChange, ...]) -> None:
        for change in changes:
            path = change.path
            pure = PurePosixPath(path)
            if (
                not path
                or pure.is_absolute()
                or pure.as_posix() != path
                or any(part in {"", ".", ".."} for part in pure.parts)
                or "\\" in path
                or any(ord(character) < 32 for character in path)
            ):
                raise RCPError(
                    code="ci_dispatch_path_invalid",
                    message="PR diff contains a non-canonical repository path.",
                )

    @staticmethod
    def _is_protocol_path(path: str) -> bool:
        return (
            path == ".research"
            or path.startswith(".research/")
            or path == PROJECT_CONFIG_NAME
            or path.startswith(f"{PROJECT_CONFIG_NAME}/")
        )

    @staticmethod
    def _dispatch_digest(
        changes: tuple[GitTreeChange, ...],
        *,
        pr_type: str,
        identity: str,
    ) -> Sha256Digest:
        return canonical_digest(
            {
                "pr_type": pr_type,
                "identity": identity,
                "changes": [
                    {
                        "path": item.path,
                        "old_mode": item.old_mode,
                        "new_mode": item.new_mode,
                        "old_object": item.old_object,
                        "new_object": item.new_object,
                        "status": item.status,
                    }
                    for item in changes
                ],
            }
        )

    @staticmethod
    def _checks(
        evidence: dict[str, Sha256Digest],
    ) -> tuple[CIValidationCheck, ...]:
        return tuple(
            CIValidationCheck(
                name=name,
                status="passed",
                evidence_digest=evidence[name],
            )
            for name in sorted(evidence)
        )

    @staticmethod
    def _sha256(content: bytes) -> Sha256Digest:
        return f"sha256:{hashlib.sha256(content).hexdigest()}"  # type: ignore[return-value]

    @staticmethod
    def _unknown_protocol_change(paths: tuple[str, ...]) -> Never:
        raise RCPError(
            code="ci_pr_type_unknown",
            message="Protected protocol changes do not match one supported PR type.",
            remediation=(
                "Use a generated Submission, Task control, Linear configure, or "
                "Impact/bootstrap proposal; add a protected-base validator before enabling "
                "another control PR type."
            ),
            context={"changed_paths": list(paths)},
        )

    @staticmethod
    def _invalid(code: str, message: str) -> Never:
        raise RCPError(code=code, message=message)


def write_ci_dispatch_artifact(
    result: CIPRDispatchResult,
    path: Path,
) -> CIPRDispatchArtifactReceipt:
    """Publish one immutable canonical dispatcher artifact outside the PR tree."""

    destination = Path(os.path.abspath(os.fspath(path)))
    parent = destination.parent
    try:
        resolved_parent = parent.resolve(strict=True)
    except OSError as error:
        raise RCPError(
            code="ci_artifact_path_invalid",
            message="CI attestation artifact parent does not exist.",
            context={"path": str(destination)},
        ) from error
    if parent.is_symlink() or resolved_parent != parent or not parent.is_dir():
        raise RCPError(
            code="ci_artifact_path_invalid",
            message="CI attestation artifact parent must not traverse a symbolic link.",
            context={"path": str(destination)},
        )
    content = result.attestation_bytes
    digest = ProtectedBasePRDispatcher._sha256(content)
    if _artifact_is_identical(destination, content):
        return CIPRDispatchArtifactReceipt(
            path=destination,
            content_digest=digest,
            size_bytes=len(content),
            created=False,
        )
    if destination.exists() or destination.is_symlink():
        _artifact_conflict(destination)

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.researchctl-",
        dir=parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, destination, follow_symlinks=False)
        except FileExistsError:
            if not _artifact_is_identical(destination, content):
                _artifact_conflict(destination)
            created = False
        else:
            created = True
            directory = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        return CIPRDispatchArtifactReceipt(
            path=destination,
            content_digest=digest,
            size_bytes=len(content),
            created=created,
        )
    finally:
        temporary.unlink(missing_ok=True)


def load_ci_dispatch_artifact(content: bytes) -> CIPRDispatchAttestation:
    """Validate a workflow envelope before consuming its typed attestation."""

    if not isinstance(content, bytes) or len(content) > _MAX_DISPATCH_ARTIFACT_BYTES:
        raise RCPError(
            code="ci_dispatch_artifact_invalid",
            message="CI dispatch artifact is not bounded canonical bytes.",
        )
    try:
        artifact = CIPRDispatchAttestation.model_validate(
            load_yaml(content.decode("utf-8"))
        )
    except (
        UnicodeError,
        SerializationError,
        TypeError,
        ValueError,
        ValidationError,
    ) as error:
        raise RCPError(
            code="ci_dispatch_artifact_invalid",
            message="CI dispatch artifact is malformed.",
        ) from error
    if dump_yaml(artifact).encode("utf-8") != content:
        raise RCPError(
            code="ci_dispatch_artifact_invalid",
            message="CI dispatch artifact is not canonical.",
        )
    return artifact


def submission_attestation_from_dispatch_artifact(
    content: bytes,
) -> CIValidationAttestation:
    """Extract a formally typed Submission attestation from a dispatch envelope."""

    artifact = load_ci_dispatch_artifact(content)
    if artifact.pr_type != "submission" or artifact.submission_attestation is None:
        raise RCPError(
            code="ci_dispatch_submission_attestation_missing",
            message="CI dispatch artifact is not a validated Submission envelope.",
        )
    return artifact.submission_attestation


def _artifact_is_identical(path: Path, expected: bytes) -> bool:
    return path.is_file() and not path.is_symlink() and path.read_bytes() == expected


def _artifact_conflict(path: Path) -> Never:
    raise RCPError(
        code="ci_artifact_conflict",
        message="CI attestation artifact path already contains different content.",
        context={"path": str(path)},
    )
