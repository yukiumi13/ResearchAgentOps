from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from researchctl.cli import app
from researchctl.domain.enums import ImpactDisposition, ProjectState
from researchctl.domain.models import (
    CIValidationAttestation,
    CIValidationCheck,
    DocumentLayoutPolicy,
    GeneratedOutputDigest,
    GitHubGovernancePolicy,
    LinearProjectionDisabled,
    LinearProjectionPolicy,
    PlanReviewPolicy,
    ProjectPolicy,
    ProjectRecord,
    ReportRecord,
    TaskRecord,
)
from researchctl.errors import RCPError
from researchctl.schema import SCHEMA_MODELS
from researchctl.serialization import (
    canonical_digest,
    dump_yaml,
    load_model,
    load_yaml,
)
from researchctl.services.bootstrap_proposal import BootstrapProposalService
from researchctl.services.ci_dispatch import (
    CIPRDispatchAttestation,
    CIPRDispatchRequest,
    ProtectedBasePRDispatcher,
    load_ci_dispatch_artifact,
    submission_attestation_from_dispatch_artifact,
    write_ci_dispatch_artifact,
)
from researchctl.services.ci_validation import (
    CIValidationRequest,
    CIValidationResult,
)
from researchctl.services.control_bootstrap import ControlBootstrapAcceptance
from researchctl.services.control_document_layout_policy import (
    ControlDocumentLayoutPolicyRepository,
)
from researchctl.services.control_github_governance_policy import (
    ControlGitHubGovernancePolicyRepository,
)
from researchctl.services.control_linear_policy import ControlLinearPolicyRepository
from researchctl.services.control_plan_review_policy import (
    ControlPlanReviewPolicyRepository,
)
from researchctl.services.control_tasks import ControlTaskRecordRepository
from researchctl.services.impact_decision_workflow import (
    ImpactDecisionWorkflowService,
)
from researchctl.services.impact_workflow import ImpactWorkflowService
from researchctl.services.requests import (
    ImpactBatchCreateRequest,
    ImpactCreateRequest,
    ImpactDecisionCreateRequest,
)

ATTESTATION_ID = "attestation_20260803T120000Z_" + "a" * 24
BOOTSTRAP_ID = "bootstrap_20260803T120000Z_" + "b" * 24
PROPOSAL_OPERATION_ID = "operation_20260803T120000Z_" + "c" * 24
ACCEPT_OPERATION_ID = "operation_20260803T120001Z_" + "d" * 24
TASK_OPERATION_ID = "operation_20260803T120002Z_" + "e" * 24
LINEAR_OPERATION_ID = "operation_20260803T120003Z_" + "f" * 24
PLAN_REVIEW_OPERATION_ID = "operation_20260803T120003Z_" + "0" * 24
DOCUMENT_LAYOUT_OPERATION_ID = "operation_20260803T120003Z_" + "9" * 24
GITHUB_GOVERNANCE_OPERATION_ID = "operation_20260803T120003Z_" + "8" * 24
GENERATED_AT = "2026-08-03T12:00:00Z"
IMPACT_ID = "impact_20260803T120004Z_" + "1" * 24
IMPACT_OPERATION_ID = "operation_20260803T120004Z_" + "2" * 24
IMPACT_DECISION_ID = "decision_20260803T120005Z_" + "3" * 24
IMPACT_DECISION_OPERATION_ID = "operation_20260803T120005Z_" + "4" * 24


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


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
    return _git(repository, "rev-parse", "HEAD").strip()


def _request(
    *,
    base: str,
    head: str,
    head_ref: str,
) -> CIPRDispatchRequest:
    return CIPRDispatchRequest(
        attestation_id=ATTESTATION_ID,
        repository="example/research",
        pull_request_number=17,
        subject_head=head,
        base_commit=base,
        head_ref=head_ref,
        base_ref="main",
        generated_at=GENERATED_AT,
    )


def _promote_test_project(repository: Path) -> str:
    project_path = repository / ".research/project.yaml"
    project = load_model(project_path, ProjectRecord)
    project_payload = project.model_dump(mode="python")
    project_payload["state"] = ProjectState.MANAGED
    project_path.write_text(
        dump_yaml(ProjectRecord.model_validate(project_payload)),
        encoding="utf-8",
    )
    policy_path = repository / ".research/policies/default.yaml"
    policy = load_model(policy_path, ProjectPolicy)
    policy_payload = policy.model_dump(mode="python")
    policy_payload["execution_domains"] = [
        {"execution_domain": "on-prem", "host_pools": ["local"]}
    ]
    policy_path.write_text(
        dump_yaml(ProjectPolicy.model_validate(policy_payload)),
        encoding="utf-8",
    )
    return _commit(
        repository,
        "test fixture: accepted bootstrap",
        ".researchctl.toml",
        ".research",
    )


def test_ordinary_source_is_explicitly_not_applicable_even_on_control_branch(
    initialized_repository: Path,
) -> None:
    base = _git(initialized_repository, "rev-parse", "HEAD").strip()
    source = initialized_repository / "src" / "feature.py"
    source.parent.mkdir()
    source.write_text("VALUE = 1\n", encoding="utf-8")
    head = _commit(initialized_repository, "ordinary source", "src/feature.py")

    result = ProtectedBasePRDispatcher().validate(
        initialized_repository,
        _request(
            base=base,
            head=head,
            head_ref=f"research/control/{TASK_OPERATION_ID}",
        ),
    )

    assert result.attestation.pr_type == "ordinary_source"
    assert result.attestation.applicability == "not_applicable"
    assert [item.name for item in result.attestation.checks] == [
        "protocol_path_absence"
    ]


def test_unknown_policy_only_protocol_change_fails_closed(
    initialized_repository: Path,
) -> None:
    base = _git(initialized_repository, "rev-parse", "HEAD").strip()
    head = _commit(
        initialized_repository,
        "unsupported policy mutation",
        ".research/policies/default.yaml",
    )

    with pytest.raises(RCPError) as raised:
        ProtectedBasePRDispatcher().validate(
            initialized_repository,
            _request(base=base, head=head, head_ref="policy/change"),
        )

    assert raised.value.code == "ci_pr_type_unknown"
    assert raised.value.context["changed_paths"] == [
        ".research/policies/default.yaml"
    ]


def test_generated_task_control_is_validated_from_path_content_and_marker(
    initialized_repository: Path,
    task_payload,
) -> None:
    base = _promote_test_project(initialized_repository)
    worktrees = initialized_repository / ".git" / "researchctl" / "worktrees"
    worktrees.mkdir(parents=True)
    task = TaskRecord.model_validate(task_payload(state="planned"))
    control = ControlTaskRecordRepository(
        repository_root=initialized_repository,
        worktrees_directory=worktrees,
        default_branch="main",
        operation_id=TASK_OPERATION_ID,
        command="task.create",
    )
    control.create(task)
    assert control.proposal_receipt is not None

    result = ProtectedBasePRDispatcher().validate(
        initialized_repository,
        _request(
            base=base,
            head=control.proposal_receipt.commit,
            head_ref=control.branch,
        ),
    )

    assert result.attestation.pr_type == "task_control"
    assert result.attestation.applicability == "validated"
    assert {item.name for item in result.attestation.checks} == {
        "pr_type_dispatch",
        "task_record",
        "task_transition",
        "trusted_base",
    }


def _linear_policy() -> LinearProjectionPolicy:
    return LinearProjectionPolicy(
        workspace_id="11111111-1111-4111-8111-111111111111",
        team_id="22222222-2222-4222-8222-222222222222",
        notification_author_ids=(
            "33333333-3333-4333-8333-333333333333",
        ),
    )


def _github_governance_policy() -> GitHubGovernancePolicy:
    return GitHubGovernancePolicy.model_validate(
        {
            "repository": "owner/project",
            "default_branch": "main",
            "agent_app": {
                "app_id": 12345,
                "installation_id": 67890,
                "login": "researchctl-agent[bot]",
            },
            "managers": [{"kind": "user", "login": "manager"}],
        }
    )


def test_generated_linear_policy_control_is_validated_without_network(
    initialized_repository: Path,
) -> None:
    base = _promote_test_project(initialized_repository)
    worktrees = initialized_repository / ".git" / "researchctl" / "worktrees"
    worktrees.mkdir(parents=True)
    control = ControlLinearPolicyRepository(
        repository_root=initialized_repository,
        worktrees_directory=worktrees,
        default_branch="main",
        operation_id=LINEAR_OPERATION_ID,
        expected_default_head=base,
    )
    written = control.configure(_linear_policy())

    result = ProtectedBasePRDispatcher().validate(
        initialized_repository,
        _request(
            base=base,
            head=written.proposal.commit,
            head_ref=control.branch,
        ),
    )

    assert result.attestation.pr_type == "linear_policy_control"
    assert result.attestation.applicability == "validated"
    assert {item.name for item in result.attestation.checks} == {
        "linear_policy",
        "linear_policy_transition",
        "pr_type_dispatch",
        "trusted_base",
    }


def test_generated_plan_review_policy_control_is_exactly_scoped(
    initialized_repository: Path,
) -> None:
    base = _promote_test_project(initialized_repository)
    worktrees = initialized_repository / ".git" / "researchctl" / "worktrees"
    worktrees.mkdir(parents=True)
    control = ControlPlanReviewPolicyRepository(
        repository_root=initialized_repository,
        worktrees_directory=worktrees,
        default_branch="main",
        operation_id=PLAN_REVIEW_OPERATION_ID,
        expected_default_head=base,
    )
    written = control.configure(
        PlanReviewPolicy(
            provider="codex",
            model="gpt-test-reviewer",
            policy_version="plan-review-v1",
            timeout_seconds=60,
        )
    )

    result = ProtectedBasePRDispatcher().validate(
        initialized_repository,
        _request(
            base=base,
            head=written.proposal.commit,
            head_ref=control.branch,
        ),
    )

    assert result.attestation.pr_type == "plan_review_policy_control"
    assert result.attestation.applicability == "validated"
    assert {item.name for item in result.attestation.checks} == {
        "plan_review_policy",
        "project_policy_transition",
        "pr_type_dispatch",
        "trusted_base",
    }


def test_plan_review_policy_control_rejects_other_project_policy_changes(
    initialized_repository: Path,
) -> None:
    base = _promote_test_project(initialized_repository)
    branch = f"research/control/{PLAN_REVIEW_OPERATION_ID}"
    _git(initialized_repository, "checkout", "-b", branch)
    path = initialized_repository / ".research" / "policies" / "default.yaml"
    policy = load_model(path, ProjectPolicy)
    replacement = policy.model_copy(
        update={
            "plan_review": PlanReviewPolicy(
                provider="codex",
                model="gpt-test-reviewer",
                policy_version="plan-review-v1",
                timeout_seconds=60,
            ),
            "plan_choices": {"undeclared_default": True},
        }
    )
    path.write_text(dump_yaml(replacement), encoding="utf-8")
    head = _commit(
        initialized_repository,
        f"researchctl: plan.configure-review {PLAN_REVIEW_OPERATION_ID}",
        ".research/policies/default.yaml",
    )

    with pytest.raises(RCPError) as raised:
        ProtectedBasePRDispatcher().validate(
            initialized_repository,
            _request(base=base, head=head, head_ref=branch),
        )

    assert raised.value.code == "ci_plan_review_policy_scope_invalid"


def test_document_layout_policy_control_is_field_scoped_and_exact_head(
    initialized_repository: Path,
) -> None:
    base = _promote_test_project(initialized_repository)
    worktrees = initialized_repository / ".git" / "researchctl" / "worktrees"
    worktrees.mkdir(parents=True, exist_ok=True)
    control = ControlDocumentLayoutPolicyRepository(
        repository_root=initialized_repository,
        worktrees_directory=worktrees,
        default_branch="main",
        operation_id=DOCUMENT_LAYOUT_OPERATION_ID,
        expected_default_head=base,
    )
    payload = DocumentLayoutPolicy().model_dump(mode="json")
    payload["routes"].append(
        {
            "classification": "research/evidence:ledger",
            "document_type": "ledger",
            "directory": "docs/ledger",
            "contract": "markdown-frontmatter",
            "rationale": "Existing evidence ledgers require this route.",
            "required_relations": [],
        }
    )
    written = control.configure(DocumentLayoutPolicy.model_validate(payload))

    result = ProtectedBasePRDispatcher().validate(
        initialized_repository,
        _request(
            base=base,
            head=written.proposal.commit,
            head_ref=control.branch,
        ),
    )

    assert result.attestation.pr_type == "document_layout_policy_control"
    assert {item.name for item in result.attestation.checks} == {
        "document_layout_policy",
        "project_policy_transition",
        "pr_type_dispatch",
        "trusted_base",
    }


def test_document_layout_policy_control_rejects_other_policy_changes(
    initialized_repository: Path,
) -> None:
    base = _promote_test_project(initialized_repository)
    branch = f"research/control/{DOCUMENT_LAYOUT_OPERATION_ID}"
    _git(initialized_repository, "checkout", "-b", branch)
    path = initialized_repository / ".research" / "policies" / "default.yaml"
    policy = load_model(path, ProjectPolicy)
    replacement = policy.model_copy(
        update={
            "document_layout": policy.document_layout.model_copy(
                update={"max_depth": 3}
            ),
            "plan_choices": {"smuggled": True},
        }
    )
    path.write_text(dump_yaml(replacement), encoding="utf-8")
    head = _commit(
        initialized_repository,
        f"researchctl: doc.configure-layout {DOCUMENT_LAYOUT_OPERATION_ID}",
        ".research/policies/default.yaml",
    )

    with pytest.raises(RCPError) as raised:
        ProtectedBasePRDispatcher().validate(
            initialized_repository,
            _request(base=base, head=head, head_ref=branch),
        )

    assert raised.value.code == "ci_document_layout_policy_scope_invalid"


def test_github_governance_policy_control_is_field_scoped_and_exact_head(
    initialized_repository: Path,
) -> None:
    base = _promote_test_project(initialized_repository)
    worktrees = initialized_repository / ".git" / "researchctl" / "worktrees"
    worktrees.mkdir(parents=True, exist_ok=True)
    control = ControlGitHubGovernancePolicyRepository(
        repository_root=initialized_repository,
        worktrees_directory=worktrees,
        default_branch="main",
        operation_id=GITHUB_GOVERNANCE_OPERATION_ID,
        expected_default_head=base,
    )
    written = control.configure(_github_governance_policy())

    result = ProtectedBasePRDispatcher().validate(
        initialized_repository,
        _request(
            base=base,
            head=written.proposal.commit,
            head_ref=control.branch,
        ),
    )

    assert result.attestation.pr_type == "github_governance_policy_control"
    assert {item.name for item in result.attestation.checks} == {
        "github_governance_policy",
        "project_policy_transition",
        "pr_type_dispatch",
        "trusted_base",
    }


def test_github_governance_policy_control_rejects_other_policy_changes(
    initialized_repository: Path,
) -> None:
    base = _promote_test_project(initialized_repository)
    branch = f"research/control/{GITHUB_GOVERNANCE_OPERATION_ID}"
    _git(initialized_repository, "checkout", "-b", branch)
    path = initialized_repository / ".research" / "policies" / "default.yaml"
    policy = load_model(path, ProjectPolicy)
    replacement = policy.model_copy(
        update={
            "github": _github_governance_policy(),
            "plan_choices": {"smuggled": True},
        }
    )
    path.write_text(dump_yaml(replacement), encoding="utf-8")
    head = _commit(
        initialized_repository,
        (
            "researchctl: github.configure-governance "
            f"{GITHUB_GOVERNANCE_OPERATION_ID}"
        ),
        ".research/policies/default.yaml",
    )

    with pytest.raises(RCPError) as raised:
        ProtectedBasePRDispatcher().validate(
            initialized_repository,
            _request(base=base, head=head, head_ref=branch),
        )

    assert raised.value.code == "ci_github_governance_policy_scope_invalid"


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("wrong_marker", "ci_linear_policy_commit_invalid"),
        ("extra_path", "ci_linear_policy_scope_invalid"),
        ("noncanonical", "ci_record_not_canonical"),
        ("executable", "ci_linear_policy_scope_invalid"),
        ("invalid_policy", "ci_record_invalid"),
    ],
)
def test_linear_policy_control_rejects_unsafe_or_malformed_changes(
    initialized_repository: Path,
    mutation: str,
    expected_code: str,
) -> None:
    base = _promote_test_project(initialized_repository)
    branch = f"research/control/{LINEAR_OPERATION_ID}"
    _git(initialized_repository, "checkout", "-b", branch)
    path = initialized_repository / ".research" / "policies" / "linear.yaml"
    if mutation == "invalid_policy":
        payload = _linear_policy().model_dump(mode="json", exclude_none=True)
        payload["notification_author_ids"] = [
            "33333333-3333-4333-8333-333333333333",
            "33333333-3333-4333-8333-333333333333",
        ]
        content = dump_yaml(payload)
    else:
        content = dump_yaml(_linear_policy())
    if mutation == "noncanonical":
        content += "\n"
    path.write_text(content, encoding="utf-8")
    paths = [".research/policies/linear.yaml"]
    if mutation == "extra_path":
        extra = initialized_repository / "smuggled.txt"
        extra.write_text("not policy\n", encoding="utf-8")
        paths.append("smuggled.txt")
    if mutation == "executable":
        path.chmod(0o755)
    message = (
        "wrong marker"
        if mutation == "wrong_marker"
        else f"researchctl: linear.configure {LINEAR_OPERATION_ID}"
    )
    head = _commit(initialized_repository, message, *paths)

    with pytest.raises(RCPError) as raised:
        ProtectedBasePRDispatcher().validate(
            initialized_repository,
            _request(base=base, head=head, head_ref=branch),
        )

    assert raised.value.code == expected_code


def test_generated_impact_is_rebuilt_from_protected_base_and_report_basis(
    initialized_repository: Path,
    report_payload,
) -> None:
    _promote_test_project(initialized_repository)
    evaluator = initialized_repository / "src" / "evaluator" / "score.py"
    evaluator.parent.mkdir(parents=True)
    evaluator.write_text("SCORE = 1\n", encoding="utf-8")
    basis_commit = _commit(
        initialized_repository,
        "accepted evaluator basis",
        "src/evaluator/score.py",
    )
    basis_tree = _git(
        initialized_repository,
        "rev-parse",
        f"{basis_commit}^{{tree}}",
    ).strip()
    report = ReportRecord.model_validate(
        report_payload(
            accepted_at_main_tree=basis_tree,
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
    _commit(
        initialized_repository,
        "accept baseline report",
        f".research/reports/{report.report_id}/1.yaml",
        f".research/reports/{report.report_id}/1.md",
    )
    evaluator.write_text("SCORE = 2\n", encoding="utf-8")
    target = _commit(
        initialized_repository,
        "fix evaluator",
        "src/evaluator/score.py",
    )
    request = ImpactCreateRequest(
        operation_id=IMPACT_OPERATION_ID,
        idempotency_key="impact-evaluator-fix",
        impact_id=IMPACT_ID,
        report_id=report.report_id,
        expected_report_revision=1,
        target_commit=target,
    )
    worktrees = initialized_repository / ".git" / "researchctl" / "worktrees"
    worktrees.mkdir(parents=True)
    prepared = ImpactWorkflowService(
        repository_root=initialized_repository,
        worktrees_directory=worktrees,
        default_branch="main",
    ).prepare_proposal(request, generated_at=GENERATED_AT)

    result = ProtectedBasePRDispatcher().validate(
        initialized_repository,
        _request(
            base=target,
            head=prepared.commit.commit,
            head_ref=prepared.commit.branch,
        ),
    )

    assert result.attestation.pr_type == "impact"
    assert result.attestation.applicability == "validated"
    assert {item.name for item in result.attestation.checks} == {
        "impact_record",
        "impact_regeneration",
        "pr_type_dispatch",
        "trusted_base",
    }
    assert prepared.bundle.impact.outcome == "overlap"
    assert prepared.bundle.proposed_report.applicability.value == "stale"

    markdown_path = next(
        item.path for item in prepared.bundle.files if item.path.endswith(".md")
    )
    markdown = prepared.commit.worktree / markdown_path
    markdown.write_text(
        markdown.read_text(encoding="utf-8") + "Unreviewed claim.\n",
        encoding="utf-8",
    )
    _git(prepared.commit.worktree, "add", "--", markdown_path)
    _git(
        prepared.commit.worktree,
        "-c",
        "user.name=Tests",
        "-c",
        "user.email=tests@example.invalid",
        "commit",
        "--amend",
        "--no-edit",
        "--no-gpg-sign",
    )
    tampered_head = _git(prepared.commit.worktree, "rev-parse", "HEAD").strip()

    with pytest.raises(RCPError) as raised:
        ProtectedBasePRDispatcher().validate(
            initialized_repository,
            _request(
                base=target,
                head=tampered_head,
                head_ref=prepared.commit.branch,
            ),
        )

    assert raised.value.code == "ci_impact_generated_output_mismatch"


def test_generated_impact_batch_is_regenerated_as_one_exact_head(
    initialized_repository: Path,
    report_payload,
) -> None:
    _promote_test_project(initialized_repository)
    evaluator = initialized_repository / "src/evaluator/score.py"
    evaluator.parent.mkdir(parents=True)
    evaluator.write_text("SCORE = 1\n", encoding="utf-8")
    basis_commit = _commit(
        initialized_repository,
        "batch report basis",
        "src/evaluator/score.py",
    )
    basis_tree = _git(
        initialized_repository,
        "rev-parse",
        f"{basis_commit}^{{tree}}",
    ).strip()
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
        (root / "1.md").write_text("# Accepted report\n", encoding="utf-8")
        report_paths.extend(
            (
                f".research/reports/{report.report_id}/1.yaml",
                f".research/reports/{report.report_id}/1.md",
            )
        )
    before = _commit(initialized_repository, "accept batch reports", *report_paths)
    evaluator.write_text("SCORE = 2\n", encoding="utf-8")
    target = _commit(
        initialized_repository,
        "fix evaluator for batch",
        "src/evaluator/score.py",
    )
    request = ImpactBatchCreateRequest(
        operation_id=IMPACT_OPERATION_ID,
        idempotency_key="impact-batch-evaluator-fix",
        impact_id=IMPACT_ID,
        before_commit=before,
        target_commit=target,
        generated_at=GENERATED_AT,
    )
    worktrees = initialized_repository / ".git/researchctl/worktrees"
    worktrees.mkdir(parents=True)
    prepared = ImpactWorkflowService(
        repository_root=initialized_repository,
        worktrees_directory=worktrees,
        default_branch="main",
    ).prepare_batch(request)
    assert prepared.bundle is not None
    assert prepared.commit is not None

    result = ProtectedBasePRDispatcher().validate(
        initialized_repository,
        _request(
            base=target,
            head=prepared.commit.commit,
            head_ref=prepared.commit.branch,
        ),
    )

    assert result.attestation.pr_type == "impact"
    assert prepared.commit.command == "impact.batch"
    assert len(prepared.bundle.report_bundles) == 2
    assert [
        item.impact.outcome for item in prepared.bundle.report_bundles
    ] == ["no_overlap", "overlap"]
    assert tuple(prepared.commit.paths) == tuple(
        item.path for item in prepared.bundle.files
    )


def test_impact_decision_is_regenerated_from_accepted_impact_and_report(
    initialized_repository: Path,
    report_payload,
) -> None:
    _promote_test_project(initialized_repository)
    evaluator = initialized_repository / "src/evaluator/score.py"
    evaluator.parent.mkdir(parents=True)
    evaluator.write_text("SCORE = 1\n", encoding="utf-8")
    basis_commit = _commit(
        initialized_repository,
        "decision report basis",
        "src/evaluator/score.py",
    )
    basis_tree = _git(
        initialized_repository,
        "rev-parse",
        f"{basis_commit}^{{tree}}",
    ).strip()
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
    _commit(
        initialized_repository,
        "accept decision report",
        f".research/reports/{report.report_id}/1.yaml",
        f".research/reports/{report.report_id}/1.md",
    )
    evaluator.write_text("SCORE = 2\n", encoding="utf-8")
    target = _commit(
        initialized_repository,
        "change evaluator for decision",
        "src/evaluator/score.py",
    )
    worktrees = initialized_repository / ".git/researchctl/worktrees"
    worktrees.mkdir(parents=True, exist_ok=True)
    impact = ImpactWorkflowService(
        repository_root=initialized_repository,
        worktrees_directory=worktrees,
        default_branch="main",
    ).prepare_proposal(
        ImpactCreateRequest(
            operation_id=IMPACT_OPERATION_ID,
            idempotency_key="prepare-impact-for-decision",
            impact_id=IMPACT_ID,
            report_id=report.report_id,
            expected_report_revision=1,
            target_commit=target,
        ),
        generated_at=GENERATED_AT,
    )
    _git(initialized_repository, "merge", "--ff-only", impact.commit.commit)
    accepted_impact = _git(initialized_repository, "rev-parse", "HEAD").strip()
    decision = ImpactDecisionWorkflowService(
        repository_root=initialized_repository,
        worktrees_directory=worktrees,
        default_branch="main",
    ).prepare(
        ImpactDecisionCreateRequest(
            operation_id=IMPACT_DECISION_OPERATION_ID,
            idempotency_key="keep-reviewed-impact-stale",
            decision_id=IMPACT_DECISION_ID,
            impact_id=IMPACT_ID,
            report_id=report.report_id,
            expected_report_revision=2,
            expected_impact_digest=impact.bundle.impact.impact_digest,
            target_commit=accepted_impact,
            disposition=ImpactDisposition.KEEP_STALE,
            reason="A rerun plan is still under review.",
        ),
        reviewer_actor="manager@example.invalid",
        decided_at="2026-08-03T12:05:00Z",
    )

    result = ProtectedBasePRDispatcher().validate(
        initialized_repository,
        _request(
            base=accepted_impact,
            head=decision.commit.commit,
            head_ref=decision.commit.branch,
        ),
    )

    assert result.attestation.pr_type == "impact_decision"
    assert {item.name for item in result.attestation.checks} == {
        "impact_decision_record",
        "impact_decision_regeneration",
        "impact_decision_task",
        "pr_type_dispatch",
        "trusted_base",
    }
    assert decision.bundle.report.revision == 3
    assert decision.bundle.as_dict()["automatically_runs_experiments"] is False

    markdown_path = next(
        item.path for item in decision.bundle.files if item.path.endswith(".md")
    )
    markdown = decision.commit.worktree / markdown_path
    markdown.write_text(
        markdown.read_text(encoding="utf-8") + "Unreviewed text.\n",
        encoding="utf-8",
    )
    _git(decision.commit.worktree, "add", "--", markdown_path)
    _git(
        decision.commit.worktree,
        "-c",
        "user.name=Tests",
        "-c",
        "user.email=tests@example.invalid",
        "commit",
        "--amend",
        "--no-edit",
        "--no-gpg-sign",
    )
    tampered = _git(decision.commit.worktree, "rev-parse", "HEAD").strip()
    with pytest.raises(RCPError) as raised:
        ProtectedBasePRDispatcher().validate(
            initialized_repository,
            _request(
                base=accepted_impact,
                head=tampered,
                head_ref=decision.commit.branch,
            ),
        )

    assert raised.value.code == "ci_impact_decision_output_mismatch"


def test_task_control_with_source_change_fails_as_mixed_protocol_change(
    initialized_repository: Path,
    task_payload,
) -> None:
    base = _promote_test_project(initialized_repository)
    branch = f"research/control/{TASK_OPERATION_ID}"
    _git(initialized_repository, "checkout", "-b", branch)
    task = TaskRecord.model_validate(task_payload(state="planned"))
    task_path = initialized_repository / ".research/tasks" / f"{task.task_id}.yaml"
    task_path.write_text(dump_yaml(task), encoding="utf-8")
    extra = initialized_repository / "src" / "smuggled.py"
    extra.parent.mkdir()
    extra.write_text("CLAIM = 'not control state'\n", encoding="utf-8")
    head = _commit(
        initialized_repository,
        f"researchctl: task.create {TASK_OPERATION_ID}",
        task_path.relative_to(initialized_repository).as_posix(),
        "src/smuggled.py",
    )

    with pytest.raises(RCPError) as raised:
        ProtectedBasePRDispatcher().validate(
            initialized_repository,
            _request(base=base, head=head, head_ref=branch),
        )

    assert raised.value.code == "ci_task_control_scope_invalid"


def test_bootstrap_proposal_and_two_commit_acceptance_are_both_supported(
    initialized_repository: Path,
) -> None:
    base = _git(initialized_repository, "rev-parse", "HEAD").strip()
    worktrees = initialized_repository / ".git" / "researchctl" / "worktrees"
    worktrees.mkdir(parents=True)
    proposal = BootstrapProposalService(
        repository_root=initialized_repository,
        worktrees_directory=worktrees,
        default_branch="main",
        expected_default_head=base,
        operation_id=PROPOSAL_OPERATION_ID,
        bootstrap_id=BOOTSTRAP_ID,
    ).prepare()

    proposed = ProtectedBasePRDispatcher().validate(
        initialized_repository,
        _request(base=base, head=proposal.commit, head_ref=proposal.branch),
    )
    assert proposed.attestation.pr_type == "bootstrap_proposal"

    acceptance = ControlBootstrapAcceptance(
        repository_root=initialized_repository,
        worktrees_directory=worktrees,
        default_branch="main",
        operation_id=ACCEPT_OPERATION_ID,
        proposal_commit=proposal.commit,
    ).prepare()
    accepted = ProtectedBasePRDispatcher().validate(
        initialized_repository,
        _request(
            base=base,
            head=acceptance.commit,
            head_ref=acceptance.branch,
        ),
    )

    assert accepted.attestation.pr_type == "bootstrap_acceptance"
    assert accepted.attestation.applicability == "validated"
    assert _git(
        initialized_repository,
        "rev-list",
        "--count",
        f"{base}..{acceptance.commit}",
    ).strip() == "2"

    accepted_after_proposal_merge = ProtectedBasePRDispatcher().validate(
        initialized_repository,
        _request(
            base=proposal.commit,
            head=acceptance.commit,
            head_ref=acceptance.branch,
        ),
    )
    assert accepted_after_proposal_merge.attestation.pr_type == (
        "bootstrap_acceptance"
    )


class _StubSubmissionValidator:
    def __init__(self) -> None:
        self.requests: list[CIValidationRequest] = []

    def validate(
        self,
        repository_root: Path,
        request: CIValidationRequest,
    ) -> CIValidationResult:
        self.requests.append(request)
        tree = _git(repository_root, "rev-parse", f"{request.subject_head}^{{tree}}").strip()
        generated = GeneratedOutputDigest(
            path=".research/submissions/output.md",
            digest="sha256:" + "1" * 64,
            size_bytes=1,
        )
        artifact_digest = canonical_digest(
            {"generated_outputs": [generated.model_dump(mode="json")]}
        )
        attestation = CIValidationAttestation(
            attestation_id=request.attestation_id,
            project_id="project_20260803T120000Z_" + "2" * 24,
            task_id="task_20260803T120000Z_" + "3" * 24,
            submission_id=request.submission_id,
            repository=request.repository,
            pull_request_number=request.pull_request_number,
            subject_head=request.subject_head,
            subject_tree=tree,
            base_commit=request.base_commit,
            validator_version="0.1.0",
            schema_manifest_digest="sha256:" + "4" * 64,
            workflow_id="research-validate-pr",
            check_identity="researchctl/exact-head",
            checks=(CIValidationCheck(name="stub", status="passed"),),
            generated_outputs=(generated,),
            submission_digest="sha256:" + "5" * 64,
            report_proposal_digest="sha256:" + "6" * 64,
            report_preview_digest="sha256:" + "7" * 64,
            projection=LinearProjectionDisabled(
                reason="integration_not_configured"
            ),
            generated_at=request.generated_at,
            artifact_digest=artifact_digest,
            overall_result="passed",
        )
        return CIValidationResult(
            attestation=attestation,
            generated_files=(),
            linear_body=None,
            head_kind="submission_proposal",
        )


def test_submission_identity_is_derived_from_changed_record_and_typed_envelope(
    initialized_repository: Path,
    tmp_path: Path,
) -> None:
    base = _git(initialized_repository, "rev-parse", "HEAD").strip()
    submission_id = "submission_20260803T120000Z_" + "8" * 24
    relative = f".research/submissions/{submission_id}/submission.yaml"
    path = initialized_repository / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("stub: exact validator owns content\n", encoding="utf-8")
    head = _commit(initialized_repository, "stub submission", relative)
    stub = _StubSubmissionValidator()
    dispatcher = ProtectedBasePRDispatcher(submissions=stub)  # type: ignore[arg-type]

    result = dispatcher.validate(
        initialized_repository,
        _request(
            base=base,
            head=head,
            head_ref=f"research/submission/{submission_id}",
        ),
    )
    artifact = tmp_path / "dispatch.yaml"
    write_ci_dispatch_artifact(result, artifact)

    assert result.attestation.pr_type == "submission"
    assert stub.requests[0].submission_id == submission_id
    loaded = load_ci_dispatch_artifact(artifact.read_bytes())
    extracted = submission_attestation_from_dispatch_artifact(
        artifact.read_bytes()
    )
    assert loaded.submission_attestation == extracted
    assert extracted.submission_id == submission_id
    assert CIPRDispatchAttestation not in SCHEMA_MODELS.values()

    forged = load_yaml(artifact.read_text(encoding="utf-8"))
    nested = forged["submission_attestation"]
    assert isinstance(nested, dict)
    nested["repository"] = "school/another-repository"
    with pytest.raises(RCPError) as raised:
        load_ci_dispatch_artifact(dump_yaml(forged).encode("utf-8"))
    assert raised.value.code == "ci_dispatch_artifact_invalid"


def test_ci_dispatch_cli_human_and_strict_json_emit_same_envelope(
    initialized_repository: Path,
    tmp_path: Path,
) -> None:
    base = _git(initialized_repository, "rev-parse", "HEAD").strip()
    source = initialized_repository / "feature.txt"
    source.write_text("source\n", encoding="utf-8")
    head = _commit(initialized_repository, "source", "feature.txt")
    request = _request(base=base, head=head, head_ref="feature/source")
    human_artifact = tmp_path / "human.yaml"
    machine_artifact = tmp_path / "machine.yaml"
    runner = CliRunner()

    human = runner.invoke(
        app,
        [
            "ci",
            "dispatch",
            "-C",
            str(initialized_repository),
            "--artifact",
            str(human_artifact),
            "--repository",
            request.repository,
            "--pull-request-number",
            str(request.pull_request_number),
            "--subject-head",
            request.subject_head,
            "--base-commit",
            request.base_commit,
            "--head-ref",
            request.head_ref,
            "--base-ref",
            request.base_ref,
            "--attestation-id",
            request.attestation_id,
            "--generated-at",
            GENERATED_AT,
        ],
    )
    machine = runner.invoke(
        app,
        [
            "ci",
            "dispatch",
            "-C",
            str(initialized_repository),
            "--json",
            "--artifact",
            str(machine_artifact),
        ],
        input=json.dumps(request.model_dump(mode="json")),
    )

    assert human.exit_code == 0, human.output
    assert machine.exit_code == 0, machine.output
    assert "PR type: ordinary_source" in human.output
    assert json.loads(machine.output)["data"]["applicability"] == "not_applicable"
    assert human_artifact.read_bytes() == machine_artifact.read_bytes()
