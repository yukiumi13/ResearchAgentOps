from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime

from pydantic import TypeAdapter

from researchctl.domain.enums import (
    ClaimScope,
    ReportApplicability,
)
from researchctl.domain.models import (
    DependencyChangeReceipt,
    ReportImpact,
    ReportImpactBatch,
    ReportRecord,
    ValidationBasis,
)
from researchctl.domain.types import UtcDateTime
from researchctl.errors import RCPError
from researchctl.serialization import canonical_digest, dump_yaml
from researchctl.services.dependency_impact import (
    GIT_TREE_CHANGE_PROVIDER_ID,
    DeclaredDependencyImpactEvaluator,
    DependencyImpactEvaluation,
    DependencyImpactEvaluator,
    path_dependency_matches,
)


IMPACT_ANALYZER_ID = "researchctl.report-impact.v3"
IMPACT_RENDERER_ID = "research-impact-report.v2"
IMPACT_RENDERER_VERSION = 2
_UTC_DATETIME_ADAPTER = TypeAdapter(UtcDateTime)


@dataclass(frozen=True, slots=True)
class RenderedImpactFile:
    path: str
    content: bytes

    @property
    def digest(self) -> str:
        import hashlib

        return f"sha256:{hashlib.sha256(self.content).hexdigest()}"

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "digest": self.digest,
            "size_bytes": len(self.content),
        }


@dataclass(frozen=True, slots=True)
class ReportImpactBundle:
    impact: ReportImpact
    current_report: ReportRecord
    proposed_report: ReportRecord
    source_digest: str
    files: tuple[RenderedImpactFile, ...]
    manifest_digest: str

    def as_dict(self) -> dict[str, object]:
        return {
            "impact_id": self.impact.impact_id,
            "report_id": self.impact.report_id,
            "expected_report_revision": self.impact.expected_report_revision,
            "proposed_report_revision": self.proposed_report.revision,
            "outcome": self.impact.outcome,
            "proposed_applicability": self.impact.proposed_applicability.value,
            "change_provider_id": self.impact.change_provider_id,
            "dependency_evaluator_id": self.impact.dependency_evaluator_id,
            "dependency_receipt_digests": [
                item.receipt_digest for item in self.impact.dependency_receipts
            ],
            "changed_paths": list(self.impact.changed_paths),
            "matched_path_dependencies": list(
                self.impact.matched_path_dependencies
            ),
            "matched_resource_dependencies": list(
                self.impact.matched_resource_dependencies
            ),
            "matched_environment_dependencies": list(
                self.impact.matched_environment_dependencies
            ),
            "unresolved_resource_dependencies": list(
                self.impact.unresolved_resource_dependencies
            ),
            "unresolved_environment_dependencies": list(
                self.impact.unresolved_environment_dependencies
            ),
            "source_digest": self.source_digest,
            "files": [item.as_dict() for item in self.files],
            "manifest_digest": self.manifest_digest,
        }


@dataclass(frozen=True, slots=True)
class ReportImpactBatchBundle:
    batch: ReportImpactBatch
    report_bundles: tuple[ReportImpactBundle, ...]
    source_digest: str
    files: tuple[RenderedImpactFile, ...]
    manifest_digest: str

    def as_dict(self) -> dict[str, object]:
        return {
            "impact_id": self.batch.impact_id,
            "before_commit": self.batch.before_commit,
            "target_commit": self.batch.target_commit,
            "target_tree": self.batch.target_tree,
            "report_count": len(self.report_bundles),
            "reports": [
                {
                    "report_id": item.impact.report_id,
                    "expected_report_revision": (
                        item.impact.expected_report_revision
                    ),
                    "proposed_report_revision": item.proposed_report.revision,
                    "outcome": item.impact.outcome,
                    "proposed_applicability": (
                        item.impact.proposed_applicability.value
                    ),
                    "change_provider_id": item.impact.change_provider_id,
                    "dependency_evaluator_id": (
                        item.impact.dependency_evaluator_id
                    ),
                    "dependency_receipt_digests": [
                        receipt.receipt_digest
                        for receipt in item.impact.dependency_receipts
                    ],
                    "matched_path_dependencies": list(
                        item.impact.matched_path_dependencies
                    ),
                }
                for item in self.report_bundles
            ],
            "snapshot_report_ids": list(self.batch.snapshot_report_ids),
            "ineligible_report_ids": list(self.batch.ineligible_report_ids),
            "up_to_date_report_ids": list(self.batch.up_to_date_report_ids),
            "no_code_change_report_ids": list(
                self.batch.no_code_change_report_ids
            ),
            "unresolved_report_ids": list(self.batch.unresolved_report_ids),
            "source_digest": self.source_digest,
            "files": [item.as_dict() for item in self.files],
            "manifest_digest": self.manifest_digest,
        }


class ReportImpactBuilder:
    """Build a deterministic, review-only Report validity revision."""

    def __init__(
        self,
        *,
        evaluator: DependencyImpactEvaluator | None = None,
    ) -> None:
        self.evaluator = evaluator or DeclaredDependencyImpactEvaluator()

    def build(
        self,
        *,
        impact_id: str,
        report: ReportRecord,
        target_commit: str,
        target_tree: str,
        changed_paths: tuple[str, ...],
        generated_at: datetime,
        change_provider_id: str = GIT_TREE_CHANGE_PROVIDER_ID,
        dependency_receipts: tuple[DependencyChangeReceipt, ...] = (),
    ) -> ReportImpactBundle:
        canonical_time = _UTC_DATETIME_ADAPTER.dump_python(
            _UTC_DATETIME_ADAPTER.validate_python(generated_at),
            mode="json",
        )
        if report.claim_scope is ClaimScope.SNAPSHOT:
            raise RCPError(
                code="report_impact_not_applicable",
                message="Snapshot Reports remain historical facts and are not advanced.",
                context={"report_id": report.report_id},
            )
        if report.applicability not in {
            ReportApplicability.CURRENT,
            ReportApplicability.IMPACT_PENDING,
        }:
            raise RCPError(
                code="report_impact_not_eligible",
                message="Only current or impact-pending baseline Reports can be analyzed.",
                context={
                    "report_id": report.report_id,
                    "applicability": report.applicability.value,
                },
            )
        basis = report.validation_basis
        if basis is None:
            raise RCPError(
                code="report_validation_basis_missing",
                message="A baseline Report has no validation basis.",
            )
        canonical_paths = tuple(sorted(set(changed_paths)))
        canonical_receipts = tuple(
            sorted(dependency_receipts, key=lambda item: item.receipt_id)
        )
        receipt_ids = tuple(item.receipt_id for item in canonical_receipts)
        if len(receipt_ids) != len(set(receipt_ids)):
            raise RCPError(
                code="impact_dependency_receipt_invalid",
                message="Dependency receipt IDs must be unique.",
            )
        for receipt in canonical_receipts:
            if (
                receipt.basis_tree != basis.main_tree
                or receipt.target_commit != target_commit
                or receipt.target_tree != target_tree
            ):
                raise RCPError(
                    code="impact_dependency_receipt_target_mismatch",
                    message=(
                        "Dependency receipt is not bound to the Report basis "
                        "and exact target."
                    ),
                    context={"receipt_id": receipt.receipt_id},
                )
        if (
            not canonical_paths
            and not canonical_receipts
            and not report.dependencies.resources
            and not report.dependencies.environments
        ):
            raise RCPError(
                code="report_impact_no_change",
                message="No code paths changed after excluding protocol-only state.",
                context={"report_id": report.report_id},
            )
        evaluator_id = self.evaluator.evaluator_id
        try:
            evaluation = self.evaluator.evaluate(
                dependencies=report.dependencies,
                changed_paths=canonical_paths,
                dependency_receipts=canonical_receipts,
            )
        except (TypeError, ValueError) as error:
            raise RCPError(
                code="impact_evaluator_invalid",
                message="Dependency evaluator rejected its typed evidence.",
                context={"evaluator_id": evaluator_id},
            ) from error
        expected_receipt_digests = tuple(
            sorted(item.receipt_digest for item in canonical_receipts)
        )
        external_observations = {
            (observation.kind, observation.dependency): observation.state
            for receipt in canonical_receipts
            for observation in receipt.observations
        }
        observation_count = sum(
            len(receipt.observations) for receipt in canonical_receipts
        )
        if len(external_observations) != observation_count:
            raise RCPError(
                code="impact_dependency_receipt_invalid",
                message="Dependency receipts contain duplicate observations.",
            )
        declared_external = {
            ("resource", value) for value in report.dependencies.resources
        } | {
            ("environment", value)
            for value in report.dependencies.environments
        }
        if not set(external_observations) <= declared_external:
            raise RCPError(
                code="impact_dependency_receipt_invalid",
                message="Dependency receipt contains an undeclared dependency.",
            )
        expected_matched_resources = tuple(
            value
            for value in report.dependencies.resources
            if external_observations.get(("resource", value)) == "changed"
        )
        expected_matched_environments = tuple(
            value
            for value in report.dependencies.environments
            if external_observations.get(("environment", value)) == "changed"
        )
        expected_unresolved_resources = tuple(
            value
            for value in report.dependencies.resources
            if external_observations.get(("resource", value))
            in {None, "unknown"}
        )
        expected_unresolved_environments = tuple(
            value
            for value in report.dependencies.environments
            if external_observations.get(("environment", value))
            in {None, "unknown"}
        )
        if not isinstance(evaluation, DependencyImpactEvaluation) or (
            evaluation.changed_paths != canonical_paths
            or not set(evaluation.matched_path_dependencies)
            <= set(report.dependencies.paths)
            or not set(evaluation.matched_resource_dependencies)
            <= set(report.dependencies.resources)
            or not set(evaluation.matched_environment_dependencies)
            <= set(report.dependencies.environments)
            or not set(evaluation.unresolved_resource_dependencies)
            <= set(report.dependencies.resources)
            or not set(evaluation.unresolved_environment_dependencies)
            <= set(report.dependencies.environments)
            or evaluation.receipt_digests != expected_receipt_digests
            or evaluation.matched_resource_dependencies
            != expected_matched_resources
            or evaluation.matched_environment_dependencies
            != expected_matched_environments
            or evaluation.unresolved_resource_dependencies
            != expected_unresolved_resources
            or evaluation.unresolved_environment_dependencies
            != expected_unresolved_environments
            or evaluation.evaluator_id != evaluator_id
            or self.evaluator.evaluator_id != evaluator_id
        ):
            raise RCPError(
                code="impact_evaluator_invalid",
                message="Dependency evaluator returned evidence outside its contract.",
                context={"evaluator_id": evaluator_id},
            )
        overlap = bool(
            evaluation.matched_path_dependencies
            or evaluation.matched_resource_dependencies
            or evaluation.matched_environment_dependencies
        )
        if not overlap and (
            evaluation.unresolved_resource_dependencies
            or evaluation.unresolved_environment_dependencies
        ):
            raise RCPError(
                code="report_impact_evidence_incomplete",
                message=(
                    "External dependency evidence is incomplete; Report "
                    "validity cannot advance."
                ),
                context={
                    "report_id": report.report_id,
                    "unresolved_resources": list(
                        evaluation.unresolved_resource_dependencies
                    ),
                    "unresolved_environments": list(
                        evaluation.unresolved_environment_dependencies
                    ),
                },
            )
        applicability = (
            ReportApplicability.STALE
            if overlap
            else ReportApplicability.CURRENT
        )
        validation_basis = (
            basis
            if overlap
            else ValidationBasis(main_tree=target_tree, assessed_at=canonical_time)
        )
        proposed = ReportRecord.model_validate(
            report.model_copy(
                update={
                    "revision": report.revision + 1,
                    "applicability": applicability,
                    "validation_basis": validation_basis,
                }
            ).model_dump(mode="json", exclude_none=True)
        )
        self._require_evidence_identity_unchanged(report, proposed)

        payload = {
            "schema_version": report.schema_version,
            "impact_id": impact_id,
            "report_id": report.report_id,
            "expected_report_revision": report.revision,
            "change_provider_id": change_provider_id,
            "dependency_evaluator_id": evaluation.evaluator_id,
            "basis_tree": basis.main_tree,
            "target_commit": target_commit,
            "target_tree": target_tree,
            "changed_paths": canonical_paths,
            "dependency_receipts": tuple(
                item.model_dump(mode="json", exclude_none=True)
                for item in canonical_receipts
            ),
            "matched_path_dependencies": (
                evaluation.matched_path_dependencies
            ),
            "matched_resource_dependencies": (
                evaluation.matched_resource_dependencies
            ),
            "matched_environment_dependencies": (
                evaluation.matched_environment_dependencies
            ),
            "unresolved_resource_dependencies": (
                evaluation.unresolved_resource_dependencies
            ),
            "unresolved_environment_dependencies": (
                evaluation.unresolved_environment_dependencies
            ),
            "outcome": "overlap" if overlap else "no_overlap",
            "proposed_applicability": applicability.value,
            "proposed_report_digest": canonical_digest(proposed),
            "generated_at": canonical_time,
        }
        impact = ReportImpact.model_validate(
            {**payload, "impact_digest": canonical_digest(payload)}
        )
        source_digest = canonical_digest(
            {
                "analyzer_id": IMPACT_ANALYZER_ID,
                "evaluator_id": evaluation.evaluator_id,
                "impact_digest": impact.impact_digest,
                "current_report_digest": canonical_digest(report),
                "proposed_report_digest": canonical_digest(proposed),
            }
        )
        report_root = f".research/reports/{report.report_id}/{proposed.revision}"
        impact_root = f".research/impacts/{impact.impact_id}"
        files = tuple(
            sorted(
                (
                    RenderedImpactFile(
                        f"{impact_root}/impact.yaml",
                        dump_yaml(impact).encode("utf-8"),
                    ),
                    RenderedImpactFile(
                        f"{report_root}.yaml",
                        dump_yaml(proposed).encode("utf-8"),
                    ),
                    RenderedImpactFile(
                        f"{report_root}.md",
                        render_impact_report(
                            impact=impact,
                            report=proposed,
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
        return ReportImpactBundle(
            impact=impact,
            current_report=report,
            proposed_report=proposed,
            source_digest=source_digest,
            files=files,
            manifest_digest=manifest_digest,
        )

    @staticmethod
    def _require_evidence_identity_unchanged(
        current: ReportRecord,
        proposed: ReportRecord,
    ) -> None:
        immutable = (
            "report_id",
            "title",
            "claim",
            "claim_scope",
            "evidence_status",
            "submission_id",
            "run_result_ids",
            "evidence_tree",
            "accepted_at_main_tree",
            "dependencies",
            "supersedes",
        )
        if any(
            getattr(current, field) != getattr(proposed, field)
            for field in immutable
        ):
            raise RCPError(
                code="report_impact_evidence_mutated",
                message="Impact analysis cannot alter Report evidence identity.",
            )


class ReportImpactBatchBuilder:
    """Combine per-Report analyses into one deterministic review proposal."""

    def build(
        self,
        *,
        impact_id: str,
        before_commit: str,
        target_commit: str,
        target_tree: str,
        report_bundles: tuple[ReportImpactBundle, ...],
        snapshot_report_ids: tuple[str, ...],
        ineligible_report_ids: tuple[str, ...],
        up_to_date_report_ids: tuple[str, ...],
        no_code_change_report_ids: tuple[str, ...],
        unresolved_report_ids: tuple[str, ...],
        generated_at: datetime,
    ) -> ReportImpactBatchBundle:
        ordered = tuple(
            sorted(report_bundles, key=lambda item: item.impact.report_id)
        )
        if not ordered:
            raise RCPError(
                code="report_impact_batch_empty",
                message="An Impact batch requires at least one Report proposal.",
            )
        if len(ordered) > 256:
            raise RCPError(
                code="report_impact_batch_too_large",
                message="Impact batch exceeds the 256-Report review bound.",
                context={"report_count": len(ordered)},
            )
        canonical_time = _UTC_DATETIME_ADAPTER.dump_python(
            _UTC_DATETIME_ADAPTER.validate_python(generated_at),
            mode="json",
        )
        impacts = tuple(item.impact for item in ordered)
        payload = {
            "schema_version": impacts[0].schema_version,
            "impact_id": impact_id,
            "before_commit": before_commit,
            "target_commit": target_commit,
            "target_tree": target_tree,
            "impacts": [
                item.model_dump(mode="json", exclude_none=True)
                for item in impacts
            ],
            "snapshot_report_ids": tuple(sorted(snapshot_report_ids)),
            "ineligible_report_ids": tuple(sorted(ineligible_report_ids)),
            "up_to_date_report_ids": tuple(sorted(up_to_date_report_ids)),
            "no_code_change_report_ids": tuple(
                sorted(no_code_change_report_ids)
            ),
            "unresolved_report_ids": tuple(sorted(unresolved_report_ids)),
            "generated_at": canonical_time,
        }
        batch = ReportImpactBatch.model_validate(
            {**payload, "batch_digest": canonical_digest(payload)}
        )
        source_digest = canonical_digest(
            {
                "analyzer_id": IMPACT_ANALYZER_ID,
                "batch_digest": batch.batch_digest,
                "reports": [
                    {
                        "current": canonical_digest(item.current_report),
                        "proposed": canonical_digest(item.proposed_report),
                        "report_source_digest": item.source_digest,
                    }
                    for item in ordered
                ],
            }
        )
        rendered: list[RenderedImpactFile] = [
            RenderedImpactFile(
                f".research/impacts/{impact_id}/impact-batch.yaml",
                dump_yaml(batch).encode("utf-8"),
            )
        ]
        for item in ordered:
            rendered.extend(
                candidate
                for candidate in item.files
                if candidate.path.startswith(".research/reports/")
            )
        files = tuple(sorted(rendered, key=lambda item: item.path))
        paths = [item.path for item in files]
        if len(paths) != len(set(paths)):
            raise RCPError(
                code="report_impact_batch_path_conflict",
                message="Impact batch generated duplicate Report revision paths.",
            )
        manifest_digest = canonical_digest(
            {
                "source_digest": source_digest,
                "files": [item.as_dict() for item in files],
            }
        )
        return ReportImpactBatchBundle(
            batch=batch,
            report_bundles=ordered,
            source_digest=source_digest,
            files=files,
            manifest_digest=manifest_digest,
        )


def _text(value: object) -> str:
    escaped = html.escape(str(value), quote=True).replace("\\", "\\\\")
    for character in ("`", "*", "_", "[", "]", "#", "|"):
        escaped = escaped.replace(character, f"\\{character}")
    return escaped.replace("\r\n", "\n").replace("\r", "\n").replace(
        "\n", "<br>\n"
    )


def render_impact_report(
    *,
    impact: ReportImpact,
    report: ReportRecord,
    source_digest: str,
) -> bytes:
    dependencies = [
        *(f"path:{value}" for value in report.dependencies.paths),
        *(f"resource:{value}" for value in report.dependencies.resources),
        *(f"environment:{value}" for value in report.dependencies.environments),
    ]
    lines = [
        f"# {_text(report.title)}",
        "",
        "> Proposed impact revision. It has no accepted authority until reviewed and merged.",
        "",
        f"- Report: `{report.report_id}` revision `{report.revision}`",
        f"- Impact: `{impact.impact_id}`",
        f"- Outcome: `{impact.outcome}`",
        f"- Proposed applicability: `{impact.proposed_applicability.value}`",
        f"- Evidence status: `{report.evidence_status.value}`",
        f"- Evidence tree: `{report.evidence_tree}`",
        f"- Accepted base tree: `{report.accepted_at_main_tree}`",
        f"- Previous validation basis: `{impact.basis_tree}`",
        f"- Target commit: `{impact.target_commit}`",
        f"- Target tree: `{impact.target_tree}`",
        f"- Change provider: `{_text(impact.change_provider_id)}`",
        f"- Dependency evaluator: `{_text(impact.dependency_evaluator_id)}`",
        f"- Canonical source digest: `{source_digest}`",
        "",
        "## Claim",
        "",
        _text(report.claim),
        "",
        "## Changed Paths",
        "",
        *(f"- `{_text(value)}`" for value in impact.changed_paths),
        "",
        "## Matched Dependencies",
        "",
    ]
    lines.extend(
        (f"- `{_text(value)}`" for value in impact.matched_path_dependencies)
        if impact.matched_path_dependencies
        else ("None.",)
    )
    lines.extend(["", "## External Dependency Evidence", ""])
    external_evidence = [
        *(
            f"- changed resource: `{_text(value)}`"
            for value in impact.matched_resource_dependencies
        ),
        *(
            f"- changed environment: `{_text(value)}`"
            for value in impact.matched_environment_dependencies
        ),
        *(
            f"- unresolved resource: `{_text(value)}`"
            for value in impact.unresolved_resource_dependencies
        ),
        *(
            f"- unresolved environment: `{_text(value)}`"
            for value in impact.unresolved_environment_dependencies
        ),
        *(
            f"- receipt: `{_text(value.receipt_id)}` "
            f"(`{_text(value.provider_id)}`, `{value.receipt_digest}`)"
            for value in impact.dependency_receipts
        ),
    ]
    lines.extend(external_evidence if external_evidence else ("None.",))
    lines.extend(["", "## Declared Dependencies", ""])
    lines.extend(
        (f"- `{_text(value)}`" for value in dependencies)
        if dependencies
        else ("None.",)
    )
    lines.extend(
        [
            "",
            "## Review Decisions",
            "",
            "- Rerun against the target tree.",
            "- Accept an explicit manager waiver.",
            "- Keep the Report stale.",
            "- Invalidate the evidence through a separate reviewed decision.",
            "- Correct a false-positive dependency declaration.",
        ]
    )
    return ("\n".join(lines) + "\n").encode("utf-8")
