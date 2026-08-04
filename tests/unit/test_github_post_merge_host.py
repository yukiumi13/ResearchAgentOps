from __future__ import annotations

import json
import tomllib
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

import researchctl.github_post_merge_host as host
from researchctl.errors import RCPError


class Handle:
    def __init__(self) -> None:
        self.service = object()
        self.actor = object()

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None


def test_github_post_merge_host_uses_trusted_bridge_and_stable_envelope(
    tmp_path: Path,
    monkeypatch,
) -> None:
    handle = Handle()
    opens: list[tuple[Path, str]] = []
    bridge_calls: list[tuple[object, object, object, str, int]] = []

    monkeypatch.setattr(
        host,
        "open_post_merge_application",
        lambda project, *, automation_identity: (
            opens.append((project, automation_identity)) or handle
        ),
    )
    github = object()
    monkeypatch.setattr(host, "GhApiPostMergeClient", lambda: github)

    class Bridge:
        def __init__(self, *, github, application, actor) -> None:
            self.github = github
            self.application = application
            self.actor = actor

        def enqueue(self, *, repository: str, pull_request_number: int):
            bridge_calls.append(
                (
                    self.github,
                    self.application,
                    self.actor,
                    repository,
                    pull_request_number,
                )
            )
            return SimpleNamespace(
                as_dict=lambda: {
                    "state": "queued",
                    "request": {"repository": repository},
                }
            )

    monkeypatch.setattr(host, "AuthenticatedGitHubPostMergeBridge", Bridge)

    result = CliRunner().invoke(
        host.app,
        [
            "--project",
            str(tmp_path),
            "--repository",
            "owner/repository",
            "--pull-request-number",
            "17",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["command"] == "github-post-merge.enqueue"
    assert payload["success"] is True
    assert payload["data"]["state"] == "queued"
    assert opens == [(tmp_path, "researchctl-github-post-merge")]
    assert bridge_calls == [
        (github, handle.service, handle.actor, "owner/repository", 17)
    ]


def test_github_post_merge_host_returns_known_error_envelope(
    tmp_path: Path,
    monkeypatch,
) -> None:
    handle = Handle()
    monkeypatch.setattr(
        host,
        "open_post_merge_application",
        lambda *_args, **_kwargs: handle,
    )
    monkeypatch.setattr(host, "GhApiPostMergeClient", object)

    class Bridge:
        def __init__(self, **_kwargs) -> None:
            pass

        def enqueue(self, **_kwargs):
            raise RCPError(
                code="github_post_merge_workflow_invalid",
                message="The trusted workflow identity did not match.",
            )

    monkeypatch.setattr(host, "AuthenticatedGitHubPostMergeBridge", Bridge)
    result = CliRunner().invoke(
        host.app,
        [
            "--project",
            str(tmp_path),
            "--repository",
            "owner/repository",
            "--pull-request-number",
            "17",
        ],
    )

    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert payload["success"] is False
    assert payload["errors"][0]["code"] == "github_post_merge_workflow_invalid"


def test_github_post_merge_host_is_installed_as_a_project_script() -> None:
    root = Path(__file__).resolve().parents[2]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))

    assert project["project"]["scripts"]["researchctl-github-post-merge"] == (
        "researchctl.github_post_merge_host:app"
    )

    help_result = CliRunner().invoke(host.app, ["--help"])
    assert help_result.exit_code == 0
    lowered = help_result.output.lower()
    assert "token" not in lowered
    assert "secret" not in lowered
