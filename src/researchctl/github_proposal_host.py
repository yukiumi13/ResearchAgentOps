from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Protocol

import typer

from researchctl.adapters._subprocess import CommandRunner, SubprocessCommandRunner
from researchctl.adapters.github_app import (
    GitHubAppInstallationCredential,
    GitHubAppTokenIssuer,
    isolated_github_app_environment,
)
from researchctl.adapters.github_submission import GitHubSubmissionDelivery
from researchctl.errors import RCPError
from researchctl.output import dump_envelope, envelope
from researchctl.request_io import read_json_request
from researchctl.runtime import RuntimeStore
from researchctl.services.actor import ActorRole
from researchctl.services.factory import actor_from_environment, open_application
from researchctl.services.project_runtime import ProjectRuntimeService
from researchctl.services.requests import SubmissionCreateRequest

app = typer.Typer(
    name="researchctl-github-proposal-host",
    help="One-shot trusted GitHub App proposal host.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)


class GitHubAppCredentialIssuer(Protocol):
    def issue(self, governance) -> GitHubAppInstallationCredential: ...


class PinnedGitCommandRunner:
    def __init__(
        self,
        executable: Path,
        *,
        delegate: CommandRunner | None = None,
    ) -> None:
        self._executable = executable
        self._delegate = delegate or SubprocessCommandRunner()

    def run(self, argv, *, cwd, env, timeout_seconds):
        if not argv or argv[0] != "git":
            raise RCPError(
                code="github_proposal_command_invalid",
                message="Trusted project locator attempted a non-Git command.",
            )
        return self._delegate.run(
            (str(self._executable), *argv[1:]),
            cwd=cwd,
            env=env,
            timeout_seconds=timeout_seconds,
        )


@app.callback()
def root() -> None:
    """Run one App-authored proposal operation outside the Agent process."""


@app.command("submit")
def submit_command(
    project: Annotated[Path, typer.Option("--project", "-C")] = Path("."),
    private_key: Annotated[Path, typer.Option("--private-key")] = ...,
    git_executable: Annotated[Path, typer.Option("--git-executable")] = ...,
    gh_executable: Annotated[Path, typer.Option("--gh-executable")] = ...,
) -> None:
    """Authenticate one Session request, then push and open its exact proposal."""

    command = "github-proposal-host.submit"
    try:
        request = read_json_request(sys.stdin.buffer, SubmissionCreateRequest)
        data = run_submission(
            project=project,
            private_key=private_key,
            request=request,
            source_environment=dict(os.environ),
            git_executable=git_executable,
            gh_executable=gh_executable,
        )
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
    except Exception as error:
        from researchctl.cli import _abort, _known_error

        known = _known_error(error)
        if known is None:
            raise
        _abort(known, command=command, json_output=True)


def run_submission(
    *,
    project: Path,
    private_key: Path,
    request: SubmissionCreateRequest,
    source_environment: Mapping[str, str],
    git_executable: Path = Path("/usr/bin/git"),
    gh_executable: Path = Path("/usr/bin/gh"),
    locator: ProjectRuntimeService | None = None,
    issuer: GitHubAppCredentialIssuer | None = None,
    application_opener=open_application,
) -> dict[str, object]:
    trusted_git = _trusted_executable(git_executable, label="Git")
    trusted_gh = _trusted_executable(gh_executable, label="GitHub CLI")
    selected_locator = locator or ProjectRuntimeService(
        runner=PinnedGitCommandRunner(trusted_git)
    )
    managed = selected_locator.discover(project)
    selected_locator.ensure_runtime_directories(managed)
    governance = managed.policy.github
    if governance is None:
        raise RCPError(
            code="submission_github_governance_not_configured",
            message="Accepted ProjectPolicy has no GitHub proposal identity policy.",
        )
    with RuntimeStore(managed.runtime.database_path) as runtime:
        actor = actor_from_environment(
            runtime,
            managed.project_id,
            environment=dict(source_environment),
        )
        if actor.role is not ActorRole.AGENT:
            raise RCPError(
                code="github_proposal_session_required",
                message="GitHub App proposal host requires an Agent Session capability.",
            )
        actor.require_session_scope(
            request.submission.session_id,
            command="github-proposal-host.submit",
            manager_allowed=False,
        )

    selected_issuer = issuer or GitHubAppTokenIssuer(
        private_key_path=private_key,
        forbidden_roots=(
            managed.repository_root,
            managed.runtime.git_common_dir,
            Path("/tmp"),
        ),
    )
    credential = selected_issuer.issue(governance)
    with isolated_github_app_environment(
        credential,
        object_directory=managed.runtime.git_common_dir / "objects",
    ) as isolated:
        identity = governance.repository
        delivery = GitHubSubmissionDelivery(
            accepted_remote_url=managed.project.repository.remote_url,
            governance=governance,
            git_environment=isolated.git(),
            identity_git_environment={
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_TERMINAL_PROMPT": "0",
                "PATH": os.defpath,
            },
            gh_environment=isolated.gh(),
            mutation_remote_url=f"https://github.com/{identity}.git",
            git_executable=str(trusted_git),
            gh_executable=str(trusted_gh),
        )
        actor_environment = {
            key: value
            for key, value in source_environment.items()
            if key in {"RESEARCHCTL_SESSION_ID", "RESEARCHCTL_SESSION_TOKEN"}
        }
        with application_opener(
            managed.repository_root,
            environment=actor_environment,
            submission_delivery=delivery,
            project_runtime_service=selected_locator,
        ) as handle:
            result = handle.service.submission_create(request, handle.actor)
    return {
        **result.data,
        "github_app": credential.public_receipt(),
    }


def _trusted_executable(path: Path, *, label: str) -> Path:
    if not path.is_absolute():
        raise RCPError(
            code="github_proposal_executable_invalid",
            message=f"Trusted {label} executable path must be absolute.",
        )
    candidate = path.absolute()
    try:
        info = candidate.stat()
    except OSError as error:
        raise RCPError(
            code="github_proposal_executable_invalid",
            message=f"Trusted {label} executable is unavailable.",
            context={"error_type": type(error).__name__},
        ) from error
    if (
        not candidate.is_file()
        or candidate.is_symlink()
        or info.st_uid == os.getuid()
        or info.st_mode & 0o022
        or os.access(candidate, os.W_OK)
    ):
        raise RCPError(
            code="github_proposal_executable_invalid",
            message=(
                f"Trusted {label} executable must be a non-symlink, "
                "administrator-owned file that the broker principal cannot modify."
            ),
        )
    if not os.access(candidate, os.X_OK):
        raise RCPError(
            code="github_proposal_executable_invalid",
            message=f"Trusted {label} executable is not executable.",
        )
    return candidate


if __name__ == "__main__":
    app()
