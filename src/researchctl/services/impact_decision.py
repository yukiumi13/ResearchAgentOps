from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime

from pydantic import TypeAdapter

from researchctl.domain.enums import (
    EvidenceStatus,
    ImpactDisposition,
    ReportApplicability,
)
from researchctl.domain.models import (
    DependencySet,
    ImpactDecision,
    ReportImpact,
    ReportRecord,
    ValidationBasis,
)
from researchctl.domain.types import UtcDateTime
from researchctl.errors import RCPError
from researchctl.serialization import canonical_digest, dump_yaml
from researchctl.services.report_impact import RenderedImpactFile

IMPACT_DECISION_RENDERER_ID = "research-impact-decision.v2"
IMPACT_DECISION_RENDERER_VERSION = 2
_UTC_DATETIME_ADAPTER = TypeAdapter(UtcDateTime)


@dataclass(frozen=True, slots=True)
class ImpactDecisionBundle:
    impact: ReportImpact
    current_report: ReportRecord
    decision: ImpactDecision
    report: ReportRecord
    source_digest: str
    files: tuple[RenderedImpactFile, ...]
    manifest_digest: str

    def as_dict(self) -> dict[str, object]:
        return {
            "decision_id": self.decision.decision_id,
            "impact_id": self.impact.impact_id,
            "impact_digest": self.impact.impact_digest,
            "report_id": self.report.report_id,
            "report_revision": self.report.revision,
            "disposition": self.decision.disposition.value,
            "rerun_task_id": self.decision.rerun_task_id,
            "source_digest": self.source_digest,
            "manifest_digest": self.manifest_digest,
            "files": [item.as_dict() for item in self.files],
            "automatically_runs_experiments": False,
        }


class ImpactDecisionBuilder:
    """Materialize one explicit manager disposition without executing work."""

    def build(
        self,
        *,
        impact: ReportImpact,
        report: ReportRecord,
        decision_id: str,
        expected_report_revision: int,
        decision_base_commit: str,
        decision_base_tree: str,
        disposition: ImpactDisposition,
        reviewer_actor: str,
        reason: str,
        decided_at: datetime,
        rerun_task_id: str | None = None,
        replacement_dependencies: DependencySet | None = None,
    ) -> ImpactDecisionBundle:
        if report.report_id != impact.report_id:
            raise RCPError(
                code="impact_decision_report_mismatch",
                message="Impact decision Report does not match the Impact record.",
            )
        if report.revision != expected_report_revision:
            raise RCPError(
                code="stale_report_revision",
                message="Report changed after the Impact decision was requested.",
                context={
                    "expected_revision": expected_report_revision,
                    "observed_revision": report.revision,
                },
            )
        if report.revision != impact.expected_report_revision + 1:
            raise RCPError(
                code="stale_impact_report",
                message=(
                    "Impact no longer identifies the current proposed Report "
                    "revision."
                ),
            )
        if canonical_digest(report) != impact.proposed_report_digest:
            raise RCPError(
                code="impact_report_digest_mismatch",
                message="Accepted Report bytes do not match the bound Impact proposal.",
            )
        if disposition is ImpactDisposition.DEPENDENCY_FIX:
            if replacement_dependencies is None:
                raise RCPError(
                    code="impact_dependency_fix_missing",
                    message="Dependency correction requires replacement dependencies.",
                )
            if replacement_dependencies == report.dependencies:
                raise RCPError(
                    code="impact_dependency_fix_no_change",
                    message="Dependency correction must change the declaration.",
                )
        elif replacement_dependencies is not None:
            raise RCPError(
                code="impact_decision_input_invalid",
                message="Only dependency_fix may replace Report dependencies.",
            )
        if (disposition is ImpactDisposition.RERUN) != (
            rerun_task_id is not None
        ):
            raise RCPError(
                code="impact_rerun_task_invalid",
                message="Rerun disposition must bind exactly one existing Task.",
            )
        if (
            disposition is ImpactDisposition.WAIVE
            and report.evidence_status is not EvidenceStatus.VERIFIED
        ):
            raise RCPError(
                code="impact_waiver_evidence_invalid",
                message="Invalid evidence cannot be made current by waiver.",
            )

        canonical_time = _UTC_DATETIME_ADAPTER.dump_python(
            _UTC_DATETIME_ADAPTER.validate_python(decided_at),
            mode="json",
        )
        report_update: dict[str, object] = {"revision": report.revision + 1}
        if disposition is ImpactDisposition.WAIVE:
            report_update.update(
                {
                    "applicability": ReportApplicability.CURRENT,
                    "validation_basis": ValidationBasis(
                        main_tree=decision_base_tree,
                        assessed_at=canonical_time,
                    ),
                }
            )
        else:
            report_update["applicability"] = ReportApplicability.STALE
        if disposition is ImpactDisposition.INVALIDATE:
            report_update["evidence_status"] = EvidenceStatus.INVALID
        if disposition is ImpactDisposition.DEPENDENCY_FIX:
            report_update["dependencies"] = replacement_dependencies
        decided_report = ReportRecord.model_validate(
            report.model_copy(update=report_update).model_dump(
                mode="json",
                exclude_none=True,
            )
        )
        payload = {
            "schema_version": report.schema_version,
            "decision_id": decision_id,
            "impact_id": impact.impact_id,
            "report_id": report.report_id,
            "expected_report_revision": expected_report_revision,
            "expected_impact_digest": impact.impact_digest,
            "impact_target_commit": impact.target_commit,
            "impact_target_tree": impact.target_tree,
            "decision_base_commit": decision_base_commit,
            "decision_base_tree": decision_base_tree,
            "disposition": disposition.value,
            "reviewer_actor": reviewer_actor,
            "reason": reason,
            "rerun_task_id": rerun_task_id,
            "replacement_dependencies": (
                replacement_dependencies.model_dump(
                    mode="json",
                    exclude_none=True,
                )
                if replacement_dependencies is not None
                else None
            ),
            "decided_at": canonical_time,
        }
        canonical_payload = {
            key: value for key, value in payload.items() if value is not None
        }
        decision = ImpactDecision.model_validate(
            {
                **canonical_payload,
                "decision_digest": canonical_digest(canonical_payload),
            }
        )
        source_digest = canonical_digest(
            {
                "renderer_id": IMPACT_DECISION_RENDERER_ID,
                "renderer_version": IMPACT_DECISION_RENDERER_VERSION,
                "impact_digest": impact.impact_digest,
                "current_report_digest": canonical_digest(report),
                "decision_digest": decision.decision_digest,
                "report_digest": canonical_digest(decided_report),
            }
        )
        report_root = (
            f".research/reports/{report.report_id}/{decided_report.revision}"
        )
        files = tuple(
            sorted(
                (
                    RenderedImpactFile(
                        f".research/decisions/{decision.decision_id}.yaml",
                        dump_yaml(decision).encode("utf-8"),
                    ),
                    RenderedImpactFile(
                        f"{report_root}.yaml",
                        dump_yaml(decided_report).encode("utf-8"),
                    ),
                    RenderedImpactFile(
                        f"{report_root}.md",
                        render_impact_decision_report(
                            impact=impact,
                            decision=decision,
                            report=decided_report,
                            source_digest=source_digest,
                        ),
                    ),
                ),
                key=lambda item: item.path,
            )
        )
        manifest_digest = canonical_digest(
            {
                "source_digest": source_digest,
                "files": [item.as_dict() for item in files],
            }
        )
        return ImpactDecisionBundle(
            impact=impact,
            current_report=report,
            decision=decision,
            report=decided_report,
            source_digest=source_digest,
            files=files,
            manifest_digest=manifest_digest,
        )


def _text(value: object) -> str:
    escaped = html.escape(str(value), quote=False).replace("\\", "\\\\")
    for character in ("`", "*", "_", "[", "]", "#", "|"):
        escaped = escaped.replace(character, f"\\{character}")
    return escaped.replace("\r\n", "\n").replace("\r", "\n").replace(
        "\n", "<br>\n"
    )


def render_impact_decision_report(
    *,
    impact: ReportImpact,
    decision: ImpactDecision,
    report: ReportRecord,
    source_digest: str,
) -> bytes:
    lines = [
        f"# {_text(report.title)}",
        "",
        (
            "> Proposed manager Impact decision. It is accepted only after "
            "protected review and merge."
        ),
        "",
        f"- Report: `{report.report_id}` revision `{report.revision}`",
        f"- Impact: `{impact.impact_id}`",
        f"- Impact digest: `{impact.impact_digest}`",
        f"- Decision: `{decision.decision_id}`",
        f"- Disposition: `{decision.disposition.value}`",
        f"- Reviewer actor: `{_text(decision.reviewer_actor)}`",
        f"- Evidence status: `{report.evidence_status.value}`",
        f"- Applicability: `{report.applicability.value}`",
        f"- Decision base commit: `{decision.decision_base_commit}`",
        f"- Decision base tree: `{decision.decision_base_tree}`",
        f"- Canonical source digest: `{source_digest}`",
        "",
        "## Reason",
        "",
        _text(decision.reason),
        "",
        "## Execution Boundary",
        "",
        (
            f"- Rerun Task: `{decision.rerun_task_id}`"
            if decision.rerun_task_id is not None
            else "- No rerun Task is referenced."
        ),
        "- This decision does not start, retry, or collect a Run.",
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")
