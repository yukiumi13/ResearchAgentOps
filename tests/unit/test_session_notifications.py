from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from researchctl.adapters.git_commit import GitSessionCommitVerifier
from researchctl.domain.enums import NotificationRoute, NotificationState, SessionState
from researchctl.domain.models import (
    AgentPolicy,
    ExecutionDomainPolicy,
    ProjectPolicy,
    TaskRecord,
)
from researchctl.errors import RCPError
from researchctl.runtime import RuntimeSession, RuntimeStore
from researchctl.services.actor import ActorContext, ActorRole, CredentialKind
from researchctl.services.application import ApplicationService
from researchctl.services.requests import (
    NotificationAckRequest,
    NotificationListRequest,
    NotificationReplyRequest,
    NotificationSendRequest,
    linear_notification_request_digest,
)
from researchctl.services.task_records import TaskRecordRepository


NOW = datetime(2026, 8, 3, 12, 0, 0, tzinfo=UTC)
PROJECT_ID = "project_20260803T120000Z_" + "a" * 24
TASK_ID = "task_20260802T123456Z_" + "a" * 24
SESSION_ID = "session_20260803T120000Z_" + "b" * 24
OTHER_SESSION_ID = "session_20260803T120000Z_" + "c" * 24
LINEAR_ISSUE_ID = "0199a213-81c0-4800-8aa1-bbab2a035a53"
LINEAR_WORKSPACE_ID = "0199a213-81c0-4800-8aa1-bbab2a035a52"
LINEAR_THREAD_ID = "0199a213-81c0-4800-8aa1-bbab2a035a54"
LINEAR_COMMENT_ID = "0199a213-81c0-4800-8aa1-bbab2a035a55"
LINEAR_ACCEPTED_COMMENT_ID = "0199a213-81c0-4800-8aa1-bbab2a035a50"
REPORT_ID = "report_20260803T120000Z_" + "d" * 24
LINEAR_EVENT_ID = "linear-event-" + "e" * 64
LINEAR_RECEIPT_ID = "linear-receipt-" + "f" * 64


def _id(kind: str, fill: str) -> str:
    return f"{kind}_20260803T120000Z_{fill * 24}"


def _manager() -> ActorContext:
    return ActorContext(
        actor_id="uid-1000",
        role=ActorRole.MANAGER,
        credential_kind=CredentialKind.LOCAL_OS,
    )


def _agent(session_id: str = SESSION_ID) -> ActorContext:
    return ActorContext(
        actor_id=f"agent-{session_id}",
        role=ActorRole.AGENT,
        credential_kind=CredentialKind.SESSION_CAPABILITY,
        bound_session_id=session_id,
    )


class RecordingCommitVerifier:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def require_reachable(self, commit_sha: str, branch: str) -> None:
        self.calls.append((commit_sha, branch))


def _origin(
    *,
    issue_id: str = LINEAR_ISSUE_ID,
    comment_id: str = LINEAR_COMMENT_ID,
) -> dict[str, object]:
    return {
        "transport": "linear",
        "workspace_id": LINEAR_WORKSPACE_ID,
        "issue_id": issue_id,
        "thread_id": LINEAR_THREAD_ID,
        "comment_id": comment_id,
        "source_marker": {
            "agent_id": "researchctl-app",
            "task_id": TASK_ID,
            "session_id": SESSION_ID,
            "report_id": REPORT_ID,
            "marker_digest": "sha256:" + "e" * 64,
        },
    }


def _send_request(
    *,
    operation_fill: str = "1",
    notification_fill: str = "1",
    comment_id: str = LINEAR_COMMENT_ID,
    issue_id: str = LINEAR_ISSUE_ID,
) -> NotificationSendRequest:
    return NotificationSendRequest(
        operation_id=_id("operation", operation_fill),
        idempotency_key=f"send-{notification_fill}",
        notification_id=_id("notification", notification_fill),
        directive_kind="reply",
        task_id=TASK_ID,
        session_id=SESSION_ID,
        commit_sha="1" * 40,
        message="Review this exact commit and reply in this Linear thread.",
        origin=_origin(issue_id=issue_id, comment_id=comment_id),
    )


def _service(
    tmp_path: Path,
    task_payload,
) -> tuple[
    ApplicationService,
    RuntimeStore,
    RecordingCommitVerifier,
    list[datetime],
]:
    (tmp_path / ".research" / "tasks").mkdir(parents=True)
    tasks = TaskRecordRepository(tmp_path)
    task = TaskRecord.model_validate(
        task_payload(linear_issue_id=LINEAR_ISSUE_ID)
    )
    tasks.create(task)
    runtime = RuntimeStore(tmp_path / "runtime.sqlite3")
    runtime.save_session(
        RuntimeSession(
            session_id=SESSION_ID,
            project_id=PROJECT_ID,
            task_id=task.task_id,
            state=SessionState.ACTIVE,
            created_at=NOW,
            updated_at=NOW,
            branch=f"research/session/{SESSION_ID}",
        )
    )
    verifier = RecordingCommitVerifier()
    current_time = [NOW + timedelta(seconds=1)]
    service = ApplicationService(
        project_id=PROJECT_ID,
        policy=ProjectPolicy(
            agent=AgentPolicy(
                accepted_paths_denied=(
                    ".research/decisions/**",
                    ".research/policies/**",
                    ".research/project.yaml",
                    ".research/reports/**",
                    ".research/tasks/**",
                )
            ),
            execution_domains=(
                ExecutionDomainPolicy(
                    execution_domain="on-prem",
                    host_pools=("interactive",),
                ),
            ),
        ),
        tasks=tasks,
        runtime=runtime,
        notification_commits=verifier,
        clock=lambda: current_time[0],
    )
    return service, runtime, verifier, current_time


def _seed_verified_linear_origin(
    runtime: RuntimeStore,
    request: NotificationSendRequest,
) -> None:
    origin = request.origin
    assert origin.source_marker is not None
    runtime.enqueue_linear_projection(
        project_id=PROJECT_ID,
        event_id=LINEAR_EVENT_ID,
        aggregate_id=f"{REPORT_ID}:1",
        payload={
            "version": 1,
            "event_id": LINEAR_EVENT_ID,
            "task_id": TASK_ID,
            "report_id": REPORT_ID,
        },
        created_at=NOW,
    )
    claim = runtime.claim_linear_delivery(
        project_id=PROJECT_ID,
        claim_id="claim-linear-accepted-result",
        claimed_at=NOW,
    )
    assert claim is not None
    runtime.finish_linear_delivery(
        claim_id=claim.claim_id,
        state="delivered",
        error_code=None,
        receipt={
            "version": 1,
            "receipt_id": LINEAR_RECEIPT_ID,
            "credential_identity": "researchctl-app",
            "event_id": LINEAR_EVENT_ID,
            "task_id": request.task_id,
            "workspace_id": origin.workspace_id,
            "issue_id": origin.issue_id,
            "thread_id": origin.thread_id,
            "comment_id": LINEAR_ACCEPTED_COMMENT_ID,
            "payload_digest": "sha256:" + "a" * 64,
            "transport_digest": "sha256:" + "b" * 64,
            "marker": "<!-- accepted result marker -->",
            "source_marker": origin.source_marker.model_dump(mode="json"),
        },
        finished_at=NOW,
    )
    ingress = runtime.record_verified_linear_ingress(
        project_id=PROJECT_ID,
        authenticated_app_id="researchctl-linear-app",
        mentioned_app_id="researchctl-linear-app",
        author_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        credential_identity="researchctl-app",
        workspace_id=origin.workspace_id,
        issue_id=origin.issue_id,
        thread_id=origin.thread_id,
        comment_id=origin.comment_id,
        webhook_event_id=f"linear-webhook:{origin.comment_id}",
        task_id=request.task_id,
        source_marker=origin.source_marker,
        command_digest=linear_notification_request_digest(request),
        observed_payload_digest="sha256:" + "d" * 64,
        verified_at=NOW,
    )
    assert ingress.source_marker == origin.source_marker


def test_source_marker_must_bind_requested_session_and_task() -> None:
    source = _origin()
    marker = dict(source["source_marker"])  # type: ignore[arg-type]
    marker["session_id"] = OTHER_SESSION_ID
    source["source_marker"] = marker

    with pytest.raises(ValidationError):
        NotificationSendRequest(
            operation_id=_id("operation", "0"),
            idempotency_key="mismatched-marker",
            notification_id=_id("notification", "0"),
            directive_kind="reply",
            task_id=TASK_ID,
            session_id=SESSION_ID,
            commit_sha="1" * 40,
            message="This must be rejected.",
            origin=source,
        )


def test_notification_round_trip_is_session_scoped_and_reply_is_durable(
    tmp_path: Path,
    task_payload,
) -> None:
    service, runtime, verifier, _ = _service(tmp_path, task_payload)
    request = _send_request()
    _seed_verified_linear_origin(runtime, request)

    sent = service.notification_send(request, _manager())
    replayed = service.notification_send(request, _manager())

    assert replayed == sent
    assert sent.terminal_result == "routed_to_session"
    assert sent.data["notification"]["route"] == "session"
    assert verifier.calls == [
        ("1" * 40, f"research/session/{SESSION_ID}")
    ]
    unauthorized = _send_request(
        operation_fill="8",
        notification_fill="8",
        comment_id="0199a213-81c0-4800-8aa1-bbab2a035a58",
    )
    with pytest.raises(RCPError) as agent_send:
        service.notification_send(unauthorized, _agent())
    assert agent_send.value.code == "authorization_denied"
    visible = service.notification_list(
        NotificationListRequest(),
        _agent(),
    )
    assert [item.notification_id for item in visible] == [
        request.notification_id
    ]

    with pytest.raises(RCPError) as cross_session:
        service.notification_list(
            NotificationListRequest(session_id=OTHER_SESSION_ID),
            _agent(),
        )
    assert cross_session.value.code == "session_scope_denied"

    acked = service.notification_ack(
        NotificationAckRequest(
            operation_id=_id("operation", "2"),
            idempotency_key="ack-notification-1",
            notification_id=request.notification_id,
            expected_revision=1,
        ),
        _agent(),
    )
    assert acked.data["notification"]["state"] == "acknowledged"
    assert acked.data["notification"]["revision"] == 2

    replied = service.notification_reply(
        NotificationReplyRequest(
            operation_id=_id("operation", "3"),
            idempotency_key="reply-notification-1",
            notification_id=request.notification_id,
            expected_revision=2,
            reply_id=_id("reply", "1"),
            body="I reviewed the commit; the evidence path is correct.",
        ),
        _agent(),
    )

    assert replied.terminal_result == "reply_queued"
    notification = runtime.get_notification(request.notification_id)
    assert notification is not None
    assert notification.state is NotificationState.REPLIED
    assert notification.revision == 3
    outbox = runtime.list_notification_reply_outbox(state="pending")
    assert len(outbox) == 1
    assert outbox[0].payload["source_thread_id"] == LINEAR_THREAD_ID
    assert outbox[0].payload["source_comment_id"] == LINEAR_COMMENT_ID
    assert outbox[0].payload["linear_issue_id"] == LINEAR_ISSUE_ID
    assert outbox[0].payload["commit_sha"] == "1" * 40
    assert outbox[0].payload["marker"] == {
        "agent_id": f"agent-{SESSION_ID}",
        "task_id": TASK_ID,
        "session_id": SESSION_ID,
        "report_id": REPORT_ID,
        "notification_id": request.notification_id,
        "reply_id": _id("reply", "1"),
    }
    assert service.notification_list(
        NotificationListRequest(),
        _agent(),
    ) == ()
    assert len(
        service.notification_list(
            NotificationListRequest(include_closed=True),
            _agent(),
        )
    ) == 1


def test_terminal_session_falls_back_without_losing_pending_notification(
    tmp_path: Path,
    task_payload,
) -> None:
    service, runtime, _, current_time = _service(tmp_path, task_payload)
    request = _send_request(
        operation_fill="4",
        notification_fill="2",
        comment_id="0199a213-81c0-4800-8aa1-bbab2a035a56",
    )
    _seed_verified_linear_origin(runtime, request)
    service.notification_send(request, _manager())
    current_time[0] += timedelta(seconds=1)

    runtime.update_session_state(
        SESSION_ID,
        SessionState.LOST,
        current_time[0],
    )

    notification = runtime.get_notification(request.notification_id)
    assert notification is not None
    assert notification.route is NotificationRoute.MANAGER_EXCEPTION
    assert notification.fallback_reason == "session_lost"
    assert notification.revision == 2
    assert service.notification_list(
        NotificationListRequest(),
        _agent(),
    ) == ()
    exceptions = service.notification_list(
        NotificationListRequest(manager_exceptions_only=True),
        _manager(),
    )
    assert [item.notification_id for item in exceptions] == [
        request.notification_id
    ]

    with pytest.raises(RCPError) as agent_reply:
        service.notification_reply(
            NotificationReplyRequest(
                operation_id=_id("operation", "5"),
                idempotency_key="terminal-agent-reply",
                notification_id=request.notification_id,
                expected_revision=1,
                reply_id=_id("reply", "2"),
                body="A lost Session must not reply.",
            ),
            _agent(),
        )
    assert agent_reply.value.code == "notification_manager_exception"

    with pytest.raises(RCPError) as stale_manager:
        service.notification_ack(
            NotificationAckRequest(
                operation_id=_id("operation", "9"),
                idempotency_key="stale-manager-fallback-ack",
                notification_id=request.notification_id,
                expected_revision=1,
            ),
            _manager(),
        )
    assert stale_manager.value.code == "stale_notification"

    manager_reply = service.notification_reply(
        NotificationReplyRequest(
            operation_id=_id("operation", "6"),
            idempotency_key="manager-fallback-reply",
            notification_id=request.notification_id,
            expected_revision=2,
            reply_id=_id("reply", "3"),
            body="The Session was lost; a manager reviewed the requested commit.",
        ),
        _manager(),
    )
    assert manager_reply.terminal_result == "reply_queued"
    assert len(runtime.list_notification_reply_outbox()) == 1


def test_linear_issue_binding_is_checked_before_commit_reachability(
    tmp_path: Path,
    task_payload,
) -> None:
    service, runtime, verifier, _ = _service(tmp_path, task_payload)
    request = _send_request(
        operation_fill="7",
        notification_fill="3",
        issue_id="0199a213-81c0-4800-8aa1-bbab2a035a57",
    )
    _seed_verified_linear_origin(runtime, request)

    with pytest.raises(RCPError) as mismatch:
        service.notification_send(request, _manager())

    assert mismatch.value.code == "notification_linear_issue_mismatch"
    assert verifier.calls == []


def test_unverified_linear_origin_is_rejected_before_commit_reachability(
    tmp_path: Path,
    task_payload,
) -> None:
    service, _, verifier, _ = _service(tmp_path, task_payload)
    request = _send_request(
        operation_fill="d",
        notification_fill="d",
        comment_id="0199a213-81c0-4800-8aa1-bbab2a035a5d",
    )

    with pytest.raises(RCPError) as unverified:
        service.notification_send(request, _manager())

    assert unverified.value.code == "linear_notification_origin_unverified"
    assert verifier.calls == []


def test_contextual_source_marker_mismatch_is_rejected_before_commit_check(
    tmp_path: Path,
    task_payload,
) -> None:
    service, runtime, verifier, _ = _service(tmp_path, task_payload)
    verified = _send_request(
        operation_fill="e",
        notification_fill="e",
        comment_id="0199a213-81c0-4800-8aa1-bbab2a035a5e",
    )
    _seed_verified_linear_origin(runtime, verified)
    forged_payload = verified.model_dump(mode="json")
    forged_marker = forged_payload["origin"]["source_marker"]
    assert isinstance(forged_marker, dict)
    forged_marker["marker_digest"] = "sha256:" + "0" * 64
    forged = NotificationSendRequest.model_validate(forged_payload)

    with pytest.raises(RCPError) as mismatch:
        service.notification_send(forged, _manager())

    assert mismatch.value.code == "linear_notification_source_receipt_mismatch"
    assert verifier.calls == []


@pytest.mark.parametrize(
    ("mutation", "error_code"),
    [
        ("session", "linear_notification_request_receipt_mismatch"),
        ("commit", "linear_notification_request_receipt_mismatch"),
        ("message", "linear_notification_request_receipt_mismatch"),
        ("notification", "linear_notification_request_receipt_mismatch"),
        ("remove_marker", "linear_notification_source_receipt_mismatch"),
    ],
)
def test_verified_comment_cannot_be_rebound_on_replay(
    tmp_path: Path,
    task_payload,
    mutation: str,
    error_code: str,
) -> None:
    service, runtime, verifier, _ = _service(tmp_path, task_payload)
    request = _send_request(
        operation_fill="6",
        notification_fill="6",
        comment_id="0199a213-81c0-4800-8aa1-bbab2a035a5f",
    )
    _seed_verified_linear_origin(runtime, request)
    service.notification_send(request, _manager())
    verifier.calls.clear()
    updates: dict[str, object] = {
        "operation_id": _id("operation", "f"),
        "idempotency_key": f"hostile-replay-{mutation}",
    }
    if mutation == "session":
        updates["session_id"] = OTHER_SESSION_ID
    elif mutation == "commit":
        updates["commit_sha"] = "2" * 40
    elif mutation == "message":
        updates["message"] = "A substituted notification body."
    elif mutation == "notification":
        updates["notification_id"] = _id("notification", "f")
    else:
        updates["origin"] = request.origin.model_copy(
            update={"source_marker": None}
        )
    hostile = request.model_copy(update=updates)

    with pytest.raises(RCPError) as mismatch:
        service.notification_send(hostile, _manager())

    assert mismatch.value.code == error_code
    assert verifier.calls == []
    original = runtime.get_notification(request.notification_id)
    assert original is not None
    assert original.commit_sha == request.commit_sha
    assert original.message == request.message
    if mutation == "notification":
        assert runtime.get_notification(_id("notification", "f")) is None


def _git(repository: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def test_git_commit_verifier_requires_commit_reachable_from_exact_session_branch(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    (repository / "result.txt").write_text("base\n", encoding="utf-8")
    _git(repository, "add", "result.txt")
    _git(
        repository,
        "-c",
        "user.name=Tests",
        "-c",
        "user.email=tests@example.invalid",
        "commit",
        "-m",
        "base",
    )
    base = _git(repository, "rev-parse", "HEAD")
    _git(repository, "branch", "research/session/test", base)
    (repository / "result.txt").write_text("unreachable\n", encoding="utf-8")
    _git(repository, "add", "result.txt")
    _git(
        repository,
        "-c",
        "user.name=Tests",
        "-c",
        "user.email=tests@example.invalid",
        "commit",
        "-m",
        "other branch only",
    )
    unreachable = _git(repository, "rev-parse", "HEAD")
    verifier = GitSessionCommitVerifier(repository)

    verifier.require_reachable(base, "research/session/test")

    with pytest.raises(RCPError) as not_reachable:
        verifier.require_reachable(unreachable, "research/session/test")
    assert not_reachable.value.code == "notification_commit_unreachable"
    with pytest.raises(RCPError) as missing_commit:
        verifier.require_reachable("f" * 40, "research/session/test")
    assert missing_commit.value.code == "notification_commit_not_found"
    with pytest.raises(RCPError) as missing_branch:
        verifier.require_reachable(base, "research/session/missing")
    assert missing_branch.value.code == "notification_session_branch_not_found"


def test_notification_mutations_resume_after_store_write_before_operation_finish(
    tmp_path: Path,
    task_payload,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, runtime, _, current_time = _service(tmp_path, task_payload)
    send = _send_request(
        operation_fill="a",
        notification_fill="a",
        comment_id="0199a213-81c0-4800-8aa1-bbab2a035a59",
    )
    _seed_verified_linear_origin(runtime, send)
    original_finish = service._finish

    def crash_before_finish(*_args, **_kwargs):
        raise RuntimeError("simulated process crash")

    monkeypatch.setattr(service, "_finish", crash_before_finish)
    with pytest.raises(RuntimeError, match="simulated process crash"):
        service.notification_send(send, _manager())
    assert runtime.get_notification(send.notification_id) is not None

    current_time[0] += timedelta(seconds=1)
    monkeypatch.setattr(service, "_finish", original_finish)
    resumed_send = service.notification_send(send, _manager())
    assert resumed_send.terminal_result == "routed_to_session"
    send_operation = runtime.get_operation(send.operation_id)
    assert send_operation is not None
    assert [event.kind for event in send_operation.events].count(
        "session_notification_persisted"
    ) == 1

    ack = NotificationAckRequest(
        operation_id=_id("operation", "b"),
        idempotency_key="crash-resume-ack",
        notification_id=send.notification_id,
        expected_revision=1,
    )
    monkeypatch.setattr(service, "_finish", crash_before_finish)
    with pytest.raises(RuntimeError, match="simulated process crash"):
        service.notification_ack(ack, _agent())
    assert runtime.get_notification(send.notification_id).revision == 2

    current_time[0] += timedelta(seconds=1)
    monkeypatch.setattr(service, "_finish", original_finish)
    resumed_ack = service.notification_ack(ack, _agent())
    assert resumed_ack.terminal_result == "already_acknowledged"
    ack_operation = runtime.get_operation(ack.operation_id)
    assert ack_operation is not None
    assert [event.kind for event in ack_operation.events].count(
        "session_notification_acknowledged"
    ) == 1

    reply = NotificationReplyRequest(
        operation_id=_id("operation", "c"),
        idempotency_key="crash-resume-reply",
        notification_id=send.notification_id,
        expected_revision=2,
        reply_id=_id("reply", "c"),
        body="The exact commit is ready for review.",
    )
    monkeypatch.setattr(service, "_finish", crash_before_finish)
    with pytest.raises(RuntimeError, match="simulated process crash"):
        service.notification_reply(reply, _agent())
    assert len(runtime.list_notification_reply_outbox()) == 1

    current_time[0] += timedelta(seconds=1)
    monkeypatch.setattr(service, "_finish", original_finish)
    resumed_reply = service.notification_reply(reply, _agent())
    assert resumed_reply.terminal_result == "reply_queued"
    assert len(runtime.list_notification_reply_outbox()) == 1
    reply_operation = runtime.get_operation(reply.operation_id)
    assert reply_operation is not None
    assert [event.kind for event in reply_operation.events].count(
        "session_notification_reply_persisted"
    ) == 1
