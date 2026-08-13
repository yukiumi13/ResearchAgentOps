from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from researchctl.output import dump_envelope, envelope
from researchctl.request_io import read_json_request
from researchctl.services.factory import open_post_merge_application
from researchctl.services.post_merge import (
    PostMergeRequest,
    post_merge_request_from_artifact,
    write_post_merge_artifact,
)

MAX_INPUT_BYTES = 8 * 1024 * 1024
app = typer.Typer(
    name="researchctl-linear-host",
    help="One-shot trusted Linear deployment host.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)


@app.callback()
def root() -> None:
    """Run one trusted deployment operation."""


@app.command("shadow")
def shadow_command(
    project: Annotated[Path, typer.Option("--project", "-C")] = Path("."),
    dispatch_artifact: Annotated[Path, typer.Option("--dispatch-artifact")] = ...,
    output: Annotated[Path, typer.Option("--output")] = ...,
    merge_commit: Annotated[str | None, typer.Option("--merge-commit")] = None,
    json_input: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Validate one accepted merge without Linear access or live outbox writes."""

    command = "linear-host.shadow"
    try:
        content = _read_artifact(dispatch_artifact)
        if json_input:
            import sys

            request = read_json_request(sys.stdin.buffer, PostMergeRequest)
        else:
            if merge_commit is None:
                raise ValueError("human mode requires --merge-commit")
            request = post_merge_request_from_artifact(
                dispatch_artifact=content,
                merge_commit=merge_commit,
            )
        if request.mode != "shadow" or request.provenance != "local_shadow":
            raise ValueError("this host command accepts local shadow requests only")
        with open_post_merge_application(
            project,
            automation_identity="researchctl-post-merge-shadow",
        ) as handle:
            result = handle.service.post_merge_process(
                request=request,
                dispatch_artifact=content,
                actor=handle.actor,
            )
        receipt = write_post_merge_artifact(result, output)
        data = {**result.as_dict(), "output": receipt.as_dict()}
        if json_input:
            typer.echo(dump_envelope(envelope(command=command, success=True, data=data)))
            return
        typer.echo(f"State: {result.state}")
        typer.echo(f"Merge: {request.merge_commit}")
        typer.echo(f"Output: {receipt.path}")
    except typer.Exit:
        raise
    except Exception as exc:
        from researchctl.cli import _abort, _known_error

        error = _known_error(exc)
        if error is None:
            raise
        _abort(error, command=command, json_output=json_input)


def _read_artifact(path: Path) -> bytes:
    candidate = path.absolute()
    if candidate.is_symlink() or not candidate.is_file():
        raise ValueError("dispatch artifact must be a regular non-symlink file")
    if candidate.stat().st_size > MAX_INPUT_BYTES:
        raise ValueError("dispatch artifact exceeds the 8 MiB input limit")
    return candidate.read_bytes()


if __name__ == "__main__":
    app()
