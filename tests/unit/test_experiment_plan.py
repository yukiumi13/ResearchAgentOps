from __future__ import annotations

from datetime import UTC, datetime
import json
import subprocess

import pytest
from pydantic import ValidationError

from researchctl.adapters.plan_reviewer import (
    EphemeralPlanReviewer,
    PlanReviewerObservation,
    PlanReviewOpinion,
)
from researchctl.domain.enums import PlanReviewOutcome, SessionState
from researchctl.domain.models import (
    AgentPolicy,
    ArtifactDeclaration,
    ExperimentPlan,
    ExecutionDomainPolicy,
    InputIdentity,
    PlanMetric,
    PlanReview,
    PlanReviewPolicy,
    PlanValueSource,
    ProjectPolicy,
    ResearchSubmission,
    ResourceRequirement,
    RunResult,
    TaskRecord,
)
from researchctl.errors import RCPError
from researchctl.runtime import RuntimeSession, RuntimeStore
from researchctl.serialization import canonical_digest
from researchctl.services.actor import ActorContext, ActorRole, CredentialKind
from researchctl.services.application import ApplicationService
from researchctl.services.experiment_plan import (
    compile_experiment_plan,
    lint_experiment_plan,
)
from researchctl.services.plan_review import IndependentPlanReviewService
from researchctl.services.requests import (
    PlanCompileRequest,
    PlanReviewCreateRequest,
    RunStartRequest,
)
from researchctl.services.submissions import (
    SubmissionBundleBuilder,
    SubmissionEvidence,
)
from researchctl.services.task_records import TaskRecordRepository


DENIED_PATHS = (
    ".research/decisions/**",
    ".research/policies/**",
    ".research/project.yaml",
    ".research/impacts/**",
    ".research/reports/**",
    ".research/tasks/**",
)
NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)


def _id(kind: str, fill: str) -> str:
    return f"{kind}_20260804T120000Z_{fill * 24}"


def _semantic_values() -> dict[str, object]:
    return {
        "argv": ["python", "train.py", "--config", "candidate"],
        "artifact_declarations": [
            {
                "name": "metrics",
                "path": "results/MAR-17/metrics.json",
                "media_type": "application/json",
                "required": True,
            }
        ],
        "comparison": "Candidate stopping policy versus the frozen baseline.",
        "config": None,
        "environment": {
            "kind": "environment",
            "logical_id": "trainer-cu128",
            "digest": "sha256:" + "3" * 64,
            "waiver_allowed": False,
        },
        "failure_conditions": ["Process exits non-zero or required artifact is absent."],
        "hypothesis": "The candidate reduces validation loss.",
        "inputs": [
            {
                "kind": "dataset",
                "logical_id": "validation-split",
                "version": "2026-08-01",
                "waiver_allowed": False,
            }
        ],
        "metrics": [{"name": "validation_loss", "direction": "minimize"}],
        "repetitions": 1,
        "requested_host": "host-a",
        "resources": {
            "gpu_count": 1,
            "gpu_type": "H100",
            "min_gpu_memory_gb": 70,
            "preferred_hosts": ["host-a"],
            "preferred_pools": ["interactive"],
        },
        "seeds": [],
        "stop_conditions": ["Stop after the declared evaluation completes."],
        "working_directory": ".",
    }


def _records(
    task_payload,
    *,
    omit_choice: str | None = None,
) -> tuple[TaskRecord, ProjectPolicy, ExperimentPlan]:
    choices = _semantic_values()
    task_choices = {key: value for key, value in choices.items() if key != omit_choice}
    task = TaskRecord.model_validate(task_payload(plan_choices=task_choices))
    policy = ProjectPolicy(
        agent=AgentPolicy(accepted_paths_denied=DENIED_PATHS),
        plan_review=PlanReviewPolicy(
            provider="codex",
            model="gpt-test-reviewer",
            policy_version="plan-review-v1",
            timeout_seconds=60,
        ),
        execution_domains=(
            ExecutionDomainPolicy(
                execution_domain="on-prem",
                host_pools=("interactive",),
            ),
        ),
    )
    task_digest = canonical_digest(task)
    policy_digest = canonical_digest(policy)
    typed_choices = {
        **choices,
        "argv": tuple(choices["argv"]),
        "artifact_declarations": tuple(
            ArtifactDeclaration.model_validate(item)
            for item in choices["artifact_declarations"]
        ),
        "environment": InputIdentity.model_validate(choices["environment"]),
        "inputs": tuple(
            InputIdentity.model_validate(item) for item in choices["inputs"]
        ),
        "metrics": tuple(PlanMetric.model_validate(item) for item in choices["metrics"]),
        "resources": ResourceRequirement.model_validate(choices["resources"]),
        "seeds": tuple(choices["seeds"]),
        "stop_conditions": tuple(choices["stop_conditions"]),
        "failure_conditions": tuple(choices["failure_conditions"]),
    }
    content = {
        "schema_version": "0.1",
        "plan_id": _id("plan", "1"),
        "task_id": task.task_id,
        "session_id": _id("session", "2"),
        "draft_invocation_id": "11111111-1111-4111-8111-111111111111",
        "task_digest": task_digest,
        "policy_digest": policy_digest,
        "run_id": _id("run", "3"),
        "operation_id": _id("operation", "4"),
        "source_commit": "1" * 40,
        "source_tree": "2" * 40,
        "baseline_commit": "0" * 40,
        **typed_choices,
        "value_sources": tuple(
            PlanValueSource(
                field_path=field,
                source_kind="accepted_task",
                source_digest=task_digest,
            )
            for field in sorted(choices)
        ),
        "created_at": NOW,
    }
    digest_content = ExperimentPlan.model_construct(**content).model_dump(
        mode="json",
        exclude={"plan_digest"},
        exclude_none=False,
    )
    plan = ExperimentPlan.model_validate(
        {**content, "plan_digest": canonical_digest(digest_content)}
    )
    return task, policy, plan


class _Reviewer:
    def __init__(self, invocation_id: str | None = None) -> None:
        self.invocation_id = invocation_id or "22222222-2222-4222-8222-222222222222"

    def review(self, **_kwargs) -> PlanReviewerObservation:
        return PlanReviewerObservation(
            provider="codex",
            model="gpt-test-reviewer",
            invocation_id=self.invocation_id,
            opinion=PlanReviewOpinion(outcome="passed"),
            output_digest="sha256:" + "9" * 64,
        )


class _Runner:
    def __init__(self, output: str) -> None:
        self.output = output
        self.calls = []

    def run(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, self.output, "")


class _RunExecution:
    terminal_result = "collected"

    def as_dict(self) -> dict[str, object]:
        return {"terminal_result": self.terminal_result}


class _RunCoordinator:
    def __init__(self) -> None:
        self.calls = []

    def execute(self, **kwargs):
        self.calls.append(kwargs)
        return _RunExecution()


def _review(
    task: TaskRecord,
    policy: ProjectPolicy,
    plan: ExperimentPlan,
    *,
    invocation_id: str | None = None,
) -> PlanReview:
    return IndependentPlanReviewService(_Reviewer(invocation_id)).review(
        plan=plan,
        task=task,
        policy=policy,
        review_id=_id("plan_review", "5"),
        operation_id=_id("operation", "6"),
        completed_at=NOW,
    )


def _application(
    root,
    *,
    task: TaskRecord,
    policy: ProjectPolicy,
    plan: ExperimentPlan,
    review_workflow=None,
) -> tuple[ApplicationService, RuntimeStore, ActorContext]:
    (root / ".research" / "tasks").mkdir(parents=True)
    tasks = TaskRecordRepository(root)
    tasks.create(task)
    runtime = RuntimeStore(root / "runtime.sqlite3")
    project_id = _id("project", "8")
    runtime.save_session(
        RuntimeSession(
            session_id=plan.session_id,
            project_id=project_id,
            task_id=task.task_id,
            state=SessionState.ACTIVE,
            created_at=NOW,
            updated_at=NOW,
            metadata={"native_session_id": plan.draft_invocation_id},
        )
    )
    service = ApplicationService(
        project_id=project_id,
        policy=policy,
        tasks=tasks,
        runtime=runtime,
        plan_review_workflow=review_workflow,
        clock=lambda: NOW,
    )
    actor = ActorContext(
        actor_id="agent-plan-drafter",
        role=ActorRole.AGENT,
        credential_kind=CredentialKind.SESSION_CAPABILITY,
        bound_session_id=plan.session_id,
    )
    return service, runtime, actor


def test_plan_lint_review_and_compile_are_digest_bound_and_deterministic(
    task_payload,
) -> None:
    task, policy, plan = _records(task_payload)

    lint = lint_experiment_plan(plan, task, policy)
    review = _review(task, policy, plan)
    first = compile_experiment_plan(plan, review, task, policy)
    repeated = compile_experiment_plan(plan, review, task, policy)

    assert lint.outcome is PlanReviewOutcome.PASSED
    assert lint.findings == ()
    assert review.plan_digest == plan.plan_digest
    assert review.task_digest == canonical_digest(task)
    assert review.policy_digest == canonical_digest(policy)
    assert review.reviewer_invocation_id != plan.draft_invocation_id
    assert first == repeated
    assert first.experiment_plan == plan
    assert first.plan_review == review
    assert first.argv == plan.argv
    assert first.spec_digest == canonical_digest(
        first.model_dump(mode="json", exclude={"spec_digest"}, exclude_none=True)
    )


def test_plan_lint_returns_needs_input_for_unaccepted_choice(task_payload) -> None:
    task, policy, plan = _records(task_payload, omit_choice="requested_host")

    lint = lint_experiment_plan(plan, task, policy)

    assert lint.outcome is PlanReviewOutcome.NEEDS_INPUT
    assert [(item.code, item.field_path) for item in lint.findings] == [
        ("plan_choice_missing", "requested_host")
    ]


def test_plan_rejects_unknown_fields_and_self_review(task_payload) -> None:
    task, policy, plan = _records(task_payload)
    payload = plan.model_dump(mode="json", exclude_none=False)
    payload["provider_default"] = True
    with pytest.raises(ValidationError):
        ExperimentPlan.model_validate(payload)

    with pytest.raises(RCPError, match="same invocation") as caught:
        _review(
            task,
            policy,
            plan,
            invocation_id=plan.draft_invocation_id,
        )
    assert caught.value.code == "plan_reviewer_not_independent"


@pytest.mark.parametrize(
    ("repetitions", "seeds", "valid"),
    (
        (1, [], True),
        (None, [1], True),
        (None, [], False),
        (1, [1], False),
    ),
)
def test_plan_requires_exactly_one_repetition_strategy(
    task_payload,
    repetitions,
    seeds,
    valid,
) -> None:
    _task, _policy, plan = _records(task_payload)
    payload = plan.model_dump(mode="json", exclude_none=False)
    payload.update(repetitions=repetitions, seeds=seeds)
    digest_content = {key: value for key, value in payload.items() if key != "plan_digest"}
    payload["plan_digest"] = canonical_digest(digest_content)

    if valid:
        ExperimentPlan.model_validate(payload)
    else:
        with pytest.raises(ValidationError, match="exactly one"):
            ExperimentPlan.model_validate(payload)


def test_plan_backed_submission_contains_separate_review_records(
    task_payload,
    run_result_payload,
    submission_payload,
) -> None:
    task, policy, plan = _records(task_payload)
    review = _review(task, policy, plan)
    spec = compile_experiment_plan(plan, review, task, policy)
    result = RunResult.model_validate(
        run_result_payload(
            run_id=spec.run_id,
            run_spec_digest=spec.spec_digest,
        )
    )
    submission = ResearchSubmission.model_validate(
        submission_payload(
            task_id=task.task_id,
            session_id=plan.session_id,
            state="open",
            run_result_ids=[result.result_id],
            metrics={"validation_loss": 0.42},
        )
    )
    proposal_payload = {
        "schema_version": "0.1",
        "submission_id": submission.submission_id,
        "report_id": _id("report", "7"),
        "expected_report_revision": 0,
        "title": "Reviewed plan result",
        "evidence_tree": spec.source_tree,
    }
    from researchctl.domain.models import ReportProposal

    bundle = SubmissionBundleBuilder().build(
        task=task,
        policy=policy,
        submission=submission,
        proposal=ReportProposal.model_validate(proposal_payload),
        evidence=(SubmissionEvidence(spec, result),),
    )

    root = f".research/submissions/{submission.submission_id}/evidence/{spec.run_id}"
    paths = {item.path for item in bundle.files}
    assert f"{root}/plan.yaml" in paths
    assert f"{root}/plan-review.yaml" in paths

    missing_metric = submission.model_copy(update={"metrics": {}})
    with pytest.raises(RCPError) as caught:
        SubmissionBundleBuilder().build(
            task=task,
            policy=policy,
            submission=missing_metric,
            proposal=ReportProposal.model_validate(proposal_payload),
            evidence=(SubmissionEvidence(spec, result),),
        )
    assert caught.value.code == "submission_plan_metrics_missing"


def test_run_start_requires_the_exact_local_plan_review_operation_receipt(
    tmp_path,
    task_payload,
) -> None:
    task, policy, plan = _records(task_payload)
    review = _review(task, policy, plan)
    spec = compile_experiment_plan(plan, review, task, policy)
    service, runtime, actor = _application(
        tmp_path / "missing",
        task=task,
        policy=policy,
        plan=plan,
    )
    coordinator = _RunCoordinator()
    service.runs = coordinator
    request = RunStartRequest(
        operation_id=spec.operation_id,
        idempotency_key="plan-run-without-review-receipt",
        spec=spec,
        attempt_id=_id("attempt", "9"),
    )

    with pytest.raises(RCPError) as missing:
        service.run_start(request, actor)

    assert missing.value.code == "plan_review_receipt_missing"
    assert coordinator.calls == []
    runtime.close()


def test_journaled_plan_review_compiles_and_authorizes_run_start(
    tmp_path,
    task_payload,
) -> None:
    task, policy, plan = _records(task_payload)
    service, runtime, actor = _application(
        tmp_path / "observed",
        task=task,
        policy=policy,
        plan=plan,
        review_workflow=IndependentPlanReviewService(_Reviewer()),
    )
    review_request = PlanReviewCreateRequest(
        operation_id=_id("operation", "6"),
        idempotency_key="independent-plan-review",
        review_id=_id("plan_review", "5"),
        plan=plan,
    )

    reviewed = service.plan_review(review_request, actor)
    review = PlanReview.model_validate(reviewed.data["review"])
    spec = service.plan_compile(
        PlanCompileRequest(plan=plan, review=review),
        actor,
    )
    coordinator = _RunCoordinator()
    service.runs = coordinator
    started = service.run_start(
        RunStartRequest(
            operation_id=spec.operation_id,
            idempotency_key="reviewed-plan-run",
            spec=spec,
            attempt_id=_id("attempt", "9"),
        ),
        actor,
    )

    assert reviewed.terminal_result == "passed"
    assert started.terminal_result == "collected"
    assert len(coordinator.calls) == 1
    review_operation = runtime.get_operation(review.review_operation_id)
    assert review_operation is not None
    assert review_operation.terminal_result == "passed"
    runtime.close()


def test_codex_reviewer_is_ephemeral_read_only_attributed_and_secretless(
    tmp_path,
    task_payload,
) -> None:
    task, policy, plan = _records(task_payload)
    invocation = "22222222-2222-4222-8222-222222222222"
    opinion = json.dumps({"outcome": "passed", "findings": []})
    output = "\n".join(
        (
            json.dumps({"type": "thread.started", "thread_id": invocation}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": opinion},
                }
            ),
        )
    )
    runner = _Runner(output)
    reviewer = EphemeralPlanReviewer(
        tmp_path,
        runner=runner,
        environment={
            "PATH": "/usr/bin",
            "OPENAI_API_KEY": "reviewer-key",
            "GH_TOKEN": "forbidden",
            "RESEARCHCTL_SESSION_TOKEN": "forbidden",
        },
    )

    observed = reviewer.review(plan=plan, task=task, policy=policy)

    assert observed.invocation_id == invocation
    assert observed.opinion.outcome is PlanReviewOutcome.PASSED
    argv, kwargs = runner.calls[0]
    assert "--ephemeral" in argv
    assert ("--sandbox", "read-only") == (
        argv[argv.index("--sandbox")],
        argv[argv.index("--sandbox") + 1],
    )
    assert argv[argv.index("--model") + 1] == "gpt-test-reviewer"
    assert kwargs["env"] == {
        "PATH": "/usr/bin",
        "OPENAI_API_KEY": "reviewer-key",
    }
    assert "accepted_task" in kwargs["input_text"]


def test_claude_reviewer_is_bare_toolless_nonpersistent_and_attributed(
    tmp_path,
    task_payload,
) -> None:
    task, policy, plan = _records(task_payload)
    policy = policy.model_copy(
        update={
            "plan_review": PlanReviewPolicy(
                provider="claude",
                model="claude-test-reviewer",
                policy_version="plan-review-v1",
                timeout_seconds=60,
            )
        }
    )
    invocation = "33333333-3333-4333-8333-333333333333"
    runner = _Runner(
        json.dumps(
            {
                "session_id": invocation,
                "structured_output": {"outcome": "passed", "findings": []},
            }
        )
    )
    reviewer = EphemeralPlanReviewer(
        tmp_path,
        runner=runner,
        environment={"PATH": "/usr/bin", "ANTHROPIC_API_KEY": "reviewer-key"},
    )

    observed = reviewer.review(plan=plan, task=task, policy=policy)

    assert observed.invocation_id == invocation
    assert observed.opinion.outcome is PlanReviewOutcome.PASSED
    argv, _kwargs = runner.calls[0]
    assert "--bare" in argv
    assert "--disable-slash-commands" in argv
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--permission-mode") + 1] == "plan"
    assert argv[argv.index("--tools") + 1] == ""
    assert "--no-session-persistence" in argv
    assert argv[argv.index("--model") + 1] == "claude-test-reviewer"
