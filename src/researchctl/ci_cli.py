from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from researchctl.domain.ids import new_id
from researchctl.domain.types import utc_now
from researchctl.phase2_cli import (
    _input_error,
    _machine_or_human_request,
    _required,
)
from researchctl.services.ci_dispatch import (
    CIPRDispatchRequest,
    ProtectedBasePRDispatcher,
    write_ci_dispatch_artifact,
)
from researchctl.services.ci_validation import (
    CIValidationRequest,
    ExactHeadCIValidator,
    write_ci_validation_artifact,
)


ci_app = typer.Typer(
    help="Validate an untrusted PR head as Git data.",
    no_args_is_help=True,
)


@ci_app.command("dispatch")
def ci_dispatch_command(
    project: Annotated[Path, typer.Option("--project", "-C")] = Path("."),
    json_input: Annotated[bool, typer.Option("--json")] = False,
    artifact: Annotated[
        Path | None,
        typer.Option("--artifact", help="New immutable dispatch artifact path."),
    ] = None,
    repository: Annotated[
        str | None,
        typer.Option("--repository", help="Stable repository identity."),
    ] = None,
    pull_request_number: Annotated[
        int | None,
        typer.Option("--pull-request-number", min=1),
    ] = None,
    subject_head: Annotated[str | None, typer.Option("--subject-head")] = None,
    base_commit: Annotated[str | None, typer.Option("--base-commit")] = None,
    head_ref: Annotated[str | None, typer.Option("--head-ref")] = None,
    base_ref: Annotated[str | None, typer.Option("--base-ref")] = None,
    attestation_id: Annotated[str | None, typer.Option("--attestation-id")] = None,
    generated_at: Annotated[str | None, typer.Option("--generated-at")] = None,
) -> None:
    """Dispatch and validate one exact PR head with protected-base code."""

    command = "ci.dispatch"

    def human_request() -> CIPRDispatchRequest:
        if pull_request_number is None:
            raise _input_error(
                "Human mode requires --pull-request-number.",
                option="--pull-request-number",
            )
        return CIPRDispatchRequest(
            attestation_id=attestation_id or new_id("attestation"),
            repository=_required(repository, "--repository"),
            pull_request_number=pull_request_number,
            subject_head=_required(subject_head, "--subject-head"),
            base_commit=_required(base_commit, "--base-commit"),
            head_ref=_required(head_ref, "--head-ref"),
            base_ref=_required(base_ref, "--base-ref"),
            generated_at=generated_at or utc_now(),
        )

    try:
        if artifact is None:
            raise _input_error(
                "CI dispatch requires --artifact in human and JSON modes.",
                option="--artifact",
            )
        request = _machine_or_human_request(
            json_input,
            CIPRDispatchRequest,
            human_request,
        )
        result = ProtectedBasePRDispatcher().validate(project, request)
        artifact_receipt = write_ci_dispatch_artifact(result, artifact)
        data = {
            **result.as_dict(),
            "artifact": artifact_receipt.as_dict(),
        }
        if json_input:
            from researchctl.output import dump_envelope, envelope

            typer.echo(
                dump_envelope(envelope(command=command, success=True, data=data))
            )
            return

        typer.echo(f"PR type: {result.attestation.pr_type}")
        typer.echo(f"Applicability: {result.attestation.applicability}")
        typer.echo(f"Result: {result.attestation.overall_result}")
        typer.echo(f"Head: {result.attestation.subject_head}")
        typer.echo(f"Tree: {result.attestation.subject_tree}")
        if result.exact_result is not None:
            typer.echo(f"Kind: {result.exact_result.head_kind}")
        typer.echo(f"Artifact: {artifact_receipt.path}")
    except typer.Exit:
        raise
    except Exception as exc:
        from researchctl.cli import _abort, _known_error

        error = _known_error(exc)
        if error is None:
            raise
        _abort(error, command=command, json_output=json_input)


@ci_app.command("validate")
def ci_validate_command(
    project: Annotated[Path, typer.Option("--project", "-C")] = Path("."),
    json_input: Annotated[bool, typer.Option("--json")] = False,
    artifact: Annotated[
        Path | None,
        typer.Option("--artifact", help="New immutable attestation artifact path."),
    ] = None,
    repository: Annotated[
        str | None,
        typer.Option("--repository", help="Stable repository identity."),
    ] = None,
    pull_request_number: Annotated[
        int | None,
        typer.Option("--pull-request-number", min=1),
    ] = None,
    subject_head: Annotated[str | None, typer.Option("--subject-head")] = None,
    base_commit: Annotated[str | None, typer.Option("--base-commit")] = None,
    submission_id: Annotated[str | None, typer.Option("--submission-id")] = None,
    attestation_id: Annotated[str | None, typer.Option("--attestation-id")] = None,
    generated_at: Annotated[str | None, typer.Option("--generated-at")] = None,
) -> None:
    """Emit an exact-head attestation without executing or publishing PR content."""

    command = "ci.validate"

    def human_request() -> CIValidationRequest:
        if pull_request_number is None:
            raise _input_error(
                "Human mode requires --pull-request-number.",
                option="--pull-request-number",
            )
        return CIValidationRequest(
            attestation_id=attestation_id or new_id("attestation"),
            repository=_required(repository, "--repository"),
            pull_request_number=pull_request_number,
            subject_head=_required(subject_head, "--subject-head"),
            base_commit=_required(base_commit, "--base-commit"),
            submission_id=_required(submission_id, "--submission-id"),
            generated_at=generated_at or utc_now(),
        )

    try:
        if artifact is None:
            raise _input_error(
                "CI validation requires --artifact in human and JSON modes.",
                option="--artifact",
            )
        request = _machine_or_human_request(
            json_input,
            CIValidationRequest,
            human_request,
        )
        result = ExactHeadCIValidator().validate(project, request)
        artifact_receipt = write_ci_validation_artifact(result, artifact)
        data = {
            **result.as_dict(),
            "artifact": artifact_receipt.as_dict(),
        }
        if json_input:
            from researchctl.output import dump_envelope, envelope

            typer.echo(
                dump_envelope(envelope(command=command, success=True, data=data))
            )
            return

        typer.echo(f"Attestation: {result.attestation.attestation_id}")
        typer.echo(f"Result: {result.attestation.overall_result}")
        typer.echo(f"Head: {result.attestation.subject_head}")
        typer.echo(f"Tree: {result.attestation.subject_tree}")
        typer.echo(f"Kind: {result.head_kind}")
        typer.echo(f"Artifact: {artifact_receipt.path}")
        typer.echo(f"Projection: {result.attestation.projection.state}")
    except typer.Exit:
        raise
    except Exception as exc:
        from researchctl.cli import _abort, _known_error

        error = _known_error(exc)
        if error is None:
            raise
        _abort(error, command=command, json_output=json_input)
