from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, NoReturn

import typer
from pydantic import ValidationError

from researchctl.constants import PROJECT_POLICY_PATH
from researchctl.domain.ids import new_id
from researchctl.domain.models import DocumentLayoutPolicy, ProjectPolicy
from researchctl.errors import RCPError
from researchctl.output import dump_envelope, envelope, error_payload
from researchctl.repository import discover_repository
from researchctl.serialization import SerializationError, load_model
from researchctl.services.project_documents import (
    DocumentLintResult,
    DocumentTreeLintResult,
    lint_document_tree,
    lint_project_document,
    load_project_document,
    render_document_index,
    render_project_document,
)
from researchctl.services.requests import DocumentLayoutConfigureRequest


doc_app = typer.Typer(
    help="Lint, render, and classify governed project documents.",
    no_args_is_help=True,
)

_STANDALONE_POLICY = ".researchctl-docs.yaml"


def _error(exc: Exception) -> RCPError:
    if isinstance(exc, RCPError):
        return exc
    if isinstance(exc, ValidationError):
        return RCPError(
            code="validation_error",
            message="Document contract schema validation failed.",
            remediation="Review the invalid fields and rerun document lint.",
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
            remediation="Use canonical YAML without duplicate keys or aliases.",
        )
    if isinstance(exc, (OSError, UnicodeError, ValueError)):
        return RCPError(
            code="invalid_local_state",
            message=str(exc),
            remediation="Check document paths, policy, and file contents.",
        )
    raise exc


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
    raise typer.Exit(code=2)


def _emit_lint(
    result: DocumentLintResult | DocumentTreeLintResult,
    *,
    command: str,
    json_output: bool,
) -> None:
    data = result.as_dict()
    if json_output:
        typer.echo(dump_envelope(envelope(command=command, success=result.passed, data=data)))
    else:
        typer.echo(f"Outcome: {result.terminal_result}")
        if isinstance(result, DocumentLintResult):
            typer.echo(f"Document: {result.document_id} ({result.document_kind})")
            typer.echo(f"Classification: {result.classification}")
        else:
            typer.echo(f"Root: {result.root}")
            typer.echo(f"Checked: {result.checked_files} files")
            typer.echo(f"Structured documents: {result.structured_documents}")
        for finding in result.findings:
            typer.echo(
                f"  {finding.kind}: {finding.path} [{finding.code}] {finding.message}"
            )
    if not result.passed:
        raise typer.Exit(code=2)


def _write_or_echo(content: bytes, output_file: Path | None) -> None:
    if output_file is None:
        typer.echo(content.decode("utf-8"), nl=False)
        return
    destination = Path(os.path.abspath(os.fspath(output_file)))
    if destination.parent.is_symlink() or not destination.parent.is_dir():
        raise RCPError(
            code="document_output_path_invalid",
            message="Document output parent must be an existing non-symlink directory.",
            context={"path": str(output_file)},
        )
    if destination.exists() or destination.is_symlink():
        if (
            destination.is_file()
            and not destination.is_symlink()
            and destination.read_bytes() == content
        ):
            typer.echo(f"Unchanged: {destination}")
            return
        raise RCPError(
            code="document_output_conflict",
            message="Document output path already contains different content.",
            remediation="Choose a new path or explicitly remove the stale generated output.",
            context={"path": str(output_file)},
        )
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    typer.echo(f"Rendered: {destination}")


def _repository_and_policy(
    project: Path,
    policy_file: Path | None,
) -> tuple[Path, DocumentLayoutPolicy]:
    repository = discover_repository(project).root
    if policy_file is not None:
        return repository, load_model(policy_file, DocumentLayoutPolicy)
    managed_policy = repository / PROJECT_POLICY_PATH
    standalone_policy = repository / _STANDALONE_POLICY
    if managed_policy.is_file() and not managed_policy.is_symlink():
        if standalone_policy.exists() or standalone_policy.is_symlink():
            raise RCPError(
                code="document_policy_shadowed",
                message="Managed projects cannot also define a standalone document policy.",
            )
        return repository, load_model(managed_policy, ProjectPolicy).document_layout
    if standalone_policy.is_file() and not standalone_policy.is_symlink():
        return repository, load_model(standalone_policy, DocumentLayoutPolicy)
    if standalone_policy.is_symlink():
        raise RCPError(
            code="document_policy_invalid",
            message="Standalone document policy cannot be a symbolic link.",
        )
    raise RCPError(
        code="document_policy_missing",
        message="Repository has no managed or standalone document policy.",
        remediation=(
            "Create .researchctl-docs.yaml, pass --policy-file, or run "
            "researchctl init."
        ),
    )


@doc_app.command("lint")
def doc_lint_command(
    document_file: Annotated[Path, typer.Argument(help="Structured document YAML.")],
    project: Annotated[Path, typer.Option("--project", "-C")] = Path("."),
    policy_file: Annotated[
        Path | None,
        typer.Option("--policy-file", help="Standalone DocumentLayoutPolicy YAML."),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Validate document schema, semantics, and accepted classification."""

    try:
        _repository, policy = _repository_and_policy(project, policy_file)
        document = load_project_document(document_file)
        result = lint_project_document(document, policy=policy)
    except Exception as exc:
        _abort(_error(exc), command="doc.lint", json_output=json_output)
    _emit_lint(result, command="doc.lint", json_output=json_output)


@doc_app.command("render")
def doc_render_command(
    document_file: Annotated[Path, typer.Argument(help="Linted structured document YAML.")],
    output_file: Annotated[
        Path | None,
        typer.Option("--output-file", help="Write deterministic Markdown here."),
    ] = None,
    project: Annotated[Path, typer.Option("--project", "-C")] = Path("."),
    policy_file: Annotated[
        Path | None,
        typer.Option("--policy-file", help="Standalone DocumentLayoutPolicy YAML."),
    ] = None,
) -> None:
    """Render a passing structured document as deterministic Markdown."""

    try:
        _repository, policy = _repository_and_policy(project, policy_file)
        document = load_project_document(document_file)
        result = lint_project_document(document, policy=policy)
        if not result.passed:
            raise RCPError(
                code="document_lint_invalid",
                message="Document does not satisfy its accepted classification route.",
                context=result.as_dict(),
            )
        _write_or_echo(render_project_document(document), output_file)
    except Exception as exc:
        _abort(_error(exc), command="doc.render", json_output=False)


@doc_app.command("tree")
def doc_tree_command(
    project: Annotated[Path, typer.Option("--project", "-C")] = Path("."),
    policy_file: Annotated[
        Path | None,
        typer.Option("--policy-file", help="Standalone DocumentLayoutPolicy YAML."),
    ] = None,
    baseline_project: Annotated[
        Path | None,
        typer.Option(
            "--baseline-project",
            help="Optional baseline checkout used to enforce frozen documents.",
        ),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Validate the complete configured document hierarchy and generated pairs."""

    try:
        repository, policy = _repository_and_policy(project, policy_file)
        baseline_repository: Path | None = None
        baseline_policy: DocumentLayoutPolicy | None = None
        if baseline_project is not None:
            baseline_repository, baseline_policy = _repository_and_policy(
                baseline_project,
                None,
            )
        result = lint_document_tree(
            repository,
            policy,
            baseline_root=baseline_repository,
            baseline_policy=baseline_policy,
        )
    except Exception as exc:
        _abort(_error(exc), command="doc.tree", json_output=json_output)
    _emit_lint(result, command="doc.tree", json_output=json_output)


@doc_app.command("index")
def doc_index_command(
    project: Annotated[Path, typer.Option("--project", "-C")] = Path("."),
    policy_file: Annotated[
        Path | None,
        typer.Option("--policy-file", help="Standalone DocumentLayoutPolicy YAML."),
    ] = None,
    output_file: Annotated[
        Path | None,
        typer.Option("--output-file", help="Write the deterministic Markdown index here."),
    ] = None,
) -> None:
    """Render the configured type/classification/directory index."""

    try:
        _repository, policy = _repository_and_policy(project, policy_file)
        _write_or_echo(render_document_index(policy), output_file)
    except Exception as exc:
        _abort(_error(exc), command="doc.index", json_output=False)


@doc_app.command("configure-layout")
def doc_configure_layout_command(
    policy_file: Annotated[
        Path,
        typer.Option("--policy-file", help="Complete DocumentLayoutPolicy YAML."),
    ],
    expected_default_head: Annotated[
        str,
        typer.Option("--expected-default-head", help="Exact protected-base commit."),
    ],
    project: Annotated[Path, typer.Option("--project", "-C")] = Path("."),
    operation_id: Annotated[str | None, typer.Option("--operation-id")] = None,
    idempotency_key: Annotated[str | None, typer.Option("--idempotency-key")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Prepare a manager-owned document classification/layout policy proposal."""

    command = "doc.configure-layout"
    selected_operation = operation_id or new_id("operation")
    try:
        request = DocumentLayoutConfigureRequest(
            operation_id=selected_operation,
            idempotency_key=idempotency_key or f"human:{selected_operation}",
            expected_default_head=expected_default_head,
            document_layout=load_model(policy_file, DocumentLayoutPolicy),
        )
        from researchctl.services.factory import open_application

        with open_application(
            project,
            document_layout_operation_id=request.operation_id,
            document_layout_expected_default_head=request.expected_default_head,
        ) as handle:
            result = handle.service.document_layout_configure(request, handle.actor)
        data = result.as_dict()
    except Exception as exc:
        _abort(_error(exc), command=command, json_output=json_output)
    if json_output:
        typer.echo(dump_envelope(envelope(command=command, success=True, data=data)))
    else:
        typer.echo(f"Operation: {data['operation_id']}")
        typer.echo(f"Outcome: {data['terminal_result']}")
        proposal = data.get("proposal")
        if isinstance(proposal, dict):
            typer.echo(f"Branch: {proposal.get('branch')}")
            typer.echo(f"Commit: {proposal.get('commit')}")
