from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from researchctl.domain.enums import (
    ClaimScope,
    CodeDisposition,
    ImpactDisposition,
    ReviewDisposition,
)
from researchctl.domain.ids import new_id
from researchctl.domain.models import (
    DependencySet,
    ReportProposal,
    ResearchSubmission,
)
from researchctl.phase2_cli import (
    _input_error,
    _operation_fields,
    _required,
    _run_command,
)
from researchctl.serialization import load_model
from researchctl.services.requests import (
    ImpactCreateRequest,
    ImpactDecisionCreateRequest,
    ReportStatusRequest,
    ReviewAcceptRequest,
    SubmissionCreateRequest,
)


review_app = typer.Typer(
    help="Prepare explicit manager review decisions.",
    no_args_is_help=True,
)
report_app = typer.Typer(
    help="Inspect accepted Report state and effective applicability.",
    no_args_is_help=True,
)


@review_app.command("impact")
def review_impact_command(
    impact_id: Annotated[str | None, typer.Argument(help="Accepted Impact ID.")] = None,
    report_id: Annotated[str | None, typer.Argument(help="Affected Report ID.")] = None,
    project: Annotated[Path, typer.Option("--project", "-C")] = Path("."),
    json_input: Annotated[bool, typer.Option("--json")] = False,
    expected_report_revision: Annotated[
        int | None,
        typer.Option("--expected-report-revision", min=1),
    ] = None,
    expected_impact_digest: Annotated[
        str | None,
        typer.Option("--expected-impact-digest"),
    ] = None,
    target_commit: Annotated[str | None, typer.Option("--target-commit")] = None,
    disposition: Annotated[
        ImpactDisposition,
        typer.Option("--disposition"),
    ] = ImpactDisposition.KEEP_STALE,
    reason: Annotated[str | None, typer.Option("--reason")] = None,
    rerun_task_id: Annotated[str | None, typer.Option("--rerun-task-id")] = None,
    dependencies_file: Annotated[
        Path | None,
        typer.Option("--dependencies-file"),
    ] = None,
    decision_id: Annotated[str | None, typer.Option("--decision-id")] = None,
    operation_id: Annotated[str | None, typer.Option("--operation-id")] = None,
    idempotency_key: Annotated[str | None, typer.Option("--idempotency-key")] = None,
) -> None:
    """Prepare a manager ImpactDecision PR; never start a Run."""

    def human_request() -> ImpactDecisionCreateRequest:
        if expected_report_revision is None:
            raise _input_error(
                "Human mode requires --expected-report-revision.",
                option="--expected-report-revision",
            )
        replacement = (
            load_model(dependencies_file, DependencySet)
            if dependencies_file is not None
            else None
        )
        return ImpactDecisionCreateRequest(
            **_operation_fields(operation_id, idempotency_key),
            decision_id=decision_id or new_id("decision"),
            impact_id=_required(impact_id, "IMPACT_ID"),
            report_id=_required(report_id, "REPORT_ID"),
            expected_report_revision=expected_report_revision,
            expected_impact_digest=_required(
                expected_impact_digest,
                "--expected-impact-digest",
            ),
            target_commit=_required(target_commit, "--target-commit"),
            disposition=disposition,
            reason=_required(reason, "--reason"),
            rerun_task_id=rerun_task_id,
            replacement_dependencies=replacement,
        )

    _run_command(
        command="impact.decide",
        method_name="impact_decide",
        project=project,
        json_input=json_input,
        request_model=ImpactDecisionCreateRequest,
        human_builder=human_request,
    )


def _render_report_status(data: dict[str, object]) -> None:
    typer.echo(
        f"Report: {data.get('report_id')} revision {data.get('report_revision')}"
    )
    typer.echo(f"Target commit: {data.get('target_commit')}")
    typer.echo(f"Target tree: {data.get('target_tree')}")
    typer.echo(f"Evidence: {data.get('evidence_status')}")
    typer.echo(f"Stored applicability: {data.get('stored_applicability')}")
    typer.echo(f"Effective applicability: {data.get('effective_applicability')}")
    typer.echo(f"Reason: {data.get('reason')}")
    paths = data.get("changed_paths")
    if isinstance(paths, list):
        for path in paths:
            typer.echo(f"  changed {path}")


@report_app.command("status")
def report_status_command(
    report_id: Annotated[str | None, typer.Argument(help="Accepted Report ID.")] = None,
    project: Annotated[Path, typer.Option("--project", "-C")] = Path("."),
    json_input: Annotated[bool, typer.Option("--json")] = False,
    target_commit: Annotated[
        str | None,
        typer.Option("--target-commit"),
    ] = None,
) -> None:
    """Derive applicability at an exact commit without mutating the Report."""

    def human_request() -> ReportStatusRequest:
        return ReportStatusRequest(
            report_id=_required(report_id, "REPORT_ID"),
            target_commit=target_commit,
        )

    _run_command(
        command="report.status",
        method_name="report_status",
        project=project,
        json_input=json_input,
        request_model=ReportStatusRequest,
        human_builder=human_request,
        human_renderer=_render_report_status,
    )


def impact_command(
    report_id: Annotated[
        str | None,
        typer.Argument(help="Accepted baseline Report ID."),
    ] = None,
    project: Annotated[Path, typer.Option("--project", "-C")] = Path("."),
    json_input: Annotated[bool, typer.Option("--json")] = False,
    expected_report_revision: Annotated[
        int | None,
        typer.Option("--expected-report-revision", min=1),
    ] = None,
    target_commit: Annotated[
        str | None,
        typer.Option("--target-commit"),
    ] = None,
    impact_id: Annotated[str | None, typer.Option("--impact-id")] = None,
    operation_id: Annotated[str | None, typer.Option("--operation-id")] = None,
    idempotency_key: Annotated[str | None, typer.Option("--idempotency-key")] = None,
) -> None:
    """Analyze code dependencies and open a reviewed Report Impact PR."""

    def human_request() -> ImpactCreateRequest:
        if expected_report_revision is None:
            raise _input_error(
                "Human mode requires --expected-report-revision.",
                option="--expected-report-revision",
            )
        return ImpactCreateRequest(
            **_operation_fields(operation_id, idempotency_key),
            impact_id=impact_id or new_id("impact"),
            report_id=_required(report_id, "REPORT_ID"),
            expected_report_revision=expected_report_revision,
            target_commit=_required(target_commit, "--target-commit"),
        )

    _run_command(
        command="impact.create",
        method_name="impact_create",
        project=project,
        json_input=json_input,
        request_model=ImpactCreateRequest,
        human_builder=human_request,
    )


def submit_command(
    run_ids: Annotated[
        list[str] | None,
        typer.Argument(help="Ordered immutable Run IDs."),
    ] = None,
    project: Annotated[Path, typer.Option("--project", "-C")] = Path("."),
    json_input: Annotated[bool, typer.Option("--json")] = False,
    submission_file: Annotated[
        Path | None,
        typer.Option("--submission-file"),
    ] = None,
    report_proposal_file: Annotated[
        Path | None,
        typer.Option("--report-proposal-file"),
    ] = None,
    base_commit: Annotated[str | None, typer.Option("--base-commit")] = None,
    operation_id: Annotated[str | None, typer.Option("--operation-id")] = None,
    idempotency_key: Annotated[str | None, typer.Option("--idempotency-key")] = None,
) -> None:
    """Push and open a generated ResearchSubmission PR without accepted fields."""

    def human_request() -> SubmissionCreateRequest:
        if submission_file is None:
            raise _input_error(
                "Human mode requires --submission-file.",
                option="--submission-file",
            )
        if report_proposal_file is None:
            raise _input_error(
                "Human mode requires --report-proposal-file.",
                option="--report-proposal-file",
            )
        if not run_ids:
            raise _input_error("Human mode requires at least one RUN_ID.")
        return SubmissionCreateRequest(
            **_operation_fields(operation_id, idempotency_key),
            base_commit=_required(base_commit, "--base-commit"),
            submission=load_model(submission_file, ResearchSubmission),
            report_proposal=load_model(report_proposal_file, ReportProposal),
            run_ids=tuple(run_ids),
        )

    _run_command(
        command="submission.create",
        method_name="submission_create",
        project=project,
        json_input=json_input,
        request_model=SubmissionCreateRequest,
        human_builder=human_request,
    )


@review_app.command("accept")
def review_accept_command(
    submission_id: Annotated[
        str | None,
        typer.Argument(help="Reviewed ResearchSubmission ID."),
    ] = None,
    project: Annotated[Path, typer.Option("--project", "-C")] = Path("."),
    json_input: Annotated[bool, typer.Option("--json")] = False,
    task_id: Annotated[str | None, typer.Option("--task-id")] = None,
    expected_head: Annotated[str | None, typer.Option("--expected-head")] = None,
    expected_report_revision: Annotated[
        int | None,
        typer.Option("--expected-report-revision", min=0),
    ] = None,
    decision_id: Annotated[str | None, typer.Option("--decision-id")] = None,
    disposition: Annotated[
        ReviewDisposition,
        typer.Option("--disposition"),
    ] = ReviewDisposition.ACCEPTED,
    condition: Annotated[list[str] | None, typer.Option("--condition")] = None,
    claim_scope: Annotated[
        ClaimScope,
        typer.Option("--claim-scope"),
    ] = ClaimScope.SNAPSHOT,
    code_disposition: Annotated[
        CodeDisposition,
        typer.Option("--code-disposition"),
    ] = CodeDisposition.RETAIN_ISOLATED,
    operation_id: Annotated[str | None, typer.Option("--operation-id")] = None,
    idempotency_key: Annotated[str | None, typer.Option("--idempotency-key")] = None,
) -> None:
    """Prepare Decision and Report at one exact proposal head; never merge."""

    def human_request() -> ReviewAcceptRequest:
        if expected_report_revision is None:
            raise _input_error(
                "Human mode requires --expected-report-revision.",
                option="--expected-report-revision",
            )
        return ReviewAcceptRequest(
            **_operation_fields(operation_id, idempotency_key),
            submission_id=_required(submission_id, "SUBMISSION_ID"),
            task_id=_required(task_id, "--task-id"),
            expected_head=_required(expected_head, "--expected-head"),
            decision_id=decision_id or new_id("decision"),
            expected_report_revision=expected_report_revision,
            disposition=disposition,
            conditions=tuple(condition or ()),
            claim_scope=claim_scope,
            code_disposition=code_disposition,
        )

    _run_command(
        command="review.accept",
        method_name="review_accept",
        project=project,
        json_input=json_input,
        request_model=ReviewAcceptRequest,
        human_builder=human_request,
    )
