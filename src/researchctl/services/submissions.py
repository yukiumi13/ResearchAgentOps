from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import PurePosixPath

from researchctl.domain.enums import RunOutcome, SubmissionCategory, SubmissionState
from researchctl.domain.models import (
    ReportProposal,
    ResearchSubmission,
    RunResult,
    RunSpec,
    TaskRecord,
)
from researchctl.errors import RCPError
from researchctl.serialization import canonical_digest, dump_yaml
from researchctl.services.report_renderer import (
    render_report_preview,
    render_submission_review,
)
from researchctl.services.run_preflight import validate_task_required_inputs


@dataclass(frozen=True, slots=True)
class SubmissionEvidence:
    spec: RunSpec
    result: RunResult


@dataclass(frozen=True, slots=True)
class RenderedSubmissionFile:
    path: str
    content: bytes

    @property
    def digest(self) -> str:
        return f"sha256:{hashlib.sha256(self.content).hexdigest()}"

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "digest": self.digest,
            "size_bytes": len(self.content),
        }


@dataclass(frozen=True, slots=True)
class SubmissionBundle:
    submission_id: str
    source_digest: str
    files: tuple[RenderedSubmissionFile, ...]
    manifest_digest: str

    def as_dict(self) -> dict[str, object]:
        return {
            "submission_id": self.submission_id,
            "source_digest": self.source_digest,
            "manifest_digest": self.manifest_digest,
            "files": [item.as_dict() for item in self.files],
        }


class SubmissionBundleBuilder:
    def build(
        self,
        *,
        task: TaskRecord,
        submission: ResearchSubmission,
        proposal: ReportProposal,
        evidence: tuple[SubmissionEvidence, ...],
    ) -> SubmissionBundle:
        ordered = self._validate_and_order(
            task=task,
            submission=submission,
            proposal=proposal,
            evidence=evidence,
        )
        source_digest = canonical_digest(
            {
                "task_digest": canonical_digest(task),
                "submission_digest": canonical_digest(submission),
                "report_proposal_digest": canonical_digest(proposal),
                "evidence": [
                    {
                        "run_spec_digest": item.spec.spec_digest,
                        "run_result_digest": canonical_digest(item.result),
                    }
                    for item in ordered
                ],
            }
        )
        root = f".research/submissions/{submission.submission_id}"
        files = [
            RenderedSubmissionFile(
                f"{root}/submission.yaml",
                dump_yaml(submission).encode("utf-8"),
            ),
            RenderedSubmissionFile(
                f"{root}/proposed-report.yaml",
                dump_yaml(proposal).encode("utf-8"),
            ),
        ]
        for item in ordered:
            evidence_root = f"{root}/evidence/{item.spec.run_id}"
            files.extend(
                (
                    RenderedSubmissionFile(
                        f"{evidence_root}/spec.yaml",
                        dump_yaml(item.spec).encode("utf-8"),
                    ),
                    RenderedSubmissionFile(
                        f"{evidence_root}/result.yaml",
                        dump_yaml(item.result).encode("utf-8"),
                    ),
                )
            )
        results = tuple(item.result for item in ordered)
        files.extend(
            (
                RenderedSubmissionFile(
                    f"{root}/review.md",
                    render_submission_review(
                        task=task,
                        submission=submission,
                        proposal=proposal,
                        results=results,
                        source_digest=source_digest,
                    ),
                ),
                RenderedSubmissionFile(
                    f"{root}/report-preview.md",
                    render_report_preview(
                        task=task,
                        submission=submission,
                        proposal=proposal,
                        results=results,
                        source_digest=source_digest,
                    ),
                ),
            )
        )
        rendered = tuple(sorted(files, key=lambda item: item.path))
        manifest_digest = canonical_digest(
            {
                "source_digest": source_digest,
                "files": [item.as_dict() for item in rendered],
            }
        )
        return SubmissionBundle(
            submission_id=submission.submission_id,
            source_digest=source_digest,
            files=rendered,
            manifest_digest=manifest_digest,
        )

    @staticmethod
    def _validate_and_order(
        *,
        task: TaskRecord,
        submission: ResearchSubmission,
        proposal: ReportProposal,
        evidence: tuple[SubmissionEvidence, ...],
    ) -> tuple[SubmissionEvidence, ...]:
        if submission.state is not SubmissionState.OPEN:
            raise RCPError(
                code="submission_state_invalid",
                message="A new ResearchSubmission must be open.",
            )
        if submission.task_id != task.task_id:
            raise RCPError(
                code="submission_task_mismatch",
                message="ResearchSubmission does not belong to the supplied Task.",
            )
        if proposal.submission_id != submission.submission_id:
            raise RCPError(
                code="submission_report_proposal_mismatch",
                message="ReportProposal does not belong to this ResearchSubmission.",
            )
        indexed: dict[str, SubmissionEvidence] = {}
        run_ids: set[str] = set()
        for item in evidence:
            if item.result.result_id in indexed or item.spec.run_id in run_ids:
                raise RCPError(
                    code="submission_evidence_duplicate",
                    message="Submission evidence identities must be unique.",
                )
            indexed[item.result.result_id] = item
            run_ids.add(item.spec.run_id)
        if set(indexed) != set(submission.run_result_ids):
            raise RCPError(
                code="submission_evidence_set_mismatch",
                message="Submission evidence does not match declared RunResult IDs.",
            )
        ordered = tuple(indexed[result_id] for result_id in submission.run_result_ids)
        for item in ordered:
            if (
                item.spec.task_id != task.task_id
                or item.spec.session_id != submission.session_id
                or item.result.run_id != item.spec.run_id
                or item.result.run_spec_digest != item.spec.spec_digest
            ):
                raise RCPError(
                    code="submission_evidence_linkage_invalid",
                    message="RunSpec and RunResult linkage does not match the Submission.",
                )
            validate_task_required_inputs(item.spec, task)
            if item.spec.source_tree != proposal.evidence_tree:
                raise RCPError(
                    code="submission_evidence_tree_mismatch",
                    message="All submitted Runs must share the proposed evidence tree.",
                )
        outcomes = {item.result.outcome for item in ordered}
        if submission.category is SubmissionCategory.FAILURE_RECORD:
            if RunOutcome.COMPLETE in outcomes:
                raise RCPError(
                    code="submission_category_evidence_invalid",
                    message="A failure_record cannot include complete RunResults.",
                )
        elif outcomes != {RunOutcome.COMPLETE}:
            raise RCPError(
                code="submission_category_evidence_invalid",
                message="Candidate and negative results require complete RunResults.",
            )
        for item in submission.review_bundle:
            if PurePosixPath(item.name).name != item.name:
                raise RCPError(
                    code="submission_review_bundle_name_invalid",
                    message="Review bundle names must be safe single path components.",
                )
        return ordered
