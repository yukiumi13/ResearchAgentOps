from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from researchctl.adapters.git_ci import GitCIObjectReader
from researchctl.domain.enums import ClaimScope, ReportApplicability
from researchctl.domain.models import ReportImpact, ReportImpactBatch, ReportRecord
from researchctl.errors import RCPError
from researchctl.serialization import SerializationError, dump_yaml, load_yaml
from researchctl.services.report_impact import (
    ReportImpactBatchBuilder,
    ReportImpactBatchBundle,
    ReportImpactBuilder,
    ReportImpactBundle,
)


_REPORT_REVISION = re.compile(
    r"^\.research/reports/"
    r"(report_\d{8}T\d{6}Z_[0-9a-f]{24})/([1-9][0-9]*)\.(md|yaml)$"
)


@dataclass(frozen=True, slots=True)
class ReportImpactBatchAnalysis:
    before_commit: str
    target_commit: str
    target_tree: str
    report_ids: tuple[str, ...]
    snapshot_report_ids: tuple[str, ...]
    ineligible_report_ids: tuple[str, ...]
    up_to_date_report_ids: tuple[str, ...]
    no_code_change_report_ids: tuple[str, ...]
    unresolved_report_ids: tuple[str, ...]
    bundle: ReportImpactBatchBundle | None

    @property
    def terminal_result(self) -> str:
        if self.bundle is not None:
            return "proposal_required"
        if self.unresolved_report_ids:
            return "impact_unresolved"
        return "no_change"

    def as_dict(self) -> dict[str, object]:
        return {
            "terminal_result": self.terminal_result,
            "before_commit": self.before_commit,
            "target_commit": self.target_commit,
            "target_tree": self.target_tree,
            "report_ids": list(self.report_ids),
            "snapshot_report_ids": list(self.snapshot_report_ids),
            "ineligible_report_ids": list(self.ineligible_report_ids),
            "up_to_date_report_ids": list(self.up_to_date_report_ids),
            "no_code_change_report_ids": list(self.no_code_change_report_ids),
            "unresolved_report_ids": list(self.unresolved_report_ids),
            "bundle": self.bundle.as_dict() if self.bundle is not None else None,
        }


class GitReportImpactAnalyzer:
    """Analyze one accepted Report directly from immutable Git objects."""

    def __init__(
        self,
        *,
        git: GitCIObjectReader | None = None,
        builder: ReportImpactBuilder | None = None,
        batch_builder: ReportImpactBatchBuilder | None = None,
    ) -> None:
        self.git = git or GitCIObjectReader()
        self.builder = builder or ReportImpactBuilder()
        self.batch_builder = batch_builder or ReportImpactBatchBuilder()

    def scan(
        self,
        repository_root: Path,
        *,
        impact_id: str,
        before_commit: str,
        target_commit: str,
        generated_at: datetime,
    ) -> ReportImpactBatchAnalysis:
        before = self.git.read_commit(repository_root, before_commit)
        target = self.git.read_commit(repository_root, target_commit)
        if not self.git.is_ancestor(
            repository_root,
            ancestor=before.object_id,
            descendant=target.object_id,
        ):
            raise RCPError(
                code="report_impact_trigger_lineage_invalid",
                message="Impact merge trigger does not describe an ancestor range.",
                context={
                    "before_commit": before.object_id,
                    "target_commit": target.object_id,
                },
            )
        report_ids = self.list_report_ids(
            repository_root,
            commit=target.object_id,
        )
        proposed = []
        snapshots: list[str] = []
        ineligible: list[str] = []
        up_to_date: list[str] = []
        no_code_change: list[str] = []
        unresolved: list[str] = []
        changed_paths_by_basis: dict[str, tuple[str, ...]] = {}
        for report_id in report_ids:
            report = self.load_latest_report(
                repository_root,
                commit=target.object_id,
                report_id=report_id,
            )
            if report.claim_scope is ClaimScope.SNAPSHOT:
                snapshots.append(report_id)
                continue
            if report.applicability not in {
                ReportApplicability.CURRENT,
                ReportApplicability.IMPACT_PENDING,
            }:
                ineligible.append(report_id)
                continue
            basis = report.validation_basis
            if basis is not None and basis.main_tree == target.tree:
                up_to_date.append(report_id)
                continue
            assert basis is not None
            changed_paths = changed_paths_by_basis.get(basis.main_tree)
            if changed_paths is None:
                changed_paths = self._changed_code_paths(
                    repository_root,
                    basis_tree=basis.main_tree,
                    target_tree=target.tree,
                )
                changed_paths_by_basis[basis.main_tree] = changed_paths
            if not changed_paths:
                if report.dependencies.resources or report.dependencies.environments:
                    unresolved.append(report_id)
                else:
                    no_code_change.append(report_id)
                continue
            try:
                proposal = self.builder.build(
                    impact_id=impact_id,
                    report=report,
                    target_commit=target.object_id,
                    target_tree=target.tree,
                    changed_paths=changed_paths,
                    generated_at=generated_at,
                )
            except RCPError as error:
                if error.code != "report_impact_evidence_incomplete":
                    raise
                unresolved.append(report_id)
                continue
            proposed.append(proposal)
        bundle = None
        if proposed:
            bundle = self.batch_builder.build(
                impact_id=impact_id,
                before_commit=before.object_id,
                target_commit=target.object_id,
                target_tree=target.tree,
                report_bundles=tuple(proposed),
                snapshot_report_ids=tuple(snapshots),
                ineligible_report_ids=tuple(ineligible),
                up_to_date_report_ids=tuple(up_to_date),
                no_code_change_report_ids=tuple(no_code_change),
                unresolved_report_ids=tuple(unresolved),
                generated_at=generated_at,
            )
        return ReportImpactBatchAnalysis(
            before_commit=before.object_id,
            target_commit=target.object_id,
            target_tree=target.tree,
            report_ids=report_ids,
            snapshot_report_ids=tuple(snapshots),
            ineligible_report_ids=tuple(ineligible),
            up_to_date_report_ids=tuple(up_to_date),
            no_code_change_report_ids=tuple(no_code_change),
            unresolved_report_ids=tuple(unresolved),
            bundle=bundle,
        )

    def list_report_ids(
        self,
        repository_root: Path,
        *,
        commit: str,
    ) -> tuple[str, ...]:
        entries = self.git.list_entries(
            repository_root,
            commit=commit,
            path=".research/reports",
        )
        report_ids: set[str] = set()
        for entry in entries:
            if entry.path == ".research/reports/.gitkeep":
                continue
            matched = _REPORT_REVISION.fullmatch(entry.path)
            if matched is None:
                raise RCPError(
                    code="report_store_invalid",
                    message="Report store contains a non-canonical path.",
                    context={"path": entry.path},
                )
            report_ids.add(matched.group(1))
        return tuple(sorted(report_ids))

    def analyze(
        self,
        repository_root: Path,
        *,
        impact_id: str,
        report_id: str,
        expected_report_revision: int,
        target_commit: str,
        generated_at: datetime,
    ) -> ReportImpactBundle:
        target = self.git.read_commit(repository_root, target_commit)
        report = self.load_latest_report(
            repository_root,
            commit=target.object_id,
            report_id=report_id,
        )
        if report.revision != expected_report_revision:
            raise RCPError(
                code="stale_report_revision",
                message="Report changed after impact analysis was requested.",
                context={
                    "report_id": report_id,
                    "expected_revision": expected_report_revision,
                    "observed_revision": report.revision,
                },
            )
        basis = report.validation_basis
        if basis is not None and basis.main_tree == target.tree:
            raise RCPError(
                code="report_impact_no_change",
                message="Report validation basis already equals the target tree.",
                context={"report_id": report_id, "target_tree": target.tree},
            )
        changed_paths: tuple[str, ...] = ()
        if basis is not None:
            changed_paths = self._changed_code_paths(
                repository_root,
                basis_tree=basis.main_tree,
                target_tree=target.tree,
            )
        return self.builder.build(
            impact_id=impact_id,
            report=report,
            target_commit=target.object_id,
            target_tree=target.tree,
            changed_paths=changed_paths,
            generated_at=generated_at,
        )

    def _changed_code_paths(
        self,
        repository_root: Path,
        *,
        basis_tree: str,
        target_tree: str,
    ) -> tuple[str, ...]:
        return tuple(
            change.path
            for change in self.git.changes(
                repository_root,
                old_commit=basis_tree,
                new_commit=target_tree,
            )
            if not change.path.startswith(".research/")
        )

    def load_latest_report(
        self,
        repository_root: Path,
        *,
        commit: str,
        report_id: str,
    ) -> ReportRecord:
        root = f".research/reports/{report_id}"
        entries = self.git.list_entries(
            repository_root,
            commit=commit,
            path=root,
        )
        yaml_entries = tuple(
            item for item in entries if item.path.endswith(".yaml")
        )
        if not yaml_entries:
            raise RCPError(
                code="report_not_found",
                message="Accepted Report was not found at the target commit.",
                context={"report_id": report_id, "target_commit": commit},
            )
        revisions: list[tuple[int, str]] = []
        for entry in yaml_entries:
            name = Path(entry.path).name.removesuffix(".yaml")
            if not name.isascii() or not name.isdecimal() or int(name) < 1:
                raise RCPError(
                    code="report_store_invalid",
                    message="Report revision path is not canonical.",
                    context={"path": entry.path},
                )
            revisions.append((int(name), entry.path))
        numbers = sorted(revision for revision, _ in revisions)
        if numbers != list(range(1, numbers[-1] + 1)):
            raise RCPError(
                code="report_store_invalid",
                message="Report revision history is not contiguous.",
                context={"report_id": report_id, "revisions": numbers},
            )
        revision, path = max(revisions)
        content = self.git.read_blob_at(
            repository_root,
            commit=commit,
            path=path,
        )
        assert content is not None
        try:
            text = content.decode("utf-8")
            report = ReportRecord.model_validate(load_yaml(text))
        except (UnicodeDecodeError, SerializationError, ValidationError) as error:
            raise RCPError(
                code="report_record_invalid",
                message="Accepted Report is not a valid canonical record.",
                context={"path": path},
            ) from error
        if dump_yaml(report).encode("utf-8") != content:
            raise RCPError(
                code="report_record_not_canonical",
                message="Accepted Report is not canonical YAML.",
                context={"path": path},
            )
        if report.report_id != report_id or report.revision != revision:
            raise RCPError(
                code="report_store_invalid",
                message="Report identity does not match its revision path.",
                context={"path": path},
            )
        return report

    def load_impact(
        self,
        repository_root: Path,
        *,
        commit: str,
        impact_id: str,
        report_id: str,
    ) -> ReportImpact:
        """Load one accepted single/batch Impact child from immutable Git data."""

        root = f".research/impacts/{impact_id}"
        impact_path = f"{root}/impact.yaml"
        batch_path = f"{root}/impact-batch.yaml"
        impact_content = self.git.read_blob_at(
            repository_root,
            commit=commit,
            path=impact_path,
            required=False,
        )
        batch_content = self.git.read_blob_at(
            repository_root,
            commit=commit,
            path=batch_path,
            required=False,
        )
        if (impact_content is None) == (batch_content is None):
            raise RCPError(
                code="impact_decision_source_invalid",
                message="Decision requires exactly one accepted Impact source record.",
            )
        if impact_content is not None:
            impact = self._parse_impact_source(
                impact_content,
                model_type=ReportImpact,
                path=impact_path,
            )
            if impact.impact_id != impact_id or impact.report_id != report_id:
                raise RCPError(
                    code="impact_decision_source_mismatch",
                    message=(
                        "Impact source identity does not match the decision "
                        "request."
                    ),
                )
            return impact
        assert batch_content is not None
        batch = self._parse_impact_source(
            batch_content,
            model_type=ReportImpactBatch,
            path=batch_path,
        )
        matches = [item for item in batch.impacts if item.report_id == report_id]
        if batch.impact_id != impact_id or len(matches) != 1:
            raise RCPError(
                code="impact_decision_source_mismatch",
                message="Impact batch has no unique decision target Report.",
            )
        return matches[0]

    @staticmethod
    def _parse_impact_source(
        content: bytes,
        *,
        model_type: type[ReportImpact] | type[ReportImpactBatch],
        path: str,
    ) -> ReportImpact | ReportImpactBatch:
        try:
            record = model_type.model_validate(load_yaml(content.decode("utf-8")))
        except (
            UnicodeDecodeError,
            SerializationError,
            ValidationError,
        ) as error:
            raise RCPError(
                code="impact_decision_source_invalid",
                message="Accepted Impact source is not a canonical record.",
                context={"path": path},
            ) from error
        if dump_yaml(record).encode("utf-8") != content:
            raise RCPError(
                code="impact_decision_source_invalid",
                message="Accepted Impact source is not canonical YAML.",
                context={"path": path},
            )
        return record
