from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from pydantic import ValidationError

from researchctl.domain.enums import (
    ClaimScope,
    InputKind,
    ReportApplicability,
    ReviewDisposition,
)
from researchctl.domain.models import (
    ExecutionDomainPolicy,
    ProjectPolicy,
    ReportRecord,
    ResearchSubmission,
    ReviewDecision,
    RunAttempt,
    RunResult,
    RunSpec,
    TaskRecord,
)

PayloadFactory = Callable[..., dict[str, Any]]


def test_task_accepts_typed_versioned_and_digested_inputs(
    task_payload: PayloadFactory,
) -> None:
    payload = task_payload(
        required_inputs=[
            {
                "kind": "dataset",
                "logical_id": "validation-split",
                "version": "2026-08-01",
            },
            {
                "kind": "checkpoint",
                "logical_id": "base-model",
                "digest": "sha256:" + "1" * 64,
                "uri": "s3://research/models/base-model",
                "resolver": "object-store-v1",
            },
            {
                "kind": "environment",
                "logical_id": "trainer-cu128",
                "version": "2.4.1",
                "digest": "sha256:" + "2" * 64,
                "waiver_allowed": True,
            },
        ]
    )

    task = TaskRecord.model_validate(payload)

    assert isinstance(task.required_inputs, tuple)
    assert [item.kind for item in task.required_inputs] == [
        InputKind.DATASET,
        InputKind.CHECKPOINT,
        InputKind.ENVIRONMENT,
    ]
    assert task.required_inputs[1].version is None
    assert task.required_inputs[1].digest == "sha256:" + "1" * 64


@pytest.mark.parametrize("waiver_allowed", [False, True])
def test_task_input_requires_a_version_or_digest_even_when_waivable(
    task_payload: PayloadFactory,
    waiver_allowed: bool,
) -> None:
    payload = task_payload(
        required_inputs=[
            {
                "kind": "dataset",
                "logical_id": "validation-split",
                "waiver_allowed": waiver_allowed,
            }
        ]
    )

    with pytest.raises(ValidationError, match="requires version or digest"):
        TaskRecord.model_validate(payload)


@pytest.mark.parametrize(
    "invalid_input",
    [
        {
            "kind": "untyped-resource",
            "logical_id": "validation-split",
            "version": "1",
        },
        {
            "kind": "dataset",
            "logical_id": "validation-split",
            "digest": "sha256:1234",
        },
        {
            "kind": "dataset",
            "logical_id": "validation-split",
            "digest": "sha256:" + "A" * 64,
        },
    ],
)
def test_task_rejects_malformed_typed_inputs(
    task_payload: PayloadFactory,
    invalid_input: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        TaskRecord.model_validate(task_payload(required_inputs=[invalid_input]))


def test_task_requires_at_least_one_done_condition(task_payload: PayloadFactory) -> None:
    with pytest.raises(ValidationError):
        TaskRecord.model_validate(task_payload(done_when=[]))


def test_protocol_models_forbid_unknown_top_level_fields(
    task_payload: PayloadFactory,
) -> None:
    with pytest.raises(ValidationError) as error:
        TaskRecord.model_validate(task_payload(unexpected_policy="allow-all"))

    assert ("unexpected_policy",) in {item["loc"] for item in error.value.errors()}


def test_protocol_models_forbid_unknown_nested_fields(
    task_payload: PayloadFactory,
) -> None:
    payload = task_payload()
    payload["required_inputs"][0]["checksum"] = "not-a-declared-field"

    with pytest.raises(ValidationError) as error:
        TaskRecord.model_validate(payload)

    locations = {item["loc"] for item in error.value.errors()}
    assert ("required_inputs", 0, "checksum") in locations


@pytest.mark.parametrize("schema_version", ["0.2", "1", "1.0", 1])
def test_protocol_models_fail_closed_on_unknown_schema_versions(
    task_payload: PayloadFactory,
    schema_version: str | int,
) -> None:
    with pytest.raises(ValidationError):
        TaskRecord.model_validate(task_payload(schema_version=schema_version))


def test_run_attempt_accepts_strictly_increasing_sequences_with_gaps(
    run_attempt_payload: PayloadFactory,
) -> None:
    attempt = RunAttempt.model_validate(run_attempt_payload((0, 2, 7)))

    assert [event.sequence for event in attempt.events] == [0, 2, 7]


@pytest.mark.parametrize("sequences", [(0, 0), (1, 0), (0, 2, 1), (3, 3, 4)])
def test_run_attempt_rejects_duplicate_or_decreasing_sequences(
    run_attempt_payload: PayloadFactory,
    sequences: tuple[int, ...],
) -> None:
    with pytest.raises(ValidationError, match="strictly increasing"):
        RunAttempt.model_validate(run_attempt_payload(sequences))


def test_run_attempt_rejects_negative_sequences(
    run_attempt_payload: PayloadFactory,
) -> None:
    with pytest.raises(ValidationError):
        RunAttempt.model_validate(run_attempt_payload((-1,)))


def test_run_attempt_requires_at_least_one_event(
    run_attempt_payload: PayloadFactory,
) -> None:
    with pytest.raises(ValidationError):
        RunAttempt.model_validate(run_attempt_payload(()))


def test_snapshot_report_is_limited_to_snapshot_applicability(
    report_payload: PayloadFactory,
) -> None:
    report = ReportRecord.model_validate(
        report_payload(
            claim_scope="snapshot",
            applicability="snapshot_only",
            validation_basis=None,
        )
    )

    assert report.claim_scope is ClaimScope.SNAPSHOT
    assert report.applicability is ReportApplicability.SNAPSHOT_ONLY
    assert report.validation_basis is None


@pytest.mark.parametrize(
    "applicability",
    ["current", "impact_pending", "stale", "superseded"],
)
def test_snapshot_report_rejects_baseline_applicability(
    report_payload: PayloadFactory,
    applicability: str,
) -> None:
    with pytest.raises(ValidationError, match="snapshot_only"):
        ReportRecord.model_validate(
            report_payload(
                claim_scope="snapshot",
                applicability=applicability,
                validation_basis=None,
            )
        )


def test_baseline_report_requires_a_validation_basis(
    report_payload: PayloadFactory,
) -> None:
    with pytest.raises(ValidationError, match="requires validation_basis"):
        ReportRecord.model_validate(report_payload(validation_basis=None))


def test_baseline_report_records_the_assessed_main_tree(
    report_payload: PayloadFactory,
) -> None:
    report = ReportRecord.model_validate(report_payload())

    assert report.claim_scope is ClaimScope.BASELINE
    assert report.applicability is ReportApplicability.CURRENT
    assert report.validation_basis is not None
    assert report.validation_basis.main_tree == "c" * 40


_AGENT_DENIED_PATHS = (
    ".research/decisions/**",
    ".research/policies/**",
    ".research/project.yaml",
    ".research/impacts/**",
    ".research/reports/**",
    ".research/tasks/**",
)


def _project_policy_payload(
    execution_domains: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "agent": {"accepted_paths_denied": list(_AGENT_DENIED_PATHS)},
        "execution_domains": execution_domains,
    }


def _artifact_payload(name: str, size_bytes: int) -> dict[str, Any]:
    return {
        "name": name,
        "uri": f"file:///review/{name}",
        "digest": "sha256:" + "a" * 64,
        "size_bytes": size_bytes,
        "media_type": "application/octet-stream",
    }


def test_execution_domain_policy_maps_a_domain_to_host_pools_without_team() -> None:
    policy = ProjectPolicy.model_validate(
        _project_policy_payload(
            [
                {
                    "execution_domain": "on-prem",
                    "host_pools": ["interactive", "batch"],
                }
            ]
        )
    )

    assert policy.execution_domains == (
        ExecutionDomainPolicy(
            execution_domain="on-prem",
            host_pools=("interactive", "batch"),
        ),
    )

    payload = _project_policy_payload(
        [{"execution_domain": "on-prem", "host_pools": ["interactive"]}]
    )
    payload["execution_domains"][0]["team"] = "School"
    with pytest.raises(ValidationError):
        ProjectPolicy.model_validate(payload)


@pytest.mark.parametrize(
    "execution_domains",
    [
        [
            {"execution_domain": "on-prem", "host_pools": ["batch", "batch"]},
        ],
        [
            {"execution_domain": "on-prem", "host_pools": ["batch"]},
            {"execution_domain": "on-prem", "host_pools": ["interactive"]},
        ],
        [
            {"execution_domain": "on-prem", "host_pools": []},
        ],
    ],
)
def test_execution_domain_policy_rejects_ambiguous_or_empty_mappings(
    execution_domains: list[dict[str, Any]],
) -> None:
    with pytest.raises(ValidationError):
        ProjectPolicy.model_validate(_project_policy_payload(execution_domains))


def _github_governance_payload() -> dict[str, Any]:
    return {
        "repository": "yukiumi13/ResearchAgentOps",
        "default_branch": "main",
        "agent_app": {
            "app_id": 12345,
            "installation_id": 67890,
            "login": "researchctl-agent[bot]",
        },
        "managers": [{"kind": "user", "login": "yukiumi13"}],
        "required_status_checks": [
            "researchctl/exact-head",
            "researchctl/source-tests",
        ],
        "required_approvals": 1,
        "require_code_owner_review": True,
        "dismiss_stale_reviews": True,
        "require_last_push_approval": True,
        "strict_status_checks": True,
        "block_force_pushes": True,
        "block_deletions": True,
        "bypass_actors": [],
    }


def test_project_policy_binds_distinct_github_agent_and_manager_principals() -> None:
    payload = _project_policy_payload([])
    payload["github"] = _github_governance_payload()

    policy = ProjectPolicy.model_validate(payload)

    assert policy.github is not None
    assert policy.github.agent_app.login == "researchctl-agent[bot]"
    assert policy.github.managers[0].kind == "user"
    assert policy.github.required_status_checks == (
        "researchctl/exact-head",
        "researchctl/source-tests",
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("require_code_owner_review", False),
        ("dismiss_stale_reviews", False),
        ("require_last_push_approval", False),
        ("strict_status_checks", False),
        ("block_force_pushes", False),
        ("block_deletions", False),
        ("required_approvals", 0),
        ("required_status_checks", ["researchctl/source-tests"]),
    ],
)
def test_github_governance_policy_cannot_weaken_fixed_merge_gates(
    field: str,
    value: object,
) -> None:
    github = _github_governance_payload()
    github[field] = value
    payload = _project_policy_payload([])
    payload["github"] = github

    with pytest.raises(ValidationError):
        ProjectPolicy.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("default_branch", "../main"),
        ("managers", []),
        ("managers", [{"kind": "user", "login": "another[bot]"}]),
        (
            "managers",
            [
                {
                    "kind": "team",
                    "organization": "another[bot]",
                    "slug": "research-managers",
                }
            ],
        ),
        ("agent_app", {"app_id": 1, "installation_id": 2, "login": "human"}),
    ],
)
def test_github_governance_policy_rejects_invalid_identity_or_branch(
    field: str,
    value: object,
) -> None:
    github = _github_governance_payload()
    github[field] = value
    payload = _project_policy_payload([])
    payload["github"] = github

    with pytest.raises(ValidationError):
        ProjectPolicy.model_validate(payload)


def test_task_records_domain_scope_deliverables_and_parent_metadata(
    task_payload: PayloadFactory,
) -> None:
    parent_id = "task_20260802T123456Z_" + "b" * 24
    task = TaskRecord.model_validate(
        task_payload(parent_task_id=parent_id, milestone="M2")
    )

    assert task.execution_domain == "on-prem"
    assert task.deliverables == ("A comparison report with uncertainty.",)
    assert task.parent_task_id == parent_id
    assert task.milestone == "M2"


def test_task_write_scope_uses_component_aware_path_prefixes(
    task_payload: PayloadFactory,
) -> None:
    task = TaskRecord.model_validate(
        task_payload(allowed_write_paths=["src", ".research-old"])
    )

    assert task.permits_write_path("src")
    assert task.permits_write_path("src/training/stop.py")
    assert task.permits_write_path(".research-old/output.json")
    assert not task.permits_write_path("src2/training.py")
    assert not task.permits_write_path("results/output.json")


def test_task_write_scope_always_denies_research_state(
    task_payload: PayloadFactory,
) -> None:
    task = TaskRecord.model_validate(task_payload(allowed_write_paths=["."]))

    assert task.permits_write_path("src/training.py")
    assert not task.permits_write_path(".research")
    assert not task.permits_write_path(".research/tasks/task.yaml")


@pytest.mark.parametrize(
    "prefix",
    [
        ".research",
        ".research/tasks",
        "../src",
        "/src",
        "src\\training",
        "src/**",
        "src/[ab]",
        "src/file?.py",
    ],
)
def test_task_rejects_protected_invalid_or_glob_write_prefixes(
    task_payload: PayloadFactory,
    prefix: str,
) -> None:
    with pytest.raises(ValidationError):
        TaskRecord.model_validate(task_payload(allowed_write_paths=[prefix]))


def test_task_rejects_duplicate_write_prefixes_and_empty_deliverables(
    task_payload: PayloadFactory,
) -> None:
    with pytest.raises(ValidationError, match="must be unique"):
        TaskRecord.model_validate(
            task_payload(allowed_write_paths=["src", "src"])
        )

    with pytest.raises(ValidationError):
        TaskRecord.model_validate(task_payload(deliverables=[]))


def test_task_cannot_be_its_own_parent(task_payload: PayloadFactory) -> None:
    payload = task_payload()
    payload["parent_task_id"] = payload["task_id"]

    with pytest.raises(ValidationError, match="own parent"):
        TaskRecord.model_validate(payload)


def test_run_attempt_requires_each_event_to_use_the_parent_operation(
    run_attempt_payload: PayloadFactory,
) -> None:
    payload = run_attempt_payload((0, 1))
    payload["events"][1]["operation_id"] = (
        "operation_20260802T123456Z_" + "e" * 24
    )

    with pytest.raises(ValidationError, match="match its parent attempt"):
        RunAttempt.model_validate(payload)


def test_run_spec_accepts_its_canonical_content_digest(
    run_spec_payload: PayloadFactory,
) -> None:
    spec = RunSpec.model_validate(run_spec_payload())

    assert spec.spec_digest.startswith("sha256:")
    assert len(spec.spec_digest) == len("sha256:") + 64


def test_run_spec_rejects_a_digest_from_different_content(
    run_spec_payload: PayloadFactory,
) -> None:
    payload = run_spec_payload()
    payload["argv"].append("--changed-after-digest")

    with pytest.raises(ValidationError, match="does not match canonical"):
        RunSpec.model_validate(payload)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {
                "inputs": [
                    {
                        "kind": "environment",
                        "logical_id": "trainer-cu128",
                        "version": "duplicate-declaration",
                    }
                ]
            },
            "run identity keys must be unique",
        ),
        (
            {
                "artifact_declarations": [
                    {
                        "name": "metrics",
                        "path": "results/first.json",
                        "media_type": "application/json",
                    },
                    {
                        "name": "metrics",
                        "path": "results/second.json",
                        "media_type": "application/json",
                    },
                ]
            },
            "run artifact names must be unique",
        ),
        (
            {
                "artifact_declarations": [
                    {
                        "name": "metrics",
                        "path": "results/shared.json",
                        "media_type": "application/json",
                    },
                    {
                        "name": "summary",
                        "path": "results/shared.json",
                        "media_type": "application/json",
                    },
                ]
            },
            "run artifact paths must be unique",
        ),
    ],
)
def test_run_spec_rejects_duplicate_identity_and_artifact_keys(
    run_spec_payload: PayloadFactory,
    overrides: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        RunSpec.model_validate(run_spec_payload(**overrides))


@pytest.mark.parametrize(
    "overrides",
    [
        {"started_at": None},
        {"exit_code": None},
        {"exit_code": 1},
    ],
)
def test_complete_run_result_requires_start_and_zero_exit(
    run_result_payload: PayloadFactory,
    overrides: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        RunResult.model_validate(run_result_payload(**overrides))


def test_run_result_rejects_duplicate_attempts_and_reversed_time(
    run_result_payload: PayloadFactory,
) -> None:
    payload = run_result_payload()
    payload["attempt_ids"] = payload["attempt_ids"] * 2
    with pytest.raises(ValidationError, match="attempt_ids must be unique"):
        RunResult.model_validate(payload)

    with pytest.raises(ValidationError, match="cannot precede"):
        RunResult.model_validate(
            run_result_payload(finished_at="2026-08-02T12:34:55Z")
        )


def test_non_complete_run_result_does_not_invent_complete_fields(
    run_result_payload: PayloadFactory,
) -> None:
    result = RunResult.model_validate(
        run_result_payload(outcome="partial", started_at=None, exit_code=None)
    )

    assert result.started_at is None
    assert result.exit_code is None


def test_review_bundle_accepts_an_aggregate_of_exactly_ten_mib(
    submission_payload: PayloadFactory,
) -> None:
    six_mib = 6 * 1024 * 1024
    four_mib = 4 * 1024 * 1024
    submission = ResearchSubmission.model_validate(
        submission_payload(
            review_bundle=[
                _artifact_payload("metrics", six_mib),
                _artifact_payload("plots", four_mib),
            ]
        )
    )

    assert sum(item.size_bytes for item in submission.review_bundle) == 10 * 1024 * 1024


def test_review_bundle_rejects_an_aggregate_above_ten_mib(
    submission_payload: PayloadFactory,
) -> None:
    with pytest.raises(ValidationError, match="exceeds 10 MiB"):
        ResearchSubmission.model_validate(
            submission_payload(
                review_bundle=[
                    _artifact_payload("metrics", 6 * 1024 * 1024),
                    _artifact_payload("plots", 4 * 1024 * 1024 + 1),
                ]
            )
        )


def test_review_decision_acceptance_dispositions_enforce_conditions(
    review_decision_payload: PayloadFactory,
) -> None:
    accepted = ReviewDecision.model_validate(review_decision_payload())
    conditional = ReviewDecision.model_validate(
        review_decision_payload(
            disposition="accepted_with_conditions",
            conditions=["Rerun after the next baseline update."],
        )
    )

    assert accepted.disposition is ReviewDisposition.ACCEPTED
    assert conditional.disposition is ReviewDisposition.ACCEPTED_WITH_CONDITIONS

    with pytest.raises(ValidationError, match="requires conditions"):
        ReviewDecision.model_validate(
            review_decision_payload(disposition="accepted_with_conditions")
        )

    with pytest.raises(ValidationError, match="cannot include conditions"):
        ReviewDecision.model_validate(
            review_decision_payload(conditions=["Unexpected condition."])
        )


@pytest.mark.parametrize("disposition", ["rejected", "changes_requested"])
def test_review_decision_rejects_non_acceptance_dispositions(
    review_decision_payload: PayloadFactory,
    disposition: str,
) -> None:
    with pytest.raises(ValidationError):
        ReviewDecision.model_validate(
            review_decision_payload(disposition=disposition)
        )
