from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Annotated, NoReturn

import typer
from pydantic import ValidationError

from researchctl.domain.models import AnalysisBrief, ResearchUpdate
from researchctl.errors import RCPError
from researchctl.output import dump_envelope, envelope, error_payload
from researchctl.serialization import SerializationError, load_model
from researchctl.services.generated_markdown import (
    atomic_replace_bytes,
    permits_generated_markdown_replacement,
)
from researchctl.services.research_writing import (
    WritingLintResult,
    lint_analysis_brief,
    lint_research_update,
    render_analysis_brief,
    render_research_update,
)

brief_app = typer.Typer(
    help="Lint and render concise analysis briefs.",
    no_args_is_help=True,
)
update_app = typer.Typer(
    help="Lint and render concise research status updates.",
    no_args_is_help=True,
)


def _error(exc: Exception) -> RCPError:
    if isinstance(exc, RCPError):
        return exc
    if isinstance(exc, ValidationError):
        return RCPError(
            code="validation_error",
            message="Writing contract schema validation failed.",
            remediation="Review the invalid fields and rerun the command.",
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
            remediation=exc.remediation or "Fix the reported canonical YAML error.",
            context=exc.context(),
        )
    if isinstance(exc, (OSError, ValueError)):
        return RCPError(
            code="invalid_local_state",
            message=str(exc),
            remediation="Check the input and output paths.",
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
    result: WritingLintResult,
    *,
    command: str,
    json_output: bool,
) -> None:
    if json_output:
        typer.echo(
            dump_envelope(
                envelope(
                    command=command,
                    success=result.passed,
                    data=result.as_dict(),
                )
            )
        )
    else:
        typer.echo(f"Kind: {result.kind}")
        typer.echo(f"Outcome: {result.terminal_result}")
        typer.echo(
            "Prose: "
            f"{result.prose.english_words}/{result.max_english_words} English words, "
            f"{result.prose.cjk_characters}/{result.max_cjk_characters} CJK characters"
        )
        for finding in result.findings:
            typer.echo(
                f"  invalid: {finding.field_path} "
                f"[{finding.code}] {finding.message}"
            )
    if not result.passed:
        raise typer.Exit(code=2)


def _write_or_echo(content: bytes, output_file: Path | None) -> None:
    if output_file is None or str(output_file) in {"-", "/dev/stdout", "/proc/self/fd/1"}:
        typer.echo(content.decode("utf-8"), nl=False)
        return
    if not output_file.parent.is_dir() or output_file.parent.is_symlink():
        raise RCPError(
            code="writing_output_path_invalid",
            message="Writing output parent must be an existing non-symlink directory.",
            context={"path": str(output_file)},
        )
    if output_file.exists() or output_file.is_symlink():
        regular_file = output_file.is_file() and not output_file.is_symlink()
        if regular_file and output_file.read_bytes() == content:
            typer.echo(f"Unchanged: {output_file}")
            return
        if regular_file:
            observed = output_file.read_bytes()
            if permits_generated_markdown_replacement(observed, content):
                atomic_replace_bytes(
                    output_file,
                    content,
                    stat.S_IMODE(output_file.stat().st_mode),
                )
                typer.echo(f"Updated: {output_file}")
                return
        raise RCPError(
            code="writing_output_conflict",
            message="Writing output path already contains different content.",
            remediation=(
                "Choose a new path, or restore an unedited renderer-owned output before "
                "refreshing it."
            ),
            context={"path": str(output_file)},
        )
    descriptor = os.open(output_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        output_file.unlink(missing_ok=True)
        raise
    typer.echo(f"Rendered: {output_file}")


@brief_app.command("lint")
def brief_lint_command(
    brief_file: Annotated[Path, typer.Argument(help="AnalysisBrief YAML record.")],
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit a stable JSON envelope."),
    ] = False,
) -> None:
    """Validate analysis structure, traceability, and prose budgets."""

    command = "brief.lint"
    try:
        brief = load_model(brief_file, AnalysisBrief)
        result = lint_analysis_brief(brief)
    except Exception as exc:
        _abort(_error(exc), command=command, json_output=json_output)
    _emit_lint(result, command=command, json_output=json_output)


@brief_app.command("render")
def brief_render_command(
    brief_file: Annotated[Path, typer.Argument(help="Linted AnalysisBrief YAML record.")],
    output_file: Annotated[
        Path | None,
        typer.Option("--output-file", help="Write deterministic Markdown to this path."),
    ] = None,
) -> None:
    """Render a passing AnalysisBrief as deterministic Markdown."""

    try:
        brief = load_model(brief_file, AnalysisBrief)
        _write_or_echo(render_analysis_brief(brief), output_file)
    except Exception as exc:
        _abort(_error(exc), command="brief.render", json_output=False)


@update_app.command("lint")
def update_lint_command(
    update_file: Annotated[Path, typer.Argument(help="ResearchUpdate YAML record.")],
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit a stable JSON envelope."),
    ] = False,
) -> None:
    """Validate one research delta and its Linear-sized prose budget."""

    command = "update.lint"
    try:
        update = load_model(update_file, ResearchUpdate)
        result = lint_research_update(update)
    except Exception as exc:
        _abort(_error(exc), command=command, json_output=json_output)
    _emit_lint(result, command=command, json_output=json_output)


@update_app.command("render")
def update_render_command(
    update_file: Annotated[Path, typer.Argument(help="Linted ResearchUpdate YAML record.")],
    output_file: Annotated[
        Path | None,
        typer.Option("--output-file", help="Write deterministic Linear Markdown here."),
    ] = None,
) -> None:
    """Render a passing ResearchUpdate as a deterministic Linear comment."""

    try:
        update = load_model(update_file, ResearchUpdate)
        _write_or_echo(render_research_update(update), output_file)
    except Exception as exc:
        _abort(_error(exc), command="update.render", json_output=False)
