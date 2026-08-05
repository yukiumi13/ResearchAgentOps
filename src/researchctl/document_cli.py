from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path
from typing import Annotated, Literal, NoReturn

import typer
from pydantic import ValidationError

from researchctl.constants import PROJECT_POLICY_PATH
from researchctl.domain.ids import new_id
from researchctl.domain.models import AgentGuideFormat, DocumentLayoutPolicy, ProjectPolicy
from researchctl.errors import RCPError
from researchctl.output import dump_envelope, envelope, error_payload
from researchctl.repository import discover_repository, safe_repository_path
from researchctl.serialization import SerializationError, load_model
from researchctl.services.project_documents import (
    DocumentLintResult,
    DocumentTreeLintResult,
    agent_guide_markers,
    lint_document_tree,
    lint_project_document,
    load_project_document,
    render_document_index,
    render_project_agent_guide,
    render_project_document,
    render_standalone_document_policy_template,
)
from researchctl.services.requests import DocumentLayoutConfigureRequest


doc_app = typer.Typer(
    help="Draft policy, lint, render, and classify governed project documents.",
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


def _agent_guide_destination(
    repository: Path,
    output_file: Path,
    policy: DocumentLayoutPolicy,
    requested_format: AgentGuideFormat | None,
) -> tuple[Path, AgentGuideFormat]:
    lexical = (
        Path(os.path.abspath(os.fspath(output_file)))
        if output_file.is_absolute()
        else Path(os.path.abspath(os.fspath(repository / output_file)))
    )
    try:
        relative = lexical.relative_to(repository).as_posix()
    except ValueError as error:
        raise RCPError(
            code="agent_guide_output_outside_repository",
            message="Agent guide output must stay inside the selected repository.",
            context={"path": str(output_file)},
        ) from error
    target = next((item for item in policy.agent_guides if item.path == relative), None)
    if target is None:
        raise RCPError(
            code="agent_guide_target_unconfigured",
            message="Agent guide output is not declared in policy.agent_guides.",
            remediation="Declare the path and format in the protected document policy.",
            context={"path": relative},
        )
    if requested_format is not None and requested_format != target.format:
        raise RCPError(
            code="agent_guide_format_mismatch",
            message="Requested Agent guide format differs from the configured target.",
            context={
                "path": relative,
                "requested_format": requested_format,
                "configured_format": target.format,
            },
        )
    return safe_repository_path(repository, target.path), target.format


def _prepare_agent_guide_parent(repository: Path, destination: Path) -> None:
    try:
        relative_parent = destination.parent.relative_to(repository)
    except ValueError as error:
        raise RCPError(
            code="agent_guide_output_outside_repository",
            message="Agent guide output parent escapes the selected repository.",
        ) from error
    current = repository
    for part in relative_parent.parts:
        current = current / part
        if current.exists() or current.is_symlink():
            if current.is_symlink() or not current.is_dir():
                raise RCPError(
                    code="agent_guide_output_path_invalid",
                    message="Agent guide output parent must use non-symlink directories.",
                    context={"path": str(current)},
                )
            continue
        current.mkdir(mode=0o755)


def _atomic_replace(destination: Path, content: bytes, mode: int) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise


def _upsert_agent_guide(
    repository: Path,
    destination: Path,
    content: bytes,
    guide_format: AgentGuideFormat,
) -> None:
    _prepare_agent_guide_parent(repository, destination)
    observed: str | None = None
    existing_mode = 0o644
    if destination.exists() or destination.is_symlink():
        if destination.is_symlink() or not destination.is_file():
            raise RCPError(
                code="agent_guide_output_path_invalid",
                message="Agent guide output must be a regular non-symlink file.",
                context={"path": str(destination)},
            )
        observed = destination.read_text(encoding="utf-8")
        existing_mode = stat.S_IMODE(destination.stat().st_mode)

    rendered = content.decode("utf-8")
    if observed is None:
        updated = rendered
        outcome = "Rendered"
    else:
        begin, end = agent_guide_markers(guide_format)
        begin_count = observed.count(begin)
        end_count = observed.count(end)
        if begin_count == 0 and end_count == 0:
            separator = "" if not observed else ("\n" if observed.endswith("\n") else "\n\n")
            updated = observed + separator + rendered
        elif begin_count == 1 and end_count == 1:
            begin_index = observed.index(begin)
            end_index = observed.index(end)
            if end_index < begin_index:
                raise RCPError(
                    code="agent_guide_marker_invalid",
                    message="Agent guide managed block markers are in the wrong order.",
                )
            end_exclusive = end_index + len(end)
            if observed.startswith("\r\n", end_exclusive):
                end_exclusive += 2
            elif observed.startswith("\n", end_exclusive):
                end_exclusive += 1
            updated = observed[:begin_index] + rendered + observed[end_exclusive:]
        else:
            raise RCPError(
                code="agent_guide_marker_invalid",
                message="Agent guide contains incomplete or duplicate managed block markers.",
            )
        outcome = "Updated"

    encoded = updated.encode("utf-8")
    if observed is not None and encoded == observed.encode("utf-8"):
        typer.echo(f"Unchanged: {destination}")
        return
    _atomic_replace(destination, encoded, existing_mode)
    typer.echo(f"{outcome}: {destination}")


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
    """Validate configured documents, Agent guides, and generated pairs."""

    try:
        repository, policy = _repository_and_policy(project, policy_file)
        baseline_repository: Path | None = None
        baseline_policy: DocumentLayoutPolicy | None = None
        if baseline_project is not None:
            try:
                baseline_repository, baseline_policy = _repository_and_policy(
                    baseline_project,
                    None,
                )
            except RCPError as error:
                if error.code != "document_policy_missing":
                    raise
                baseline_repository = discover_repository(baseline_project).root
                baseline_policy = policy
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


@doc_app.command("policy-template")
def doc_policy_template_command(
    agent_format: Annotated[
        Literal["claude", "agents"],
        typer.Option(
            "--agent-format",
            help="Project instruction target included in the example policy.",
        ),
    ] = "claude",
    output_file: Annotated[
        Path | None,
        typer.Option("--output-file", help="Write the standalone policy candidate here."),
    ] = None,
) -> None:
    """Render a complete, strict standalone document-policy example."""

    try:
        _write_or_echo(
            render_standalone_document_policy_template(agent_format),
            output_file,
        )
    except Exception as exc:
        _abort(_error(exc), command="doc.policy-template", json_output=False)


@doc_app.command("policy-lint")
def doc_policy_lint_command(
    policy_file: Annotated[
        Path,
        typer.Argument(help="Standalone DocumentLayoutPolicy YAML candidate."),
    ],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Validate a document policy without requiring a repository or research init."""

    command = "doc.policy-lint"
    try:
        if policy_file.is_symlink() or not policy_file.is_file():
            raise RCPError(
                code="document_policy_invalid",
                message="Document policy must be an existing non-symlink regular file.",
                context={"path": str(policy_file)},
            )
        policy = load_model(policy_file, DocumentLayoutPolicy)
        data = {
            "path": str(policy_file),
            "terminal_result": "passed",
            "routes": len(policy.routes),
            "agent_guides": len(policy.agent_guides),
            "classification_depth": {
                "minimum": policy.classification_depth.minimum,
                "maximum": policy.classification_depth.maximum,
            },
            "max_depth": policy.max_depth,
        }
    except Exception as exc:
        _abort(_error(exc), command=command, json_output=json_output)
    if json_output:
        typer.echo(dump_envelope(envelope(command=command, success=True, data=data)))
    else:
        typer.echo("Outcome: passed")
        typer.echo(f"Policy: {data['path']}")
        typer.echo(f"Routes: {data['routes']}")
        typer.echo(f"Agent guides: {data['agent_guides']}")
        depth = data["classification_depth"]
        if not isinstance(depth, dict):
            raise AssertionError("classification depth output must be a mapping")
        typer.echo(f"Classification depth: {depth['minimum']}..{depth['maximum']}")
        typer.echo(f"Filesystem max depth: {data['max_depth']}")


@doc_app.command("agent-guide")
def doc_agent_guide_command(
    project: Annotated[Path, typer.Option("--project", "-C")] = Path("."),
    policy_file: Annotated[
        Path | None,
        typer.Option("--policy-file", help="Standalone DocumentLayoutPolicy YAML."),
    ] = None,
    guide_format: Annotated[
        Literal["claude", "agents"] | None,
        typer.Option("--format", help="Guide target format; inferred when writing."),
    ] = None,
    output_file: Annotated[
        Path | None,
        typer.Option(
            "--output-file",
            help="Insert or update the managed block in a configured guide target.",
        ),
    ] = None,
) -> None:
    """Render project-local instructions that teach Agents the document workflow."""

    try:
        repository, policy = _repository_and_policy(project, policy_file)
        if output_file is None:
            selected_format: AgentGuideFormat
            if guide_format is not None:
                selected_format = guide_format
            else:
                configured_formats = {target.format for target in policy.agent_guides}
                if len(configured_formats) > 1:
                    raise RCPError(
                        code="agent_guide_format_required",
                        message="Policy declares multiple Agent guide formats.",
                        remediation="Select one with --format.",
                    )
                selected_format = next(iter(configured_formats), "claude")
            typer.echo(
                render_project_agent_guide(policy, selected_format).decode("utf-8"),
                nl=False,
            )
            return
        destination, selected_format = _agent_guide_destination(
            repository,
            output_file,
            policy,
            guide_format,
        )
        _upsert_agent_guide(
            repository,
            destination,
            render_project_agent_guide(policy, selected_format),
            selected_format,
        )
    except Exception as exc:
        _abort(_error(exc), command="doc.agent-guide", json_output=False)


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
