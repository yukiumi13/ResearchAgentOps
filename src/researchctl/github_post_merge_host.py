from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from researchctl.adapters.github_post_merge import GhApiPostMergeClient
from researchctl.output import dump_envelope, envelope
from researchctl.services.factory import open_post_merge_application
from researchctl.services.github_post_merge import AuthenticatedGitHubPostMergeBridge
from researchctl.services.post_merge import write_post_merge_artifact

app = typer.Typer(
    name="researchctl-github-post-merge",
    help="Authenticate one merged GitHub PR and enqueue its accepted result.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)


@app.callback(invoke_without_command=True)
def enqueue_command(
    project: Annotated[Path, typer.Option("--project", "-C")] = Path("."),
    repository: Annotated[str, typer.Option("--repository")] = ...,
    pull_request_number: Annotated[
        int,
        typer.Option("--pull-request-number", min=1),
    ] = ...,
    output: Annotated[Path | None, typer.Option("--output")] = None,
) -> None:
    """Authenticate GitHub provenance before creating the durable Linear outbox event."""

    command = "github-post-merge.enqueue"
    try:
        with open_post_merge_application(
            project,
            automation_identity="researchctl-github-post-merge",
        ) as handle:
            bridge = AuthenticatedGitHubPostMergeBridge(
                github=GhApiPostMergeClient(),
                application=handle.service,
                actor=handle.actor,
            )
            result = bridge.enqueue(
                repository=repository,
                pull_request_number=pull_request_number,
            )
        data = result.as_dict()
        if output is not None:
            data = {
                **data,
                "output": write_post_merge_artifact(result, output).as_dict(),
            }
        typer.echo(
            dump_envelope(
                envelope(
                    command=command,
                    success=True,
                    data=data,
                )
            )
        )
    except typer.Exit:
        raise
    except Exception as exc:
        from researchctl.cli import _abort, _known_error

        error = _known_error(exc)
        if error is None:
            raise
        _abort(error, command=command, json_output=True)


if __name__ == "__main__":
    app()
