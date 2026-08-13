from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from researchctl.domain.enums import ReportApplicability
from researchctl.domain.models import (
    DependencyChangeObservation,
    DependencyChangeReceipt,
    DependencySet,
    ReportRecord,
)
from researchctl.errors import RCPError
from researchctl.serialization import canonical_digest, dump_yaml
from researchctl.services.dependency_impact import (
    DECLARED_DEPENDENCY_EVALUATOR_ID,
    GIT_TREE_CHANGE_PROVIDER_ID,
    DependencyImpactEvaluation,
    PathDependencyImpactEvaluator,
    path_dependency_matches,
)
from researchctl.services.git_report_impact import (
    GitReportImpactAnalyzer,
    ReportImpactBatchAnalysis,
)
from researchctl.services.impact_workflow import ImpactWorkflowService
from researchctl.services.report_impact import (
    ReportImpactBuilder,
)
from researchctl.services.requests import ImpactBatchCreateRequest

IMPACT_ID = "impact_20260803T120000Z_" + "1" * 24
TARGET_COMMIT = "d" * 40
TARGET_TREE = "e" * 40
GENERATED_AT = "2026-08-03T12:00:00Z"


def _report(report_payload, **overrides) -> ReportRecord:
    dependencies = overrides.pop(
        "dependencies",
        {
            "paths": ["configs/aime25.yaml", "src/evaluator/**"],
            "resources": [],
            "environments": [],
        },
    )
    payload = report_payload(
        dependencies=dependencies,
        **overrides,
    )
    return ReportRecord.model_validate(payload)


def _dependency_receipt(
    *observations: dict[str, object],
    basis_tree: str = "c" * 40,
    target_commit: str = TARGET_COMMIT,
    target_tree: str = TARGET_TREE,
) -> DependencyChangeReceipt:
    ordered = tuple(
        sorted(
            observations,
            key=lambda item: (str(item["kind"]), str(item["dependency"])),
        )
    )
    dependencies = tuple(
        (str(item["kind"]), str(item["dependency"])) for item in ordered
    )
    payload = {
        "schema_version": "0.1",
        "receipt_id": "receipt_20260803T120000Z_" + "9" * 24,
        "provider_id": "test.lineage.v1",
        "provider_version": "1.0.0",
        "basis_tree": basis_tree,
        "target_commit": target_commit,
        "target_tree": target_tree,
        "observations": ordered,
        "provider_query_digest": "sha256:" + "0" * 64,
        "query_digest": DependencyChangeReceipt.calculate_query_digest(
            provider_id="test.lineage.v1",
            provider_version="1.0.0",
            basis_tree=basis_tree,
            target_commit=target_commit,
            target_tree=target_tree,
            provider_query_digest="sha256:" + "0" * 64,
            dependencies=dependencies,
        ),
        "observed_at": GENERATED_AT,
    }
    return DependencyChangeReceipt.model_validate(
        {**payload, "receipt_digest": canonical_digest(payload)}
    )


@pytest.mark.parametrize(
    ("dependency", "changed", "matches"),
    [
        ("configs/aime25.yaml", "configs/aime25.yaml", True),
        ("configs/aime25.yaml", "configs/aime26.yaml", False),
        ("src/evaluator/**", "src/evaluator/score.py", True),
        ("src/evaluator/**", "src/evaluator/nested/score.py", True),
        ("src/evaluator/**", "src/evaluator", True),
        ("src/evaluator/**", "src/evaluator_old/score.py", False),
    ],
)
def test_path_dependency_matcher_has_segment_prefix_semantics(
    dependency: str,
    changed: str,
    matches: bool,
) -> None:
    assert path_dependency_matches(dependency, changed) is matches


@pytest.mark.parametrize(
    "path",
    [
        "src/**/score.py",
        "src/*.py",
        "src/evaluator/?",
        "src/evaluator/[ab]",
        "src/evaluator/foo]",
    ],
)
def test_dependency_set_rejects_ambiguous_glob_syntax(path: str) -> None:
    with pytest.raises(ValidationError, match="trailing /\\*\\*"):
        DependencySet(paths=(path,))


def test_dependency_set_rejects_control_record_dependencies() -> None:
    with pytest.raises(ValidationError, match="control records"):
        DependencySet(paths=(".research/reports/**",))


def test_path_dependency_evaluator_returns_canonical_declared_matches() -> None:
    evaluation = PathDependencyImpactEvaluator().evaluate(
        dependencies=DependencySet(
            paths=("configs/aime25.yaml", "src/evaluator/**")
        ),
        changed_paths=("src/evaluator/z.py", "README.md", "src/evaluator/a.py"),
    )

    assert evaluation.changed_paths == (
        "README.md",
        "src/evaluator/a.py",
        "src/evaluator/z.py",
    )
    assert evaluation.matched_path_dependencies == ("src/evaluator/**",)


def test_report_builder_rejects_evaluator_that_matches_undeclared_dependency(
    report_payload,
) -> None:
    class _InvalidEvaluator:
        evaluator_id = "invalid.evaluator.v1"

        def evaluate(self, *, dependencies, changed_paths, dependency_receipts):
            del dependencies
            del dependency_receipts
            return DependencyImpactEvaluation(
                evaluator_id=self.evaluator_id,
                changed_paths=changed_paths,
                matched_path_dependencies=("undeclared/**",),
                matched_resource_dependencies=(),
                matched_environment_dependencies=(),
                unresolved_resource_dependencies=(),
                unresolved_environment_dependencies=(),
                receipt_digests=(),
            )

    with pytest.raises(RCPError) as raised:
        ReportImpactBuilder(evaluator=_InvalidEvaluator()).build(  # type: ignore[arg-type]
            impact_id=IMPACT_ID,
            report=_report(report_payload),
            target_commit=TARGET_COMMIT,
            target_tree=TARGET_TREE,
            changed_paths=("src/evaluator/score.py",),
            generated_at=GENERATED_AT,
        )

    assert raised.value.code == "impact_evaluator_invalid"


def test_report_builder_rejects_evaluator_that_changes_path_evidence(
    report_payload,
) -> None:
    class _EvidenceMutatingEvaluator:
        evaluator_id = "invalid.evidence-mutating.v1"

        def evaluate(self, *, dependencies, changed_paths, dependency_receipts):
            del dependencies
            del dependency_receipts
            return DependencyImpactEvaluation(
                evaluator_id=self.evaluator_id,
                changed_paths=("README.md", *changed_paths),
                matched_path_dependencies=(),
                matched_resource_dependencies=(),
                matched_environment_dependencies=(),
                unresolved_resource_dependencies=(),
                unresolved_environment_dependencies=(),
                receipt_digests=(),
            )

    with pytest.raises(RCPError) as raised:
        ReportImpactBuilder(
            evaluator=_EvidenceMutatingEvaluator()  # type: ignore[arg-type]
        ).build(
            impact_id=IMPACT_ID,
            report=_report(report_payload),
            target_commit=TARGET_COMMIT,
            target_tree=TARGET_TREE,
            changed_paths=("src/evaluator/score.py",),
            generated_at=GENERATED_AT,
        )

    assert raised.value.code == "impact_evaluator_invalid"


def test_report_builder_rejects_evaluator_identity_drift(report_payload) -> None:
    class _IdentityDriftingEvaluator:
        evaluator_id = "invalid.identity-before.v1"

        def evaluate(self, *, dependencies, changed_paths, dependency_receipts):
            del dependencies
            del dependency_receipts
            self.evaluator_id = "invalid.identity-after.v1"
            return DependencyImpactEvaluation(
                evaluator_id=self.evaluator_id,
                changed_paths=changed_paths,
                matched_path_dependencies=(),
                matched_resource_dependencies=(),
                matched_environment_dependencies=(),
                unresolved_resource_dependencies=(),
                unresolved_environment_dependencies=(),
                receipt_digests=(),
            )

    with pytest.raises(RCPError) as raised:
        ReportImpactBuilder(
            evaluator=_IdentityDriftingEvaluator()  # type: ignore[arg-type]
        ).build(
            impact_id=IMPACT_ID,
            report=_report(report_payload),
            target_commit=TARGET_COMMIT,
            target_tree=TARGET_TREE,
            changed_paths=("src/evaluator/score.py",),
            generated_at=GENERATED_AT,
        )

    assert raised.value.code == "impact_evaluator_invalid"


def test_report_builder_rejects_evaluator_that_hides_missing_external_evidence(
    report_payload,
) -> None:
    class _EvidenceOmittingEvaluator:
        evaluator_id = "invalid.external-omission.v1"

        def evaluate(self, *, dependencies, changed_paths, dependency_receipts):
            del dependencies
            del dependency_receipts
            return DependencyImpactEvaluation(
                evaluator_id=self.evaluator_id,
                changed_paths=changed_paths,
                matched_path_dependencies=(),
                matched_resource_dependencies=(),
                matched_environment_dependencies=(),
                unresolved_resource_dependencies=(),
                unresolved_environment_dependencies=(),
                receipt_digests=(),
            )

    report = _report(
        report_payload,
        dependencies={
            "paths": [],
            "resources": ["dataset:aime25"],
            "environments": [],
        },
    )
    with pytest.raises(RCPError) as raised:
        ReportImpactBuilder(
            evaluator=_EvidenceOmittingEvaluator()  # type: ignore[arg-type]
        ).build(
            impact_id=IMPACT_ID,
            report=report,
            target_commit=TARGET_COMMIT,
            target_tree=TARGET_TREE,
            changed_paths=("README.md",),
            generated_at=GENERATED_AT,
        )

    assert raised.value.code == "impact_evaluator_invalid"


def test_overlap_proposes_stale_without_rewriting_evidence(report_payload) -> None:
    current = _report(report_payload)

    bundle = ReportImpactBuilder().build(
        impact_id=IMPACT_ID,
        report=current,
        target_commit=TARGET_COMMIT,
        target_tree=TARGET_TREE,
        changed_paths=("README.md", "src/evaluator/score.py"),
        generated_at=GENERATED_AT,
    )

    assert bundle.impact.outcome == "overlap"
    assert bundle.impact.change_provider_id == GIT_TREE_CHANGE_PROVIDER_ID
    assert bundle.impact.dependency_evaluator_id == (
        DECLARED_DEPENDENCY_EVALUATOR_ID
    )
    assert bundle.as_dict()["change_provider_id"] == GIT_TREE_CHANGE_PROVIDER_ID
    assert bundle.as_dict()["dependency_evaluator_id"] == (
        DECLARED_DEPENDENCY_EVALUATOR_ID
    )
    assert bundle.impact.matched_path_dependencies == ("src/evaluator/**",)
    assert bundle.proposed_report.applicability is ReportApplicability.STALE
    assert bundle.proposed_report.validation_basis == current.validation_basis
    assert bundle.proposed_report.evidence_tree == current.evidence_tree
    assert bundle.proposed_report.accepted_at_main_tree == (
        current.accepted_at_main_tree
    )
    assert bundle.proposed_report.run_result_ids == current.run_result_ids
    assert bundle.proposed_report.dependencies == current.dependencies
    assert [item.path for item in bundle.files] == sorted(
        [
            f".research/impacts/{IMPACT_ID}/impact.yaml",
            f".research/reports/{current.report_id}/2.md",
            f".research/reports/{current.report_id}/2.yaml",
        ]
    )


def test_no_overlap_proposes_reviewed_validation_basis_advance(
    report_payload,
) -> None:
    current = _report(report_payload)

    bundle = ReportImpactBuilder().build(
        impact_id=IMPACT_ID,
        report=current,
        target_commit=TARGET_COMMIT,
        target_tree=TARGET_TREE,
        changed_paths=("README.md",),
        generated_at=GENERATED_AT,
    )

    assert bundle.impact.outcome == "no_overlap"
    assert bundle.impact.matched_path_dependencies == ()
    assert bundle.proposed_report.applicability is ReportApplicability.CURRENT
    assert bundle.proposed_report.validation_basis is not None
    assert bundle.proposed_report.validation_basis.main_tree == TARGET_TREE
    assert bundle.proposed_report.validation_basis.assessed_at.isoformat() == (
        "2026-08-03T12:00:00+00:00"
    )


def test_no_overlap_external_dependencies_require_complete_receipts(
    report_payload,
) -> None:
    report = _report(
        report_payload,
        dependencies={
            "paths": ["src/evaluator/**"],
            "resources": ["dataset:aime25-v1"],
            "environments": ["runtime:cuda-12.8"],
        },
    )

    with pytest.raises(RCPError) as raised:
        ReportImpactBuilder().build(
            impact_id=IMPACT_ID,
            report=report,
            target_commit=TARGET_COMMIT,
            target_tree=TARGET_TREE,
            changed_paths=("README.md",),
            generated_at=GENERATED_AT,
        )

    assert raised.value.code == "report_impact_evidence_incomplete"
    assert raised.value.context["unresolved_resources"] == [
        "dataset:aime25-v1"
    ]
    assert raised.value.context["unresolved_environments"] == [
        "runtime:cuda-12.8"
    ]


def test_complete_unchanged_receipt_allows_no_overlap(report_payload) -> None:
    report = _report(
        report_payload,
        dependencies={
            "paths": ["src/evaluator/**"],
            "resources": ["dataset:aime25-v1"],
            "environments": ["runtime:cuda-12.8"],
        },
    )
    receipt = _dependency_receipt(
        {
            "kind": "resource",
            "dependency": "dataset:aime25-v1",
            "state": "unchanged",
            "basis_identity": {"version": "aime25-v1"},
            "target_identity": {"version": "aime25-v1"},
            "evidence_digest": "sha256:" + "1" * 64,
        },
        {
            "kind": "environment",
            "dependency": "runtime:cuda-12.8",
            "state": "unchanged",
            "basis_identity": {"version": "cuda-12.8"},
            "target_identity": {"version": "cuda-12.8"},
            "evidence_digest": "sha256:" + "2" * 64,
        },
    )

    bundle = ReportImpactBuilder().build(
        impact_id=IMPACT_ID,
        report=report,
        target_commit=TARGET_COMMIT,
        target_tree=TARGET_TREE,
        changed_paths=("README.md",),
        generated_at=GENERATED_AT,
        dependency_receipts=(receipt,),
    )

    assert bundle.impact.outcome == "no_overlap"
    assert bundle.impact.dependency_receipts == (receipt,)
    assert bundle.impact.unresolved_resource_dependencies == ()
    assert bundle.impact.unresolved_environment_dependencies == ()
    assert bundle.proposed_report.applicability is ReportApplicability.CURRENT


def test_changed_resource_receipt_proposes_stale_without_code_change(
    report_payload,
) -> None:
    report = _report(
        report_payload,
        dependencies={
            "paths": [],
            "resources": ["dataset:aime25"],
            "environments": [],
        },
    )
    receipt = _dependency_receipt(
        {
            "kind": "resource",
            "dependency": "dataset:aime25",
            "state": "changed",
            "basis_identity": {"version": "v1"},
            "target_identity": {"version": "v2"},
            "evidence_digest": "sha256:" + "3" * 64,
        }
    )

    bundle = ReportImpactBuilder().build(
        impact_id=IMPACT_ID,
        report=report,
        target_commit=TARGET_COMMIT,
        target_tree=TARGET_TREE,
        changed_paths=(),
        generated_at=GENERATED_AT,
        dependency_receipts=(receipt,),
    )

    assert bundle.impact.outcome == "overlap"
    assert bundle.impact.matched_resource_dependencies == ("dataset:aime25",)
    assert bundle.proposed_report.applicability is ReportApplicability.STALE


def test_unknown_external_observation_cannot_advance_validity(
    report_payload,
) -> None:
    report = _report(
        report_payload,
        dependencies={
            "paths": [],
            "resources": ["dataset:aime25"],
            "environments": [],
        },
    )
    receipt = _dependency_receipt(
        {
            "kind": "resource",
            "dependency": "dataset:aime25",
            "state": "unknown",
            "reason": "provider timeout",
        }
    )

    with pytest.raises(RCPError) as raised:
        ReportImpactBuilder().build(
            impact_id=IMPACT_ID,
            report=report,
            target_commit=TARGET_COMMIT,
            target_tree=TARGET_TREE,
            changed_paths=(),
            generated_at=GENERATED_AT,
            dependency_receipts=(receipt,),
        )

    assert raised.value.code == "report_impact_evidence_incomplete"


def test_dependency_receipt_must_bind_exact_impact_target(report_payload) -> None:
    report = _report(
        report_payload,
        dependencies={
            "paths": [],
            "resources": ["dataset:aime25"],
            "environments": [],
        },
    )
    receipt = _dependency_receipt(
        {
            "kind": "resource",
            "dependency": "dataset:aime25",
            "state": "unchanged",
            "basis_identity": {"version": "v1"},
            "target_identity": {"version": "v1"},
            "evidence_digest": "sha256:" + "4" * 64,
        },
        target_tree="f" * 40,
    )

    with pytest.raises(RCPError) as raised:
        ReportImpactBuilder().build(
            impact_id=IMPACT_ID,
            report=report,
            target_commit=TARGET_COMMIT,
            target_tree=TARGET_TREE,
            changed_paths=(),
            generated_at=GENERATED_AT,
            dependency_receipts=(receipt,),
        )

    assert raised.value.code == "impact_dependency_receipt_target_mismatch"


@pytest.mark.parametrize(
    "observation",
    [
        {
            "kind": "resource",
            "dependency": "dataset:aime25",
            "state": "changed",
            "basis_identity": {"version": "v1"},
            "target_identity": {"version": "v1"},
            "evidence_digest": "sha256:" + "5" * 64,
        },
        {
            "kind": "environment",
            "dependency": "runtime:cuda",
            "state": "unchanged",
            "basis_identity": {"version": "12.8"},
            "target_identity": {"version": "12.9"},
            "evidence_digest": "sha256:" + "6" * 64,
        },
        {
            "kind": "resource",
            "dependency": "dataset:aime25",
            "state": "unknown",
        },
    ],
)
def test_dependency_observation_state_must_match_evidence(
    observation: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        DependencyChangeObservation.model_validate(observation)


def test_snapshot_report_is_not_impact_eligible(report_payload) -> None:
    report = _report(
        report_payload,
        claim_scope="snapshot",
        applicability="snapshot_only",
        validation_basis=None,
    )

    with pytest.raises(RCPError) as raised:
        ReportImpactBuilder().build(
            impact_id=IMPACT_ID,
            report=report,
            target_commit=TARGET_COMMIT,
            target_tree=TARGET_TREE,
            changed_paths=("src/evaluator/score.py",),
            generated_at=GENERATED_AT,
        )

    assert raised.value.code == "report_impact_not_applicable"


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(repository: Path, message: str, *paths: str) -> str:
    _git(repository, "add", "--", *paths)
    _git(
        repository,
        "-c",
        "user.name=Tests",
        "-c",
        "user.email=tests@example.invalid",
        "commit",
        "--no-gpg-sign",
        "-m",
        message,
    )
    return _git(repository, "rev-parse", "HEAD")


def test_git_analyzer_diffs_from_each_report_basis_and_ignores_protocol_state(
    initialized_repository: Path,
    report_payload,
) -> None:
    evaluator = initialized_repository / "src/evaluator/score.py"
    evaluator.parent.mkdir(parents=True)
    evaluator.write_text("SCORE = 1\n", encoding="utf-8")
    basis_commit = _commit(initialized_repository, "basis", "src/evaluator/score.py")
    basis_tree = _git(initialized_repository, "rev-parse", f"{basis_commit}^{{tree}}")
    report = _report(
        report_payload,
        validation_basis={"main_tree": basis_tree, "assessed_at": GENERATED_AT},
    )
    report_path = (
        initialized_repository
        / ".research/reports"
        / report.report_id
        / "1.yaml"
    )
    report_path.parent.mkdir(parents=True)
    report_path.write_text(dump_yaml(report), encoding="utf-8")
    _commit(
        initialized_repository,
        "accept report",
        report_path.relative_to(initialized_repository).as_posix(),
    )
    evaluator.write_text("SCORE = 2\n", encoding="utf-8")
    target_commit = _commit(
        initialized_repository,
        "fix evaluator",
        "src/evaluator/score.py",
    )

    bundle = GitReportImpactAnalyzer().analyze(
        initialized_repository,
        impact_id=IMPACT_ID,
        report_id=report.report_id,
        expected_report_revision=1,
        target_commit=target_commit,
        generated_at=GENERATED_AT,
    )

    assert bundle.impact.changed_paths == ("src/evaluator/score.py",)
    assert bundle.impact.outcome == "overlap"


def test_git_analyzer_rejects_stale_expected_revision(
    initialized_repository: Path,
    report_payload,
) -> None:
    basis = _git(initialized_repository, "rev-parse", "HEAD^{tree}")
    report = _report(
        report_payload,
        validation_basis={"main_tree": basis, "assessed_at": GENERATED_AT},
    )
    report_path = (
        initialized_repository
        / ".research/reports"
        / report.report_id
        / "1.yaml"
    )
    report_path.parent.mkdir(parents=True)
    report_path.write_text(dump_yaml(report), encoding="utf-8")
    source = initialized_repository / "README.md"
    source.write_text("changed\n", encoding="utf-8")
    target = _commit(
        initialized_repository,
        "report and source",
        report_path.relative_to(initialized_repository).as_posix(),
        "README.md",
    )

    with pytest.raises(RCPError) as raised:
        GitReportImpactAnalyzer().analyze(
            initialized_repository,
            impact_id=IMPACT_ID,
            report_id=report.report_id,
            expected_report_revision=2,
            target_commit=target,
            generated_at=GENERATED_AT,
        )

    assert raised.value.code == "stale_report_revision"


def test_batch_scan_combines_overlap_and_no_overlap_from_report_bases(
    initialized_repository: Path,
    report_payload,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evaluator = initialized_repository / "src/evaluator/score.py"
    evaluator.parent.mkdir(parents=True)
    evaluator.write_text("SCORE = 1\n", encoding="utf-8")
    basis_commit = _commit(
        initialized_repository,
        "shared report basis",
        "src/evaluator/score.py",
    )
    basis_tree = _git(initialized_repository, "rev-parse", f"{basis_commit}^{{tree}}")
    reports = (
        ReportRecord.model_validate(
            report_payload(
                report_id="report_20260803T120000Z_" + "d" * 24,
                validation_basis={
                    "main_tree": basis_tree,
                    "assessed_at": GENERATED_AT,
                },
                dependencies={
                    "paths": ["README.md"],
                    "resources": [],
                    "environments": [],
                },
            )
        ),
        ReportRecord.model_validate(
            report_payload(
                report_id="report_20260803T120000Z_" + "e" * 24,
                validation_basis={
                    "main_tree": basis_tree,
                    "assessed_at": GENERATED_AT,
                },
                dependencies={
                    "paths": ["src/evaluator/**"],
                    "resources": [],
                    "environments": [],
                },
            )
        ),
    )
    report_paths: list[str] = []
    for report in reports:
        root = initialized_repository / ".research/reports" / report.report_id
        root.mkdir(parents=True)
        (root / "1.yaml").write_text(dump_yaml(report), encoding="utf-8")
        (root / "1.md").write_text("# Accepted Report\n", encoding="utf-8")
        report_paths.extend(
            (
                f".research/reports/{report.report_id}/1.yaml",
                f".research/reports/{report.report_id}/1.md",
            )
        )
    before = _commit(initialized_repository, "accept reports", *report_paths)
    evaluator.write_text("SCORE = 2\n", encoding="utf-8")
    target = _commit(initialized_repository, "change evaluator", "src/evaluator/score.py")

    analyzer = GitReportImpactAnalyzer()
    changes = analyzer.git.changes
    diff_calls: list[tuple[str, str]] = []

    def counted_changes(*args, old_commit: str, new_commit: str, **kwargs):
        diff_calls.append((old_commit, new_commit))
        return changes(*args, old_commit=old_commit, new_commit=new_commit, **kwargs)

    monkeypatch.setattr(analyzer.git, "changes", counted_changes)
    analysis = analyzer.scan(
        initialized_repository,
        impact_id=IMPACT_ID,
        before_commit=before,
        target_commit=target,
        generated_at=GENERATED_AT,
    )

    assert analysis.terminal_result == "proposal_required"
    assert analysis.bundle is not None
    assert [item.impact.outcome for item in analysis.bundle.report_bundles] == [
        "no_overlap",
        "overlap",
    ]
    rendered_reports = analysis.bundle.as_dict()["reports"]
    assert isinstance(rendered_reports, list)
    assert {item["dependency_evaluator_id"] for item in rendered_reports} == {
        DECLARED_DEPENDENCY_EVALUATOR_ID
    }
    assert [
        item.proposed_report.applicability.value
        for item in analysis.bundle.report_bundles
    ] == ["current", "stale"]
    assert len(analysis.bundle.files) == 5
    assert analysis.bundle.files[0].path == (
        f".research/impacts/{IMPACT_ID}/impact-batch.yaml"
    )
    assert all(
        item.impact.basis_tree == basis_tree
        for item in analysis.bundle.report_bundles
    )
    assert diff_calls == [(basis_tree, analysis.target_tree)]


def test_batch_keeps_unresolved_external_report_out_of_validity_proposal(
    initialized_repository: Path,
    report_payload,
) -> None:
    evaluator = initialized_repository / "src/evaluator/score.py"
    evaluator.parent.mkdir(parents=True)
    evaluator.write_text("SCORE = 1\n", encoding="utf-8")
    basis_commit = _commit(
        initialized_repository,
        "external report basis",
        "src/evaluator/score.py",
    )
    basis_tree = _git(initialized_repository, "rev-parse", f"{basis_commit}^{{tree}}")
    overlap_report = ReportRecord.model_validate(
        report_payload(
            report_id="report_20260803T120000Z_" + "7" * 24,
            validation_basis={
                "main_tree": basis_tree,
                "assessed_at": GENERATED_AT,
            },
            dependencies={
                "paths": ["src/evaluator/**"],
                "resources": [],
                "environments": [],
            },
        )
    )
    unresolved_report = ReportRecord.model_validate(
        report_payload(
            report_id="report_20260803T120000Z_" + "8" * 24,
            validation_basis={
                "main_tree": basis_tree,
                "assessed_at": GENERATED_AT,
            },
            dependencies={
                "paths": ["README.md"],
                "resources": ["dataset:aime25"],
                "environments": [],
            },
        )
    )
    report_paths: list[str] = []
    for report in (overlap_report, unresolved_report):
        root = initialized_repository / ".research/reports" / report.report_id
        root.mkdir(parents=True)
        (root / "1.yaml").write_text(dump_yaml(report), encoding="utf-8")
        (root / "1.md").write_text("# Accepted Report\n", encoding="utf-8")
        report_paths.extend(
            (
                f".research/reports/{report.report_id}/1.yaml",
                f".research/reports/{report.report_id}/1.md",
            )
        )
    before = _commit(initialized_repository, "accept external reports", *report_paths)
    evaluator.write_text("SCORE = 2\n", encoding="utf-8")
    target = _commit(initialized_repository, "change evaluator", "src/evaluator/score.py")

    analysis = GitReportImpactAnalyzer().scan(
        initialized_repository,
        impact_id=IMPACT_ID,
        before_commit=before,
        target_commit=target,
        generated_at=GENERATED_AT,
    )

    assert analysis.terminal_result == "proposal_required"
    assert analysis.unresolved_report_ids == (unresolved_report.report_id,)
    assert analysis.bundle is not None
    assert [
        item.impact.report_id for item in analysis.bundle.report_bundles
    ] == [overlap_report.report_id]
    assert analysis.bundle.batch.unresolved_report_ids == (
        unresolved_report.report_id,
    )


def test_batch_no_change_does_not_create_branch_or_call_delivery(
    initialized_repository: Path,
) -> None:
    before = _git(initialized_repository, "rev-parse", "HEAD")
    readme = initialized_repository / "README.md"
    readme.write_text("# No accepted Reports yet\n", encoding="utf-8")
    target = _commit(initialized_repository, "source only", "README.md")

    class _ForbiddenDelivery:
        def push_exact(self, **_: object) -> object:
            raise AssertionError("no-change scan must not push")

        def open_or_observe(self, **_: object) -> object:
            raise AssertionError("no-change scan must not open a PR")

    request = ImpactBatchCreateRequest(
        operation_id="operation_20260803T120000Z_" + "2" * 24,
        idempotency_key="no-report-impact",
        impact_id=IMPACT_ID,
        before_commit=before,
        target_commit=target,
        generated_at=GENERATED_AT,
    )
    worktrees = initialized_repository / ".git/researchctl/worktrees"
    worktrees.mkdir(parents=True)
    receipt = ImpactWorkflowService(
        repository_root=initialized_repository,
        worktrees_directory=worktrees,
        default_branch="main",
        delivery=_ForbiddenDelivery(),  # type: ignore[arg-type]
    ).propose_batch(request)

    assert receipt.terminal_result == "no_change"
    assert receipt.prepared.commit is None
    assert not (worktrees / f"impact-{IMPACT_ID}").exists()


def test_unresolved_only_batch_returns_before_delivery(
    initialized_repository: Path,
) -> None:
    before = _git(initialized_repository, "rev-parse", "HEAD")
    readme = initialized_repository / "README.md"
    readme.write_text("# External evidence unresolved\n", encoding="utf-8")
    target = _commit(initialized_repository, "source change", "README.md")
    target_tree = _git(initialized_repository, "rev-parse", f"{target}^{{tree}}")
    unresolved_id = "report_20260803T120000Z_" + "6" * 24

    class _UnresolvedAnalyzer:
        def scan(self, *args, **kwargs):
            del args
            del kwargs
            return ReportImpactBatchAnalysis(
                before_commit=before,
                target_commit=target,
                target_tree=target_tree,
                report_ids=(unresolved_id,),
                snapshot_report_ids=(),
                ineligible_report_ids=(),
                up_to_date_report_ids=(),
                no_code_change_report_ids=(),
                unresolved_report_ids=(unresolved_id,),
                bundle=None,
            )

    class _ForbiddenDelivery:
        def push_exact(self, **_: object) -> object:
            raise AssertionError("unresolved scan must not push")

        def open_or_observe(self, **_: object) -> object:
            raise AssertionError("unresolved scan must not open a PR")

    request = ImpactBatchCreateRequest(
        operation_id="operation_20260803T120000Z_" + "5" * 24,
        idempotency_key="unresolved-only-impact",
        impact_id=IMPACT_ID,
        before_commit=before,
        target_commit=target,
        generated_at=GENERATED_AT,
    )
    worktrees = initialized_repository / ".git/researchctl/worktrees"
    worktrees.mkdir(parents=True)
    events: list[str] = []

    receipt = ImpactWorkflowService(
        repository_root=initialized_repository,
        worktrees_directory=worktrees,
        default_branch="main",
        analyzer=_UnresolvedAnalyzer(),  # type: ignore[arg-type]
        delivery=_ForbiddenDelivery(),  # type: ignore[arg-type]
    ).propose_batch(
        request,
        event_callback=lambda kind, payload: events.append(kind),
    )

    assert receipt.terminal_result == "impact_unresolved"
    assert receipt.prepared.analysis.unresolved_report_ids == (unresolved_id,)
    assert receipt.as_dict()["requires_input"] is True
    assert events == ["impact_batch_unresolved"]
    assert not (worktrees / f"impact-{IMPACT_ID}").exists()


def test_batch_rejects_stale_default_head_before_scan_or_mutation(
    initialized_repository: Path,
) -> None:
    before = _git(initialized_repository, "rev-parse", "HEAD")
    readme = initialized_repository / "README.md"
    readme.write_text("# First main change\n", encoding="utf-8")
    stale_target = _commit(initialized_repository, "first", "README.md")
    readme.write_text("# Current main change\n", encoding="utf-8")
    _commit(initialized_repository, "second", "README.md")
    request = ImpactBatchCreateRequest(
        operation_id="operation_20260803T120000Z_" + "4" * 24,
        idempotency_key="stale-main-impact",
        impact_id=IMPACT_ID,
        before_commit=before,
        target_commit=stale_target,
        generated_at=GENERATED_AT,
    )
    worktrees = initialized_repository / ".git/researchctl/worktrees"
    worktrees.mkdir(parents=True)

    with pytest.raises(RCPError) as raised:
        ImpactWorkflowService(
            repository_root=initialized_repository,
            worktrees_directory=worktrees,
            default_branch="main",
        ).prepare_batch(request)

    assert raised.value.code == "stale_impact_target"
    assert not (worktrees / f"impact-{IMPACT_ID}").exists()


def test_batch_commit_sha_is_stable_across_clean_runners(
    initialized_repository: Path,
    report_payload,
    tmp_path: Path,
) -> None:
    evaluator = initialized_repository / "src/evaluator/score.py"
    evaluator.parent.mkdir(parents=True)
    evaluator.write_text("SCORE = 1\n", encoding="utf-8")
    basis_commit = _commit(initialized_repository, "basis", "src/evaluator/score.py")
    basis_tree = _git(initialized_repository, "rev-parse", f"{basis_commit}^{{tree}}")
    report = ReportRecord.model_validate(
        report_payload(
            validation_basis={
                "main_tree": basis_tree,
                "assessed_at": GENERATED_AT,
            },
            dependencies={
                "paths": ["src/evaluator/**"],
                "resources": [],
                "environments": [],
            },
        )
    )
    report_root = initialized_repository / ".research/reports" / report.report_id
    report_root.mkdir(parents=True)
    (report_root / "1.yaml").write_text(dump_yaml(report), encoding="utf-8")
    (report_root / "1.md").write_text("# Accepted report\n", encoding="utf-8")
    before = _commit(
        initialized_repository,
        "accept report",
        f".research/reports/{report.report_id}/1.yaml",
        f".research/reports/{report.report_id}/1.md",
    )
    evaluator.write_text("SCORE = 2\n", encoding="utf-8")
    target = _commit(initialized_repository, "fix evaluator", "src/evaluator/score.py")
    request = ImpactBatchCreateRequest(
        operation_id="operation_20260803T120000Z_" + "3" * 24,
        idempotency_key="stable-clean-runner-impact",
        impact_id=IMPACT_ID,
        before_commit=before,
        target_commit=target,
        generated_at=GENERATED_AT,
    )
    commits: list[str] = []
    for name in ("runner-a", "runner-b"):
        clone = tmp_path / name
        subprocess.run(
            ["git", "clone", "--no-hardlinks", str(initialized_repository), str(clone)],
            check=True,
            capture_output=True,
            text=True,
        )
        worktrees = clone / ".git/researchctl/worktrees"
        worktrees.mkdir(parents=True)
        prepared = ImpactWorkflowService(
            repository_root=clone,
            worktrees_directory=worktrees,
            default_branch="main",
        ).prepare_batch(request)
        assert prepared.commit is not None
        commits.append(prepared.commit.commit)

    assert commits[0] == commits[1]
