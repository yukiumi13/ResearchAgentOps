from __future__ import annotations

import json
import unicodedata
from pathlib import Path
from typing import Annotated, Any

import typer

from researchctl.domain.ids import new_id
from researchctl.domain.models import SessionNotificationOrigin
from researchctl.phase2_cli import (
    _input_error,
    _operation_fields,
    _required,
    _run_command,
)
from researchctl.services.requests import (
    NotificationAckRequest,
    NotificationListRequest,
    NotificationReplyRequest,
    NotificationSendRequest,
)


notification_app = typer.Typer(
    help="Address durable messages to one governed Session.",
    no_args_is_help=True,
)


def _single_line_json_text(value: str) -> str:
    """Render untrusted text without emitting terminal control characters."""

    rendered = json.dumps(value, ensure_ascii=False)
    unsafe_categories = {"Cc", "Cf", "Cs", "Zl", "Zp"}
    return "".join(
        json.dumps(character, ensure_ascii=True)[1:-1]
        if unicodedata.category(character) in unsafe_categories
        else character
        for character in rendered
    )


def _body_value(
    value: str | None,
    path: Path | None,
    *,
    option: str,
) -> str:
    if value is not None and path is not None:
        raise _input_error(f"Use either {option} or {option}-file, not both.")
    if path is not None:
        return path.read_text(encoding="utf-8")
    return _required(value, option)


def _render_notification_list(data: dict[str, Any]) -> None:
    items = data.get("items")
    if not isinstance(items, list) or not items:
        typer.echo("Notification inbox is clear.")
        return
    for item in items:
        typer.echo(
            f"[{item['route']}/{item['state']}] {item['notification_id']} "
            f"revision {item['revision']}"
        )
        typer.echo(f"  Session: {item['session_id']}")
        typer.echo(f"  Commit: {item['commit_sha']}")
        typer.echo(f"  Message: {_single_line_json_text(item['message'])}")


@notification_app.command("send")
def notification_send_command(
    session_id: Annotated[
        str | None,
        typer.Argument(help="Exact target Session ID."),
    ] = None,
    project: Annotated[Path, typer.Option("--project", "-C")] = Path("."),
    json_input: Annotated[bool, typer.Option("--json")] = False,
    task_id: Annotated[str | None, typer.Option("--task-id")] = None,
    commit_sha: Annotated[str | None, typer.Option("--commit")] = None,
    message: Annotated[str | None, typer.Option("--message")] = None,
    message_file: Annotated[Path | None, typer.Option("--message-file")] = None,
    workspace_id: Annotated[str | None, typer.Option("--workspace-id")] = None,
    issue_id: Annotated[str | None, typer.Option("--issue-id")] = None,
    thread_id: Annotated[str | None, typer.Option("--thread-id")] = None,
    comment_id: Annotated[str | None, typer.Option("--comment-id")] = None,
    notification_id: Annotated[
        str | None,
        typer.Option("--notification-id"),
    ] = None,
    operation_id: Annotated[str | None, typer.Option("--operation-id")] = None,
    idempotency_key: Annotated[str | None, typer.Option("--idempotency-key")] = None,
) -> None:
    """Ingest an authenticated Linear request into one Session inbox."""

    def human_request() -> NotificationSendRequest:
        return NotificationSendRequest(
            **_operation_fields(operation_id, idempotency_key),
            notification_id=notification_id or new_id("notification"),
            task_id=_required(task_id, "--task-id"),
            session_id=_required(session_id, "SESSION_ID"),
            commit_sha=_required(commit_sha, "--commit"),
            message=_body_value(message, message_file, option="--message"),
            origin=SessionNotificationOrigin(
                workspace_id=_required(workspace_id, "--workspace-id"),
                issue_id=_required(issue_id, "--issue-id"),
                thread_id=_required(thread_id, "--thread-id"),
                comment_id=_required(comment_id, "--comment-id"),
            ),
        )

    _run_command(
        command="notification.send",
        method_name="notification_send",
        project=project,
        json_input=json_input,
        request_model=NotificationSendRequest,
        human_builder=human_request,
    )


@notification_app.command("list")
def notification_list_command(
    session_id: Annotated[
        str | None,
        typer.Argument(help="Optional Session filter."),
    ] = None,
    project: Annotated[Path, typer.Option("--project", "-C")] = Path("."),
    json_input: Annotated[bool, typer.Option("--json")] = False,
    manager_exceptions_only: Annotated[
        bool,
        typer.Option("--manager-exceptions"),
    ] = False,
    include_closed: Annotated[bool, typer.Option("--include-closed")] = False,
    limit: Annotated[int, typer.Option("--limit", min=1, max=1000)] = 100,
) -> None:
    """Read the bound Session inbox or the manager fallback inbox."""

    _run_command(
        command="notification.list",
        method_name="notification_list",
        project=project,
        json_input=json_input,
        request_model=NotificationListRequest,
        human_builder=lambda: NotificationListRequest(
            session_id=session_id,
            manager_exceptions_only=manager_exceptions_only,
            include_closed=include_closed,
            limit=limit,
        ),
        human_renderer=_render_notification_list,
    )


@notification_app.command("ack")
def notification_ack_command(
    notification_id: Annotated[
        str | None,
        typer.Argument(help="Notification to acknowledge."),
    ] = None,
    project: Annotated[Path, typer.Option("--project", "-C")] = Path("."),
    json_input: Annotated[bool, typer.Option("--json")] = False,
    expected_revision: Annotated[
        int | None,
        typer.Option("--expected-revision", min=1),
    ] = None,
    operation_id: Annotated[str | None, typer.Option("--operation-id")] = None,
    idempotency_key: Annotated[str | None, typer.Option("--idempotency-key")] = None,
) -> None:
    """Acknowledge receipt using the revision shown by notification list."""

    def human_request() -> NotificationAckRequest:
        if expected_revision is None:
            raise _input_error(
                "Human mode requires --expected-revision.",
                option="--expected-revision",
            )
        return NotificationAckRequest(
            **_operation_fields(operation_id, idempotency_key),
            notification_id=_required(notification_id, "NOTIFICATION_ID"),
            expected_revision=expected_revision,
        )

    _run_command(
        command="notification.ack",
        method_name="notification_ack",
        project=project,
        json_input=json_input,
        request_model=NotificationAckRequest,
        human_builder=human_request,
    )


@notification_app.command("reply")
def notification_reply_command(
    notification_id: Annotated[
        str | None,
        typer.Argument(help="Notification whose Linear thread receives the reply."),
    ] = None,
    project: Annotated[Path, typer.Option("--project", "-C")] = Path("."),
    json_input: Annotated[bool, typer.Option("--json")] = False,
    expected_revision: Annotated[
        int | None,
        typer.Option("--expected-revision", min=1),
    ] = None,
    body: Annotated[str | None, typer.Option("--body")] = None,
    body_file: Annotated[Path | None, typer.Option("--body-file")] = None,
    reply_id: Annotated[str | None, typer.Option("--reply-id")] = None,
    operation_id: Annotated[str | None, typer.Option("--operation-id")] = None,
    idempotency_key: Annotated[str | None, typer.Option("--idempotency-key")] = None,
) -> None:
    """Queue one reply to the originating Linear thread."""

    def human_request() -> NotificationReplyRequest:
        if expected_revision is None:
            raise _input_error(
                "Human mode requires --expected-revision.",
                option="--expected-revision",
            )
        return NotificationReplyRequest(
            **_operation_fields(operation_id, idempotency_key),
            notification_id=_required(notification_id, "NOTIFICATION_ID"),
            expected_revision=expected_revision,
            reply_id=reply_id or new_id("reply"),
            body=_body_value(body, body_file, option="--body"),
        )

    _run_command(
        command="notification.reply",
        method_name="notification_reply",
        project=project,
        json_input=json_input,
        request_model=NotificationReplyRequest,
        human_builder=human_request,
    )
