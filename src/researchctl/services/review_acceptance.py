from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from researchctl.domain.enums import (
    ClaimScope,
    CodeDisposition,
    EvidenceStatus,
    ReportApplicability,
    ReviewDisposition,
    SubmissionCategory,
    SubmissionState,
)
from researchctl.domain.models import (
    ProjectPolicy,
    ReportProposal,
    ReportRecord,
    ResearchSubmission,
    ReviewDecision,
    TaskRecord,
    ValidationBasis,
)
from researchctl.errors import RCPError
from researchctl.serialization import canonical_digest, dump_yaml
from researchctl.services.report_renderer import render_accepted_report
from researchctl.services.submissions import (
    RenderedSubmissionFile,
    SubmissionBundleBuilder,
    SubmissionEvidence,
)


@dataclass(frozen=True, slots=True)
class AcceptanceBundle:
    submission: ResearchSubmission
    decision: ReviewDecision
    report: ReportRecord
    source_digest: str
    files: tuple[RenderedSubmissionFile, ...]
    manifest_digest: str

    def as_dict(self) -> dict[str, object]:
        return {
            "submission_id": self.submission.submission_id,
            "decision_id": self.decision.decision_id,
            "report_id": self.report.report_id,
            "report_revision": self.report.revision,
            "source_digest": self.source_digest,
            "manifest_digest": self.manifest_digest,
            "files": [item.as_dict() for item in self.files],
        }


class ReviewAcceptanceBuilder:
    def __init__(self, submissions: SubmissionBundleBuilder | None = None) -> None:
        self.submissions = submissions or SubmissionBundleBuilder()

    def build(
        self,
        *,
        task: TaskRecord,
        submission: ResearchSubmission,
        proposal: ReportProposal,
        evidence: tuple[SubmissionEvidence, ...],
        current_report: ReportRecord | None,
        decision_id: str,
        reviewer_actor: str,
        decided_at: datetime,
        disposition: ReviewDisposition,
        conditions: tuple[str, ...],
        claim_scope: ClaimScope,
        code_disposition: CodeDisposition,
        accepted_base_tree: str,
        policy: ProjectPolicy | None = None,
    ) -> AcceptanceBundle:
        open_bundle = self.submissions.build(
            task=task,
            submission=submission,
            proposal=proposal,
            evidence=evidence,
            policy=policy,
        )
        self._validate_revision(proposal, current_report)
        if (
            submission.category is SubmissionCategory.FAILURE_RECORD
            and claim_scope is not ClaimScope.SNAPSHOT
        ):
            raise RCPError(
                code="failure_submission_scope_invalid",
                message="Failure records can only be accepted as snapshot-scoped claims.",
            )
        accepted_submission = submission.model_copy(
            update={"state": SubmissionState.ACCEPTED}
        )
        accepted_submission = ResearchSubmission.model_validate(
            accepted_submission.model_dump(mode="json", exclude_none=True)
        )
        decision = ReviewDecision(
            decision_id=decision_id,
            submission_id=submission.submission_id,
            disposition=disposition,
            reviewer_actor=reviewer_actor,
            decided_at=decided_at,
            conditions=conditions,
            claim_scope=claim_scope,
            code_disposition=code_disposition,
            report_id=proposal.report_id,
            expected_report_revision=proposal.expected_report_revision,
            accepted_submission_digest=canonical_digest(accepted_submission),
        )
        applicability = (
            ReportApplicability.SNAPSHOT_ONLY
            if claim_scope is ClaimScope.SNAPSHOT
            else ReportApplicability.CURRENT
        )
        validation_basis = (
            None
            if claim_scope is ClaimScope.SNAPSHOT
            else ValidationBasis(main_tree=accepted_base_tree, assessed_at=decided_at)
        )
        report = ReportRecord(
            report_id=proposal.report_id,
            revision=proposal.expected_report_revision + 1,
            title=proposal.title,
            claim=submission.claim,
            claim_scope=claim_scope,
            evidence_status=EvidenceStatus.VERIFIED,
            applicability=applicability,
            submission_id=submission.submission_id,
            run_result_ids=submission.run_result_ids,
            evidence_tree=proposal.evidence_tree,
            accepted_at_main_tree=accepted_base_tree,
            validation_basis=validation_basis,
            dependencies=submission.dependencies,
            supersedes=proposal.supersedes,
        )
        source_digest = canonical_digest(
            {
                "open_bundle_source_digest": open_bundle.source_digest,
                "accepted_submission_digest": canonical_digest(accepted_submission),
                "decision_digest": canonical_digest(decision),
                "report_digest": canonical_digest(report),
            }
        )
        report_root = f".research/reports/{report.report_id}/{report.revision}"
        evidence_by_result = {item.result.result_id: item for item in evidence}
        results = tuple(
            evidence_by_result[result_id].result
            for result_id in submission.run_result_ids
        )
        files = (
            RenderedSubmissionFile(
                (
                    f".research/submissions/{submission.submission_id}/"
                    "submission.yaml"
                ),
                dump_yaml(accepted_submission).encode("utf-8"),
            ),
            RenderedSubmissionFile(
                f".research/decisions/{decision.decision_id}.yaml",
                dump_yaml(decision).encode("utf-8"),
            ),
            RenderedSubmissionFile(
                f"{report_root}.yaml",
                dump_yaml(report).encode("utf-8"),
            ),
            RenderedSubmissionFile(
                f"{report_root}.md",
                render_accepted_report(
                    task=task,
                    submission=accepted_submission,
                    decision=decision,
                    report=report,
                    results=results,
                    source_digest=source_digest,
                ),
            ),
        )
        rendered = tuple(sorted(files, key=lambda item: item.path))
        manifest_digest = canonical_digest(
            {
                "source_digest": source_digest,
                "files": [item.as_dict() for item in rendered],
            }
        )
        return AcceptanceBundle(
            submission=accepted_submission,
            decision=decision,
            report=report,
            source_digest=source_digest,
            files=rendered,
            manifest_digest=manifest_digest,
        )

    @staticmethod
    def _validate_revision(
        proposal: ReportProposal,
        current: ReportRecord | None,
    ) -> None:
        observed = 0 if current is None else current.revision
        if current is not None and current.report_id != proposal.report_id:
            raise RCPError(
                code="report_identity_mismatch",
                message="Current Report does not match the proposal target.",
            )
        if proposal.expected_report_revision != observed:
            raise RCPError(
                code="stale_report_revision",
                message="Report changed after the proposal was prepared.",
                context={
                    "expected_revision": proposal.expected_report_revision,
                    "observed_revision": observed,
                },
            )
