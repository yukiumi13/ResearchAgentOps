from __future__ import annotations

import hashlib
import os
import re
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Never, TypeVar

from pydantic import BaseModel, Field, StrictInt, ValidationError

from researchctl.adapters.git_ci import (
    GitCIObjectReader,
    GitCommitData,
    GitTreeChange,
)
from researchctl.adapters.git_scope import GitWriteScopeValidator
from researchctl.config import ProjectConfig, dump_project_config
from researchctl.constants import (
    LINEAR_PROJECTION_POLICY_PATH,
    PROJECT_POLICY_PATH,
    __version__,
)
from researchctl.domain.enums import SubmissionState
from researchctl.domain.models import (
    CIValidationAttestation,
    CIValidationCheck,
    GeneratedOutputDigest,
    LinearProjectionPolicy,
    ProjectPolicy,
    ProjectRecord,
    ReportProposal,
    ReportRecord,
    ResearchSubmission,
    ReviewDecision,
    RunResult,
    RunSpec,
    StrictModel,
    TaskRecord,
)
from researchctl.domain.types import (
    AttestationId,
    GitObjectId,
    ShortText,
    SubmissionId,
    UtcDateTime,
)
from researchctl.errors import RCPError
from researchctl.schema import schema_manifest_digest
from researchctl.serialization import canonical_digest, dump_yaml, load_yaml
from researchctl.services.linear_preview import (
    LinearPreview,
    build_linear_preview,
    disabled_linear_preview,
)
from researchctl.services.review_acceptance import (
    ReviewAcceptanceBuilder,
)
from researchctl.services.submissions import (
    RenderedSubmissionFile,
    SubmissionBundle,
    SubmissionBundleBuilder,
    SubmissionEvidence,
)

CI_VALIDATOR_VERSION = __version__
CI_WORKFLOW_ID = "research-validate-pr"
CI_CHECK_IDENTITY = "researchctl/exact-head"
_PROJECT_CONFIG_PATH = ".researchctl.toml"
_SCHEMA_MANIFEST_PATH = ".research/schemas/manifest.json"
_PROPOSAL_MARKER = re.compile(
    r"^researchctl: submission\.create "
    r"(submission_\d{8}T\d{6}Z_[0-9a-f]{24}) "
    r"operation_\d{8}T\d{6}Z_[0-9a-f]{24}$"
)
_ACCEPTANCE_MARKER = re.compile(
    r"^researchctl: review\.accept "
    r"(submission_\d{8}T\d{6}Z_[0-9a-f]{24}) "
    r"operation_\d{8}T\d{6}Z_[0-9a-f]{24}$"
)
_EVIDENCE_SPEC_PATH = re.compile(
    r"^\.research/submissions/"
    r"(submission_\d{8}T\d{6}Z_[0-9a-f]{24})/evidence/"
    r"(run_\d{8}T\d{6}Z_[0-9a-f]{24})/spec\.yaml$"
)
_DECISION_PATH = re.compile(
    r"^\.research/decisions/"
    r"(decision_\d{8}T\d{6}Z_[0-9a-f]{24})\.yaml$"
)
_REPORT_PATH = re.compile(
    r"^\.research/reports/"
    r"(report_\d{8}T\d{6}Z_[0-9a-f]{24})/([1-9][0-9]*)\.yaml$"
)
_REPORT_MARKDOWN_PATH = re.compile(
    r"^\.research/reports/"
    r"(report_\d{8}T\d{6}Z_[0-9a-f]{24})/([1-9][0-9]*)\.md$"
)
_ModelT = TypeVar("_ModelT", bound=BaseModel)


class CIValidationRequest(StrictModel):
    attestation_id: AttestationId
    repository: ShortText
    pull_request_number: StrictInt = Field(ge=1)
    subject_head: GitObjectId
    base_commit: GitObjectId
    submission_id: SubmissionId
    generated_at: UtcDateTime


@dataclass(frozen=True, slots=True)
class CIValidationResult:
    attestation: CIValidationAttestation
    generated_files: tuple[RenderedSubmissionFile, ...]
    linear_body: bytes | None
    head_kind: str

    @property
    def attestation_bytes(self) -> bytes:
        return dump_yaml(self.attestation).encode("utf-8")

    def as_dict(self) -> dict[str, object]:
        return {
            "head_kind": self.head_kind,
            "attestation": self.attestation.model_dump(
                mode="json",
                exclude_none=True,
            ),
            "attestation_size_bytes": len(self.attestation_bytes),
            "linear_body_size_bytes": (
                len(self.linear_body) if self.linear_body is not None else None
            ),
        }


@dataclass(frozen=True, slots=True)
class CIValidationArtifactReceipt:
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


def write_ci_validation_artifact(
    result: CIValidationResult,
    path: Path,
) -> CIValidationArtifactReceipt:
    """Publish an immutable canonical attestation artifact outside the PR tree."""

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
    digest = ExactHeadCIValidator._sha256(content)
    if _artifact_is_identical(destination, content):
        return CIValidationArtifactReceipt(
            path=destination,
            content_digest=digest,
            size_bytes=len(content),
            created=False,
        )
    if destination.exists() or destination.is_symlink():
        _raise_artifact_conflict(destination)

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
                _raise_artifact_conflict(destination)
            created = False
        else:
            created = True
            directory = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        return CIValidationArtifactReceipt(
            path=destination,
            content_digest=digest,
            size_bytes=len(content),
            created=created,
        )
    finally:
        temporary.unlink(missing_ok=True)


def _artifact_is_identical(path: Path, expected: bytes) -> bool:
    return path.is_file() and not path.is_symlink() and path.read_bytes() == expected


def _raise_artifact_conflict(path: Path) -> Never:
    raise RCPError(
        code="ci_artifact_conflict",
        message="CI attestation artifact path already contains different content.",
        context={"path": str(path)},
    )


@dataclass(frozen=True, slots=True)
class _TrustedBase:
    config: ProjectConfig
    project: ProjectRecord
    task: TaskRecord
    policy: ProjectPolicy
    linear_policy: LinearProjectionPolicy | None


@dataclass(frozen=True, slots=True)
class _ValidatedHead:
    kind: str
    submission: ResearchSubmission
    proposal: ReportProposal
    open_bundle: SubmissionBundle
    evidence: tuple[SubmissionEvidence, ...]
    generated_files: tuple[RenderedSubmissionFile, ...]
    projection: LinearPreview
    checks: tuple[CIValidationCheck, ...]
    decision: ReviewDecision | None = None
    report: ReportRecord | None = None


class ExactHeadCIValidator:
    """Rebuild one Submission PR from immutable Git objects and attest its head."""

    def __init__(
        self,
        *,
        git: GitCIObjectReader | None = None,
        write_scope: GitWriteScopeValidator | None = None,
        submissions: SubmissionBundleBuilder | None = None,
        acceptance: ReviewAcceptanceBuilder | None = None,
    ) -> None:
        self.git = git or GitCIObjectReader()
        self.write_scope = write_scope or GitWriteScopeValidator(
            object_reader=self.git,
        )
        self.submissions = submissions or SubmissionBundleBuilder()
        self.acceptance = acceptance or ReviewAcceptanceBuilder(self.submissions)

    def validate(
        self,
        repository_root: Path,
        request: CIValidationRequest,
    ) -> CIValidationResult:
        root = repository_root
        base = self.git.read_commit(root, request.base_commit)
        head = self.git.read_commit(root, request.subject_head)
        subject_submission_path = self._submission_path(request.submission_id)
        subject_submission = self._record(
            root,
            commit=head.object_id,
            path=subject_submission_path,
            model_type=ResearchSubmission,
        )
        if subject_submission.submission_id != request.submission_id:
            self._invalid(
                "ci_submission_identity_mismatch",
                "The exact PR head contains a different Submission identity.",
            )
        trusted = self._trusted_base(
            root,
            base=base,
            task_id=subject_submission.task_id,
        )
        if subject_submission.state is SubmissionState.OPEN:
            validated = self._validate_proposal(
                root,
                base=base,
                head=head,
                task=trusted.task,
                policy=trusted.policy,
                expected_submission_id=request.submission_id,
                linear_policy=trusted.linear_policy,
            )
        elif subject_submission.state is SubmissionState.ACCEPTED:
            validated = self._validate_acceptance(
                root,
                base=base,
                head=head,
                task=trusted.task,
                policy=trusted.policy,
                expected_submission_id=request.submission_id,
                linear_policy=trusted.linear_policy,
            )
        else:
            self._invalid(
                "ci_submission_state_invalid",
                "Submission CI accepts only open or acceptance-prepared heads.",
            )

        generated_outputs = tuple(
            GeneratedOutputDigest(
                path=item.path,
                digest=item.digest,
                size_bytes=len(item.content),
            )
            for item in validated.generated_files
        )
        artifact_digest = canonical_digest(
            {
                "generated_outputs": [
                    item.model_dump(mode="json") for item in generated_outputs
                ]
            }
        )
        decision = validated.decision
        report = validated.report
        rendered_report_path = (
            f".research/reports/{report.report_id}/{report.revision}.md"
            if report is not None
            else (
                f".research/submissions/{request.submission_id}/"
                "report-preview.md"
            )
        )
        rendered_report = next(
            item
            for item in validated.generated_files
            if item.path == rendered_report_path
        )
        attestation = CIValidationAttestation(
            attestation_id=request.attestation_id,
            project_id=trusted.project.project_id,
            task_id=trusted.task.task_id,
            submission_id=request.submission_id,
            repository=request.repository,
            pull_request_number=request.pull_request_number,
            subject_head=head.object_id,
            subject_tree=head.tree,
            base_commit=base.object_id,
            validator_version=CI_VALIDATOR_VERSION,
            schema_manifest_digest=trusted.config.schema_manifest_digest,
            workflow_id=CI_WORKFLOW_ID,
            check_identity=CI_CHECK_IDENTITY,
            checks=validated.checks,
            generated_outputs=generated_outputs,
            submission_digest=canonical_digest(validated.submission),
            report_proposal_digest=canonical_digest(validated.proposal),
            decision_digest=(
                canonical_digest(decision) if decision is not None else None
            ),
            report_id=report.report_id if report is not None else None,
            report_revision=report.revision if report is not None else None,
            report_digest=canonical_digest(report) if report is not None else None,
            report_preview_digest=self._sha256(rendered_report.content),
            projection=validated.projection.projection,
            generated_at=request.generated_at,
            artifact_digest=artifact_digest,
            overall_result="passed",
        )
        return CIValidationResult(
            attestation=attestation,
            generated_files=validated.generated_files,
            linear_body=validated.projection.body,
            head_kind=validated.kind,
        )

    def _validate_proposal(
        self,
        root: Path,
        *,
        base: GitCommitData,
        head: GitCommitData,
        task: TaskRecord,
        policy: ProjectPolicy,
        expected_submission_id: str,
        linear_policy: LinearProjectionPolicy | None,
    ) -> _ValidatedHead:
        self._require_commit_marker(
            head,
            parent=base.object_id,
            pattern=_PROPOSAL_MARKER,
            submission_id=expected_submission_id,
            code="ci_proposal_commit_invalid",
        )
        submission, proposal, evidence, bundle = self._open_bundle(
            root,
            commit=head.object_id,
            task=task,
            policy=policy,
            submission_id=expected_submission_id,
        )
        expected_paths = tuple(item.path for item in bundle.files)
        proposal_changes = self._require_changes(
            root,
            old_commit=base.object_id,
            new_commit=head.object_id,
            expected_paths=expected_paths,
            additions=expected_paths,
        )
        source_evidence = self._verify_sources(
            root,
            evidence,
            task=task,
            trusted_base_commit=base.object_id,
        )
        projection = (
            disabled_linear_preview()
            if linear_policy is None
            else disabled_linear_preview("report_not_acceptance_prepared")
        )
        checks = self._checks(
            {
                "changed_path_scope": self._changes_digest(proposal_changes),
                "generated_outputs": bundle.manifest_digest,
                "proposal_authority": canonical_digest(
                    {"state": submission.state.value, "parent": base.object_id}
                ),
                "record_linkage": bundle.source_digest,
                "source_identity": source_evidence,
                "trusted_base_records": canonical_digest(
                    {
                        "task_digest": canonical_digest(task),
                        "policy_digest": canonical_digest(policy),
                    }
                ),
            }
        )
        return _ValidatedHead(
            kind="submission_proposal",
            submission=submission,
            proposal=proposal,
            open_bundle=bundle,
            evidence=evidence,
            generated_files=bundle.files,
            projection=projection,
            checks=checks,
        )

    def _validate_acceptance(
        self,
        root: Path,
        *,
        base: GitCommitData,
        head: GitCommitData,
        task: TaskRecord,
        policy: ProjectPolicy,
        expected_submission_id: str,
        linear_policy: LinearProjectionPolicy | None,
    ) -> _ValidatedHead:
        if len(head.parents) != 1:
            self._invalid(
                "ci_acceptance_commit_invalid",
                "Acceptance preparation must have exactly one proposal parent.",
            )
        proposal_commit = self.git.read_commit(root, head.parents[0])
        self._require_commit_marker(
            proposal_commit,
            parent=base.object_id,
            pattern=_PROPOSAL_MARKER,
            submission_id=expected_submission_id,
            code="ci_proposal_commit_invalid",
        )
        self._require_commit_marker(
            head,
            parent=proposal_commit.object_id,
            pattern=_ACCEPTANCE_MARKER,
            submission_id=expected_submission_id,
            code="ci_acceptance_commit_invalid",
        )
        open_submission, proposal, evidence, open_bundle = self._open_bundle(
            root,
            commit=proposal_commit.object_id,
            task=task,
            policy=policy,
            submission_id=expected_submission_id,
        )
        proposal_paths = tuple(item.path for item in open_bundle.files)
        proposal_changes = self._require_changes(
            root,
            old_commit=base.object_id,
            new_commit=proposal_commit.object_id,
            expected_paths=proposal_paths,
            additions=proposal_paths,
        )

        acceptance_changes = self.git.changes(
            root,
            old_commit=proposal_commit.object_id,
            new_commit=head.object_id,
        )
        submission_path = self._submission_path(expected_submission_id)
        decision_paths = [
            change.path
            for change in acceptance_changes
            if _DECISION_PATH.fullmatch(change.path)
        ]
        report_paths = [
            change.path
            for change in acceptance_changes
            if _REPORT_PATH.fullmatch(change.path)
        ]
        report_markdown_paths = [
            change.path
            for change in acceptance_changes
            if _REPORT_MARKDOWN_PATH.fullmatch(change.path)
        ]
        if (
            len(acceptance_changes) != 4
            or len(decision_paths) != 1
            or len(report_paths) != 1
            or len(report_markdown_paths) != 1
            or submission_path not in {change.path for change in acceptance_changes}
        ):
            self._path_scope_invalid(
                expected=(
                    submission_path,
                    "one Decision",
                    "one Report YAML revision",
                    "one accepted Report Markdown revision",
                ),
                observed=tuple(change.path for change in acceptance_changes),
            )
        accepted_submission = self._record(
            root,
            commit=head.object_id,
            path=submission_path,
            model_type=ResearchSubmission,
        )
        decision = self._record(
            root,
            commit=head.object_id,
            path=decision_paths[0],
            model_type=ReviewDecision,
        )
        report = self._record(
            root,
            commit=head.object_id,
            path=report_paths[0],
            model_type=ReportRecord,
        )
        decision_match = _DECISION_PATH.fullmatch(decision_paths[0])
        report_match = _REPORT_PATH.fullmatch(report_paths[0])
        report_markdown_match = _REPORT_MARKDOWN_PATH.fullmatch(
            report_markdown_paths[0]
        )
        assert (
            decision_match is not None
            and report_match is not None
            and report_markdown_match is not None
        )
        if (
            decision.decision_id != decision_match.group(1)
            or report.report_id != report_match.group(1)
            or report.revision != int(report_match.group(2))
            or report_markdown_match.groups() != report_match.groups()
        ):
            self._invalid(
                "ci_acceptance_path_identity_mismatch",
                "Decision or Report identity does not match its immutable path.",
            )
        current_report = self._current_report(
            root,
            base_commit=base.object_id,
            proposal=proposal,
        )
        accepted = self.acceptance.build(
            task=task,
            submission=open_submission,
            proposal=proposal,
            evidence=evidence,
            current_report=current_report,
            decision_id=decision.decision_id,
            reviewer_actor=decision.reviewer_actor,
            decided_at=decision.decided_at,
            disposition=decision.disposition,
            conditions=decision.conditions,
            claim_scope=decision.claim_scope,
            code_disposition=decision.code_disposition,
            accepted_base_tree=base.tree,
            policy=policy,
        )
        if (
            accepted.submission != accepted_submission
            or accepted.decision != decision
            or accepted.report != report
        ):
            self._invalid(
                "ci_acceptance_linkage_invalid",
                "Acceptance records cannot be reproduced from the reviewed proposal.",
            )
        acceptance_paths = tuple(item.path for item in accepted.files)
        acceptance_changes = self._require_changes(
            root,
            old_commit=proposal_commit.object_id,
            new_commit=head.object_id,
            expected_paths=acceptance_paths,
            additions=tuple(path for path in acceptance_paths if path != submission_path),
            modifications=(submission_path,),
        )
        self._require_generated_files(root, commit=head.object_id, files=accepted.files)
        final_files_by_path = {item.path: item for item in open_bundle.files}
        final_files_by_path.update({item.path: item for item in accepted.files})
        final_files = tuple(
            final_files_by_path[path] for path in sorted(final_files_by_path)
        )
        aggregate_changes = self._require_changes(
            root,
            old_commit=base.object_id,
            new_commit=head.object_id,
            expected_paths=tuple(item.path for item in final_files),
            additions=tuple(item.path for item in final_files),
        )
        source_evidence = self._verify_sources(
            root,
            evidence,
            task=task,
            trusted_base_commit=base.object_id,
        )
        projection = build_linear_preview(
            policy=linear_policy,
            task=task,
            submission=accepted_submission,
            decision=decision,
            report=report,
        )
        checks = self._checks(
            {
                "acceptance_materialization": accepted.manifest_digest,
                "changed_path_scope": canonical_digest(
                    {
                        "proposal": self._changes_digest(proposal_changes),
                        "acceptance": self._changes_digest(acceptance_changes),
                        "aggregate": self._changes_digest(aggregate_changes),
                    }
                ),
                "generated_outputs": canonical_digest(
                    {"files": [item.as_dict() for item in final_files]}
                ),
                "linear_projection": canonical_digest(
                    projection.projection.model_dump(mode="json")
                ),
                "record_linkage": accepted.source_digest,
                "report_revision": canonical_digest(
                    {
                        "expected": proposal.expected_report_revision,
                        "materialized": report.revision,
                    }
                ),
                "source_identity": source_evidence,
                "trusted_base_records": canonical_digest(
                    {
                        "task_digest": canonical_digest(task),
                        "policy_digest": canonical_digest(policy),
                    }
                ),
            }
        )
        return _ValidatedHead(
            kind="acceptance_prepared",
            submission=accepted_submission,
            proposal=proposal,
            open_bundle=open_bundle,
            evidence=evidence,
            generated_files=final_files,
            projection=projection,
            checks=checks,
            decision=decision,
            report=report,
        )

    def _trusted_base(
        self,
        root: Path,
        *,
        base: GitCommitData,
        task_id: str,
    ) -> _TrustedBase:
        config_bytes = self._blob(root, base.object_id, _PROJECT_CONFIG_PATH)
        try:
            config = ProjectConfig.model_validate(
                tomllib.loads(config_bytes.decode("utf-8"))
            )
        except (UnicodeError, tomllib.TOMLDecodeError, ValidationError) as error:
            raise RCPError(
                code="ci_project_config_invalid",
                message="Protected-base project configuration is malformed.",
            ) from error
        if dump_project_config(config) != config_bytes:
            self._invalid(
                "ci_project_config_invalid",
                "Protected-base project configuration is not canonical.",
            )
        manifest = self._blob(root, base.object_id, _SCHEMA_MANIFEST_PATH)
        observed_manifest_digest = self._sha256(manifest)
        if (
            observed_manifest_digest != config.schema_manifest_digest
            or config.schema_manifest_digest != schema_manifest_digest()
        ):
            self._invalid(
                "ci_schema_manifest_mismatch",
                "Protected validator and protected-base schema manifest do not match.",
            )
        project = self._record(
            root,
            commit=base.object_id,
            path=config.project_file,
            model_type=ProjectRecord,
        )
        if project.project_id != config.project_id:
            self._invalid(
                "ci_project_identity_mismatch",
                "Protected-base Project identities do not match.",
            )
        task = self._record(
            root,
            commit=base.object_id,
            path=f".research/tasks/{task_id}.yaml",
            model_type=TaskRecord,
        )
        if task.task_id != task_id:
            self._invalid(
                "ci_task_identity_mismatch",
                "Protected-base Task identity does not match its canonical path.",
            )
        project_policy = self._record(
            root,
            commit=base.object_id,
            path=PROJECT_POLICY_PATH,
            model_type=ProjectPolicy,
        )
        policy_bytes = self.git.read_blob_at(
            root,
            commit=base.object_id,
            path=LINEAR_PROJECTION_POLICY_PATH,
            required=False,
        )
        linear_policy = (
            None
            if policy_bytes is None
            else self._parse_canonical(
                policy_bytes,
                model_type=LinearProjectionPolicy,
                path=LINEAR_PROJECTION_POLICY_PATH,
            )
        )
        return _TrustedBase(
            config=config,
            project=project,
            task=task,
            policy=project_policy,
            linear_policy=linear_policy,
        )

    def _open_bundle(
        self,
        root: Path,
        *,
        commit: str,
        task: TaskRecord,
        policy: ProjectPolicy,
        submission_id: str,
    ) -> tuple[
        ResearchSubmission,
        ReportProposal,
        tuple[SubmissionEvidence, ...],
        SubmissionBundle,
    ]:
        bundle_root = f".research/submissions/{submission_id}"
        entries = self.git.list_entries(
            root,
            commit=commit,
            path=bundle_root,
        )
        if not entries:
            self._invalid(
                "ci_submission_missing",
                "The exact head does not contain the declared Submission bundle.",
            )
        for entry in entries:
            if entry.mode != "100644" or entry.object_type != "blob":
                raise RCPError(
                    code="ci_tree_entry_invalid",
                    message="Submission files must be regular non-executable Git blobs.",
                    context={"commit": commit, "path": entry.path},
                )
        paths = {entry.path for entry in entries}
        submission = self._record(
            root,
            commit=commit,
            path=self._submission_path(submission_id),
            model_type=ResearchSubmission,
        )
        if submission.state is not SubmissionState.OPEN:
            self._invalid(
                "ci_proposal_state_invalid",
                "The proposal commit must contain an open Submission.",
            )
        proposal_path = f"{bundle_root}/proposed-report.yaml"
        proposal = self._record(
            root,
            commit=commit,
            path=proposal_path,
            model_type=ReportProposal,
        )
        spec_paths = sorted(path for path in paths if _EVIDENCE_SPEC_PATH.fullmatch(path))
        evidence: list[SubmissionEvidence] = []
        for spec_path in spec_paths:
            match = _EVIDENCE_SPEC_PATH.fullmatch(spec_path)
            assert match is not None
            if match.group(1) != submission_id:
                self._invalid(
                    "ci_evidence_path_invalid",
                    "Submission evidence is stored under another Submission identity.",
                )
            run_id = match.group(2)
            result_path = spec_path.removesuffix("spec.yaml") + "result.yaml"
            spec = self._record(
                root,
                commit=commit,
                path=spec_path,
                model_type=RunSpec,
            )
            result = self._record(
                root,
                commit=commit,
                path=result_path,
                model_type=RunResult,
            )
            if spec.run_id != run_id:
                self._invalid(
                    "ci_evidence_path_invalid",
                    "RunSpec identity does not match its Submission evidence path.",
                )
            evidence.append(SubmissionEvidence(spec=spec, result=result))
        bundle = self.submissions.build(
            task=task,
            submission=submission,
            proposal=proposal,
            evidence=tuple(evidence),
            policy=policy,
        )
        expected_paths = {item.path for item in bundle.files}
        if paths != expected_paths:
            self._path_scope_invalid(
                expected=tuple(sorted(expected_paths)),
                observed=tuple(sorted(paths)),
            )
        self._require_generated_files(root, commit=commit, files=bundle.files)
        return submission, proposal, tuple(evidence), bundle

    def _current_report(
        self,
        root: Path,
        *,
        base_commit: str,
        proposal: ReportProposal,
    ) -> ReportRecord | None:
        report_root = f".research/reports/{proposal.report_id}"
        entries = self.git.list_entries(
            root,
            commit=base_commit,
            path=report_root,
        )
        if not entries:
            return None
        revisions: list[tuple[int, ReportRecord]] = []
        markdown_revisions: set[int] = set()
        for entry in entries:
            match = _REPORT_PATH.fullmatch(entry.path)
            markdown_match = _REPORT_MARKDOWN_PATH.fullmatch(entry.path)
            if (
                entry.mode != "100644"
                or entry.object_type != "blob"
            ):
                self._invalid(
                    "ci_report_store_invalid",
                    "Protected-base Report revisions are not canonical immutable files.",
                )
            if markdown_match is not None and markdown_match.group(1) == proposal.report_id:
                markdown_revisions.add(int(markdown_match.group(2)))
                continue
            if match is None or match.group(1) != proposal.report_id:
                self._invalid(
                    "ci_report_store_invalid",
                    "Protected-base Report revisions must be YAML and Markdown pairs.",
                )
            revision = int(match.group(2))
            report = self._record(
                root,
                commit=base_commit,
                path=entry.path,
                model_type=ReportRecord,
            )
            if report.report_id != proposal.report_id or report.revision != revision:
                self._invalid(
                    "ci_report_store_invalid",
                    "Protected-base Report identity does not match its revision path.",
                )
            revisions.append((revision, report))
        observed_revisions = sorted(revision for revision, _report in revisions)
        if set(observed_revisions) != markdown_revisions:
            self._invalid(
                "ci_report_store_invalid",
                "Protected-base Report revisions must have matching Markdown renders.",
            )
        if observed_revisions != list(range(1, max(observed_revisions) + 1)):
            self._invalid(
                "ci_report_store_invalid",
                "Protected-base Report revision history contains a gap.",
            )
        return max(revisions, key=lambda item: item[0])[1]

    def _verify_sources(
        self,
        root: Path,
        evidence: tuple[SubmissionEvidence, ...],
        *,
        task: TaskRecord,
        trusted_base_commit: str,
    ) -> str:
        identities: list[dict[str, object]] = []
        for item in evidence:
            source = self.git.read_commit(root, item.spec.source_commit)
            if source.tree != item.spec.source_tree:
                self._invalid(
                    "ci_run_source_mismatch",
                    "RunSpec source commit does not bind the declared source tree.",
                )
            scope = self.write_scope.validate_source(
                task=task,
                repository_root=root,
                trusted_base_commit=trusted_base_commit,
                baseline_commit=item.spec.baseline_commit,
                source_commit=item.spec.source_commit,
            )
            identities.append(
                {
                    "run_id": item.spec.run_id,
                    "source_commit": item.spec.source_commit,
                    "source_tree": item.spec.source_tree,
                    "write_scope": scope.as_dict(),
                    "spec_digest": item.spec.spec_digest,
                    "result_digest": canonical_digest(item.result),
                }
            )
        return canonical_digest({"runs": identities})

    def _require_generated_files(
        self,
        root: Path,
        *,
        commit: str,
        files: tuple[RenderedSubmissionFile, ...],
    ) -> None:
        for item in files:
            observed = self._blob(root, commit, item.path)
            if observed != item.content:
                raise RCPError(
                    code="ci_generated_output_mismatch",
                    message="A generated Submission file is not byte reproducible.",
                    context={"path": item.path},
                )

    def _require_changes(
        self,
        root: Path,
        *,
        old_commit: str,
        new_commit: str,
        expected_paths: tuple[str, ...],
        additions: tuple[str, ...] = (),
        modifications: tuple[str, ...] = (),
    ) -> tuple[GitTreeChange, ...]:
        changes = self.git.changes(
            root,
            old_commit=old_commit,
            new_commit=new_commit,
        )
        observed_paths = tuple(change.path for change in changes)
        if observed_paths != tuple(sorted(expected_paths)):
            self._path_scope_invalid(
                expected=tuple(sorted(expected_paths)),
                observed=observed_paths,
            )
        addition_set = set(additions)
        modification_set = set(modifications)
        if addition_set & modification_set:
            raise AssertionError("CI expected path classifications overlap")
        for change in changes:
            if change.path in addition_set:
                expected = ("000000", "100644", "A")
            elif change.path in modification_set:
                expected = ("100644", "100644", "M")
            else:
                self._invalid(
                    "ci_changed_path_classification_invalid",
                    "A changed path has no trusted CI classification.",
                )
            observed = (change.old_mode, change.new_mode, change.status)
            if observed != expected:
                raise RCPError(
                    code="ci_changed_path_mode_invalid",
                    message="A changed path has an unsafe type, mode, or operation.",
                    context={
                        "path": change.path,
                        "expected": list(expected),
                        "observed": list(observed),
                    },
                )
        return changes

    @staticmethod
    def _require_commit_marker(
        commit: GitCommitData,
        *,
        parent: str,
        pattern: re.Pattern[str],
        submission_id: str,
        code: str,
    ) -> None:
        matched = pattern.fullmatch(commit.message.rstrip("\n"))
        if (
            commit.parents != (parent,)
            or matched is None
            or matched.group(1) != submission_id
        ):
            raise RCPError(
                code=code,
                message="Submission commit parent or operation marker is invalid.",
            )

    def _record(
        self,
        root: Path,
        *,
        commit: str,
        path: str,
        model_type: type[_ModelT],
    ) -> _ModelT:
        return self._parse_canonical(
            self._blob(root, commit, path),
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
            text = content.decode("utf-8")
            record = model_type.model_validate(load_yaml(text))
        except (UnicodeError, TypeError, ValueError, ValidationError) as error:
            raise RCPError(
                code="ci_record_invalid",
                message="Exact-head validation found a malformed protocol record.",
                context={"path": path},
            ) from error
        if dump_yaml(record).encode("utf-8") != content:
            raise RCPError(
                code="ci_record_not_canonical",
                message="Exact-head protocol records must use canonical YAML.",
                context={"path": path},
            )
        return record

    def _blob(self, root: Path, commit: str, path: str) -> bytes:
        content = self.git.read_blob_at(
            root,
            commit=commit,
            path=path,
        )
        assert content is not None
        return content

    @staticmethod
    def _submission_path(submission_id: str) -> str:
        return f".research/submissions/{submission_id}/submission.yaml"

    @staticmethod
    def _sha256(content: bytes) -> str:
        return f"sha256:{hashlib.sha256(content).hexdigest()}"

    @staticmethod
    def _changes_digest(changes: tuple[GitTreeChange, ...]) -> str:
        return canonical_digest(
            {
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
                ]
            }
        )

    @staticmethod
    def _checks(evidence: dict[str, str]) -> tuple[CIValidationCheck, ...]:
        return tuple(
            CIValidationCheck(
                name=name,
                status="passed",
                evidence_digest=evidence[name],
            )
            for name in sorted(evidence)
        )

    @staticmethod
    def _path_scope_invalid(
        *,
        expected: tuple[str, ...],
        observed: tuple[str, ...],
    ) -> Never:
        raise RCPError(
            code="ci_changed_path_scope_invalid",
            message="The PR changes do not match a closed Submission path set.",
            context={
                "expected": list(expected),
                "observed": list(observed),
            },
        )

    @staticmethod
    def _invalid(code: str, message: str) -> Never:
        raise RCPError(code=code, message=message)
