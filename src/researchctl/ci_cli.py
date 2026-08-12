from __future__ import annotations

import hashlib
from datetime import UTC
from pathlib import Path
from typing import Annotated

import typer
from pydantic import TypeAdapter

from researchctl.domain.ids import new_id
from researchctl.domain.types import UtcDateTime, utc_now
from researchctl.phase2_cli import (
    _input_error,
    _machine_or_human_request,
    _render_result,
    _required,
    _result_data,
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
from researchctl.services.requests import ImpactBatchCreateRequest

ci_app = typer.Typer(
    help="Validate an untrusted PR head as Git data.",
    no_args_is_help=True,
)

_UTC_DATETIME_ADAPTER = TypeAdapter(UtcDateTime)


def _impact_automation_id(
    kind: str,
    *,
    generated_at: object,
    before_commit: str,
    target_commit: str,
) -> str:
    timestamp = _UTC_DATETIME_ADAPTER.validate_python(generated_at).astimezone(UTC)
    suffix = hashlib.sha256(
        f"researchctl:{kind}:{before_commit}:{target_commit}".encode("ascii")
    ).hexdigest()[:24]
    return f"{kind}_{timestamp.strftime('%Y%m%dT%H%M%SZ')}_{suffix}"


@ci_app.command("impact")
def ci_impact_command(
    project: Annotated[Path, typer.Option("--project", "-C")] = Path("."),
    json_input: Annotated[bool, typer.Option("--json")] = False,
    before_commit: Annotated[str | None, typer.Option("--before")] = None,
    target_commit: Annotated[str | None, typer.Option("--after")] = None,
    generated_at: Annotated[str | None, typer.Option("--generated-at")] = None,
    impact_id: Annotated[str | None, typer.Option("--impact-id")] = None,
    operation_id: Annotated[str | None, typer.Option("--operation-id")] = None,
    idempotency_key: Annotated[str | None, typer.Option("--idempotency-key")] = None,
) -> None:
    """Scan all baseline Reports and open at most one reviewed Impact PR."""

    command = "ci.impact"

    def human_request() -> ImpactBatchCreateRequest:
        before = _required(before_commit, "--before")
        target = _required(target_commit, "--after")
        generated = _required(generated_at, "--generated-at")
        selected_impact_id = impact_id or _impact_automation_id(
            "impact",
            generated_at=generated,
            before_commit=before,
            target_commit=target,
        )
        selected_operation_id = operation_id or _impact_automation_id(
            "operation",
            generated_at=generated,
            before_commit=before,
            target_commit=target,
        )
        return ImpactBatchCreateRequest(
            operation_id=selected_operation_id,
            idempotency_key=(
                idempotency_key
                or f"ci-impact:{before}:{target}"
            ),
            impact_id=selected_impact_id,
            before_commit=before,
            target_commit=target,
            generated_at=generated,
        )

    request: ImpactBatchCreateRequest | None = None
    try:
        request = _machine_or_human_request(
            json_input,
            ImpactBatchCreateRequest,
            human_request,
        )
        from researchctl.services.factory import (
            open_impact_automation_application,
        )

        with open_impact_automation_application(project) as handle:
            result = handle.service.impact_batch_create(request, handle.actor)
        data = _result_data(result)
        if json_input:
            from researchctl.output import dump_envelope, envelope

            typer.echo(
                dump_envelope(envelope(command=command, success=True, data=data))
            )
            return
        _render_result(data)
    except typer.Exit:
        raise
    except Exception as exc:
        from researchctl.cli import _abort, _known_error

        error = _known_error(exc)
        if error is None:
            raise
        if request is not None and "operation_id" not in error.context:
            from researchctl.errors import RCPError

            error = RCPError(
                code=error.code,
                message=error.message,
                remediation=error.remediation,
                context={
                    **error.context,
                    "operation_id": request.operation_id,
                },
                exit_code=error.exit_code,
            )
        _abort(error, command=command, json_output=json_input)


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
