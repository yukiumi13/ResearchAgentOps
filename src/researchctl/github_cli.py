from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from researchctl.adapters.github_governance import GitHubGovernanceClient
from researchctl.adapters.github_protection import GitHubProtectionManager
from researchctl.domain.ids import new_id
from researchctl.domain.models import GitHubGovernancePolicy
from researchctl.errors import RCPError
from researchctl.output import dump_envelope, envelope, error_payload
from researchctl.serialization import load_model
from researchctl.services.github_governance import audit_github_governance
from researchctl.services.project_runtime import ProjectRuntimeService
from researchctl.services.requests import GitHubGovernanceConfigureRequest


github_app = typer.Typer(
    help="Audit and configure GitHub governance for research proposals.",
    no_args_is_help=True,
)


def _accepted_github_policy(project: Path) -> GitHubGovernancePolicy:
    managed = ProjectRuntimeService().discover(project)
    policy = managed.policy.github
    if policy is None:
        raise RCPError(
            code="github_governance_policy_missing",
            message="Managed ProjectPolicy has no accepted GitHub governance policy.",
            remediation="Prepare and accept the protected GitHub policy first.",
        )
    return policy


def _abort(error: RCPError, *, command: str, json_output: bool) -> NoReturn:
    if json_output:
        typer.echo(
            dump_envelope(
                envelope(
                    command=command,
                    success=False,
                    errors=[error_payload(error)],
                )
            )
        )
    else:
        typer.echo(f"Error [{error.code}]: {error.message}", err=True)
        if error.remediation:
            typer.echo(f"Next: {error.remediation}", err=True)
    raise typer.Exit(code=error.exit_code)


@github_app.command("doctor")
def github_doctor_command(
    repository: Annotated[
        str | None,
        typer.Option(
            "--repository",
            help="Canonical GitHub OWNER/REPOSITORY; required without --project.",
        ),
    ] = None,
    project: Annotated[
        Path | None,
        typer.Option(
            "--project",
            help="Managed project whose accepted GitHub policy supplies the target.",
        ),
    ] = None,
    branch: Annotated[
        str | None,
        typer.Option("--branch", help="Branch to audit; defaults to the repository default."),
    ] = None,
    hostname: Annotated[
        str,
        typer.Option("--hostname", help="GitHub host used by gh api."),
    ] = "github.com",
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit a stable JSON envelope."),
    ] = False,
) -> None:
    """Read GitHub merge gates without modifying repository settings."""

    command = "github.doctor"
    try:
        policy = None
        if project is not None:
            policy = _accepted_github_policy(project)
            if repository is not None and repository.lower() != policy.repository.lower():
                raise RCPError(
                    code="github_governance_request_invalid",
                    message="Explicit repository conflicts with accepted ProjectPolicy.",
                )
            if branch is not None and branch != policy.default_branch:
                raise RCPError(
                    code="github_governance_request_invalid",
                    message="Explicit branch conflicts with accepted ProjectPolicy.",
                )
            repository = policy.repository
            branch = policy.default_branch
        if repository is None:
            raise RCPError(
                code="github_governance_request_invalid",
                message="GitHub governance requires --repository or a managed --project.",
            )
        observation = GitHubGovernanceClient().observe(
            repository=repository,
            branch=branch,
            hostname=hostname,
        )
        report = audit_github_governance(observation, policy=policy)
    except RCPError as error:
        _abort(error, command=command, json_output=json_output)

    if json_output:
        errors = [
            {
                "code": check.name,
                "message": check.message,
                "remediation": check.remediation,
                "context": {},
            }
            for check in report.checks
            if check.status == "error"
        ]
        warnings = [check.message for check in report.checks if check.status == "warn"]
        typer.echo(
            dump_envelope(
                envelope(
                    command=command,
                    success=report.healthy,
                    data=report.as_dict(),
                    warnings=warnings,
                    errors=errors,
                )
            )
        )
    else:
        typer.echo(f"Repository: {report.observation.repository}")
        typer.echo(f"Branch: {report.observation.branch}")
        for check in report.checks:
            typer.echo(f"[{check.status.upper()}] {check.name}: {check.message}")
    if not report.healthy:
        raise typer.Exit(code=2)


@github_app.command("apply-governance")
def github_apply_governance_command(
    project: Annotated[Path, typer.Option("--project", "-C")] = Path("."),
    expected_policy_digest: Annotated[
        str | None,
        typer.Option(
            "--expected-policy-digest",
            help="Policy digest emitted by a reviewed preview.",
        ),
    ] = None,
    expected_observation_digest: Annotated[
        str | None,
        typer.Option(
            "--expected-observation-digest",
            help="Observation digest emitted by the same reviewed preview.",
        ),
    ] = None,
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Apply the exact reviewed preview."),
    ] = False,
    hostname: Annotated[
        str,
        typer.Option("--hostname", help="GitHub host used by gh api."),
    ] = "github.com",
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit a stable JSON envelope."),
    ] = False,
) -> None:
    """Preview or explicitly apply accepted default-branch governance."""

    command = "github.apply-governance"
    try:
        policy = _accepted_github_policy(project)
        observer = GitHubGovernanceClient()
        manager = GitHubProtectionManager(observer=observer)
        if not apply:
            if expected_policy_digest is not None or expected_observation_digest is not None:
                raise RCPError(
                    code="github_governance_request_invalid",
                    message="Expected digests are only accepted together with --apply.",
                )
            receipt = manager.preview(policy, hostname=hostname)
        else:
            if os.environ.get("RESEARCHCTL_SESSION_ID") or os.environ.get(
                "RESEARCHCTL_SESSION_TOKEN"
            ):
                raise RCPError(
                    code="authorization_denied",
                    message="A Session capability environment cannot apply GitHub governance.",
                    remediation="Use a Manager shell and authenticate gh as a human Manager.",
                )
            if expected_policy_digest is None or expected_observation_digest is None:
                raise RCPError(
                    code="github_governance_expected_digest_required",
                    message="--apply requires both digests from one reviewed preview.",
                    remediation="Run without --apply, review the output, then pass both digests.",
                )
            receipt = manager.apply(
                policy,
                expected_policy_digest=expected_policy_digest,
                expected_observation_digest=expected_observation_digest,
                hostname=hostname,
            )
        data = receipt.as_dict()
    except RCPError as error:
        _abort(error, command=command, json_output=json_output)

    if json_output:
        typer.echo(dump_envelope(envelope(command=command, success=True, data=data)))
    else:
        typer.echo(f"Outcome: {data['terminal_result']}")
        preview = data["preview"]
        assert isinstance(preview, dict)
        typer.echo(f"Policy digest: {preview['policy_digest']}")
        typer.echo(f"Observation digest: {preview['observation_digest']}")
        typer.echo(f"Mutation required: {str(preview['mutation_required']).lower()}")
        if data.get("manager_login") is not None:
            typer.echo(f"Manager: {data['manager_login']}")
        if data.get("final_observation_digest") is not None:
            typer.echo(f"Final observation digest: {data['final_observation_digest']}")


@github_app.command("configure-governance")
def github_configure_governance_command(
    policy_file: Annotated[
        Path,
        typer.Option("--policy-file", help="Complete GitHubGovernancePolicy YAML."),
    ],
    expected_default_head: Annotated[
        str,
        typer.Option("--expected-default-head", help="Exact protected-base commit."),
    ],
    project: Annotated[Path, typer.Option("--project", "-C")] = Path("."),
    operation_id: Annotated[str | None, typer.Option("--operation-id")] = None,
    idempotency_key: Annotated[str | None, typer.Option("--idempotency-key")] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit a stable JSON envelope."),
    ] = False,
) -> None:
    """Prepare a manager-owned GitHub governance policy proposal."""

    command = "github.configure-governance"
    selected_operation = operation_id or new_id("operation")
    try:
        request = GitHubGovernanceConfigureRequest(
            operation_id=selected_operation,
            idempotency_key=idempotency_key or f"human:{selected_operation}",
            expected_default_head=expected_default_head,
            governance=load_model(policy_file, GitHubGovernancePolicy),
        )
        from researchctl.services.factory import open_application

        with open_application(
            project,
            github_governance_operation_id=request.operation_id,
            github_governance_expected_default_head=request.expected_default_head,
        ) as handle:
            result = handle.service.github_governance_configure(
                request,
                handle.actor,
            )
        data = result.as_dict()
    except typer.Exit:
        raise
    except Exception as exc:
        from researchctl.cli import _known_error

        error = _known_error(exc)
        if error is None:
            raise
        if "operation_id" not in error.context:
            error = RCPError(
                code=error.code,
                message=error.message,
                remediation=error.remediation,
                context={**error.context, "operation_id": selected_operation},
                exit_code=error.exit_code,
            )
        _abort(error, command=command, json_output=json_output)
    if json_output:
        typer.echo(dump_envelope(envelope(command=command, success=True, data=data)))
    else:
        typer.echo(f"Operation: {data['operation_id']}")
        typer.echo(f"Outcome: {data['terminal_result']}")
        proposal = data.get("proposal")
        if isinstance(proposal, dict):
            typer.echo(f"Branch: {proposal.get('branch')}")
            typer.echo(f"Commit: {proposal.get('commit')}")
