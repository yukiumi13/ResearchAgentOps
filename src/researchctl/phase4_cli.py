from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from researchctl.domain.enums import (
    ClaimScope,
    CodeDisposition,
    ReviewDisposition,
)
from researchctl.domain.ids import new_id
from researchctl.domain.models import ReportProposal, ResearchSubmission
from researchctl.phase2_cli import (
    _input_error,
    _operation_fields,
    _required,
    _run_command,
)
from researchctl.serialization import load_model
from researchctl.services.requests import (
    ReviewAcceptRequest,
    SubmissionCreateRequest,
)


review_app = typer.Typer(
    help="Prepare explicit manager review decisions.",
    no_args_is_help=True,
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
    """Create a generated ResearchSubmission branch without accepted fields."""

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
