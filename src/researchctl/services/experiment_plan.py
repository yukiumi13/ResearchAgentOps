from __future__ import annotations

from dataclasses import dataclass

from researchctl.domain.enums import (
    PlanFindingKind,
    PlanReviewOutcome,
    PlanValueSourceKind,
)
from researchctl.domain.models import (
    ExperimentPlan,
    PlanReview,
    PlanReviewFinding,
    ProjectPolicy,
    RunSpec,
    TaskRecord,
)
from researchctl.errors import RCPError
from researchctl.serialization import canonical_digest, canonical_json_bytes
from researchctl.services.run_preflight import validate_task_required_input_identities


@dataclass(frozen=True, slots=True)
class PlanLintResult:
    plan_id: str
    plan_digest: str
    task_digest: str
    policy_digest: str
    outcome: PlanReviewOutcome
    findings: tuple[PlanReviewFinding, ...]

    @property
    def terminal_result(self) -> str:
        return self.outcome.value

    def as_dict(self) -> dict[str, object]:
        return {
            "terminal_result": self.terminal_result,
            "plan_id": self.plan_id,
            "plan_digest": self.plan_digest,
            "task_digest": self.task_digest,
            "policy_digest": self.policy_digest,
            "findings": [
                item.model_dump(mode="json", exclude_none=True)
                for item in self.findings
            ],
        }


def _finding(
    kind: PlanFindingKind,
    code: str,
    message: str,
    *,
    field_path: str | None = None,
) -> PlanReviewFinding:
    return PlanReviewFinding(
        kind=kind,
        code=code,
        field_path=field_path,
        message=message,
    )


def _same_json(left: object, right: object) -> bool:
    return canonical_json_bytes({"value": left}) == canonical_json_bytes(
        {"value": right}
    )


def lint_experiment_plan(
    plan: ExperimentPlan,
    task: TaskRecord,
    policy: ProjectPolicy,
) -> PlanLintResult:
    """Validate intent provenance without creating a Run or invoking a reviewer."""

    findings: list[PlanReviewFinding] = []
    task_digest = canonical_digest(task)
    policy_digest = canonical_digest(policy)
    if plan.task_id != task.task_id:
        findings.append(
            _finding(
                PlanFindingKind.INVALID,
                "plan_task_mismatch",
                "ExperimentPlan belongs to another accepted Task.",
                field_path="task_id",
            )
        )
    if plan.task_digest != task_digest:
        findings.append(
            _finding(
                PlanFindingKind.INVALID,
                "plan_task_digest_stale",
                "ExperimentPlan does not bind the current accepted Task bytes.",
                field_path="task_digest",
            )
        )
    if plan.policy_digest != policy_digest:
        findings.append(
            _finding(
                PlanFindingKind.INVALID,
                "plan_policy_digest_stale",
                "ExperimentPlan does not bind the current accepted Project policy.",
                field_path="policy_digest",
            )
        )

    plan_values = plan.model_dump(
        mode="json",
        exclude={"plan_digest", "value_sources"},
        exclude_none=True,
    )
    for source in plan.value_sources:
        if source.source_kind is PlanValueSourceKind.ACCEPTED_TASK:
            choices = task.plan_choices
            expected_digest = task_digest
        else:
            choices = policy.plan_choices
            expected_digest = policy_digest
        if source.source_digest != expected_digest:
            findings.append(
                _finding(
                    PlanFindingKind.INVALID,
                    "plan_source_digest_stale",
                    "Plan value source does not bind the referenced accepted record.",
                    field_path=source.field_path,
                )
            )
            continue
        if source.field_path not in choices:
            findings.append(
                _finding(
                    PlanFindingKind.NEEDS_INPUT,
                    "plan_choice_missing",
                    "No accepted Task or Project-policy choice supplies this value.",
                    field_path=source.field_path,
                )
            )
            continue
        plan_value = (
            None
            if getattr(plan, source.field_path) is None
            else plan_values[source.field_path]
        )
        if not _same_json(plan_value, choices[source.field_path]):
            findings.append(
                _finding(
                    PlanFindingKind.INVALID,
                    "plan_choice_mismatch",
                    "Plan value differs from its accepted Task or policy source.",
                    field_path=source.field_path,
                )
            )

    try:
        identities = [plan.environment]
        if plan.config is not None:
            identities.append(plan.config)
        identities.extend(plan.inputs)
        validate_task_required_input_identities(
            tuple(identities),
            task,
        )
    except RCPError as error:
        findings.append(
            _finding(
                PlanFindingKind.INVALID,
                error.code,
                error.message,
                field_path="inputs",
            )
        )

    ordered = tuple(
        sorted(
            findings,
            key=lambda item: (
                item.field_path or "",
                item.code,
                item.kind.value,
                item.message,
            ),
        )
    )
    kinds = {item.kind for item in ordered}
    if PlanFindingKind.INVALID in kinds:
        outcome = PlanReviewOutcome.INVALID
    elif PlanFindingKind.NEEDS_INPUT in kinds:
        outcome = PlanReviewOutcome.NEEDS_INPUT
    else:
        outcome = PlanReviewOutcome.PASSED
    return PlanLintResult(
        plan_id=plan.plan_id,
        plan_digest=plan.plan_digest,
        task_digest=task_digest,
        policy_digest=policy_digest,
        outcome=outcome,
        findings=ordered,
    )


def require_passing_plan_review(
    plan: ExperimentPlan,
    review: PlanReview,
    task: TaskRecord,
    policy: ProjectPolicy,
) -> PlanLintResult:
    lint = lint_experiment_plan(plan, task, policy)
    if lint.outcome is not PlanReviewOutcome.PASSED:
        raise RCPError(
            code=f"plan_{lint.outcome.value}",
            message="ExperimentPlan cannot compile until deterministic lint passes.",
            context=lint.as_dict(),
        )
    expected = (
        plan.plan_id,
        plan.plan_digest,
        task.task_id,
        lint.task_digest,
        lint.policy_digest,
        plan.draft_invocation_id,
    )
    observed = (
        review.plan_id,
        review.plan_digest,
        review.task_id,
        review.task_digest,
        review.policy_digest,
        review.drafter_invocation_id,
    )
    if observed != expected:
        raise RCPError(
            code="plan_review_binding_invalid",
            message="PlanReview does not bind the exact Plan, Task, and Project policy.",
        )
    if review.outcome is not PlanReviewOutcome.PASSED:
        raise RCPError(
            code=f"plan_review_{review.outcome.value}",
            message="Independent PlanReview did not pass.",
            context={
                "review_id": review.review_id,
                "findings": [
                    item.model_dump(mode="json", exclude_none=True)
                    for item in review.findings
                ],
            },
        )
    return lint


def compile_experiment_plan(
    plan: ExperimentPlan,
    review: PlanReview,
    task: TaskRecord,
    policy: ProjectPolicy,
) -> RunSpec:
    require_passing_plan_review(plan, review, task, policy)
    content = {
        "schema_version": plan.schema_version,
        "run_id": plan.run_id,
        "task_id": plan.task_id,
        "session_id": plan.session_id,
        "operation_id": plan.operation_id,
        "source_commit": plan.source_commit,
        "source_tree": plan.source_tree,
        "baseline_commit": plan.baseline_commit,
        "argv": plan.argv,
        "working_directory": plan.working_directory,
        "environment": plan.environment,
        "config": plan.config,
        "inputs": plan.inputs,
        "resources": plan.resources,
        "requested_host": plan.requested_host,
        "artifact_declarations": plan.artifact_declarations,
        "experiment_plan": plan,
        "plan_review": review,
        "created_at": plan.created_at,
    }
    digest_content = RunSpec.model_construct(**content).model_dump(
        mode="json",
        exclude_none=True,
    )
    return RunSpec(**content, spec_digest=canonical_digest(digest_content))
