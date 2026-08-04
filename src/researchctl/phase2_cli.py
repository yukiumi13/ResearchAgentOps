from __future__ import annotations

import json
import shlex
import socket
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, TypeVar

import typer
from click import Choice
from pydantic import BaseModel

from researchctl.domain.enums import InputKind, Priority, SessionState, StatusKind
from researchctl.domain.ids import new_id
from researchctl.domain.models import (
    DecisionRequest,
    ExecutionPreferences,
    InputIdentity,
    LinearProjectionPolicy,
    RunSpec,
    StatusEvidence,
    StatusUpdate,
    TaskRecord,
)
from researchctl.domain.types import utc_now
from researchctl.errors import RCPError
from researchctl.output import dump_envelope, envelope
from researchctl.request_io import read_json_request
from researchctl.runtime import SessionNotification
from researchctl.serialization import load_model
from researchctl.services.application import ServiceResult
from researchctl.services.requests import (
    AgentKind,
    BootstrapAcceptRequest,
    BootstrapProposalRequest,
    InboxAckRequest,
    InboxListRequest,
    InboxResolveRequest,
    InboxSnoozeRequest,
    LinearConfigureRequest,
    LinearDeliveryListRequest,
    LinearDeliveryShowRequest,
    MutationRequest,
    RunCollectRequest,
    RunRetryRequest,
    RunStartRequest,
    SessionAddressRequest,
    SessionAttachRequest,
    SessionContinueRequest,
    SessionListRequest,
    SessionPauseRequest,
    SessionShowRequest,
    SessionStartRequest,
    StatusPublishRequest,
    TaskCancelRequest,
    TaskCreateRequest,
    TaskUpdateRequest,
)


bootstrap_app = typer.Typer(
    help="Prepare explicit Project bootstrap changes.",
    no_args_is_help=True,
)
task_app = typer.Typer(help="Create and revise governed Tasks.", no_args_is_help=True)
session_app = typer.Typer(help="Start and inspect Agent Sessions.", no_args_is_help=True)
status_app = typer.Typer(help="Publish structured Agent status.", no_args_is_help=True)
inbox_app = typer.Typer(help="Review exception-oriented attention.", no_args_is_help=True)
run_app = typer.Typer(help="Execute immutable governed Runs.", no_args_is_help=True)
linear_app = typer.Typer(
    help="Configure the governed Linear projection policy.",
    no_args_is_help=True,
)
linear_delivery_app = typer.Typer(
    help="Inspect local Linear delivery state.",
    no_args_is_help=True,
)
linear_app.add_typer(linear_delivery_app, name="delivery")

RequestT = TypeVar("RequestT", bound=BaseModel)
HumanBuilder = Callable[[], RequestT]
HumanRenderer = Callable[[dict[str, Any]], None]

_INBOX_GROUPS = (
    ("needs_decision", "Needs Decision", frozenset({"needs_decision", "needs_input"})),
    ("blocked", "Blocked", frozenset({"blocked"})),
    ("needs_review", "Needs Review", frozenset({"needs_review"})),
    (
        "stale_or_needs_rerun",
        "Stale or Needs Rerun",
        frozenset({"stale_or_needs_rerun"}),
    ),
    ("failed_or_lost", "Failed or Lost", frozenset({"failed_or_lost"})),
    ("running", "Running", frozenset({"running"})),
    ("waiting", "Waiting", frozenset({"waiting"})),
)

_TASK_COMMANDS = frozenset({"task.create", "task.update", "task.cancel"})
_BOOTSTRAP_COMMANDS = frozenset({"bootstrap.accept", "bootstrap.propose"})
_RUN_COMMANDS = frozenset({"run.start", "run.retry", "run.collect"})
_LINEAR_COMMANDS = frozenset({"linear.configure"})


def _input_error(message: str, *, option: str | None = None) -> RCPError:
    context = {"option": option} if option is not None else {}
    return RCPError(
        code="invalid_human_request",
        message=message,
        remediation="Review the command flags or use --json with a complete request object.",
        context=context,
    )


def _required(value: str | None, option: str) -> str:
    if value is None or not value.strip():
        raise _input_error(f"Human mode requires {option}.", option=option)
    return value


def _required_many(values: list[str] | None, option: str) -> tuple[str, ...]:
    selected = tuple(value for value in (values or ()) if value.strip())
    if not selected:
        raise _input_error(
            f"Human mode requires at least one {option}.",
            option=option,
        )
    return selected


def _prompt_many(prompt: str) -> tuple[str, ...]:
    values = [typer.prompt(prompt)]
    while typer.confirm("Add another?", default=False):
        values.append(typer.prompt(prompt))
    return tuple(values)


def _prompt_optional(prompt: str) -> str | None:
    value = typer.prompt(prompt, default="", show_default=False).strip()
    return value or None


def _guided_required_inputs() -> tuple[InputIdentity, ...]:
    inputs: list[InputIdentity] = []
    while typer.confirm("Add a required input?", default=False):
        kind = typer.prompt(
            "Input kind",
            type=Choice([item.value for item in InputKind], case_sensitive=False),
        )
        logical_id = typer.prompt("Input logical ID")
        identity_kind = typer.prompt(
            "Input identity",
            type=Choice(["version", "digest"], case_sensitive=False),
        )
        identity_value = typer.prompt(
            "Immutable version" if identity_kind == "version" else "SHA-256 digest"
        )
        inputs.append(
            InputIdentity(
                kind=kind,
                logical_id=logical_id,
                version=identity_value if identity_kind == "version" else None,
                digest=identity_value if identity_kind == "digest" else None,
                uri=_prompt_optional("Input URI (optional)"),
                resolver=_prompt_optional("Resolver policy (optional)"),
                waiver_allowed=typer.confirm("May a manager waive this input?", default=False),
            )
        )
    return tuple(inputs)


def _load_required_inputs(paths: list[Path] | None) -> tuple[InputIdentity, ...]:
    return tuple(load_model(path, InputIdentity) for path in (paths or ()))


def _operation_fields(
    operation_id: str | None,
    idempotency_key: str | None,
) -> dict[str, str]:
    selected_operation = operation_id or new_id("operation")
    return {
        "operation_id": selected_operation,
        "idempotency_key": idempotency_key or f"human:{selected_operation}",
    }


def _prompt_value(prompt: str | None, prompt_file: Path | None) -> str:
    if prompt is not None and prompt_file is not None:
        raise _input_error("Use either --prompt or --prompt-file, not both.")
    if prompt_file is not None:
        return prompt_file.read_text(encoding="utf-8")
    return _required(prompt, "--prompt")


def _machine_or_human_request(
    json_input: bool,
    model: type[RequestT],
    human_builder: HumanBuilder[RequestT],
) -> RequestT:
    if json_input:
        return read_json_request(sys.stdin.buffer, model)
    return human_builder()


def _attention_data(item: Any) -> dict[str, Any]:
    def timestamp(value: datetime | None) -> str | None:
        if value is None:
            return None
        timespec = "microseconds" if value.microsecond else "seconds"
        return value.isoformat(timespec=timespec).replace("+00:00", "Z")

    return {
        "attention_key": item.dedupe_key,
        "generation": item.generation,
        "state": item.state,
        "kind": item.kind,
        "priority": item.priority,
        "task_id": item.task_id,
        "session_id": item.session_id,
        "current_update": item.current_update.model_dump(mode="json", exclude_none=True),
        "first_seen_at": timestamp(item.first_seen_at),
        "last_seen_at": timestamp(item.last_seen_at),
        "acknowledged_by": item.acknowledged_by,
        "acknowledged_at": timestamp(item.acknowledged_at),
        "snoozed_by": item.snoozed_by,
        "snoozed_until": timestamp(item.snoozed_until),
        "resolved_by": item.resolved_by,
        "resolved_at": timestamp(item.resolved_at),
        "resolution_reason": item.resolution_reason,
    }


def _result_data(value: Any) -> dict[str, Any]:
    if isinstance(value, ServiceResult):
        return value.as_dict()
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, tuple):
        if value and all(isinstance(item, SessionNotification) for item in value):
            return {"items": [_notification_item_data(item) for item in value]}
        items = [_attention_data(item) for item in value]
        groups = []
        for group_id, title, kinds in _INBOX_GROUPS:
            grouped = [item for item in items if item["kind"] in kinds]
            if grouped:
                groups.append(
                    {
                        "group": group_id,
                        "title": title,
                        "count": len(grouped),
                        "items": grouped,
                    }
                )
        return {"items": items, "groups": groups}
    if isinstance(value, dict):
        return value
    raise TypeError(f"unsupported ApplicationService result: {type(value).__name__}")


def _notification_item_data(item: SessionNotification) -> dict[str, Any]:
    def timestamp(value: datetime | None) -> str | None:
        if value is None:
            return None
        timespec = "microseconds" if value.microsecond else "seconds"
        return value.isoformat(timespec=timespec).replace("+00:00", "Z")

    return {
        "notification_id": item.notification_id,
        "task_id": item.task_id,
        "session_id": item.session_id,
        "commit_sha": item.commit_sha,
        "message": item.message,
        "origin": item.origin.model_dump(mode="json", exclude_none=True),
        "route": item.route.value,
        "state": item.state.value,
        "revision": item.revision,
        "created_at": timestamp(item.created_at),
        "routed_at": timestamp(item.routed_at),
        "fallback_reason": item.fallback_reason,
        "acknowledged_by": item.acknowledged_by,
        "acknowledged_at": timestamp(item.acknowledged_at),
        "reply_id": item.reply_id,
        "replied_by": item.replied_by,
        "replied_at": timestamp(item.replied_at),
    }


def _render_result(data: dict[str, Any]) -> None:
    operation_id = data.get("operation_id")
    terminal_result = data.get("terminal_result")
    if operation_id is not None and terminal_result is not None:
        typer.echo(f"{terminal_result}: {operation_id}")
    task = data.get("task")
    if isinstance(task, dict):
        typer.echo(f"Task: {task.get('key')} ({task.get('task_id')})")
        typer.echo(f"State: {task.get('state')}")
        if data.get("path") is not None:
            typer.echo(f"Record: {data['path']}")
        proposal = data.get("proposal")
        if isinstance(proposal, dict):
            typer.echo(f"Proposal branch: {proposal.get('branch')}")
            typer.echo(f"Proposal commit: {proposal.get('commit')}")
            typer.echo(f"Proposal worktree: {proposal.get('worktree')}")
            if proposal.get("delivery") == "local_control_change":
                typer.echo(
                    "Next: push the proposal branch and open a reviewed PR "
                    "against the accepted branch."
                )
    linear_policy = data.get("linear_policy")
    if isinstance(linear_policy, dict):
        typer.echo(f"Linear workspace: {linear_policy.get('workspace_id')}")
        typer.echo(f"Policy digest: {data.get('policy_digest')}")
        proposal = data.get("proposal")
        if isinstance(proposal, dict):
            typer.echo(f"Proposal branch: {proposal.get('branch')}")
            typer.echo(f"Proposal commit: {proposal.get('commit')}")
            typer.echo(f"Proposal worktree: {proposal.get('worktree')}")
            if proposal.get("effect_applied"):
                typer.echo(
                    "Next: push the proposal branch and open a reviewed PR "
                    "against the accepted branch."
                )
    session = data.get("session")
    if isinstance(session, dict):
        typer.echo(f"Session: {session.get('session_id')}")
        typer.echo(f"State: {session.get('state')} on {session.get('host')}")
    update = data.get("update")
    if isinstance(update, dict):
        typer.echo(f"Update: {update.get('update_id')} ({update.get('status')})")
    attention = data.get("attention")
    if isinstance(attention, dict):
        typer.echo(
            f"Attention: {attention.get('current_update_id')} "
            f"generation {attention.get('generation')} ({attention.get('state')})"
        )
    if data.get("bootstrap_id") is not None:
        proposal = data.get("proposal")
        if isinstance(proposal, dict):
            typer.echo(f"Bootstrap: {data['bootstrap_id']}")
            typer.echo(f"Proposal branch: {proposal.get('branch')}")
            typer.echo(f"Proposal commit: {proposal.get('commit')}")
            typer.echo(f"Proposal worktree: {proposal.get('worktree')}")
            if proposal.get("proposal_only") is True:
                typer.echo(
                    "Next: review this proposal, then run bootstrap accept "
                    "with its exact commit."
                )
            else:
                typer.echo(
                    "Next: push this branch and merge its reviewed PR; "
                    "the Project remains bootstrapping until that merge."
                )
    run = data.get("run")
    if isinstance(run, dict):
        typer.echo(f"Run: {run.get('run_id')}")
        typer.echo(f"Attempt: {run.get('attempt_id')}")
        result = run.get("result")
        if isinstance(result, dict):
            typer.echo(f"Outcome: {result.get('outcome')}")
        frozen = run.get("frozen")
        if isinstance(frozen, dict):
            typer.echo(f"Frozen tag: {frozen.get('tag')}")
    submission = data.get("submission")
    if isinstance(submission, dict):
        proposal = submission.get("proposal")
        bundle = submission.get("bundle")
        delivery = submission.get("delivery")
        if isinstance(bundle, dict):
            typer.echo(f"Submission: {bundle.get('submission_id')}")
            typer.echo(f"Bundle digest: {bundle.get('manifest_digest')}")
        if isinstance(proposal, dict):
            typer.echo(f"Proposal branch: {proposal.get('branch')}")
            typer.echo(f"Proposal commit: {proposal.get('commit')}")
        if isinstance(delivery, dict):
            pull_request = delivery.get("pull_request")
            if isinstance(pull_request, dict):
                typer.echo(
                    "Pull request: "
                    f"#{_terminal_text(pull_request.get('number'))} "
                    f"{_terminal_text(pull_request.get('url'))}"
                )
        typer.echo(
            "Proposal opened: human review, exact-head CI, approval, and merge "
            "are still required."
        )
    impact = data.get("impact")
    if isinstance(impact, dict):
        proposal = impact.get("proposal")
        bundle = impact.get("bundle")
        delivery = impact.get("delivery")
        if isinstance(bundle, dict):
            typer.echo(f"Impact: {bundle.get('impact_id')}")
            if bundle.get("report_count") is not None:
                typer.echo(f"Reports proposed: {bundle.get('report_count')}")
            else:
                typer.echo(
                    f"Report: {bundle.get('report_id')} revision "
                    f"{bundle.get('proposed_report_revision')}"
                )
                typer.echo(f"Outcome: {bundle.get('outcome')}")
        analysis = impact.get("analysis")
        if isinstance(analysis, dict):
            typer.echo(f"Reports scanned: {len(analysis.get('report_ids') or [])}")
            unresolved = analysis.get("unresolved_report_ids") or []
            if unresolved:
                typer.echo(f"Reports unresolved: {len(unresolved)}")
        if isinstance(proposal, dict):
            typer.echo(f"Proposal branch: {proposal.get('branch')}")
            typer.echo(f"Proposal commit: {proposal.get('commit')}")
        if isinstance(delivery, dict):
            pull_request = delivery.get("pull_request")
            if isinstance(pull_request, dict):
                typer.echo(
                    "Pull request: "
                    f"#{_terminal_text(pull_request.get('number'))} "
                    f"{_terminal_text(pull_request.get('url'))}"
                )
        if impact.get("terminal_result") == "impact_unresolved":
            typer.echo(
                "Impact unresolved: external dependency evidence is incomplete; "
                "no Report validity changed and no experiment was started."
            )
        elif impact.get("terminal_result") == "no_change":
            typer.echo(
                "No Impact proposal was needed; no Report changed and no "
                "experiment was started."
            )
        else:
            typer.echo(
                "Impact proposed only: no Report validity changed and no experiment "
                "was started."
            )
    impact_decision = data.get("impact_decision")
    if isinstance(impact_decision, dict):
        proposal = impact_decision.get("proposal")
        bundle = impact_decision.get("bundle")
        delivery = impact_decision.get("delivery")
        if isinstance(bundle, dict):
            typer.echo(f"Decision: {bundle.get('decision_id')}")
            typer.echo(
                f"Report: {bundle.get('report_id')} revision "
                f"{bundle.get('report_revision')}"
            )
            typer.echo(f"Disposition: {bundle.get('disposition')}")
        if isinstance(proposal, dict):
            typer.echo(f"Proposal branch: {proposal.get('branch')}")
            typer.echo(f"Proposal commit: {proposal.get('commit')}")
        if isinstance(delivery, dict):
            pull_request = delivery.get("pull_request")
            if isinstance(pull_request, dict):
                typer.echo(
                    "Pull request: "
                    f"#{_terminal_text(pull_request.get('number'))} "
                    f"{_terminal_text(pull_request.get('url'))}"
                )
        typer.echo(
            "Decision proposed only: protected review and merge are required; "
            "no experiment was started."
        )
    review = data.get("review")
    if isinstance(review, dict):
        proposal = review.get("proposal")
        bundle = review.get("bundle")
        if isinstance(bundle, dict):
            typer.echo(f"Submission: {bundle.get('submission_id')}")
            typer.echo(
                f"Report: {bundle.get('report_id')} revision "
                f"{bundle.get('report_revision')}"
            )
        if isinstance(proposal, dict):
            typer.echo(f"Acceptance commit: {proposal.get('commit')}")
        typer.echo(
            "Prepared only: exact-head CI, CODEOWNER approval, and merge are "
            "still required."
        )
    notification = data.get("notification")
    if isinstance(notification, dict):
        typer.echo(f"Notification: {notification.get('notification_id')}")
        typer.echo(
            f"Route: {notification.get('route')} "
            f"({notification.get('state')}, revision {notification.get('revision')})"
        )
        typer.echo(f"Session: {notification.get('session_id')}")
        typer.echo(f"Commit: {notification.get('commit_sha')}")
    reply = data.get("reply")
    if isinstance(reply, dict):
        typer.echo(f"Reply: {reply.get('reply_id')}")
    notification_outbox = data.get("outbox")
    if isinstance(notification_outbox, dict):
        typer.echo(
            f"Outbox: {notification_outbox.get('outbox_id')} "
            f"({notification_outbox.get('state')})"
        )


def _render_inbox(data: dict[str, Any]) -> None:
    groups = data.get("groups")
    if not isinstance(groups, list) or not groups:
        typer.echo("Inbox is clear.")
        return
    for group in groups:
        items = group.get("items")
        if not isinstance(items, list):
            raise TypeError("inbox result contains a malformed group")
        typer.echo(f"{_terminal_text(group.get('title'))} ({len(items)})")
        for item in items:
            update = item.get("current_update")
            if not isinstance(update, dict):
                raise TypeError("inbox result contains a malformed item")
            state = "" if item.get("state") == "open" else f" [{item.get('state')}]"
            typer.echo(f"  {_terminal_text(update.get('summary'))}{state}")
            typer.echo(
                "    "
                f"Task {_terminal_text(item.get('task_id'))} | "
                f"Session {_terminal_text(item.get('session_id'))} | "
                f"Update {_terminal_text(update.get('update_id'))} | "
                f"Generation {_terminal_text(item.get('generation'))}"
            )
            if update.get("blocker_detail") is not None:
                typer.echo(f"    Blocker: {_terminal_text(update['blocker_detail'])}")
            decision = update.get("decision_needed")
            if isinstance(decision, dict):
                typer.echo(f"    Decision: {_terminal_text(decision.get('question'))}")
                options = decision.get("options")
                if isinstance(options, list):
                    typer.echo(
                        "    Options: "
                        + " | ".join(_terminal_text(option) for option in options)
                    )
            if update.get("suggested_next_action") is not None:
                typer.echo(
                    f"    Next: {_terminal_text(update['suggested_next_action'])}"
                )


def _render_attach(data: dict[str, Any]) -> None:
    argv = data.get("attach_argv")
    if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
        raise RCPError(
            code="invalid_attach_command",
            message="Session attach did not return a valid argv.",
        )
    typer.echo(f"Attach: {shlex.join(argv)}")
    completed = subprocess.run(argv, check=False)
    if completed.returncode:
        raise typer.Exit(code=completed.returncode)


def _terminal_text(value: object) -> str:
    """Escape runtime-controlled values before writing them to a terminal."""

    if value is None:
        return "-"
    return json.dumps(str(value), ensure_ascii=True)[1:-1]


def _render_session_list(data: dict[str, Any]) -> None:
    items = data.get("items")
    if not isinstance(items, list) or not items:
        typer.echo("No Sessions found.")
        return
    for item in items:
        if not isinstance(item, dict):
            raise TypeError("session.list returned a malformed item")
        typer.echo(
            f"{_terminal_text(item.get('session_id'))} "
            f"[{_terminal_text(item.get('state'))}] "
            f"task={_terminal_text(item.get('task_id'))} "
            f"host={_terminal_text(item.get('host'))}"
        )


def _render_session_show(data: dict[str, Any]) -> None:
    session = data.get("session")
    if not isinstance(session, dict):
        raise TypeError("session.show returned a malformed Session")
    fields = (
        ("Session", "session_id"),
        ("Task", "task_id"),
        ("State", "state"),
        ("Host", "host"),
        ("Branch", "branch"),
        ("Updated", "last_observed_at"),
        ("Agent", "agent"),
        ("Native", "native_session_id"),
    )
    for label, key in fields:
        typer.echo(f"{label}: {_terminal_text(session.get(key))}")


def _render_session_address(data: dict[str, Any]) -> None:
    command_header = data.get("command_header")
    if not isinstance(command_header, str) or data.get("message_required") is not True:
        raise TypeError("session.address returned a malformed command header")
    typer.echo(_terminal_text(command_header))


def _render_linear_delivery_summary(delivery: dict[str, Any]) -> None:
    typer.echo(
        f"{_terminal_text(delivery.get('outbox_id'))} "
        f"[{_terminal_text(delivery.get('state'))}] "
        f"topic={_terminal_text(delivery.get('topic'))}"
    )
    typer.echo(
        f"  Created: {_terminal_text(delivery.get('created_at'))} "
        f"age={_terminal_text(delivery.get('age_seconds'))}s "
        f"attempts={_terminal_text(delivery.get('attempt_count'))}"
    )
    if delivery.get("last_error_code") is not None:
        typer.echo(f"  Last error: {_terminal_text(delivery['last_error_code'])}")
    claim = delivery.get("active_claim")
    if isinstance(claim, dict):
        typer.echo(
            f"  Active claim: {_terminal_text(claim.get('claim_id'))} "
            f"until {_terminal_text(claim.get('expires_at'))}"
        )
    receipt = delivery.get("receipt")
    if isinstance(receipt, dict):
        typer.echo(f"  Receipt: {_terminal_text(receipt.get('receipt_id'))}")


def _render_linear_delivery_list(data: dict[str, Any]) -> None:
    items = data.get("items")
    if not isinstance(items, list) or not items:
        typer.echo("No Linear deliveries found.")
        return
    for item in items:
        if not isinstance(item, dict):
            raise TypeError("linear.delivery.list returned a malformed item")
        _render_linear_delivery_summary(item)


def _render_linear_delivery_show(data: dict[str, Any]) -> None:
    delivery = data.get("delivery")
    if not isinstance(delivery, dict):
        raise TypeError("linear.delivery.show returned a malformed delivery")
    _render_linear_delivery_summary(delivery)
    typer.echo(f"Aggregate: {_terminal_text(delivery.get('aggregate_id'))}")
    typer.echo(
        "Pending age: "
        + (
            f"{_terminal_text(delivery.get('pending_age_seconds'))}s"
            if delivery.get("pending_age_seconds") is not None
            else "-"
        )
    )
    typer.echo(f"Last claim: {_terminal_text(delivery.get('last_claim_id'))}")
    lineage = delivery.get("lineage")
    if isinstance(lineage, dict):
        typer.echo(
            "Lineage: "
            + json.dumps(lineage, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        )
    receipt = delivery.get("receipt")
    if isinstance(receipt, dict):
        typer.echo(
            f"Linear comment: {_terminal_text(receipt.get('comment_id'))} "
            f"thread={_terminal_text(receipt.get('thread_id'))}"
        )
        typer.echo(
            f"Payload digest: {_terminal_text(receipt.get('payload_digest'))}"
        )


def _run_command(
    *,
    command: str,
    method_name: str,
    project: Path,
    json_input: bool,
    request_model: type[RequestT],
    human_builder: HumanBuilder[RequestT],
    human_renderer: HumanRenderer = _render_result,
) -> None:
    request: RequestT | None = None
    try:
        request = _machine_or_human_request(json_input, request_model, human_builder)
        if not json_input and isinstance(request, MutationRequest):
            typer.echo(f"Operation: {request.operation_id}")

        from researchctl.services.factory import open_application

        factory_options: dict[str, str] = {}
        if command in _TASK_COMMANDS:
            if not isinstance(request, MutationRequest):
                raise TypeError("Task command request must be a mutation request")
            factory_options = {
                "task_operation_id": request.operation_id,
                "task_command": command,
            }
        elif command in _BOOTSTRAP_COMMANDS:
            if isinstance(request, BootstrapAcceptRequest):
                factory_options = {
                    "bootstrap_operation_id": request.operation_id,
                    "bootstrap_proposal_commit": request.proposal_commit,
                }
            elif isinstance(request, BootstrapProposalRequest):
                factory_options = {
                    "bootstrap_proposal_operation_id": request.operation_id,
                    "bootstrap_id": request.bootstrap_id,
                    "bootstrap_expected_default_head": request.expected_default_head,
                }
            else:
                raise TypeError("Unsupported Bootstrap mutation request")
        elif command in _LINEAR_COMMANDS:
            if not isinstance(request, LinearConfigureRequest):
                raise TypeError("Linear command requires a strict configure request")
            factory_options = {
                "linear_operation_id": request.operation_id,
                "linear_expected_default_head": request.expected_default_head,
            }
        elif command in _RUN_COMMANDS:
            if not isinstance(
                request,
                (RunStartRequest, RunRetryRequest, RunCollectRequest),
            ):
                raise TypeError("Run command requires a strict Run request")
            factory_options = {"run_spec": request.spec}
        with open_application(project, **factory_options) as handle:
            method = getattr(handle.service, method_name)
            value = method(request, handle.actor)
        data = _result_data(value)

        if json_input:
            typer.echo(
                dump_envelope(envelope(command=command, success=True, data=data))
            )
        else:
            human_renderer(data)
    except typer.Exit:
        raise
    except Exception as exc:
        # Imported lazily so cli.py can register these Typer groups without a cycle.
        from researchctl.cli import _abort, _known_error

        error = _known_error(exc)
        if error is None:
            raise
        if (
            isinstance(request, MutationRequest)
            and "operation_id" not in error.context
        ):
            error = RCPError(
                code=error.code,
                message=error.message,
                remediation=error.remediation,
                context={
                    **error.context,
                    "operation_id": request.operation_id,
                },
                exit_code=error.exit_code,
            )
        _abort(error, command=command, json_output=json_input)


@linear_app.command("configure")
def linear_configure_command(
    project: Annotated[Path, typer.Option("--project", "-C")] = Path("."),
    json_input: Annotated[bool, typer.Option("--json")] = False,
    policy_file: Annotated[
        Path | None,
        typer.Option("--policy-file", help="Complete Linear projection policy."),
    ] = None,
    expected_default_head: Annotated[
        str | None,
        typer.Option("--expected-default-head", help="Exact default-branch base."),
    ] = None,
    operation_id: Annotated[str | None, typer.Option("--operation-id")] = None,
    idempotency_key: Annotated[str | None, typer.Option("--idempotency-key")] = None,
) -> None:
    """Prepare a manager-reviewed Linear policy proposal without network access."""

    def human_request() -> LinearConfigureRequest:
        if policy_file is None:
            raise _input_error(
                "Human mode requires --policy-file.",
                option="--policy-file",
            )
        return LinearConfigureRequest(
            **_operation_fields(operation_id, idempotency_key),
            expected_default_head=_required(
                expected_default_head,
                "--expected-default-head",
            ),
            policy=load_model(policy_file, LinearProjectionPolicy),
        )

    _run_command(
        command="linear.configure",
        method_name="linear_configure",
        project=project,
        json_input=json_input,
        request_model=LinearConfigureRequest,
        human_builder=human_request,
    )


@linear_delivery_app.command("list")
def linear_delivery_list_command(
    project: Annotated[Path, typer.Option("--project", "-C")] = Path("."),
    json_input: Annotated[bool, typer.Option("--json")] = False,
    topic: Annotated[str | None, typer.Option("--topic")] = None,
    state: Annotated[str | None, typer.Option("--state")] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, max=1000)] = 100,
) -> None:
    """List bounded local delivery status across both Linear topics."""

    _run_command(
        command="linear.delivery.list",
        method_name="linear_delivery_list",
        project=project,
        json_input=json_input,
        request_model=LinearDeliveryListRequest,
        human_builder=lambda: LinearDeliveryListRequest(
            topic=topic,
            state=state,
            limit=limit,
        ),
        human_renderer=_render_linear_delivery_list,
    )


@linear_delivery_app.command("show")
def linear_delivery_show_command(
    outbox_id: Annotated[
        str | None,
        typer.Argument(help="Exact durable outbox identity."),
    ] = None,
    project: Annotated[Path, typer.Option("--project", "-C")] = Path("."),
    json_input: Annotated[bool, typer.Option("--json")] = False,
    topic: Annotated[str | None, typer.Option("--topic")] = None,
) -> None:
    """Show one local delivery using its exact topic and outbox identity."""

    _run_command(
        command="linear.delivery.show",
        method_name="linear_delivery_show",
        project=project,
        json_input=json_input,
        request_model=LinearDeliveryShowRequest,
        human_builder=lambda: LinearDeliveryShowRequest(
            topic=_required(topic, "--topic"),
            outbox_id=_required(outbox_id, "OUTBOX_ID"),
        ),
        human_renderer=_render_linear_delivery_show,
    )


@bootstrap_app.command("propose")
def bootstrap_propose_command(
    project: Annotated[Path, typer.Option("--project", "-C")] = Path("."),
    json_input: Annotated[bool, typer.Option("--json")] = False,
    bootstrap_id: Annotated[str | None, typer.Option("--bootstrap-id")] = None,
    expected_default_head: Annotated[
        str | None,
        typer.Option("--expected-default-head", help="Exact default-branch base."),
    ] = None,
    operation_id: Annotated[str | None, typer.Option("--operation-id")] = None,
    idempotency_key: Annotated[str | None, typer.Option("--idempotency-key")] = None,
) -> None:
    """Copy canonical init state into an isolated local bootstrap proposal."""

    _run_command(
        command="bootstrap.propose",
        method_name="bootstrap_propose",
        project=project,
        json_input=json_input,
        request_model=BootstrapProposalRequest,
        human_builder=lambda: BootstrapProposalRequest(
            **_operation_fields(operation_id, idempotency_key),
            bootstrap_id=bootstrap_id or new_id("bootstrap"),
            expected_default_head=_required(
                expected_default_head,
                "--expected-default-head",
            ),
        ),
    )


@bootstrap_app.command("accept")
def bootstrap_accept_command(
    bootstrap_id: Annotated[
        str | None,
        typer.Argument(help="Bootstrap proposal identity."),
    ] = None,
    project: Annotated[Path, typer.Option("--project", "-C")] = Path("."),
    json_input: Annotated[bool, typer.Option("--json")] = False,
    proposal_commit: Annotated[
        str | None,
        typer.Option("--proposal-commit", help="Exact reviewed bootstrap proposal head."),
    ] = None,
    operation_id: Annotated[str | None, typer.Option("--operation-id")] = None,
    idempotency_key: Annotated[str | None, typer.Option("--idempotency-key")] = None,
) -> None:
    """Prepare, but never merge, bootstrapping-to-managed acceptance."""

    _run_command(
        command="bootstrap.accept",
        method_name="bootstrap_accept",
        project=project,
        json_input=json_input,
        request_model=BootstrapAcceptRequest,
        human_builder=lambda: BootstrapAcceptRequest(
            **_operation_fields(operation_id, idempotency_key),
            bootstrap_id=_required(bootstrap_id, "BOOTSTRAP_ID"),
            proposal_commit=_required(proposal_commit, "--proposal-commit"),
        ),
    )


def _run_spec_file(path: Path | None) -> RunSpec:
    if path is None:
        raise _input_error(
            "Human mode requires --spec-file.",
            option="--spec-file",
        )
    return load_model(path, RunSpec)


@run_app.command("start")
def run_start_command(
    project: Annotated[Path, typer.Option("--project", "-C")] = Path("."),
    json_input: Annotated[bool, typer.Option("--json")] = False,
    spec_file: Annotated[Path | None, typer.Option("--spec-file")] = None,
    attempt_id: Annotated[str | None, typer.Option("--attempt-id")] = None,
    gpu_uuid: Annotated[list[str] | None, typer.Option("--gpu-uuid")] = None,
    operation_id: Annotated[str | None, typer.Option("--operation-id")] = None,
    idempotency_key: Annotated[str | None, typer.Option("--idempotency-key")] = None,
) -> None:
    """Freeze, preflight, execute, and observe one local Run Attempt."""

    def human_request() -> RunStartRequest:
        spec = _run_spec_file(spec_file)
        return RunStartRequest(
            **_operation_fields(
                operation_id or spec.operation_id,
                idempotency_key,
            ),
            spec=spec,
            attempt_id=attempt_id or new_id("attempt"),
            assigned_gpu_uuids=tuple(gpu_uuid or ()),
        )

    _run_command(
        command="run.start",
        method_name="run_start",
        project=project,
        json_input=json_input,
        request_model=RunStartRequest,
        human_builder=human_request,
    )


@run_app.command("retry")
def run_retry_command(
    run_id: Annotated[
        str | None,
        typer.Argument(help="Frozen Run identity."),
    ] = None,
    project: Annotated[Path, typer.Option("--project", "-C")] = Path("."),
    json_input: Annotated[bool, typer.Option("--json")] = False,
    spec_file: Annotated[Path | None, typer.Option("--spec-file")] = None,
    retry_of: Annotated[str | None, typer.Option("--retry-of")] = None,
    attempt_id: Annotated[str | None, typer.Option("--attempt-id")] = None,
    gpu_uuid: Annotated[list[str] | None, typer.Option("--gpu-uuid")] = None,
    operation_id: Annotated[str | None, typer.Option("--operation-id")] = None,
    idempotency_key: Annotated[str | None, typer.Option("--idempotency-key")] = None,
) -> None:
    """Create a new Attempt under one unchanged frozen RunSpec."""

    def human_request() -> RunRetryRequest:
        spec = _run_spec_file(spec_file)
        selected_run_id = _required(run_id, "RUN_ID")
        if spec.run_id != selected_run_id:
            raise _input_error(
                "RUN_ID does not match --spec-file.",
                option="RUN_ID",
            )
        return RunRetryRequest(
            **_operation_fields(operation_id, idempotency_key),
            spec=spec,
            attempt_id=attempt_id or new_id("attempt"),
            retry_of=_required(retry_of, "--retry-of"),
            assigned_gpu_uuids=tuple(gpu_uuid or ()),
        )

    _run_command(
        command="run.retry",
        method_name="run_retry",
        project=project,
        json_input=json_input,
        request_model=RunRetryRequest,
        human_builder=human_request,
    )


@run_app.command("collect")
def run_collect_command(
    run_id: Annotated[
        str | None,
        typer.Argument(help="Frozen Run identity."),
    ] = None,
    project: Annotated[Path, typer.Option("--project", "-C")] = Path("."),
    json_input: Annotated[bool, typer.Option("--json")] = False,
    spec_file: Annotated[Path | None, typer.Option("--spec-file")] = None,
    attempt_id: Annotated[str | None, typer.Option("--attempt-id")] = None,
    operation_id: Annotated[str | None, typer.Option("--operation-id")] = None,
    idempotency_key: Annotated[str | None, typer.Option("--idempotency-key")] = None,
) -> None:
    """Finalize one terminal Attempt as the Run's unique evidence record."""

    def human_request() -> RunCollectRequest:
        spec = _run_spec_file(spec_file)
        selected_run_id = _required(run_id, "RUN_ID")
        if spec.run_id != selected_run_id:
            raise _input_error(
                "RUN_ID does not match --spec-file.",
                option="RUN_ID",
            )
        return RunCollectRequest(
            **_operation_fields(operation_id, idempotency_key),
            spec=spec,
            attempt_id=_required(attempt_id, "--attempt-id"),
        )

    _run_command(
        command="run.collect",
        method_name="run_collect",
        project=project,
        json_input=json_input,
        request_model=RunCollectRequest,
        human_builder=human_request,
    )


@task_app.command("create")
def task_create_command(
    project: Annotated[Path, typer.Option("--project", "-C")] = Path("."),
    json_input: Annotated[bool, typer.Option("--json")] = False,
    task_file: Annotated[Path | None, typer.Option("--task-file")] = None,
    guided: Annotated[
        bool,
        typer.Option("--guided", help="Prompt for a compact, well-formed Task."),
    ] = False,
    task_id: Annotated[str | None, typer.Option("--task-id")] = None,
    key: Annotated[str | None, typer.Option("--key")] = None,
    title: Annotated[str | None, typer.Option("--title")] = None,
    goal: Annotated[str | None, typer.Option("--goal")] = None,
    done_when: Annotated[list[str] | None, typer.Option("--done-when")] = None,
    execution_domain: Annotated[str | None, typer.Option("--execution-domain")] = None,
    allowed_write: Annotated[list[str] | None, typer.Option("--allow-write")] = None,
    deliverable: Annotated[list[str] | None, typer.Option("--deliverable")] = None,
    priority: Annotated[Priority, typer.Option("--priority")] = Priority.MEDIUM,
    parent_task_id: Annotated[str | None, typer.Option("--parent-task-id")] = None,
    milestone: Annotated[str | None, typer.Option("--milestone")] = None,
    constraint: Annotated[list[str] | None, typer.Option("--constraint")] = None,
    required_input_file: Annotated[
        list[Path] | None,
        typer.Option(
            "--required-input-file",
            help="YAML or JSON InputIdentity record; repeat for multiple inputs.",
        ),
    ] = None,
    waiting_on: Annotated[str | None, typer.Option("--waiting-on")] = None,
    next_decision: Annotated[str | None, typer.Option("--next-decision")] = None,
    preferred_host: Annotated[list[str] | None, typer.Option("--preferred-host")] = None,
    preferred_pool: Annotated[list[str] | None, typer.Option("--preferred-pool")] = None,
    gpu_count: Annotated[int, typer.Option("--gpu-count", min=0)] = 0,
    gpu_type: Annotated[str | None, typer.Option("--gpu-type")] = None,
    min_gpu_memory_gb: Annotated[int | None, typer.Option("--min-gpu-memory-gb", min=0)] = None,
    operation_id: Annotated[str | None, typer.Option("--operation-id")] = None,
    idempotency_key: Annotated[str | None, typer.Option("--idempotency-key")] = None,
) -> None:
    """Create a planned Task proposal."""

    def human_request() -> TaskCreateRequest:
        if task_file is not None:
            if guided:
                raise _input_error(
                    "Use either --task-file or --guided, not both.",
                    option="--guided",
                )
            task = load_model(task_file, TaskRecord)
        else:
            now = utc_now()
            selected_key = key
            selected_title = title
            selected_goal = goal
            selected_done_when = tuple(done_when or ())
            selected_domain = execution_domain
            selected_allowed_write = tuple(allowed_write or ())
            selected_deliverables = tuple(deliverable or ())
            selected_waiting_on = waiting_on
            selected_next_decision = next_decision
            selected_inputs = _load_required_inputs(required_input_file)
            if guided:
                selected_key = selected_key or typer.prompt("Task key")
                selected_title = selected_title or typer.prompt("Title")
                selected_goal = selected_goal or typer.prompt("Goal")
                selected_done_when = selected_done_when or _prompt_many("Done when")
                selected_domain = selected_domain or typer.prompt(
                    "Execution domain / team"
                )
                selected_allowed_write = selected_allowed_write or _prompt_many(
                    "Allowed write path"
                )
                selected_deliverables = selected_deliverables or _prompt_many(
                    "Deliverable"
                )
                if selected_waiting_on is None:
                    selected_waiting_on = _prompt_optional("Waiting on (optional)")
                if selected_next_decision is None:
                    selected_next_decision = _prompt_optional(
                        "Next human decision (optional)"
                    )
                if not selected_inputs:
                    selected_inputs = _guided_required_inputs()
            task = TaskRecord(
                task_id=task_id or new_id("task"),
                key=_required(selected_key, "--key"),
                title=_required(selected_title, "--title"),
                goal=_required(selected_goal, "--goal"),
                done_when=(
                    selected_done_when
                    or _required_many(done_when, "--done-when")
                ),
                execution_domain=_required(selected_domain, "--execution-domain"),
                allowed_write_paths=(
                    selected_allowed_write
                    or _required_many(allowed_write, "--allow-write")
                ),
                deliverables=(
                    selected_deliverables
                    or _required_many(deliverable, "--deliverable")
                ),
                priority=priority,
                parent_task_id=parent_task_id,
                milestone=milestone,
                constraints=tuple(constraint or ()),
                required_inputs=selected_inputs,
                execution=ExecutionPreferences(
                    preferred_hosts=tuple(preferred_host or ()),
                    preferred_pools=tuple(preferred_pool or ()),
                    gpu_count=gpu_count,
                    gpu_type=gpu_type,
                    min_gpu_memory_gb=min_gpu_memory_gb,
                ),
                waiting_on=selected_waiting_on,
                next_decision=selected_next_decision,
                created_at=now,
                updated_at=now,
            )
        return TaskCreateRequest(
            **_operation_fields(operation_id, idempotency_key), task=task
        )

    _run_command(
        command="task.create",
        method_name="task_create",
        project=project,
        json_input=json_input,
        request_model=TaskCreateRequest,
        human_builder=human_request,
    )


@task_app.command("update")
def task_update_command(
    task_id: Annotated[str | None, typer.Argument(help="Canonical Task ID.")] = None,
    project: Annotated[Path, typer.Option("--project", "-C")] = Path("."),
    json_input: Annotated[bool, typer.Option("--json")] = False,
    expected_digest: Annotated[str | None, typer.Option("--expected-digest")] = None,
    replacement: Annotated[Path | None, typer.Option("--replacement")] = None,
    operation_id: Annotated[str | None, typer.Option("--operation-id")] = None,
    idempotency_key: Annotated[str | None, typer.Option("--idempotency-key")] = None,
) -> None:
    """Propose a full Task replacement guarded by its canonical digest."""

    def human_request() -> TaskUpdateRequest:
        if replacement is None:
            raise _input_error("Human mode requires --replacement.", option="--replacement")
        return TaskUpdateRequest(
            **_operation_fields(operation_id, idempotency_key),
            task_id=_required(task_id, "TASK_ID"),
            expected_digest=_required(expected_digest, "--expected-digest"),
            replacement=load_model(replacement, TaskRecord),
        )

    _run_command(
        command="task.update",
        method_name="task_update",
        project=project,
        json_input=json_input,
        request_model=TaskUpdateRequest,
        human_builder=human_request,
    )


@task_app.command("cancel")
def task_cancel_command(
    task_id: Annotated[str | None, typer.Argument(help="Canonical Task ID.")] = None,
    project: Annotated[Path, typer.Option("--project", "-C")] = Path("."),
    json_input: Annotated[bool, typer.Option("--json")] = False,
    expected_digest: Annotated[str | None, typer.Option("--expected-digest")] = None,
    reason: Annotated[str | None, typer.Option("--reason")] = None,
    updated_at: Annotated[str | None, typer.Option("--updated-at")] = None,
    operation_id: Annotated[str | None, typer.Option("--operation-id")] = None,
    idempotency_key: Annotated[str | None, typer.Option("--idempotency-key")] = None,
) -> None:
    """Propose explicit Task cancellation without stopping Sessions implicitly."""

    def human_request() -> TaskCancelRequest:
        return TaskCancelRequest(
            **_operation_fields(operation_id, idempotency_key),
            task_id=_required(task_id, "TASK_ID"),
            expected_digest=_required(expected_digest, "--expected-digest"),
            reason=_required(reason, "--reason"),
            updated_at=updated_at or utc_now(),
        )

    _run_command(
        command="task.cancel",
        method_name="task_cancel",
        project=project,
        json_input=json_input,
        request_model=TaskCancelRequest,
        human_builder=human_request,
    )


@session_app.command("list")
def session_list_command(
    project: Annotated[Path, typer.Option("--project", "-C")] = Path("."),
    json_input: Annotated[bool, typer.Option("--json")] = False,
    task_id: Annotated[
        str | None,
        typer.Option("--task-id", "--task"),
    ] = None,
    state: Annotated[SessionState | None, typer.Option("--state")] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, max=1000)] = 100,
) -> None:
    """List Sessions visible to the authenticated actor."""

    _run_command(
        command="session.list",
        method_name="session_list",
        project=project,
        json_input=json_input,
        request_model=SessionListRequest,
        human_builder=lambda: SessionListRequest(
            task_id=task_id,
            state=state,
            limit=limit,
        ),
        human_renderer=_render_session_list,
    )


@session_app.command("show")
def session_show_command(
    session_id: Annotated[
        str | None,
        typer.Argument(help="Exact Session ID."),
    ] = None,
    project: Annotated[Path, typer.Option("--project", "-C")] = Path("."),
    json_input: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show the stable addressing identity of one visible Session."""

    _run_command(
        command="session.show",
        method_name="session_show",
        project=project,
        json_input=json_input,
        request_model=SessionShowRequest,
        human_builder=lambda: SessionShowRequest(
            session_id=_required(session_id, "SESSION_ID")
        ),
        human_renderer=_render_session_show,
    )


@session_app.command("address")
def session_address_command(
    session_id: Annotated[
        str | None,
        typer.Argument(help="Exact target Session ID."),
    ] = None,
    project: Annotated[Path, typer.Option("--project", "-C")] = Path("."),
    json_input: Annotated[bool, typer.Option("--json")] = False,
    commit_sha: Annotated[str | None, typer.Option("--commit")] = None,
    app_name: Annotated[str, typer.Option("--app")] = "researchctl-app",
) -> None:
    """Render a verified command header; add the message on the next line."""

    _run_command(
        command="session.address",
        method_name="session_address",
        project=project,
        json_input=json_input,
        request_model=SessionAddressRequest,
        human_builder=lambda: SessionAddressRequest(
            session_id=_required(session_id, "SESSION_ID"),
            commit_sha=_required(commit_sha, "--commit"),
            app=app_name,
        ),
        human_renderer=_render_session_address,
    )


@session_app.command("start")
def session_start_command(
    task_id: Annotated[str | None, typer.Argument(help="Runnable Task ID.")] = None,
    project: Annotated[Path, typer.Option("--project", "-C")] = Path("."),
    json_input: Annotated[bool, typer.Option("--json")] = False,
    session_id: Annotated[str | None, typer.Option("--session-id")] = None,
    base_commit: Annotated[str | None, typer.Option("--base-commit")] = None,
    host: Annotated[str | None, typer.Option("--host")] = None,
    agent: Annotated[AgentKind, typer.Option("--agent")] = AgentKind.CODEX,
    prompt: Annotated[str | None, typer.Option("--prompt")] = None,
    prompt_file: Annotated[Path | None, typer.Option("--prompt-file")] = None,
    operation_id: Annotated[str | None, typer.Option("--operation-id")] = None,
    idempotency_key: Annotated[str | None, typer.Option("--idempotency-key")] = None,
) -> None:
    """Start or observe one Agent Session in its isolated worktree."""

    def human_request() -> SessionStartRequest:
        return SessionStartRequest(
            **_operation_fields(operation_id, idempotency_key),
            session_id=session_id or new_id("session"),
            task_id=_required(task_id, "TASK_ID"),
            base_commit=_required(base_commit, "--base-commit"),
            host=host or socket.gethostname().split(".", maxsplit=1)[0],
            agent=agent,
            prompt=_prompt_value(prompt, prompt_file),
        )

    _run_command(
        command="session.start",
        method_name="session_start",
        project=project,
        json_input=json_input,
        request_model=SessionStartRequest,
        human_builder=human_request,
    )


@session_app.command("pause")
def session_pause_command(
    session_id: Annotated[str | None, typer.Argument(help="Session to pause.")] = None,
    project: Annotated[Path, typer.Option("--project", "-C")] = Path("."),
    json_input: Annotated[bool, typer.Option("--json")] = False,
    mode: Annotated[str, typer.Option("--mode", help="idle or stop")] = "idle",
    operation_id: Annotated[str | None, typer.Option("--operation-id")] = None,
    idempotency_key: Annotated[str | None, typer.Option("--idempotency-key")] = None,
) -> None:
    """Pause or stop an observed Session on its owning host."""

    def human_request() -> SessionPauseRequest:
        return SessionPauseRequest(
            **_operation_fields(operation_id, idempotency_key),
            session_id=_required(session_id, "SESSION_ID"),
            mode=mode,
        )

    _run_command(
        command="session.pause",
        method_name="session_pause",
        project=project,
        json_input=json_input,
        request_model=SessionPauseRequest,
        human_builder=human_request,
    )


@session_app.command("attach")
def session_attach_command(
    session_id: Annotated[str | None, typer.Argument(help="Session to attach.")] = None,
    project: Annotated[Path, typer.Option("--project", "-C")] = Path("."),
    json_input: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Attach interactively, or return exact attach argv in JSON mode."""

    _run_command(
        command="session.attach",
        method_name="session_attach",
        project=project,
        json_input=json_input,
        request_model=SessionAttachRequest,
        human_builder=lambda: SessionAttachRequest(
            session_id=_required(session_id, "SESSION_ID")
        ),
        human_renderer=_render_attach,
    )


@session_app.command("continue")
def session_continue_command(
    source_session_id: Annotated[
        str | None,
        typer.Argument(help="Lost or stopped source Session."),
    ] = None,
    project: Annotated[Path, typer.Option("--project", "-C")] = Path("."),
    json_input: Annotated[bool, typer.Option("--json")] = False,
    new_session: Annotated[
        bool,
        typer.Option("--new-session", help="Acknowledge creation of a new identity."),
    ] = False,
    new_session_id: Annotated[str | None, typer.Option("--new-session-id")] = None,
    target_host: Annotated[str | None, typer.Option("--target-host")] = None,
    prompt: Annotated[str | None, typer.Option("--prompt")] = None,
    prompt_file: Annotated[Path | None, typer.Option("--prompt-file")] = None,
    operation_id: Annotated[str | None, typer.Option("--operation-id")] = None,
    idempotency_key: Annotated[str | None, typer.Option("--idempotency-key")] = None,
) -> None:
    """Continue with a new Session, branch, and worktree identity."""

    def human_request() -> SessionContinueRequest:
        if not new_session:
            raise RCPError(
                code="new_session_required",
                message="Continuation requires explicit --new-session acknowledgement.",
                remediation="Retry with --new-session; the source Session remains unchanged.",
            )
        return SessionContinueRequest(
            **_operation_fields(operation_id, idempotency_key),
            source_session_id=_required(source_session_id, "SESSION_ID"),
            new_session_id=new_session_id or new_id("session"),
            target_host=(
                target_host or socket.gethostname().split(".", maxsplit=1)[0]
            ),
            prompt=_prompt_value(prompt, prompt_file),
        )

    _run_command(
        command="session.continue",
        method_name="session_continue_new",
        project=project,
        json_input=json_input,
        request_model=SessionContinueRequest,
        human_builder=human_request,
    )


def _evidence_values(values: list[str] | None) -> tuple[StatusEvidence, ...]:
    evidence: list[StatusEvidence] = []
    for value in values or ():
        kind, separator, detail = value.partition("=")
        if not separator:
            raise _input_error(
                "--evidence values must use KIND=VALUE.", option="--evidence"
            )
        evidence.append(StatusEvidence(kind=kind, value=detail))
    return tuple(evidence)


@status_app.command("publish")
def status_publish_command(
    project: Annotated[Path, typer.Option("--project", "-C")] = Path("."),
    json_input: Annotated[bool, typer.Option("--json")] = False,
    update_file: Annotated[Path | None, typer.Option("--update-file")] = None,
    update_id: Annotated[str | None, typer.Option("--update-id")] = None,
    task_id: Annotated[str | None, typer.Option("--task-id")] = None,
    session_id: Annotated[str | None, typer.Option("--session-id")] = None,
    status: Annotated[StatusKind, typer.Option("--status")] = StatusKind.RUNNING,
    summary: Annotated[str | None, typer.Option("--summary")] = None,
    observed_at: Annotated[str | None, typer.Option("--observed-at")] = None,
    evidence: Annotated[list[str] | None, typer.Option("--evidence")] = None,
    blocker_category: Annotated[str | None, typer.Option("--blocker-category")] = None,
    blocker_detail: Annotated[str | None, typer.Option("--blocker-detail")] = None,
    question: Annotated[str | None, typer.Option("--question")] = None,
    option: Annotated[list[str] | None, typer.Option("--option")] = None,
    suggested_next_action: Annotated[
        str | None,
        typer.Option("--suggested-next-action"),
    ] = None,
    operation_id: Annotated[str | None, typer.Option("--operation-id")] = None,
    idempotency_key: Annotated[str | None, typer.Option("--idempotency-key")] = None,
) -> None:
    """Publish one append-only structured status observation."""

    def human_request() -> StatusPublishRequest:
        if update_file is not None:
            update = load_model(update_file, StatusUpdate)
        else:
            decision = None
            if question is not None or option:
                decision = DecisionRequest(
                    question=_required(question, "--question"),
                    options=tuple(option or ()),
                )
            update = StatusUpdate(
                update_id=update_id or new_id("update"),
                task_id=_required(task_id, "--task-id"),
                session_id=_required(session_id, "--session-id"),
                status=status,
                summary=_required(summary, "--summary"),
                observed_at=observed_at or utc_now(),
                evidence=_evidence_values(evidence),
                blocker_category=blocker_category,
                blocker_detail=blocker_detail,
                decision_needed=decision,
                suggested_next_action=suggested_next_action,
            )
        return StatusPublishRequest(
            **_operation_fields(operation_id, idempotency_key), update=update
        )

    _run_command(
        command="status.publish",
        method_name="status_publish",
        project=project,
        json_input=json_input,
        request_model=StatusPublishRequest,
        human_builder=human_request,
    )


@inbox_app.command("list")
def inbox_list_command(
    project: Annotated[Path, typer.Option("--project", "-C")] = Path("."),
    json_input: Annotated[bool, typer.Option("--json")] = False,
    include_resolved: Annotated[bool, typer.Option("--include-resolved")] = False,
    limit: Annotated[int, typer.Option("--limit", min=1, max=1000)] = 100,
    now: Annotated[str | None, typer.Option("--now")] = None,
) -> None:
    """List current attention, ordered by the runtime store."""

    _run_command(
        command="inbox.list",
        method_name="inbox_list",
        project=project,
        json_input=json_input,
        request_model=InboxListRequest,
        human_builder=lambda: InboxListRequest(
            include_resolved=include_resolved,
            limit=limit,
            now=now,
        ),
        human_renderer=_render_inbox,
    )


@inbox_app.command("ack")
def inbox_ack_command(
    update_id: Annotated[str | None, typer.Argument(help="Visible StatusUpdate ID.")] = None,
    project: Annotated[Path, typer.Option("--project", "-C")] = Path("."),
    json_input: Annotated[bool, typer.Option("--json")] = False,
    expected_generation: Annotated[
        int | None,
        typer.Option("--expected-generation", min=1),
    ] = None,
    operation_id: Annotated[str | None, typer.Option("--operation-id")] = None,
    idempotency_key: Annotated[str | None, typer.Option("--idempotency-key")] = None,
) -> None:
    """Acknowledge the currently visible generation of an attention item."""

    def human_request() -> InboxAckRequest:
        if expected_generation is None:
            raise _input_error(
                "Human mode requires --expected-generation.",
                option="--expected-generation",
            )
        return InboxAckRequest(
            **_operation_fields(operation_id, idempotency_key),
            update_id=_required(update_id, "UPDATE_ID"),
            expected_generation=expected_generation,
        )

    _run_command(
        command="inbox.ack",
        method_name="inbox_ack",
        project=project,
        json_input=json_input,
        request_model=InboxAckRequest,
        human_builder=human_request,
    )


@inbox_app.command("snooze")
def inbox_snooze_command(
    update_id: Annotated[str | None, typer.Argument(help="Visible StatusUpdate ID.")] = None,
    project: Annotated[Path, typer.Option("--project", "-C")] = Path("."),
    json_input: Annotated[bool, typer.Option("--json")] = False,
    expected_generation: Annotated[
        int | None,
        typer.Option("--expected-generation", min=1),
    ] = None,
    until: Annotated[str | None, typer.Option("--until")] = None,
    operation_id: Annotated[str | None, typer.Option("--operation-id")] = None,
    idempotency_key: Annotated[str | None, typer.Option("--idempotency-key")] = None,
) -> None:
    """Hide the observed generation until an explicit UTC time."""

    def human_request() -> InboxSnoozeRequest:
        if expected_generation is None:
            raise _input_error(
                "Human mode requires --expected-generation.",
                option="--expected-generation",
            )
        if until is None:
            raise _input_error("Human mode requires --until.", option="--until")
        return InboxSnoozeRequest(
            **_operation_fields(operation_id, idempotency_key),
            update_id=_required(update_id, "UPDATE_ID"),
            expected_generation=expected_generation,
            until=until,
        )

    _run_command(
        command="inbox.snooze",
        method_name="inbox_snooze",
        project=project,
        json_input=json_input,
        request_model=InboxSnoozeRequest,
        human_builder=human_request,
    )


@inbox_app.command("resolve")
def inbox_resolve_command(
    update_id: Annotated[str | None, typer.Argument(help="Visible StatusUpdate ID.")] = None,
    project: Annotated[Path, typer.Option("--project", "-C")] = Path("."),
    json_input: Annotated[bool, typer.Option("--json")] = False,
    expected_generation: Annotated[
        int | None,
        typer.Option("--expected-generation", min=1),
    ] = None,
    reason: Annotated[str | None, typer.Option("--reason")] = None,
    operation_id: Annotated[str | None, typer.Option("--operation-id")] = None,
    idempotency_key: Annotated[str | None, typer.Option("--idempotency-key")] = None,
) -> None:
    """Resolve attention only; this never changes the Task record."""

    def human_request() -> InboxResolveRequest:
        if expected_generation is None:
            raise _input_error(
                "Human mode requires --expected-generation.",
                option="--expected-generation",
            )
        return InboxResolveRequest(
            **_operation_fields(operation_id, idempotency_key),
            update_id=_required(update_id, "UPDATE_ID"),
            expected_generation=expected_generation,
            reason=_required(reason, "--reason"),
        )

    _run_command(
        command="inbox.resolve",
        method_name="inbox_resolve",
        project=project,
        json_input=json_input,
        request_model=InboxResolveRequest,
        human_builder=human_request,
    )
