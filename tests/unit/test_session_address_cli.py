from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from researchctl.cli import app
from researchctl.domain.enums import SessionState
from researchctl.services.requests import (
    SessionAddressRequest,
    SessionListRequest,
    SessionShowRequest,
)


SESSION_ID = "session_20260803T120000Z_" + "a" * 24
TASK_ID = "task_20260803T120000Z_" + "b" * 24
COMMIT = "c" * 40


@dataclass(frozen=True)
class Call:
    method: str
    request: object
    actor: object


class SpyService:
    def __init__(self) -> None:
        self.calls: list[Call] = []
        self.session = {
            "session_id": SESSION_ID,
            "task_id": TASK_ID,
            "state": "active",
            "host": "host-a\nspoofed",
            "branch": "research/task/test\n\x1b]8;;https://invalid.example\x07link",
            "worktree_path": "/worktrees/session",
            "continued_from": None,
            "tmux_session": "research-session",
            "agent": "codex",
            "native_session_id": "native\u202eidentity",
            "last_observed_at": "2026-08-03T12:00:00Z",
        }

    def session_list(self, request: object, actor: object) -> dict[str, object]:
        self.calls.append(Call("session_list", request, actor))
        return {"items": [self.session]}

    def session_show(self, request: object, actor: object) -> dict[str, object]:
        self.calls.append(Call("session_show", request, actor))
        return {"session": self.session}

    def session_address(self, request: object, actor: object) -> dict[str, object]:
        self.calls.append(Call("session_address", request, actor))
        assert isinstance(request, SessionAddressRequest)
        return {
            "command_header": (
                f"@{request.app} notify session:{request.session_id} "
                f"commit:{request.commit_sha}"
            ),
            "message_required": True,
            "session": self.session,
        }


class Handle:
    def __init__(self, service: SpyService, actor: object) -> None:
        self.service = service
        self.actor = actor

    def __enter__(self) -> Handle:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


@pytest.fixture
def cli_spy(monkeypatch: pytest.MonkeyPatch) -> tuple[SpyService, object]:
    from researchctl.services import factory

    service = SpyService()
    actor = object()
    monkeypatch.setattr(
        factory,
        "open_application",
        lambda _path, **_options: Handle(service, actor),
    )
    return service, actor


@pytest.mark.parametrize(
    ("human_args", "expected_request", "method"),
    [
        (
            [
                "session",
                "list",
                "--task",
                TASK_ID,
                "--state",
                "active",
                "--limit",
                "7",
            ],
            SessionListRequest(task_id=TASK_ID, state=SessionState.ACTIVE, limit=7),
            "session_list",
        ),
        (
            ["session", "show", SESSION_ID],
            SessionShowRequest(session_id=SESSION_ID),
            "session_show",
        ),
        (
            ["session", "address", SESSION_ID, "--commit", COMMIT],
            SessionAddressRequest(session_id=SESSION_ID, commit_sha=COMMIT),
            "session_address",
        ),
    ],
)
def test_session_human_and_strict_json_use_the_same_service_boundary(
    tmp_path: Path,
    cli_spy: tuple[SpyService, object],
    human_args: list[str],
    expected_request: SessionListRequest | SessionShowRequest | SessionAddressRequest,
    method: str,
) -> None:
    service, actor = cli_spy
    runner = CliRunner()
    human = runner.invoke(app, [*human_args, "--project", str(tmp_path)])

    assert human.exit_code == 0, human.output
    human_call = service.calls[-1]
    assert human_call == Call(method, expected_request, actor)

    machine = runner.invoke(
        app,
        ["session", human_args[1], "--json", "--project", str(tmp_path)],
        input=json.dumps(expected_request.model_dump(mode="json", exclude_none=True)),
    )
    assert machine.exit_code == 0, machine.output
    machine_call = service.calls[-1]
    assert machine_call == human_call
    output = json.loads(machine.output)
    assert output["command"] == f"session.{human_args[1]}"
    assert output["success"] is True
    assert output["errors"] == []

    if method == "session_address":
        assert human.output == (
            f"@researchctl-app notify session:{SESSION_ID} commit:{COMMIT}\n"
        )


def test_session_human_rendering_escapes_terminal_controls(
    tmp_path: Path,
    cli_spy: tuple[SpyService, object],
) -> None:
    del cli_spy
    runner = CliRunner()
    listed = runner.invoke(
        app,
        ["session", "list", "--project", str(tmp_path)],
    )
    shown = runner.invoke(
        app,
        ["session", "show", SESSION_ID, "--project", str(tmp_path)],
    )

    assert listed.exit_code == 0, listed.output
    assert shown.exit_code == 0, shown.output
    for output in (listed.output, shown.output):
        assert "\x1b" not in output
        assert "\x07" not in output
        assert "\u202e" not in output
    assert "host-a\\nspoofed" in listed.output
    assert "\\u001b" in shown.output
    assert "native\\u202eidentity" in shown.output


def test_session_address_json_rejects_authority_fields(
    tmp_path: Path,
    cli_spy: tuple[SpyService, object],
) -> None:
    service, _actor = cli_spy
    payload: dict[str, Any] = {
        "session_id": SESSION_ID,
        "commit_sha": COMMIT,
        "app": "researchctl-app",
        "actor_role": "manager",
    }

    result = CliRunner().invoke(
        app,
        ["session", "address", "--json", "--project", str(tmp_path)],
        input=json.dumps(payload),
    )

    assert result.exit_code != 0
    output = json.loads(result.output)
    assert output["errors"][0]["code"] == "validation_error"
    assert service.calls == []
