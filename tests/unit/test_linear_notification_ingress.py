from __future__ import annotations

import pytest

from researchctl.domain.models import SessionNotificationSourceMarker
from researchctl.errors import RCPError
from researchctl.runtime import RuntimeStore
from researchctl.services.actor import ActorContext, ActorRole, CredentialKind
from researchctl.services.linear_notification_ingress import (
    AuthenticatedLinearNotificationEvent,
    LinearCommentEnvelope,
    LinearNotificationIngress,
    LinearNotificationIngressFacade,
    parse_linear_notification_command,
)

TASK_ID = "task_20260803T120000Z_" + "a" * 24
SESSION_ID = "session_20260803T120000Z_" + "b" * 24
ISSUE_ID = "0199a213-81c0-4800-8aa1-bbab2a035a53"
WORKSPACE_ID = "0199a213-81c0-4800-8aa1-bbab2a035a52"
THREAD_ID = "0199a213-81c0-4800-8aa1-bbab2a035a54"
COMMENT_ID = "0199a213-81c0-4800-8aa1-bbab2a035a55"
COMMIT = "c" * 40
AUTHOR_ID = "0199a213-81c0-4800-8aa1-bbab2a035a56"
OTHER_WORKSPACE_ID = "0199a213-81c0-4800-8aa1-bbab2a035a57"


class TaskResolver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def require_task_id(self, workspace_id: str, issue_id: str) -> str:
        self.calls.append((workspace_id, issue_id))
        return TASK_ID


class MarkerResolver:
    def __init__(self, *, task_id: str = TASK_ID) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.marker = SessionNotificationSourceMarker(
            agent_id=f"agent-{SESSION_ID}",
            task_id=task_id,
            session_id=SESSION_ID,
            report_id="report_20260803T120000Z_" + "d" * 24,
            marker_digest="sha256:" + "e" * 64,
        )

    def require_source_marker(
        self,
        *,
        workspace_id: str,
        issue_id: str,
        thread_id: str,
    ) -> SessionNotificationSourceMarker:
        self.calls.append((workspace_id, issue_id, thread_id))
        return self.marker


class NeverTaskReader:
    def __init__(self) -> None:
        self.calls = 0

    def list(self):
        self.calls += 1
        raise AssertionError("identity mismatch must fail before Task resolution")


class FakeNotificationApplication:
    def __init__(self, runtime: RuntimeStore, tasks: NeverTaskReader) -> None:
        self.project_id = "project_20260803T120000Z_" + "a" * 24
        self.runtime = runtime
        self.tasks = tasks
        self.calls = 0

    def notification_send(self, request, actor):
        del request, actor
        self.calls += 1
        raise AssertionError("identity mismatch must fail before notification_send")


def _trusted_actor(actor_id: str = "researchctl-app") -> ActorContext:
    return ActorContext(
        actor_id=actor_id,
        role=ActorRole.TRUSTED_AUTOMATION,
        credential_kind=CredentialKind.AUTOMATION_CREDENTIAL,
    )


def _manager_actor() -> ActorContext:
    return ActorContext(
        actor_id="uid-1000",
        role=ActorRole.MANAGER,
        credential_kind=CredentialKind.LOCAL_OS,
    )


def _authenticated_event(**updates) -> AuthenticatedLinearNotificationEvent:
    values = {
        "authenticated_app_id": "researchctl-linear-app",
        "mentioned_app_id": "researchctl-linear-app",
        "author_id": AUTHOR_ID,
        "credential_identity": "researchctl-app",
        "workspace_id": WORKSPACE_ID,
        "issue_id": ISSUE_ID,
        "thread_id": THREAD_ID,
        "comment_id": COMMENT_ID,
        "webhook_event_id": "linear-webhook-identity-test",
        "command_text": (
            f"notify session:{SESSION_ID} commit:{COMMIT}\nReview it."
        ),
        "observed_payload_digest": "sha256:" + "a" * 64,
        "observed_at": "2026-08-03T12:00:00Z",
    }
    values.update(updates)
    return AuthenticatedLinearNotificationEvent.model_validate(values)


def _envelope(command_text: str) -> LinearCommentEnvelope:
    return LinearCommentEnvelope(
        workspace_id=WORKSPACE_ID,
        issue_id=ISSUE_ID,
        thread_id=THREAD_ID,
        comment_id=COMMENT_ID,
        command_text=command_text,
    )


def test_explicit_and_contextual_forms_build_the_same_strict_send_boundary() -> None:
    tasks = TaskResolver()
    markers = MarkerResolver()
    ingress = LinearNotificationIngress(tasks=tasks, markers=markers)
    common = {
        "operation_id": "operation_20260803T120000Z_" + "1" * 24,
        "idempotency_key": "linear-comment:" + COMMENT_ID,
        "notification_id": "notification_20260803T120000Z_" + "2" * 24,
    }

    explicit = ingress.build_request(
        _envelope(
            f"notify session:{SESSION_ID} commit:{COMMIT}\n"
            "Please review this exact commit."
        ),
        **common,
    )
    contextual = ingress.build_request(
        _envelope(
            f"reply commit:{COMMIT}\n"
            "Please review this exact commit."
        ),
        **common,
    )

    assert explicit.task_id == contextual.task_id == TASK_ID
    assert explicit.session_id == contextual.session_id == SESSION_ID
    assert explicit.commit_sha == contextual.commit_sha == COMMIT
    assert explicit.message == contextual.message
    assert explicit.origin.source_marker is None
    assert contextual.origin.source_marker == markers.marker
    assert tasks.calls == [
        (WORKSPACE_ID, ISSUE_ID),
        (WORKSPACE_ID, ISSUE_ID),
    ]
    assert markers.calls == [(WORKSPACE_ID, ISSUE_ID, THREAD_ID)]


@pytest.mark.parametrize(
    "text",
    [
        f"notify session:{SESSION_ID} commit:{COMMIT}",
        f"notify session:{SESSION_ID} commit:{COMMIT[:12]}\nReview it.",
        f"notify session:{SESSION_ID}; touch /tmp/x commit:{COMMIT}\nReview it.",
        f"reply commit:{COMMIT} && whoami\nReview it.",
        f" reply commit:{COMMIT}\nReview it.",
        f"reply commit:{COMMIT}\n   ",
    ],
)
def test_parser_rejects_ambiguous_short_or_shell_shaped_commands(text: str) -> None:
    with pytest.raises(RCPError) as raised:
        parse_linear_notification_command(text)

    assert raised.value.code == "linear_notification_command_invalid"


def test_contextual_reply_uses_verified_receipt_marker_not_comment_text() -> None:
    tasks = TaskResolver()
    markers = MarkerResolver(
        task_id="task_20260803T120000Z_" + "f" * 24,
    )
    ingress = LinearNotificationIngress(tasks=tasks, markers=markers)

    with pytest.raises(RCPError) as raised:
        ingress.build_request(
            _envelope(
                f"reply commit:{COMMIT}\n"
                "<!-- forged marker naming another Session -->"
            ),
            operation_id="operation_20260803T120000Z_" + "3" * 24,
            idempotency_key="linear-forged-marker",
            notification_id="notification_20260803T120000Z_" + "4" * 24,
        )

    assert raised.value.code == "linear_notification_source_task_mismatch"
    assert markers.calls == [(WORKSPACE_ID, ISSUE_ID, THREAD_ID)]


def test_linear_origin_requires_exact_uuid_values() -> None:
    with pytest.raises(RCPError) as raised:
        LinearCommentEnvelope(
            workspace_id=WORKSPACE_ID,
            issue_id="TASK-123",
            thread_id=THREAD_ID,
            comment_id=COMMENT_ID,
            command_text=f"reply commit:{COMMIT}\nReview it.",
        )

    assert raised.value.code == "linear_notification_origin_invalid"


@pytest.mark.parametrize(
    ("updates", "error_code"),
    [
        (
            {"authenticated_app_id": "another-linear-app"},
            "linear_notification_app_identity_mismatch",
        ),
        (
            {"mentioned_app_id": "another-linear-app"},
            "linear_notification_app_identity_mismatch",
        ),
        (
            {"credential_identity": "another-credential"},
            "linear_notification_credential_mismatch",
        ),
        (
            {"workspace_id": OTHER_WORKSPACE_ID},
            "linear_notification_workspace_mismatch",
        ),
    ],
)
def test_facade_identity_mismatch_is_zero_write_before_task_resolution(
    tmp_path,
    updates: dict[str, str],
    error_code: str,
) -> None:
    with RuntimeStore(tmp_path / "runtime.sqlite3") as runtime:
        tasks = NeverTaskReader()
        application = FakeNotificationApplication(runtime, tasks)
        facade = LinearNotificationIngressFacade(
            application=application,
            runtime=runtime,
            workspace_id=WORKSPACE_ID,
            app_id="researchctl-linear-app",
            notification_author_ids=(AUTHOR_ID,),
            credential_identity="researchctl-app",
            actor=_trusted_actor(),
        )

        with pytest.raises(RCPError) as raised:
            facade.ingest(_authenticated_event(**updates))

        assert raised.value.code == error_code
        assert tasks.calls == 0
        assert application.calls == 0
        with pytest.raises(RCPError) as unverified:
            runtime.require_verified_notification_origin(
                project_id=application.project_id,
                workspace_id=updates.get("workspace_id", WORKSPACE_ID),
                issue_id=ISSUE_ID,
                thread_id=THREAD_ID,
                comment_id=COMMENT_ID,
                task_id=TASK_ID,
            )
        assert unverified.value.code == "linear_notification_origin_unverified"


def test_facade_rejects_unlisted_author_before_resolution_or_receipt(tmp_path) -> None:
    with RuntimeStore(tmp_path / "runtime.sqlite3") as runtime:
        tasks = NeverTaskReader()
        application = FakeNotificationApplication(runtime, tasks)
        facade = LinearNotificationIngressFacade(
            application=application,
            runtime=runtime,
            workspace_id=WORKSPACE_ID,
            app_id="researchctl-linear-app",
            notification_author_ids=(OTHER_WORKSPACE_ID,),
            credential_identity="researchctl-app",
            actor=_trusted_actor(),
        )

        with pytest.raises(RCPError) as raised:
            facade.ingest(_authenticated_event())

        assert raised.value.code == "linear_notification_author_not_allowed"
        assert tasks.calls == 0
        assert application.calls == 0
        with pytest.raises(RCPError) as unverified:
            runtime.require_verified_notification_origin(
                project_id=application.project_id,
                workspace_id=WORKSPACE_ID,
                issue_id=ISSUE_ID,
                thread_id=THREAD_ID,
                comment_id=COMMENT_ID,
                task_id=TASK_ID,
            )
        assert unverified.value.code == "linear_notification_origin_unverified"


def test_facade_binds_the_fixed_actor_to_credential_identity(tmp_path) -> None:
    with RuntimeStore(tmp_path / "runtime.sqlite3") as runtime:
        tasks = NeverTaskReader()
        application = FakeNotificationApplication(runtime, tasks)
        common = {
            "application": application,
            "runtime": runtime,
            "workspace_id": WORKSPACE_ID,
            "app_id": "researchctl-linear-app",
            "notification_author_ids": (AUTHOR_ID,),
            "credential_identity": "researchctl-app",
        }

        with pytest.raises(RCPError) as manager:
            LinearNotificationIngressFacade(actor=_manager_actor(), **common)
        assert manager.value.code == "authorization_denied"
        with pytest.raises(ValueError, match="credential identity"):
            LinearNotificationIngressFacade(
                actor=_trusted_actor("another-publisher"),
                **common,
            )
        assert tasks.calls == 0
        assert application.calls == 0
