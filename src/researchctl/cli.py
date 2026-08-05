from __future__ import annotations

from pathlib import Path
from typing import Annotated, NoReturn

import typer
from pydantic import ValidationError

from researchctl.ci_cli import ci_app
from researchctl.constants import __version__
from researchctl.document_cli import doc_app
from researchctl.errors import RCPError
from researchctl.notification_cli import notification_app
from researchctl.output import dump_envelope, envelope, error_payload
from researchctl.phase2_cli import (
    bootstrap_app,
    inbox_app,
    linear_app,
    plan_app,
    run_app,
    session_app,
    status_app,
    task_app,
)
from researchctl.phase4_cli import (
    impact_command,
    report_app,
    review_app,
    submit_command,
)
from researchctl.reconcile_cli import reconcile_command
from researchctl.serialization import SerializationError
from researchctl.services.doctor import doctor
from researchctl.services.init_project import initialize_project
from researchctl.services.upgrade import check_upgrade
from researchctl.writing_cli import brief_app, update_app

app = typer.Typer(
    name="researchctl",
    help="Git-native control plane for governed research agents.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)

app.add_typer(task_app, name="task")
app.add_typer(bootstrap_app, name="bootstrap")
app.add_typer(session_app, name="session")
app.add_typer(status_app, name="status")
app.add_typer(inbox_app, name="inbox")
app.add_typer(run_app, name="run")
app.add_typer(plan_app, name="plan")
app.add_typer(linear_app, name="linear")
app.add_typer(review_app, name="review")
app.add_typer(report_app, name="report")
app.add_typer(doc_app, name="doc")
app.add_typer(brief_app, name="brief")
app.add_typer(update_app, name="update")
app.add_typer(notification_app, name="notification")
app.add_typer(ci_app, name="ci")
app.command("submit")(submit_command)
app.command("impact")(impact_command)
app.command("reconcile")(reconcile_command)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def root(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True),
    ] = False,
) -> None:
    """Manage portable research-control state."""
    del version


def _known_error(exc: Exception) -> RCPError | None:
    if isinstance(exc, RCPError):
        return exc
    if isinstance(exc, ValidationError):
        return RCPError(
            code="validation_error",
            message="Protocol record validation failed.",
            remediation="Review invalid fields; strict records reject unknown values.",
            context={
                "details": exc.errors(
                    include_url=False,
                    include_context=False,
                    include_input=False,
                )
            },
        )
    if isinstance(exc, SerializationError):
        return RCPError(
            code="serialization_error",
            message=str(exc),
            remediation="Use canonical YAML without duplicate keys or non-finite values.",
        )
    if isinstance(exc, FileNotFoundError):
        return RCPError(
            code="file_not_found",
            message=str(exc),
            remediation="Run researchctl init or restore the managed file from Git.",
        )
    if isinstance(exc, (OSError, ValueError)):
        return RCPError(
            code="invalid_local_state",
            message=str(exc),
            remediation="Run researchctl doctor and review the affected managed file.",
        )
    return None


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


@app.command("init")
def init_command(
    path: Annotated[
        Path,
        typer.Argument(help="Existing Git repository to initialize."),
    ] = Path("."),
    name: Annotated[
        str | None,
        typer.Option(help="Human-readable project name."),
    ] = None,
    key: Annotated[
        str | None,
        typer.Option(help="Portable human-readable project key."),
    ] = None,
    default_branch: Annotated[
        str | None,
        typer.Option(help="Accepted-state branch when origin/HEAD is unavailable."),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Preview without writing."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit a stable JSON envelope."),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Reserved for non-interactive approval."),
    ] = False,
) -> None:
    """Initialize the repository protocol without touching project files."""
    del yes
    command = "init"
    try:
        result = initialize_project(
            path,
            name=name,
            key=key,
            default_branch=default_branch,
            dry_run=dry_run,
        )
    except Exception as exc:
        error = _known_error(exc)
        if error is None:
            raise
        _abort(error, command=command, json_output=json_output)

    if json_output:
        typer.echo(
            dump_envelope(
                envelope(
                    command=command,
                    success=True,
                    data=result.as_dict(),
                    warnings=list(result.warnings),
                )
            )
        )
        return

    mode = "Dry run" if result.dry_run else "Initialized"
    typer.echo(f"{mode}: {result.repository}")
    typer.echo(f"Project: {result.project_id}")
    for created in result.created:
        typer.echo(f"  create {created}")
    for warning in result.warnings:
        typer.echo(f"Warning: {warning}")


@app.command("doctor")
def doctor_command(
    path: Annotated[
        Path,
        typer.Argument(help="Managed Git repository to inspect."),
    ] = Path("."),
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit a stable JSON envelope."),
    ] = False,
) -> None:
    """Validate protocol state without modifying the repository."""
    command = "doctor"
    try:
        report = doctor(path)
    except Exception as exc:
        error = _known_error(exc)
        if error is None:
            raise
        _abort(error, command=command, json_output=json_output)

    if json_output:
        error_checks = [
            {
                "code": check.name,
                "message": check.message,
                "remediation": check.remediation,
                "context": {},
            }
            for check in report.checks
            if check.status == "error"
        ]
        warning_messages = [
            check.message for check in report.checks if check.status == "warn"
        ]
        typer.echo(
            dump_envelope(
                envelope(
                    command=command,
                    success=report.healthy,
                    data=report.as_dict(),
                    warnings=warning_messages,
                    errors=error_checks,
                )
            )
        )
    else:
        typer.echo(f"Repository: {report.repository}")
        for check in report.checks:
            typer.echo(f"[{check.status.upper()}] {check.name}: {check.message}")

    if not report.healthy:
        raise typer.Exit(code=2)


@app.command("upgrade")
def upgrade_command(
    path: Annotated[
        Path,
        typer.Argument(help="Managed Git repository to inspect."),
    ] = Path("."),
    check: Annotated[
        bool,
        typer.Option("--check", help="Preview protocol compatibility."),
    ] = True,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit a stable JSON envelope."),
    ] = False,
) -> None:
    """Check whether an explicit protocol migration is required."""
    command = "upgrade"
    if not check:
        _abort(
            RCPError(
                code="upgrade_apply_not_implemented",
                message="Phase 1 supports upgrade checks only.",
                remediation="Use --check and review a future migration.",
            ),
            command=command,
            json_output=json_output,
        )

    try:
        report = check_upgrade(path)
    except Exception as exc:
        error = _known_error(exc)
        if error is None:
            raise
        _abort(error, command=command, json_output=json_output)

    if json_output:
        typer.echo(
            dump_envelope(
                envelope(
                    command=command,
                    success=True,
                    data=report.as_dict(),
                )
            )
        )
    else:
        state = "migration required" if report.migration_required else "current"
        typer.echo(f"Protocol {report.current} -> {report.target}: {state}")
