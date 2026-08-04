from __future__ import annotations

from datetime import datetime

from researchctl.adapters.plan_reviewer import EphemeralPlanReviewer
from researchctl.domain.enums import PlanReviewOutcome
from researchctl.domain.models import (
    ExperimentPlan,
    PlanReview,
    ProjectPolicy,
    TaskRecord,
)
from researchctl.errors import RCPError
from researchctl.serialization import canonical_digest
from researchctl.services.experiment_plan import lint_experiment_plan


class IndependentPlanReviewService:
    def __init__(self, reviewer: EphemeralPlanReviewer) -> None:
        self.reviewer = reviewer

    def review(
        self,
        *,
        plan: ExperimentPlan,
        task: TaskRecord,
        policy: ProjectPolicy,
        review_id: str,
        operation_id: str,
        completed_at: datetime,
    ) -> PlanReview:
        lint = lint_experiment_plan(plan, task, policy)
        if lint.outcome is not PlanReviewOutcome.PASSED:
            raise RCPError(
                code=f"plan_{lint.outcome.value}",
                message="Independent review cannot run until deterministic Plan lint passes.",
                context=lint.as_dict(),
            )
        review_policy = policy.plan_review
        if review_policy is None:
            raise RCPError(
                code="plan_reviewer_not_configured",
                message="Accepted Project policy does not configure PlanReview.",
            )
        observed = self.reviewer.review(plan=plan, task=task, policy=policy)
        if (
            observed.provider != review_policy.provider
            or observed.model != review_policy.model
        ):
            raise RCPError(
                code="plan_reviewer_identity_mismatch",
                message="Reviewer observation differs from accepted Project policy.",
            )
        if observed.invocation_id == plan.draft_invocation_id:
            raise RCPError(
                code="plan_reviewer_not_independent",
                message="Plan drafter and independent reviewer used the same invocation.",
            )
        findings = observed.opinion.findings
        receipt_digest = PlanReview.calculate_completion_receipt(
            review_operation_id=operation_id,
            plan_digest=plan.plan_digest,
            task_digest=lint.task_digest,
            policy_digest=lint.policy_digest,
            review_policy_version=review_policy.policy_version,
            reviewer_invocation_id=observed.invocation_id,
            reviewer_provider=observed.provider,
            reviewer_model=observed.model,
            outcome=observed.opinion.outcome.value,
            findings=findings,
            reviewer_output_digest=observed.output_digest,
        )
        content = {
            "schema_version": plan.schema_version,
            "review_id": review_id,
            "review_operation_id": operation_id,
            "plan_id": plan.plan_id,
            "plan_digest": plan.plan_digest,
            "task_id": task.task_id,
            "task_digest": lint.task_digest,
            "policy_digest": lint.policy_digest,
            "review_policy_version": review_policy.policy_version,
            "drafter_invocation_id": plan.draft_invocation_id,
            "reviewer_invocation_id": observed.invocation_id,
            "reviewer_provider": observed.provider,
            "reviewer_model": observed.model,
            "outcome": observed.opinion.outcome,
            "findings": findings,
            "reviewer_output_digest": observed.output_digest,
            "completed_at": completed_at,
            "completion_receipt_digest": receipt_digest,
        }
        digest = canonical_digest(
            PlanReview.model_construct(**content).model_dump(
                mode="json",
                exclude_none=True,
            )
        )
        return PlanReview(**content, review_digest=digest)
