from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from researchctl.cli import app
from researchctl.domain.models import (
    InputIdentity,
    LinearProjectionPolicy,
    PlanReviewPolicy,
    RunSpec,
    StatusUpdate,
    TaskRecord,
)
from researchctl.errors import RCPError
from researchctl.output import envelope
from researchctl.phase2_cli import _write_generated_model
from researchctl.serialization import canonical_digest, dump_yaml
from researchctl.services.application import ServiceResult
from researchctl.services.requests import (
    BootstrapAcceptRequest,
    BootstrapProposalRequest,
    InboxAckRequest,
    InboxListRequest,
    InboxResolveRequest,
    InboxSnoozeRequest,
    LinearConfigureRequest,
    LinearDeliveryListRequest,
    LinearDeliveryShowRequest,
    PlanReviewConfigureRequest,
    RunCollectRequest,
    RunRetryRequest,
    RunStartRequest,
    SessionAttachRequest,
    SessionContinueRequest,
    SessionPauseRequest,
    SessionStartRequest,
    StatusPublishRequest,
    TaskCancelRequest,
    TaskCreateRequest,
    TaskUpdateRequest,
)


def _id(kind: str, fill: str) -> str:
    return f"{kind}_20260803T120000Z_{fill * 24}"


@dataclass
class _Call:
    method: str
    request: Any
    actor: object


class _SpyService:
    def __init__(self) -> None:
        self.calls: list[_Call] = []
        self.error: RCPError | None = None
        self.inbox_items: tuple[object, ...] = ()

    def __getattr__(self, method: str):
        def invoke(request: Any, actor: object) -> Any:
            self.calls.append(_Call(method, request, actor))
            if self.error is not None:
                raise self.error
            if method == "inbox_list":
                return self.inbox_items
            if method == "session_attach":
                return {"attach_argv": ["true"], "session": {"state": "active"}}
            if method in {"linear_delivery_list", "linear_delivery_show"}:
                delivery = {
                    "project_id": _id("project", "a"),
                    "topic": request.topic or "linear.accepted-result.v1",
                    "outbox_id": getattr(request, "outbox_id", "linear-event-test"),
                    "aggregate_id": "report:test:1",
                    "state": "pending",
                    "created_at": "2026-08-03T12:00:00Z",
                    "age_seconds": 30,
                    "pending_age_seconds": 30,
                    "attempt_count": 2,
                    "last_error_code": "linear_delivery_unavailable",
                    "last_claim_id": "claim-previous",
                    "active_claim": {
                        "claim_id": "claim-active",
                        "claimed_at": "2026-08-03T12:00:20Z",
                        "expires_at": "2026-08-03T12:05:20Z",
                    },
                    "receipt": {
                        "receipt_id": "linear-receipt-test",
                        "comment_id": "44444444-4444-4444-8444-444444444444",
                        "thread_id": "33333333-3333-4333-8333-333333333333",
                        "payload_digest": "sha256:" + "a" * 64,
                    },
                    "lineage": {"task_id": _id("task", "b")},
                }
                if method == "linear_delivery_list":
                    return {
                        "topic": request.topic,
                        "state": request.state,
                        "limit": request.limit,
                        "count": 1,
                        "items": [delivery],
                    }
                return {"delivery": delivery}
            command = {
                "bootstrap_accept": "bootstrap.accept",
                "bootstrap_propose": "bootstrap.propose",
                "task_create": "task.create",
                "task_update": "task.update",
                "task_cancel": "task.cancel",
                "session_start": "session.start",
                "session_pause": "session.pause",
                "session_continue_new": "session.continue",
                "status_publish": "status.publish",
                "inbox_ack": "inbox.ack",
                "inbox_snooze": "inbox.snooze",
                "inbox_resolve": "inbox.resolve",
                "linear_configure": "linear.configure",
                "plan_review_configure": "plan.configure-review",
                "run_collect": "run.collect",
                "run_retry": "run.retry",
                "run_start": "run.start",
            }[method]
            data = {}
            if method == "bootstrap_accept":
                data = {
                    "bootstrap_id": request.bootstrap_id,
                    "project_state": "bootstrapping",
                    "proposal": {
                        "branch": f"research/control/{request.operation_id}",
                        "commit": "b" * 40,
                        "proposal_commit": request.proposal_commit,
                        "worktree": "/runtime/bootstrap-control-worktree",
                        "accepted": False,
                        "requires_merge": True,
                    },
                }
            elif method == "bootstrap_propose":
                data = {
                    "bootstrap_id": request.bootstrap_id,
                    "project_state": "bootstrapping",
                    "proposal": {
                        "operation_id": request.operation_id,
                        "bootstrap_id": request.bootstrap_id,
                        "branch": f"research/bootstrap/{request.bootstrap_id}",
                        "commit": "8" * 40,
                        "base_commit": request.expected_default_head,
                        "worktree": "/runtime/bootstrap-proposal-worktree",
                        "proposal_only": True,
                        "accepted": False,
                    },
                }
            elif method in {"run_start", "run_retry", "run_collect"}:
                data = {
                    "run": {
                        "run_id": request.spec.run_id,
                        "attempt_id": request.attempt_id,
                        "result": {"outcome": "complete"},
                        "frozen": {"tag": f"research-run/{request.spec.run_id}"},
                    }
                }
            elif method == "linear_configure":
                data = {
                    "linear_policy": request.policy.model_dump(
                        mode="json",
                        exclude_none=True,
                    ),
                    "policy_digest": canonical_digest(request.policy),
                    "proposal": {
                        "branch": f"research/control/{request.operation_id}",
                        "commit": "c" * 40,
                        "worktree": "/runtime/linear-control-worktree",
                        "effect_applied": True,
                        "delivery": "local_control_change",
                    },
                }
            elif method == "plan_review_configure":
                data = {
                    "plan_review_policy": request.review_policy.model_dump(
                        mode="json",
                        exclude_none=True,
                    ),
                    "policy_digest": canonical_digest(request.review_policy),
                    "proposal": {
                        "branch": f"research/control/{request.operation_id}",
                        "commit": "d" * 40,
                        "worktree": "/runtime/plan-review-control-worktree",
                        "effect_applied": True,
                        "delivery": "local_control_change",
                    },
                }
            if method.startswith("task_"):
                if hasattr(request, "task_id"):
                    task = {
                        "task_id": request.task_id,
                        "key": "CLI-1",
                        "state": "planned",
                    }
                else:
                    task = {
                        "task_id": request.task.task_id,
                        "key": request.task.key,
                        "state": request.task.state.value,
                    }
                data = {
                    "task": task,
                    "proposal": {
                        "branch": f"research/control/{request.operation_id}",
                        "commit": "a" * 40,
                        "worktree": "/runtime/control-worktree",
                        "delivery": "local_control_change",
                    },
                }
            return ServiceResult(
                command=command,
                operation_id=request.operation_id,
                terminal_result="observed",
                data=data,
            )

        return invoke


class _Handle:
    def __init__(self, service: _SpyService, actor: object) -> None:
        self.service = service
        self.actor = actor

    def __enter__(self) -> _Handle:
        return self

    def __exit__(self, *_: object) -> None:
        return None


@pytest.fixture
def cli_spy(monkeypatch: pytest.MonkeyPatch):
    from researchctl.services import factory

    service = _SpyService()
    actor = object()
    opens: list[tuple[Path, dict[str, Any]]] = []
    subprocess_calls: list[list[str]] = []

    def open_application(path: Path, **options: Any) -> _Handle:
        opens.append((path, options))
        return _Handle(service, actor)

    monkeypatch.setattr(factory, "open_application", open_application)
    monkeypatch.setattr(
        "researchctl.phase2_cli.subprocess.run",
        lambda argv, **_kwargs: (
            subprocess_calls.append(argv)
            or type("Result", (), {"returncode": 0})()
        ),
    )
    return service, actor, opens, subprocess_calls


def _payload(request: Any) -> str:
    return json.dumps(request.model_dump(mode="json", exclude_none=True))


def _attention_item(
    *,
    kind: str,
    fill: str,
    status: str,
    summary: str,
    blocker_category: str | None = None,
    blocker_detail: str | None = None,
    decision_needed: dict[str, object] | None = None,
    suggested_next_action: str | None = None,
) -> SimpleNamespace:
    observed_at = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    update = StatusUpdate(
        update_id=_id("update", fill),
        task_id=_id("task", fill),
        session_id=_id("session", fill),
        status=status,
        summary=summary,
        observed_at=observed_at,
        blocker_category=blocker_category,
        blocker_detail=blocker_detail,
        decision_needed=decision_needed,
        suggested_next_action=suggested_next_action,
    )
    return SimpleNamespace(
        dedupe_key="sha256:" + fill * 64,
        generation=1,
        state="open",
        kind=kind,
        priority=10,
        task_id=update.task_id,
        session_id=update.session_id,
        current_update=update,
        first_seen_at=observed_at,
        last_seen_at=observed_at,
        acknowledged_by=None,
        acknowledged_at=None,
        snoozed_by=None,
        snoozed_until=None,
        resolved_by=None,
        resolved_at=None,
        resolution_reason=None,
    )


def _case_data(
    tmp_path: Path,
    task_payload,
    run_spec_payload,
) -> list[tuple[list[str], Any]]:
    operation = _id("operation", "1")
    task = TaskRecord.model_validate(task_payload(state="planned"))
    replacement = TaskRecord.model_validate(
        task_payload(updated_at="2026-08-03T12:00:01Z")
    )
    task_path = tmp_path / "task.yaml"
    replacement_path = tmp_path / "replacement.yaml"
    task_path.write_text(dump_yaml(task), encoding="utf-8")
    replacement_path.write_text(dump_yaml(replacement), encoding="utf-8")
    digest = canonical_digest(task)
    session = _id("session", "2")
    new_session = _id("session", "3")
    update = _id("update", "4")
    run_spec = RunSpec.model_validate(
        run_spec_payload(
            operation_id=operation,
            task_id=task.task_id,
            session_id=session,
            requested_host="host-a",
            resources={
                "gpu_count": 1,
                "preferred_hosts": [],
                "preferred_pools": [],
            },
        )
    )
    run_spec_path = tmp_path / "run-spec.yaml"
    run_spec_path.write_text(dump_yaml(run_spec), encoding="utf-8")
    retry_operation = _id("operation", "5")
    collect_operation = _id("operation", "9")

    common = {"operation_id": operation, "idempotency_key": "stable-key"}
    return [
        (
            [
                "bootstrap",
                "propose",
                "--bootstrap-id",
                _id("bootstrap", "8"),
                "--expected-default-head",
                "7" * 40,
                "--operation-id",
                operation,
                "--idempotency-key",
                "stable-key",
            ],
            BootstrapProposalRequest(
                **common,
                bootstrap_id=_id("bootstrap", "8"),
                expected_default_head="7" * 40,
            ),
        ),
        (
            [
                "bootstrap",
                "accept",
                _id("bootstrap", "8"),
                "--proposal-commit",
                "9" * 40,
                "--operation-id",
                operation,
                "--idempotency-key",
                "stable-key",
            ],
            BootstrapAcceptRequest(
                **common,
                bootstrap_id=_id("bootstrap", "8"),
                proposal_commit="9" * 40,
            ),
        ),
        (
            [
                "task",
                "create",
                "--task-file",
                str(task_path),
                "--operation-id",
                operation,
                "--idempotency-key",
                "stable-key",
            ],
            TaskCreateRequest(**common, task=task),
        ),
        (
            [
                "run",
                "start",
                "--spec-file",
                str(run_spec_path),
                "--attempt-id",
                _id("attempt", "6"),
                "--gpu-uuid",
                "GPU-1",
                "--operation-id",
                operation,
                "--idempotency-key",
                "stable-key",
            ],
            RunStartRequest(
                **common,
                spec=run_spec,
                attempt_id=_id("attempt", "6"),
                assigned_gpu_uuids=("GPU-1",),
            ),
        ),
        (
            [
                "run",
                "collect",
                run_spec.run_id,
                "--spec-file",
                str(run_spec_path),
                "--attempt-id",
                _id("attempt", "6"),
                "--operation-id",
                collect_operation,
                "--idempotency-key",
                "collect-key",
            ],
            RunCollectRequest(
                operation_id=collect_operation,
                idempotency_key="collect-key",
                spec=run_spec,
                attempt_id=_id("attempt", "6"),
            ),
        ),
        (
            [
                "run",
                "retry",
                run_spec.run_id,
                "--spec-file",
                str(run_spec_path),
                "--retry-of",
                _id("attempt", "6"),
                "--attempt-id",
                _id("attempt", "7"),
                "--gpu-uuid",
                "GPU-1",
                "--operation-id",
                retry_operation,
                "--idempotency-key",
                "retry-key",
            ],
            RunRetryRequest(
                operation_id=retry_operation,
                idempotency_key="retry-key",
                spec=run_spec,
                attempt_id=_id("attempt", "7"),
                retry_of=_id("attempt", "6"),
                assigned_gpu_uuids=("GPU-1",),
            ),
        ),
        (
            [
                "task",
                "update",
                task.task_id,
                "--replacement",
                str(replacement_path),
                "--expected-digest",
                digest,
                "--operation-id",
                operation,
                "--idempotency-key",
                "stable-key",
            ],
            TaskUpdateRequest(
                **common,
                task_id=task.task_id,
                expected_digest=digest,
                replacement=replacement,
            ),
        ),
        (
            [
                "task",
                "cancel",
                task.task_id,
                "--expected-digest",
                digest,
                "--reason",
                "No longer needed.",
                "--updated-at",
                "2026-08-03T12:00:02Z",
                "--operation-id",
                operation,
                "--idempotency-key",
                "stable-key",
            ],
            TaskCancelRequest(
                **common,
                task_id=task.task_id,
                expected_digest=digest,
                reason="No longer needed.",
                updated_at="2026-08-03T12:00:02Z",
            ),
        ),
        (
            [
                "session",
                "start",
                task.task_id,
                "--session-id",
                session,
                "--base-commit",
                "a" * 40,
                "--host",
                "host-a",
                "--agent",
                "codex",
                "--prompt",
                "Implement the accepted Task.",
                "--operation-id",
                operation,
                "--idempotency-key",
                "stable-key",
            ],
            SessionStartRequest(
                **common,
                session_id=session,
                task_id=task.task_id,
                base_commit="a" * 40,
                host="host-a",
                agent="codex",
                prompt="Implement the accepted Task.",
            ),
        ),
        (
            [
                "session",
                "pause",
                session,
                "--mode",
                "stop",
                "--operation-id",
                operation,
                "--idempotency-key",
                "stable-key",
            ],
            SessionPauseRequest(**common, session_id=session, mode="stop"),
        ),
        (["session", "attach", session], SessionAttachRequest(session_id=session)),
        (
            [
                "session",
                "continue",
                session,
                "--new-session",
                "--new-session-id",
                new_session,
                "--target-host",
                "host-a",
                "--prompt",
                "Continue with a new identity.",
                "--operation-id",
                operation,
                "--idempotency-key",
                "stable-key",
            ],
            SessionContinueRequest(
                **common,
                source_session_id=session,
                new_session_id=new_session,
                target_host="host-a",
                prompt="Continue with a new identity.",
            ),
        ),
        (
            [
                "status",
                "publish",
                "--update-id",
                update,
                "--task-id",
                task.task_id,
                "--session-id",
                session,
                "--status",
                "running",
                "--summary",
                "Training is active.",
                "--observed-at",
                "2026-08-03T12:00:00Z",
                "--evidence",
                "log=run-1",
                "--operation-id",
                operation,
                "--idempotency-key",
                "stable-key",
            ],
            StatusPublishRequest.model_validate(
                {
                    **common,
                    "update": {
                        "update_id": update,
                        "task_id": task.task_id,
                        "session_id": session,
                        "status": "running",
                        "summary": "Training is active.",
                        "observed_at": "2026-08-03T12:00:00Z",
                        "evidence": [{"kind": "log", "value": "run-1"}],
                    },
                }
            ),
        ),
        (
            [
                "inbox",
                "list",
                "--include-resolved",
                "--limit",
                "12",
                "--now",
                "2026-08-03T12:00:00Z",
            ],
            InboxListRequest(
                include_resolved=True,
                limit=12,
                now="2026-08-03T12:00:00Z",
            ),
        ),
        (
            [
                "inbox",
                "ack",
                update,
                "--expected-generation",
                "2",
                "--operation-id",
                operation,
                "--idempotency-key",
                "stable-key",
            ],
            InboxAckRequest(**common, update_id=update, expected_generation=2),
        ),
        (
            [
                "inbox",
                "snooze",
                update,
                "--expected-generation",
                "2",
                "--until",
                "2026-08-03T16:00:00Z",
                "--operation-id",
                operation,
                "--idempotency-key",
                "stable-key",
            ],
            InboxSnoozeRequest(
                **common,
                update_id=update,
                expected_generation=2,
                until="2026-08-03T16:00:00Z",
            ),
        ),
        (
            [
                "inbox",
                "resolve",
                update,
                "--expected-generation",
                "2",
                "--reason",
                "Decision recorded.",
                "--operation-id",
                operation,
                "--idempotency-key",
                "stable-key",
            ],
            InboxResolveRequest(
                **common,
                update_id=update,
                expected_generation=2,
                reason="Decision recorded.",
            ),
        ),
    ]


def test_human_flags_and_agent_json_call_the_same_service_requests(
    tmp_path: Path,
    task_payload,
    run_spec_payload,
    cli_spy,
) -> None:
    service, actor, opens, subprocess_calls = cli_spy
    runner = CliRunner()
    cases = _case_data(tmp_path, task_payload, run_spec_payload)
    run_spec = next(
        expected.spec
        for _args, expected in cases
        if isinstance(expected, RunStartRequest)
    )

    for args, expected in cases:
        method_count = len(service.calls)
        human = runner.invoke(app, [*args, "--project", str(tmp_path)])
        assert human.exit_code == 0, human.output
        human_call = service.calls[method_count]
        assert human_call.request == expected
        assert human_call.actor is actor
        if args[:2] == ["session", "attach"]:
            assert subprocess_calls == [["true"]]
        if args[0] == "task":
            assert "Proposal branch: research/control/" in human.output
            assert f"Proposal commit: {'a' * 40}" in human.output
        elif args[:2] == ["bootstrap", "accept"]:
            assert "the Project remains bootstrapping until that merge" in human.output
        elif args[:2] == ["bootstrap", "propose"]:
            assert "then run bootstrap accept with its exact commit" in human.output
        elif args[0] == "run":
            assert f"Run: {expected.spec.run_id}" in human.output
            assert f"Attempt: {expected.attempt_id}" in human.output

        machine_args = [args[0], args[1], "--json", "--project", str(tmp_path)]
        subprocess_count = len(subprocess_calls)
        machine = runner.invoke(app, machine_args, input=_payload(expected))
        assert machine.exit_code == 0, machine.output
        machine_call = service.calls[method_count + 1]
        assert machine_call.method == human_call.method
        assert machine_call.request == human_call.request
        assert machine_call.actor is actor
        output = json.loads(machine.output)
        assert output["success"] is True
        assert output["errors"] == []
        assert "Operation:" not in machine.output
        assert len(subprocess_calls) == subprocess_count

    mutation_opens = [options for _path, options in opens if options]
    bootstrap_options = {
        "bootstrap_operation_id": _id("operation", "1"),
        "bootstrap_proposal_commit": "9" * 40,
    }
    proposal_options = {
        "bootstrap_proposal_operation_id": _id("operation", "1"),
        "bootstrap_id": _id("bootstrap", "8"),
        "bootstrap_expected_default_head": "7" * 40,
    }
    assert mutation_opens == [
        proposal_options,
        proposal_options,
        bootstrap_options,
        bootstrap_options,
        *[
            {"task_operation_id": _id("operation", "1"), "task_command": command}
            for command in (
                "task.create",
                "task.create",
            )
        ],
        {"run_spec": run_spec},
        {"run_spec": run_spec},
        {"run_spec": run_spec},
        {"run_spec": run_spec},
        {"run_spec": run_spec},
        {"run_spec": run_spec},
        *[
            {"task_operation_id": _id("operation", "1"), "task_command": command}
            for command in (
                "task.update",
                "task.update",
                "task.cancel",
                "task.cancel",
            )
        ],
    ]


@pytest.mark.parametrize(
    ("content", "code"),
    [
        (
            '{"include_resolved":false,"limit":5,"actor_role":"manager"}',
            "validation_error",
        ),
        (
            '{"include_resolved":false,"limit":5,"unexpected":true}',
            "validation_error",
        ),
        (
            '{"include_resolved":false,"limit":5,"limit":6}',
            "invalid_json_request",
        ),
    ],
)
def test_machine_mode_returns_stable_envelope_before_opening_application(
    content: str,
    code: str,
    cli_spy,
) -> None:
    _service, _actor, opens, subprocess_calls = cli_spy
    result = CliRunner().invoke(app, ["inbox", "list", "--json"], input=content)

    assert result.exit_code == 2
    output = json.loads(result.output)
    assert set(output) == set(envelope(command="x", success=True))
    assert output["command"] == "inbox.list"
    assert output["success"] is False
    assert output["errors"][0]["code"] == code
    assert opens == []
    assert subprocess_calls == []


def test_machine_mutation_error_keeps_operation_identity_without_text_pollution(
    cli_spy,
) -> None:
    service, _actor, opens, subprocess_calls = cli_spy
    operation_id = _id("operation", "9")
    request = InboxAckRequest(
        operation_id=operation_id,
        idempotency_key="failing-ack",
        update_id=_id("update", "8"),
        expected_generation=1,
    )
    service.error = RCPError(
        code="stale_attention",
        message="Attention changed.",
    )

    result = CliRunner().invoke(
        app,
        ["inbox", "ack", "--json"],
        input=_payload(request),
    )

    assert result.exit_code == 2
    assert "Operation:" not in result.output
    output = json.loads(result.output)
    assert output["errors"][0]["code"] == "stale_attention"
    assert output["errors"][0]["context"]["operation_id"] == operation_id
    assert len(opens) == 1
    assert subprocess_calls == []


def test_inbox_human_and_json_present_the_same_management_groups(cli_spy) -> None:
    service, _actor, _opens, _subprocess_calls = cli_spy
    service.inbox_items = (
        _attention_item(
            kind="waiting",
            fill="7",
            status="blocked",
            summary="Waiting for the external benchmark.",
            blocker_category="waiting",
            blocker_detail="The benchmark service has not returned.",
        ),
        _attention_item(
            kind="running",
            fill="6",
            status="running",
            summary="Training is still running.",
        ),
        _attention_item(
            kind="failed_or_lost",
            fill="5",
            status="blocked",
            summary="The remote Session was lost.",
            blocker_category="lost",
            blocker_detail="The owning host is unreachable.",
        ),
        _attention_item(
            kind="stale_or_needs_rerun",
            fill="4",
            status="blocked",
            summary="The evaluator changed after this result.",
            blocker_category="stale",
            blocker_detail="The result needs an impact decision.",
        ),
        _attention_item(
            kind="needs_review",
            fill="3",
            status="ready_for_review",
            summary="The result bundle is ready.",
            suggested_next_action="Review the exact Submission head.",
        ),
        _attention_item(
            kind="blocked",
            fill="2",
            status="blocked",
            summary="Dataset access is blocked.",
            blocker_category="data_access",
            blocker_detail="The immutable dataset URI is not reachable.",
        ),
        _attention_item(
            kind="needs_decision",
            fill="1",
            status="needs_input",
            summary="Choose whether to rerun seed 3.\x1b[31m",
            decision_needed={
                "question": "Rerun the missing seed?",
                "options": ("rerun", "accept four seeds"),
            },
            suggested_next_action="Choose one option before submission.",
        ),
    )
    runner = CliRunner()

    human = runner.invoke(app, ["inbox", "list"])
    machine = runner.invoke(
        app,
        ["inbox", "list", "--json"],
        input=_payload(InboxListRequest()),
    )

    assert human.exit_code == 0, human.output
    assert machine.exit_code == 0, machine.output
    headings = [
        "Needs Decision (1)",
        "Blocked (1)",
        "Needs Review (1)",
        "Stale or Needs Rerun (1)",
        "Failed or Lost (1)",
        "Running (1)",
        "Waiting (1)",
    ]
    assert [human.output.index(heading) for heading in headings] == sorted(
        human.output.index(heading) for heading in headings
    )
    assert "Decision: Rerun the missing seed?" in human.output
    assert "Blocker: The immutable dataset URI is not reachable." in human.output
    assert "Next: Review the exact Submission head." in human.output
    assert "\\u001b[31m" in human.output
    assert "\x1b[31m" not in human.output

    payload = json.loads(machine.output)
    assert [group["group"] for group in payload["data"]["groups"]] == [
        "needs_decision",
        "blocked",
        "needs_review",
        "stale_or_needs_rerun",
        "failed_or_lost",
        "running",
        "waiting",
    ]
    assert sum(group["count"] for group in payload["data"]["groups"]) == len(
        payload["data"]["items"]
    )


def test_guided_and_flag_task_creation_build_the_same_typed_request(
    tmp_path: Path,
    cli_spy,
    monkeypatch,
) -> None:
    service, _actor, _opens, _subprocess_calls = cli_spy
    observed_at = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    monkeypatch.setattr("researchctl.phase2_cli.utc_now", lambda: observed_at)
    operation_id = _id("operation", "8")
    task_id = _id("task", "9")
    common = [
        "--task-id",
        task_id,
        "--operation-id",
        operation_id,
        "--idempotency-key",
        "guided-parity",
        "--project",
        str(tmp_path),
    ]
    required_input = InputIdentity(
        kind="dataset",
        logical_id="validation-split",
        version="2026-08-01",
    )
    input_path = tmp_path / "required-input.yaml"
    input_path.write_text(dump_yaml(required_input), encoding="utf-8")
    runner = CliRunner()

    guided = runner.invoke(
        app,
        ["task", "create", "--guided", *common],
        input=(
            "MAR-17\n"
            "Evaluate a stopping policy\n"
            "Determine whether the policy improves validation loss.\n"
            "The comparison and uncertainty are recorded.\n"
            "n\n"
            "on-prem\n"
            "src/training\n"
            "n\n"
            "A comparison report with uncertainty.\n"
            "n\n"
            "\n"
            "Whether to promote the candidate policy.\n"
            "y\n"
            "dataset\n"
            "validation-split\n"
            "version\n"
            "2026-08-01\n"
            "\n"
            "\n"
            "n\n"
            "n\n"
        ),
    )
    flags = runner.invoke(
        app,
        [
            "task",
            "create",
            *common,
            "--key",
            "MAR-17",
            "--title",
            "Evaluate a stopping policy",
            "--goal",
            "Determine whether the policy improves validation loss.",
            "--done-when",
            "The comparison and uncertainty are recorded.",
            "--execution-domain",
            "on-prem",
            "--allow-write",
            "src/training",
            "--deliverable",
            "A comparison report with uncertainty.",
            "--required-input-file",
            str(input_path),
            "--next-decision",
            "Whether to promote the candidate policy.",
        ],
    )

    assert guided.exit_code == 0, guided.output
    assert flags.exit_code == 0, flags.output
    guided_request = service.calls[-2].request
    flags_request = service.calls[-1].request
    assert isinstance(guided_request, TaskCreateRequest)
    assert guided_request == flags_request
    assert guided_request.task.execution_domain == "on-prem"
    assert guided_request.task.required_inputs == (required_input,)
    assert guided_request.task.next_decision == (
        "Whether to promote the candidate policy."
    )


def test_flag_task_creation_identifies_a_missing_done_condition(
    tmp_path: Path,
    cli_spy,
) -> None:
    _service, _actor, opens, _subprocess_calls = cli_spy
    result = CliRunner().invoke(
        app,
        [
            "task",
            "create",
            "--project",
            str(tmp_path),
            "--key",
            "MAR-17",
            "--title",
            "Evaluate a stopping policy",
            "--goal",
            "Determine whether the policy improves validation loss.",
            "--execution-domain",
            "on-prem",
            "--allow-write",
            "src/training",
            "--deliverable",
            "A comparison report with uncertainty.",
        ],
    )

    assert result.exit_code == 2
    assert "Human mode requires at least one --done-when." in result.output
    assert "--done-when" in result.output
    assert opens == []


def test_linear_configure_human_and_json_share_one_secretless_request_path(
    tmp_path: Path,
    cli_spy,
) -> None:
    service, actor, opens, _subprocess_calls = cli_spy
    operation_id = _id("operation", "c")
    policy = LinearProjectionPolicy(
        workspace_id="11111111-1111-4111-8111-111111111111",
        team_id="22222222-2222-4222-8222-222222222222",
        notification_author_ids=(
            "33333333-3333-4333-8333-333333333333",
        ),
    )
    policy_file = tmp_path / "linear-policy.yaml"
    policy_file.write_text(dump_yaml(policy), encoding="utf-8")
    request = LinearConfigureRequest(
        operation_id=operation_id,
        idempotency_key="linear-configure",
        expected_default_head="a" * 40,
        policy=policy,
    )
    runner = CliRunner()

    human = runner.invoke(
        app,
        [
            "linear",
            "configure",
            "--project",
            str(tmp_path),
            "--policy-file",
            str(policy_file),
            "--expected-default-head",
            "a" * 40,
            "--operation-id",
            operation_id,
            "--idempotency-key",
            "linear-configure",
        ],
    )
    machine = runner.invoke(
        app,
        ["linear", "configure", "--json", "--project", str(tmp_path)],
        input=_payload(request),
    )

    assert human.exit_code == 0, human.output
    assert machine.exit_code == 0, machine.output
    assert service.calls[-2].method == "linear_configure"
    assert service.calls[-2].request == request
    assert service.calls[-2].actor is actor
    assert service.calls[-1].request == request
    expected_options = {
        "linear_operation_id": operation_id,
        "linear_expected_default_head": "a" * 40,
    }
    assert opens == [(tmp_path, expected_options), (tmp_path, expected_options)]
    assert "Proposal branch: research/control/" in human.output
    assert json.loads(machine.output)["command"] == "linear.configure"

    help_result = runner.invoke(app, ["linear", "configure", "--help"])
    assert help_result.exit_code == 0
    lowered = help_result.output.lower()
    assert "api-key" not in lowered
    assert "token" not in lowered
    assert "secret" not in lowered


def test_plan_review_configure_has_one_explicit_manager_request_path(
    tmp_path: Path,
    cli_spy,
) -> None:
    service, actor, opens, _subprocess_calls = cli_spy
    operation_id = _id("operation", "e")
    review_policy = PlanReviewPolicy(
        provider="codex",
        model="gpt-test-reviewer",
        policy_version="plan-review-v1",
        timeout_seconds=60,
    )
    request = PlanReviewConfigureRequest(
        operation_id=operation_id,
        idempotency_key="plan-review-configure",
        expected_default_head="a" * 40,
        review_policy=review_policy,
    )
    runner = CliRunner()

    human = runner.invoke(
        app,
        [
            "plan",
            "configure-review",
            "--project",
            str(tmp_path),
            "--provider",
            "codex",
            "--model",
            "gpt-test-reviewer",
            "--policy-version",
            "plan-review-v1",
            "--timeout-seconds",
            "60",
            "--expected-default-head",
            "a" * 40,
            "--operation-id",
            operation_id,
            "--idempotency-key",
            "plan-review-configure",
        ],
    )
    machine = runner.invoke(
        app,
        ["plan", "configure-review", "--json", "--project", str(tmp_path)],
        input=_payload(request),
    )

    assert human.exit_code == 0, human.output
    assert machine.exit_code == 0, machine.output
    assert service.calls[-2].method == "plan_review_configure"
    assert service.calls[-2].request == request
    assert service.calls[-2].actor is actor
    assert service.calls[-1].request == request
    expected_options = {
        "plan_review_operation_id": operation_id,
        "plan_review_expected_default_head": "a" * 40,
    }
    assert opens == [(tmp_path, expected_options), (tmp_path, expected_options)]
    assert "Proposal branch: research/control/" in human.output
    assert json.loads(machine.output)["command"] == "plan.configure-review"


def test_plan_generated_output_is_relative_to_discovered_project_root(
    initialized_repository: Path,
) -> None:
    nested = initialized_repository / "src" / "nested"
    nested.mkdir(parents=True)
    output_parent = initialized_repository / "generated"
    output_parent.mkdir()
    value = PlanReviewPolicy(
        provider="codex",
        model="gpt-test-reviewer",
        policy_version="plan-review-v1",
        timeout_seconds=60,
    )

    written = _write_generated_model(
        project=nested,
        output_file=Path("generated/review-policy.yaml"),
        value=value,
    )

    assert written == output_parent / "review-policy.yaml"
    assert written.read_text(encoding="utf-8") == dump_yaml(value)


def test_linear_configure_json_cannot_spoof_actor_identity(cli_spy) -> None:
    _service, _actor, opens, _subprocess_calls = cli_spy
    payload = {
        "operation_id": _id("operation", "d"),
        "idempotency_key": "forged-actor",
        "expected_default_head": "a" * 40,
        "policy": {
            "schema_version": "0.1",
            "workspace_id": "11111111-1111-4111-8111-111111111111",
            "team_id": "22222222-2222-4222-8222-222222222222",
        },
        "actor_role": "manager",
    }

    result = CliRunner().invoke(
        app,
        ["linear", "configure", "--json"],
        input=json.dumps(payload),
    )

    assert result.exit_code == 2
    assert json.loads(result.output)["errors"][0]["code"] == "validation_error"
    assert opens == []


def test_linear_delivery_list_show_human_and_json_share_read_only_service(
    tmp_path: Path,
    cli_spy,
) -> None:
    service, actor, opens, _subprocess_calls = cli_spy
    list_request = LinearDeliveryListRequest(
        topic="linear.session-reply.v1",
        state="pending",
        limit=7,
    )
    show_request = LinearDeliveryShowRequest(
        topic="linear.accepted-result.v1",
        outbox_id="linear-event-test",
    )
    runner = CliRunner()

    human_list = runner.invoke(
        app,
        [
            "linear",
            "delivery",
            "list",
            "--project",
            str(tmp_path),
            "--topic",
            list_request.topic,
            "--state",
            "pending",
            "--limit",
            "7",
        ],
    )
    json_list = runner.invoke(
        app,
        [
            "linear",
            "delivery",
            "list",
            "--json",
            "--project",
            str(tmp_path),
        ],
        input=_payload(list_request),
    )
    human_show = runner.invoke(
        app,
        [
            "linear",
            "delivery",
            "show",
            show_request.outbox_id,
            "--topic",
            show_request.topic,
            "--project",
            str(tmp_path),
        ],
    )
    json_show = runner.invoke(
        app,
        [
            "linear",
            "delivery",
            "show",
            "--json",
            "--project",
            str(tmp_path),
        ],
        input=_payload(show_request),
    )

    for result in (human_list, json_list, human_show, json_show):
        assert result.exit_code == 0, result.output
    assert [call.method for call in service.calls[-4:]] == [
        "linear_delivery_list",
        "linear_delivery_list",
        "linear_delivery_show",
        "linear_delivery_show",
    ]
    assert service.calls[-4].request == list_request
    assert service.calls[-3].request == list_request
    assert service.calls[-2].request == show_request
    assert service.calls[-1].request == show_request
    assert all(call.actor is actor for call in service.calls[-4:])
    assert opens == [(tmp_path, {})] * 4
    assert "[pending]" in human_list.output
    assert "attempts=2" in human_list.output
    assert "Active claim: claim-active" in human_list.output
    assert "Receipt: linear-receipt-test" in human_show.output
    assert "Lineage:" in human_show.output
    assert json.loads(json_list.output)["command"] == "linear.delivery.list"
    assert json.loads(json_show.output)["command"] == "linear.delivery.show"

    help_result = runner.invoke(app, ["linear", "delivery", "list", "--help"])
    assert help_result.exit_code == 0
    lowered = help_result.output.lower()
    assert "api-key" not in lowered
    assert "token" not in lowered
    assert "secret" not in lowered


def test_linear_delivery_json_rejects_actor_spoof_before_open(cli_spy) -> None:
    _service, _actor, opens, _subprocess_calls = cli_spy
    result = CliRunner().invoke(
        app,
        ["linear", "delivery", "list", "--json"],
        input=json.dumps({"limit": 10, "actor_role": "manager"}),
    )

    assert result.exit_code == 2
    assert json.loads(result.output)["errors"][0]["code"] == "validation_error"
    assert opens == []
