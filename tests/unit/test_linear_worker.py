from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from researchctl.domain.enums import NotificationRoute, NotificationState, SessionState
from researchctl.domain.models import (
    AgentPolicy,
    CIValidationAttestation,
    ExecutionDomainPolicy,
    LinearProjectionConfigured,
    ProjectPolicy,
    SessionNotificationOrigin,
    SessionNotificationSourceMarker,
    TaskRecord,
)
from researchctl.errors import RCPError
from researchctl.runtime import RuntimeSession, RuntimeStore
from researchctl.serialization import canonical_digest
from researchctl.services.actor import ActorContext, ActorRole, CredentialKind
from researchctl.services.application import ApplicationService
from researchctl.services.linear_delivery import (
    AcceptedMergeSnapshot,
    LinearAcceptedResultDeliveryService,
    LinearAcceptedResultEvent,
    LinearCommentObservation,
    LinearDeliveryOutcome,
    LinearDeliveryPort,
    LinearDeliveryUnavailable,
    LinearTarget,
    LinearTargetObservation,
    add_linear_transport_envelope,
    linear_delivery_event_id,
    linear_delivery_marker,
)
from researchctl.services.linear_notification_ingress import (
    AuthenticatedLinearNotificationEvent,
    LinearNotificationIngressFacade,
)
from researchctl.services.linear_preview import (
    LINEAR_RENDERER_ID,
    LINEAR_RENDERER_VERSION,
)
from researchctl.services.linear_worker import (
    LinearReplyTarget,
    LinearReplyTargetObservation,
    LinearTransportWorker,
    build_linear_session_reply_event,
)
from researchctl.services.requests import (
    NotificationAckRequest,
    NotificationListRequest,
    NotificationReplyRequest,
)
from researchctl.services.task_records import TaskRecordRepository

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
PROJECT_ID = "project_20260803T120000Z_" + "a" * 24
TASK_ID = "task_20260803T120000Z_" + "b" * 24
SESSION_ID = "session_20260803T120000Z_" + "c" * 24
SUBMISSION_ID = "submission_20260803T120000Z_" + "d" * 24
DECISION_ID = "decision_20260803T120000Z_" + "e" * 24
REPORT_ID = "report_20260803T120000Z_" + "f" * 24
NOTIFICATION_ID = "notification_20260803T120000Z_" + "1" * 24
REPLY_ID = "reply_20260803T120000Z_" + "2" * 24
OPERATION_ID = "operation_20260803T120000Z_" + "3" * 24
WORKSPACE_ID = "11111111-1111-4111-8111-111111111111"
TEAM_ID = "22222222-2222-4222-8222-222222222222"
LINEAR_PROJECT_ID = "33333333-3333-4333-8333-333333333333"
ISSUE_ID = "44444444-4444-4444-8444-444444444444"
THREAD_ID = "55555555-5555-4555-8555-555555555555"
SOURCE_COMMENT_ID = "66666666-6666-4666-8666-666666666666"
CREATED_COMMENT_ID = "77777777-7777-4777-8777-777777777777"
OTHER_ISSUE_ID = "88888888-8888-4888-8888-888888888888"
INGRESS_COMMENT_ID = "99999999-9999-4999-8999-999999999999"
FOLLOWUP_COMMENT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
AUTHOR_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
REPLY_CREATED_COMMENT_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
EXPLICIT_THREAD_ID = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
EXPLICIT_COMMENT_ID = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
FORGED_COMMENT_ID = "ffffffff-ffff-4fff-8fff-ffffffffffff"
MANAGER_REPLY_COMMENT_ID = "12121212-1212-4212-8212-121212121212"
MANAGER_FOLLOWUP_COMMENT_ID = "13131313-1313-4313-8313-131313131313"
MERGE_COMMIT = "9" * 40
CI_SUBJECT_HEAD = "a" * 40
ACCEPTED_TOPIC = "linear.accepted-result.v1"
REPLY_TOPIC = "linear.session-reply.v1"
APP_ID = "researchctl-linear-app"


def _trusted_actor() -> ActorContext:
    return ActorContext(
        actor_id="researchctl-app",
        role=ActorRole.TRUSTED_AUTOMATION,
        credential_kind=CredentialKind.AUTOMATION_CREDENTIAL,
    )


def _agent_actor() -> ActorContext:
    return ActorContext(
        actor_id=f"agent-{SESSION_ID}",
        role=ActorRole.AGENT,
        credential_kind=CredentialKind.SESSION_CAPABILITY,
        bound_session_id=SESSION_ID,
    )


def _linear_actor() -> ActorContext:
    return ActorContext(
        actor_id="researchctl-app",
        role=ActorRole.TRUSTED_AUTOMATION,
        credential_kind=CredentialKind.AUTOMATION_CREDENTIAL,
    )


def _other_linear_actor() -> ActorContext:
    return ActorContext(
        actor_id="another-linear-publisher",
        role=ActorRole.TRUSTED_AUTOMATION,
        credential_kind=CredentialKind.AUTOMATION_CREDENTIAL,
    )


def _manager_actor() -> ActorContext:
    return ActorContext(
        actor_id="uid-1000",
        role=ActorRole.MANAGER,
        credential_kind=CredentialKind.LOCAL_OS,
    )


class _NoAcceptedMergeReader:
    def read_accepted_merge(
        self,
        *,
        project_id: str,
        merge_commit: str,
        ci: CIValidationAttestation,
    ) -> AcceptedMergeSnapshot | None:
        del ci
        raise AssertionError(
            f"delivery must not reread accepted merge {project_id}:{merge_commit}"
        )


class StaticAcceptedService(LinearAcceptedResultDeliveryService):
    """Keep worker tests focused on transport orchestration and durability."""

    def __init__(self, event: LinearAcceptedResultEvent) -> None:
        super().__init__(_NoAcceptedMergeReader())
        self.event = event
        self.enqueue_calls: list[tuple[str, str]] = []
        self.deliver_calls: list[str] = []

    def enqueue(
        self,
        *,
        actor: ActorContext,
        project_id: str,
        merge_commit: str,
        ci: CIValidationAttestation,
    ) -> LinearAcceptedResultEvent:
        del actor, ci
        self.enqueue_calls.append((project_id, merge_commit))
        return self.event

    def deliver(
        self,
        *,
        actor: ActorContext,
        event: LinearAcceptedResultEvent,
        remote: LinearDeliveryPort,
        expected_author_app_id: str,
    ) -> LinearDeliveryOutcome:
        self.deliver_calls.append(event.event_id)
        return super().deliver(
            actor=actor,
            event=event,
            remote=remote,
            expected_author_app_id=expected_author_app_id,
        )


class SimulatedWorkerCrash(RuntimeError):
    pass


class RecordingCommitVerifier:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def require_reachable(self, commit_sha: str, branch: str) -> None:
        self.calls.append((commit_sha, branch))


class FakeLinearWorkerPort:
    def __init__(self) -> None:
        self.comments: list[LinearCommentObservation] = []
        self.calls: list[tuple[str, object]] = []
        self.created: list[tuple[str, str | None, bytes]] = []
        self.unavailable_at: str | None = None
        self.crash_after_create = False
        self.ignore_expected_author_filter = False
        self.created_author_app_id = APP_ID
        self.reply_target_updates: dict[str, object] = {}

    def preflight_target(
        self,
        target: LinearTarget,
    ) -> LinearTargetObservation | None:
        self.calls.append(("preflight_target", target))
        if self.unavailable_at == "preflight_target":
            raise LinearDeliveryUnavailable("accepted target lookup unavailable")
        return LinearTargetObservation(
            workspace_id=target.workspace_id,
            team_id=target.team_id,
            team_workspace_id=target.workspace_id,
            project_id=target.project_id,
            project_workspace_id=(
                target.workspace_id if target.project_id is not None else None
            ),
            project_team_ids=(
                (target.team_id,) if target.project_id is not None else ()
            ),
            issue_id=target.issue_id,
            issue_workspace_id=target.workspace_id,
            issue_team_id=target.team_id,
            issue_project_ids=(
                (target.project_id,) if target.project_id is not None else ()
            ),
        )

    def preflight_reply_target(
        self,
        target: LinearReplyTarget,
    ) -> LinearReplyTargetObservation | None:
        self.calls.append(("preflight_reply_target", target))
        if self.unavailable_at == "preflight_reply_target":
            raise LinearDeliveryUnavailable("reply target lookup unavailable")
        observed = LinearReplyTargetObservation(
            workspace_id=target.workspace_id,
            issue_id=target.issue_id,
            thread_id=target.thread_id,
            source_comment_id=target.source_comment_id,
        )
        return replace(observed, **self.reply_target_updates)

    def observe_comment(
        self,
        *,
        issue_id: str,
        marker: str,
        expected_author_app_id: str,
        thread_id: str | None = None,
    ) -> LinearCommentObservation | None:
        self.calls.append(("observe_comment", (issue_id, thread_id, marker)))
        if self.unavailable_at == "observe_comment":
            raise LinearDeliveryUnavailable("comment lookup unavailable")
        encoded_marker = marker.encode("ascii")
        return next(
            (
                comment
                for comment in self.comments
                if comment.issue_id == issue_id
                and (thread_id is None or comment.thread_id == thread_id)
                and (
                    self.ignore_expected_author_filter
                    or comment.author_app_id == expected_author_app_id
                )
                and encoded_marker in comment.body
            ),
            None,
        )

    def create_comment(
        self,
        *,
        issue_id: str,
        body: bytes,
        thread_id: str | None = None,
    ) -> LinearCommentObservation:
        self.calls.append(("create_comment", (issue_id, thread_id)))
        if self.unavailable_at == "create_comment":
            raise LinearDeliveryUnavailable("comment create unavailable")
        comment_ids = (
            CREATED_COMMENT_ID,
            REPLY_CREATED_COMMENT_ID,
            MANAGER_REPLY_COMMENT_ID,
        )
        comment = LinearCommentObservation(
            comment_id=comment_ids[len(self.comments)],
            issue_id=issue_id,
            thread_id=thread_id or THREAD_ID,
            author_app_id=self.created_author_app_id,
            body=body,
        )
        self.created.append((issue_id, thread_id, body))
        self.comments.append(comment)
        if self.crash_after_create:
            raise SimulatedWorkerCrash("worker stopped after Linear accepted the write")
        return comment


def _accepted_event() -> LinearAcceptedResultEvent:
    renderer_payload = (
        f"<!-- researchctl-renderer:{LINEAR_RENDERER_ID} -->\n"
        "Accepted result.\n\n"
        f"- Agent: `agent-{SESSION_ID}`\n"
        f"- Session: `{SESSION_ID}`\n"
        f"- Task: `{TASK_ID}`\n"
        f"- Report: `{REPORT_ID}`\n"
    ).encode()
    payload_digest = f"sha256:{hashlib.sha256(renderer_payload).hexdigest()}"
    event_id = linear_delivery_event_id(
        project_id=PROJECT_ID,
        task_id=TASK_ID,
        report_id=REPORT_ID,
        report_revision=1,
    )
    marker = linear_delivery_marker(
        event_id=event_id,
        payload_digest=payload_digest,
        agent_id=f"agent-{SESSION_ID}",
        session_id=SESSION_ID,
        task_id=TASK_ID,
        report_id=REPORT_ID,
    )
    target = LinearTarget(
        workspace_id=WORKSPACE_ID,
        team_id=TEAM_ID,
        project_id=LINEAR_PROJECT_ID,
        issue_id=ISSUE_ID,
    )
    return LinearAcceptedResultEvent(
        event_id=event_id,
        agent_id=f"agent-{SESSION_ID}",
        project_id=PROJECT_ID,
        task_id=TASK_ID,
        session_id=SESSION_ID,
        submission_id=SUBMISSION_ID,
        decision_id=DECISION_ID,
        report_id=REPORT_ID,
        report_revision=1,
        accepted_merge_commit=MERGE_COMMIT,
        ci_subject_head=CI_SUBJECT_HEAD,
        ci_attestation_id="attestation_20260803T120000Z_" + "f" * 24,
        workflow_id="research-validate-pr",
        check_identity="researchctl/exact-head",
        task_digest="sha256:" + "1" * 64,
        submission_digest="sha256:" + "2" * 64,
        decision_digest="sha256:" + "3" * 64,
        report_digest="sha256:" + "4" * 64,
        target=target,
        ci_projection=LinearProjectionConfigured(
            workspace_id=WORKSPACE_ID,
            team_id=TEAM_ID,
            project_id=LINEAR_PROJECT_ID,
            issue_id=ISSUE_ID,
            renderer_id=LINEAR_RENDERER_ID,
            renderer_version=LINEAR_RENDERER_VERSION,
            payload_digest=payload_digest,
        ),
        renderer_id=LINEAR_RENDERER_ID,
        renderer_version=LINEAR_RENDERER_VERSION,
        payload_digest=payload_digest,
        renderer_payload=renderer_payload,
        marker=marker,
        transport_body=add_linear_transport_envelope(renderer_payload, marker),
    )


def _worker(
    store: RuntimeStore,
    accepted: StaticAcceptedService,
    remote: FakeLinearWorkerPort,
) -> LinearTransportWorker:
    return LinearTransportWorker(
        runtime=store,
        accepted=accepted,
        remote=remote,
        app_id=APP_ID,
        credential_identity="researchctl-app",
        clock=lambda: NOW,
        lease_seconds=30,
    )


def _enqueue_accepted(worker: LinearTransportWorker) -> str:
    event_id = worker.enqueue_accepted(
        actor=_trusted_actor(),
        project_id=PROJECT_ID,
        merge_commit=MERGE_COMMIT,
        ci=cast(CIValidationAttestation, object()),
    )
    assert event_id is not None
    return event_id


def _enqueue_session_reply(store: RuntimeStore) -> str:
    store.save_session(
        RuntimeSession(
            session_id=SESSION_ID,
            project_id=PROJECT_ID,
            task_id=TASK_ID,
            state=SessionState.ACTIVE,
            created_at=NOW,
            updated_at=NOW,
            branch=f"research/session/{SESSION_ID}",
        )
    )
    source_marker = SessionNotificationSourceMarker(
        agent_id=f"agent-{SESSION_ID}",
        task_id=TASK_ID,
        session_id=SESSION_ID,
        report_id=REPORT_ID,
        marker_digest="sha256:" + "b" * 64,
    )
    store.create_notification(
        notification_id=NOTIFICATION_ID,
        project_id=PROJECT_ID,
        task_id=TASK_ID,
        session_id=SESSION_ID,
        commit_sha="c" * 40,
        message="Review this commit and reply in the originating Linear thread.",
        origin=SessionNotificationOrigin(
            workspace_id=WORKSPACE_ID,
            issue_id=ISSUE_ID,
            thread_id=THREAD_ID,
            comment_id=SOURCE_COMMENT_ID,
            source_marker=source_marker,
        ),
        created_at=NOW,
    )
    store.begin_operation(
        PROJECT_ID,
        "notification.reply",
        "reply-to-linear-comment",
        canonical_digest({"reply_id": REPLY_ID}),
        OPERATION_ID,
        NOW,
    )
    published = store.reply_notification(
        notification_id=NOTIFICATION_ID,
        reply_id=REPLY_ID,
        actor_id=f"agent-{SESSION_ID}",
        body="The commit has been reviewed and the evidence is complete.",
        observed_at=NOW,
        expected_revision=1,
        operation_id=OPERATION_ID,
    )
    return published.outbox.outbox_id


def test_agent_cannot_enqueue_or_claim_linear_delivery(tmp_path) -> None:
    event = _accepted_event()
    accepted = StaticAcceptedService(event)
    remote = FakeLinearWorkerPort()
    with RuntimeStore(tmp_path / "runtime.sqlite3") as store:
        worker = _worker(store, accepted, remote)

        with pytest.raises(RCPError) as enqueue_error:
            worker.enqueue_accepted(
                actor=_agent_actor(),
                project_id=PROJECT_ID,
                merge_commit=MERGE_COMMIT,
                ci=cast(CIValidationAttestation, object()),
            )
        assert enqueue_error.value.code == "authorization_denied"
        assert accepted.enqueue_calls == []
        assert store.get_linear_projection_outbox(event.event_id) is None

        with pytest.raises(RCPError) as wrong_publisher:
            worker.enqueue_accepted(
                actor=_other_linear_actor(),
                project_id=PROJECT_ID,
                merge_commit=MERGE_COMMIT,
                ci=cast(CIValidationAttestation, object()),
            )
        assert wrong_publisher.value.code == "authorization_denied"
        assert accepted.enqueue_calls == []
        assert store.get_linear_projection_outbox(event.event_id) is None

        _enqueue_accepted(worker)
        with pytest.raises(RCPError) as delivery_error:
            worker.run_once(
                actor=_agent_actor(),
                project_id=PROJECT_ID,
                claim_id="agent-controlled-claim",
            )
        assert delivery_error.value.code == "authorization_denied"
        assert accepted.deliver_calls == []
        assert remote.calls == []
        with pytest.raises(RCPError) as wrong_delivery_identity:
            worker.run_once(
                actor=_other_linear_actor(),
                project_id=PROJECT_ID,
                claim_id="wrong-publisher-claim",
            )
        assert wrong_delivery_identity.value.code == "authorization_denied"
        assert accepted.deliver_calls == []
        assert remote.calls == []
        assert store.get_linear_projection_outbox(event.event_id).state == "pending"
        status = store.get_linear_delivery_status(
            topic=ACCEPTED_TOPIC,
            outbox_id=event.event_id,
        )
        assert status is not None
        assert status["attempt_count"] == 0


def test_accepted_enqueue_is_idempotent_and_receipt_records_identity(
    tmp_path,
) -> None:
    event = _accepted_event()
    accepted = StaticAcceptedService(event)
    remote = FakeLinearWorkerPort()
    with RuntimeStore(tmp_path / "runtime.sqlite3") as store:
        worker = _worker(store, accepted, remote)

        assert _enqueue_accepted(worker) == event.event_id
        assert _enqueue_accepted(worker) == event.event_id
        assert len(store.list_linear_projection_outbox(PROJECT_ID)) == 1

        result = worker.run_once(
            actor=_trusted_actor(),
            project_id=PROJECT_ID,
            claim_id="accepted-delivery-1",
        )

        assert result.state == "delivered"
        assert result.topic == ACCEPTED_TOPIC
        assert len(remote.created) == 1
        assert remote.created[0] == (ISSUE_ID, None, event.transport_body)
        receipt = store.get_linear_delivery_receipt(
            topic=ACCEPTED_TOPIC,
            outbox_id=event.event_id,
        )
        assert receipt is not None
        assert receipt.credential_identity == "researchctl-app"
        assert receipt.workspace_id == WORKSPACE_ID
        assert receipt.issue_id == ISSUE_ID
        assert receipt.source_marker is not None
        assert receipt.source_marker.session_id == SESSION_ID
        assert store.get_linear_projection_outbox(event.event_id).state == "delivered"

        assert _enqueue_accepted(worker) == event.event_id
        assert len(store.list_linear_projection_outbox(PROJECT_ID)) == 1


def test_attacker_marker_cannot_suppress_trusted_accepted_result(tmp_path) -> None:
    event = _accepted_event()
    accepted = StaticAcceptedService(event)
    remote = FakeLinearWorkerPort()
    remote.ignore_expected_author_filter = True
    remote.comments.append(
        LinearCommentObservation(
            comment_id=OTHER_ISSUE_ID,
            issue_id=ISSUE_ID,
            thread_id=THREAD_ID,
            author_app_id="attacker-linear-app",
            body=event.transport_body,
        )
    )
    with RuntimeStore(tmp_path / "runtime.sqlite3") as store:
        worker = _worker(store, accepted, remote)
        _enqueue_accepted(worker)

        result = worker.run_once(
            actor=_trusted_actor(),
            project_id=PROJECT_ID,
            claim_id="attacker-marker-accepted",
        )

        assert result.state == "delivered"
        assert len(remote.created) == 1
        receipt = store.get_linear_delivery_receipt(
            topic=ACCEPTED_TOPIC,
            outbox_id=event.event_id,
        )
        assert receipt is not None
        assert receipt.comment_id != OTHER_ISSUE_ID


def test_wrong_create_author_cannot_form_accepted_result_receipt(tmp_path) -> None:
    event = _accepted_event()
    accepted = StaticAcceptedService(event)
    remote = FakeLinearWorkerPort()
    remote.created_author_app_id = "attacker-linear-app"
    with RuntimeStore(tmp_path / "runtime.sqlite3") as store:
        worker = _worker(store, accepted, remote)
        _enqueue_accepted(worker)

        with pytest.raises(RCPError) as raised:
            worker.run_once(
                actor=_trusted_actor(),
                project_id=PROJECT_ID,
                claim_id="wrong-create-author-accepted",
            )

        assert raised.value.code == "linear_delivery_port_contract_invalid"
        assert store.get_linear_delivery_receipt(
            topic=ACCEPTED_TOPIC,
            outbox_id=event.event_id,
        ) is None


def test_create_crash_resumes_claim_by_observing_marker_without_duplicate(
    tmp_path,
) -> None:
    event = _accepted_event()
    accepted = StaticAcceptedService(event)
    remote = FakeLinearWorkerPort()
    remote.crash_after_create = True
    with RuntimeStore(tmp_path / "runtime.sqlite3") as store:
        worker = _worker(store, accepted, remote)
        _enqueue_accepted(worker)

        with pytest.raises(SimulatedWorkerCrash):
            worker.run_once(
                actor=_trusted_actor(),
                project_id=PROJECT_ID,
                claim_id="crash-resume-claim",
            )

        assert len(remote.created) == 1
        assert store.get_linear_projection_outbox(event.event_id).state == "pending"
        status_before_resume = store.get_linear_delivery_status(
            topic=ACCEPTED_TOPIC,
            outbox_id=event.event_id,
        )
        assert status_before_resume is not None
        assert status_before_resume["attempt_count"] == 0

        resumed = worker.run_once(
            actor=_trusted_actor(),
            project_id=PROJECT_ID,
            claim_id="crash-resume-claim",
        )

        assert resumed.state == "delivered"
        assert len(remote.created) == 1
        assert sum(call[0] == "observe_comment" for call in remote.calls) == 2
        assert store.get_linear_delivery_receipt(
            topic=ACCEPTED_TOPIC,
            outbox_id=event.event_id,
        ) is not None


def test_transport_outage_keeps_outbox_pending_then_retry_delivers(
    tmp_path,
) -> None:
    event = _accepted_event()
    accepted = StaticAcceptedService(event)
    remote = FakeLinearWorkerPort()
    remote.unavailable_at = "preflight_target"
    with RuntimeStore(tmp_path / "runtime.sqlite3") as store:
        worker = _worker(store, accepted, remote)
        _enqueue_accepted(worker)

        retryable = worker.run_once(
            actor=_trusted_actor(),
            project_id=PROJECT_ID,
            claim_id="outage-claim",
        )
        assert retryable.state == "retryable"
        assert retryable.error_code == "linear_delivery_unavailable"
        assert store.get_linear_projection_outbox(event.event_id).state == "pending"
        first_status = store.get_linear_delivery_status(
            topic=ACCEPTED_TOPIC,
            outbox_id=event.event_id,
        )
        assert first_status is not None
        assert first_status["attempt_count"] == 1

        remote.unavailable_at = None
        delivered = worker.run_once(
            actor=_trusted_actor(),
            project_id=PROJECT_ID,
            claim_id="outage-retry-claim",
        )

        assert delivered.state == "delivered"
        assert len(remote.created) == 1
        final_status = store.get_linear_delivery_status(
            topic=ACCEPTED_TOPIC,
            outbox_id=event.event_id,
        )
        assert final_status is not None
        assert final_status["attempt_count"] == 2
        assert final_status["last_error_code"] is None


def test_session_reply_uses_same_worker_and_exact_source_thread(tmp_path) -> None:
    event = _accepted_event()
    accepted = StaticAcceptedService(event)
    remote = FakeLinearWorkerPort()
    with RuntimeStore(tmp_path / "runtime.sqlite3") as store:
        outbox_id = _enqueue_session_reply(store)
        worker = _worker(store, accepted, remote)

        result = worker.run_once(
            actor=_trusted_actor(),
            project_id=PROJECT_ID,
            claim_id="session-reply-claim",
        )

        assert result.state == "delivered"
        assert result.topic == REPLY_TOPIC
        assert accepted.deliver_calls == []
        assert len(remote.created) == 1
        preflight_name, preflight_target = remote.calls[0]
        assert preflight_name == "preflight_reply_target"
        assert preflight_target == LinearReplyTarget(
            workspace_id=WORKSPACE_ID,
            issue_id=ISSUE_ID,
            thread_id=THREAD_ID,
            source_comment_id=SOURCE_COMMENT_ID,
        )
        issue_id, thread_id, body = remote.created[0]
        assert (issue_id, thread_id) == (ISSUE_ID, THREAD_ID)
        assert SOURCE_COMMENT_ID.encode() not in body
        assert f"Session: `{SESSION_ID}`".encode() in body
        receipt = store.get_linear_delivery_receipt(
            topic=REPLY_TOPIC,
            outbox_id=outbox_id,
        )
        assert receipt is not None
        assert receipt.credential_identity == "researchctl-app"
        assert receipt.workspace_id == WORKSPACE_ID
        assert receipt.issue_id == ISSUE_ID
        assert receipt.thread_id == THREAD_ID
        assert store.get_notification_reply_outbox(outbox_id).state == "delivered"


def test_attacker_marker_cannot_suppress_trusted_session_reply(tmp_path) -> None:
    accepted = StaticAcceptedService(_accepted_event())
    remote = FakeLinearWorkerPort()
    remote.ignore_expected_author_filter = True
    with RuntimeStore(tmp_path / "runtime.sqlite3") as store:
        outbox_id = _enqueue_session_reply(store)
        outbox = store.get_notification_reply_outbox(outbox_id)
        assert outbox is not None
        reply_event = build_linear_session_reply_event(
            project_id=PROJECT_ID,
            outbox_id=outbox_id,
            payload=outbox.payload,
        )
        remote.comments.append(
            LinearCommentObservation(
                comment_id=OTHER_ISSUE_ID,
                issue_id=ISSUE_ID,
                thread_id=THREAD_ID,
                author_app_id="attacker-linear-app",
                body=reply_event.transport_body,
            )
        )
        worker = _worker(store, accepted, remote)

        result = worker.run_once(
            actor=_trusted_actor(),
            project_id=PROJECT_ID,
            claim_id="attacker-marker-reply",
        )

        assert result.state == "delivered"
        assert len(remote.created) == 1
        receipt = store.get_linear_delivery_receipt(
            topic=REPLY_TOPIC,
            outbox_id=outbox_id,
        )
        assert receipt is not None
        assert receipt.comment_id != OTHER_ISSUE_ID


def test_wrong_create_author_cannot_form_session_reply_receipt(tmp_path) -> None:
    accepted = StaticAcceptedService(_accepted_event())
    remote = FakeLinearWorkerPort()
    remote.created_author_app_id = "attacker-linear-app"
    with RuntimeStore(tmp_path / "runtime.sqlite3") as store:
        outbox_id = _enqueue_session_reply(store)
        worker = _worker(store, accepted, remote)

        result = worker.run_once(
            actor=_trusted_actor(),
            project_id=PROJECT_ID,
            claim_id="wrong-create-author-reply",
        )

        assert result.state == "dead_letter"
        assert result.error_code == "linear_reply_comment_author_mismatch"
        assert store.get_linear_delivery_receipt(
            topic=REPLY_TOPIC,
            outbox_id=outbox_id,
        ) is None


@pytest.mark.parametrize(
    ("target_updates", "expected_error"),
    [
        ({"issue_id": OTHER_ISSUE_ID}, "linear_reply_target_mismatch"),
        ({"thread_archived": True}, "linear_reply_target_archived"),
    ],
)
def test_session_reply_target_mismatch_or_archive_dead_letters_without_write(
    tmp_path,
    target_updates: dict[str, object],
    expected_error: str,
) -> None:
    event = _accepted_event()
    accepted = StaticAcceptedService(event)
    remote = FakeLinearWorkerPort()
    remote.reply_target_updates = target_updates
    with RuntimeStore(tmp_path / "runtime.sqlite3") as store:
        outbox_id = _enqueue_session_reply(store)
        worker = _worker(store, accepted, remote)

        result = worker.run_once(
            actor=_trusted_actor(),
            project_id=PROJECT_ID,
            claim_id=f"dead-letter-{expected_error}",
        )

        assert result.state == "dead_letter"
        assert result.error_code == expected_error
        assert remote.created == []
        assert [call[0] for call in remote.calls] == ["preflight_reply_target"]
        assert store.get_notification_reply_outbox(outbox_id).state == "dead_letter"
        assert store.get_linear_delivery_receipt(
            topic=REPLY_TOPIC,
            outbox_id=outbox_id,
        ) is None


def test_authenticated_ingress_agent_reply_and_contextual_followup_are_one_flow(
    tmp_path,
    task_payload,
) -> None:
    (tmp_path / ".research" / "tasks").mkdir(parents=True)
    tasks = TaskRecordRepository(tmp_path)
    task = TaskRecord.model_validate(
        task_payload(
            task_id=TASK_ID,
            state="active",
            linear_issue_id=ISSUE_ID,
        )
    )
    tasks.create(task)
    remote = FakeLinearWorkerPort()
    accepted = StaticAcceptedService(_accepted_event())
    worker_clock = [NOW]
    service_clock = [NOW + timedelta(seconds=1)]
    verifier = RecordingCommitVerifier()
    policy = ProjectPolicy(
        agent=AgentPolicy(
            accepted_paths_denied=(
                ".research/decisions/**",
                ".research/policies/**",
                ".research/project.yaml",
                ".research/impacts/**",
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
    )
    with RuntimeStore(tmp_path / "runtime.sqlite3") as store:
        store.save_session(
            RuntimeSession(
                session_id=SESSION_ID,
                project_id=PROJECT_ID,
                task_id=TASK_ID,
                state=SessionState.ACTIVE,
                created_at=NOW,
                updated_at=NOW,
                branch=f"research/session/{SESSION_ID}",
            )
        )
        service = ApplicationService(
            project_id=PROJECT_ID,
            policy=policy,
            tasks=tasks,
            runtime=store,
            notification_commits=verifier,
            clock=lambda: service_clock[0],
        )
        with pytest.raises(RCPError) as hidden_configuration:
            service.linear_delivery_run_once(
                claim_id="agent-must-not-probe-worker",
                actor=_agent_actor(),
            )
        assert hidden_configuration.value.code == "authorization_denied"
        with pytest.raises(RCPError) as not_configured:
            service.linear_delivery_run_once(
                claim_id="trusted-worker-probe",
                actor=_linear_actor(),
            )
        assert not_configured.value.code == "linear_worker_not_configured"

        worker = LinearTransportWorker(
            runtime=store,
            accepted=accepted,
            remote=remote,
            app_id=APP_ID,
            credential_identity="researchctl-app",
            clock=lambda: worker_clock[0],
            lease_seconds=30,
        )
        service._bind_linear_worker(worker)
        facade = LinearNotificationIngressFacade(
            application=service,
            runtime=store,
            workspace_id=WORKSPACE_ID,
            app_id=APP_ID,
            notification_author_ids=(AUTHOR_ID,),
            credential_identity="researchctl-app",
            actor=_linear_actor(),
        )

        event_id = service.linear_enqueue_accepted(
            merge_commit=MERGE_COMMIT,
            ci=cast(CIValidationAttestation, object()),
            actor=_linear_actor(),
        )
        assert event_id == _accepted_event().event_id
        accepted_delivery = service.linear_delivery_run_once(
            claim_id="accepted-e2e",
            actor=_linear_actor(),
        )
        assert accepted_delivery.state == "delivered"
        assert len(remote.created) == 1
        accepted_receipt = store.get_linear_delivery_receipt(
            topic=ACCEPTED_TOPIC,
            outbox_id=event_id,
        )
        assert accepted_receipt is not None
        assert accepted_receipt.source_marker is not None

        first_event = AuthenticatedLinearNotificationEvent(
            authenticated_app_id=APP_ID,
            mentioned_app_id=APP_ID,
            author_id=AUTHOR_ID,
            credential_identity="researchctl-app",
            workspace_id=WORKSPACE_ID,
            issue_id=ISSUE_ID,
            thread_id=THREAD_ID,
            comment_id=INGRESS_COMMENT_ID,
            webhook_event_id="linear-webhook-e2e-1",
            command_text=(
                f"reply commit:{'d' * 40}\n"
                "Review this exact Session commit and reply here."
            ),
            observed_payload_digest="sha256:" + "1" * 64,
            observed_at=NOW + timedelta(seconds=1),
        )
        sent = facade.ingest(first_event)
        assert facade.ingest(first_event) == sent
        notification_id = sent.data["notification"]["notification_id"]
        ingress_receipt = store.require_verified_notification_origin(
            project_id=PROJECT_ID,
            workspace_id=WORKSPACE_ID,
            issue_id=ISSUE_ID,
            thread_id=THREAD_ID,
            comment_id=INGRESS_COMMENT_ID,
            task_id=TASK_ID,
        )
        assert ingress_receipt.author_id == AUTHOR_ID
        assert ingress_receipt.credential_identity == "researchctl-app"
        assert ingress_receipt.source_marker == accepted_receipt.source_marker
        inbox = service.notification_list(NotificationListRequest(), _agent_actor())
        assert [item.notification_id for item in inbox] == [notification_id]

        service.notification_ack(
            NotificationAckRequest(
                operation_id="operation_20260803T120000Z_" + "4" * 24,
                idempotency_key="e2e-agent-ack",
                notification_id=notification_id,
                expected_revision=1,
            ),
            _agent_actor(),
        )
        queued_reply = service.notification_reply(
            NotificationReplyRequest(
                operation_id="operation_20260803T120000Z_" + "5" * 24,
                idempotency_key="e2e-agent-reply",
                notification_id=notification_id,
                expected_revision=2,
                reply_id=REPLY_ID,
                body="Reviewed. The requested commit is ready.",
            ),
            _agent_actor(),
        )
        reply_outbox_id = queued_reply.data["outbox"]["outbox_id"]
        worker_clock[0] = NOW + timedelta(seconds=4)
        reply_delivery = service.linear_delivery_run_once(
            claim_id="session-reply-e2e",
            actor=_linear_actor(),
        )
        assert reply_delivery.state == "delivered"
        assert remote.created[-1][0:2] == (ISSUE_ID, THREAD_ID)
        reply_body = remote.created[-1][2]
        assert f"- Agent: `agent-{SESSION_ID}`".encode() in reply_body
        assert f"- Session: `{SESSION_ID}`".encode() in reply_body
        assert f"- Task: `{TASK_ID}`".encode() in reply_body
        assert f"- Report: `{REPORT_ID}`".encode() in reply_body
        reply_receipt = store.get_linear_delivery_receipt(
            topic=REPLY_TOPIC,
            outbox_id=reply_outbox_id,
        )
        assert reply_receipt is not None
        assert reply_receipt.source_marker is not None
        assert reply_receipt.source_marker.marker_digest != (
            accepted_receipt.source_marker.marker_digest
        )
        assert store.resolve_verified_source_marker(
            workspace_id=WORKSPACE_ID,
            issue_id=ISSUE_ID,
            thread_id=THREAD_ID,
        ) == reply_receipt.source_marker
        assert store.get_notification(notification_id).state is NotificationState.REPLIED

        # A delayed replay retains this comment's historical accepted marker.
        assert facade.ingest(first_event) == sent
        followup_event = AuthenticatedLinearNotificationEvent(
            authenticated_app_id=APP_ID,
            mentioned_app_id=APP_ID,
            author_id=AUTHOR_ID,
            credential_identity="researchctl-app",
            workspace_id=WORKSPACE_ID,
            issue_id=ISSUE_ID,
            thread_id=THREAD_ID,
            comment_id=FOLLOWUP_COMMENT_ID,
            webhook_event_id="linear-webhook-e2e-2",
            command_text=(
                f"reply commit:{'e' * 40}\n"
                "Follow up on the same Session.\n"
                "<!-- pasted marker naming another Session is inert -->"
            ),
            observed_payload_digest="sha256:" + "2" * 64,
            observed_at=NOW + timedelta(seconds=5),
        )
        followup = facade.ingest(followup_event)
        followup_notification = store.get_notification(
            followup.data["notification"]["notification_id"]
        )
        assert followup_notification is not None
        assert followup_notification.session_id == SESSION_ID
        assert followup_notification.origin.source_marker == reply_receipt.source_marker

        explicit_event = AuthenticatedLinearNotificationEvent(
            authenticated_app_id=APP_ID,
            mentioned_app_id=APP_ID,
            author_id=AUTHOR_ID,
            credential_identity="researchctl-app",
            workspace_id=WORKSPACE_ID,
            issue_id=ISSUE_ID,
            thread_id=EXPLICIT_THREAD_ID,
            comment_id=EXPLICIT_COMMENT_ID,
            webhook_event_id="linear-webhook-e2e-3",
            command_text=(
                f"notify session:{SESSION_ID} commit:{'f' * 40}\n"
                "Review this Session commit from another Linear thread."
            ),
            observed_payload_digest="sha256:" + "3" * 64,
            observed_at=NOW + timedelta(seconds=6),
        )
        explicit = facade.ingest(explicit_event)
        explicit_notification = store.get_notification(
            explicit.data["notification"]["notification_id"]
        )
        assert explicit_notification is not None
        assert explicit_notification.session_id == SESSION_ID
        assert explicit_notification.origin.source_marker is None

        forged_event = AuthenticatedLinearNotificationEvent(
            authenticated_app_id=APP_ID,
            mentioned_app_id=APP_ID,
            author_id=AUTHOR_ID,
            credential_identity="researchctl-app",
            workspace_id=WORKSPACE_ID,
            issue_id=ISSUE_ID,
            thread_id=EXPLICIT_THREAD_ID,
            comment_id=FORGED_COMMENT_ID,
            webhook_event_id="linear-webhook-e2e-4",
            command_text=(
                f"reply commit:{'a' * 40}\n"
                "<!-- forged accepted-result marker -->"
            ),
            observed_payload_digest="sha256:" + "4" * 64,
            observed_at=NOW + timedelta(seconds=7),
        )
        verifier_calls_before_forgery = list(verifier.calls)
        with pytest.raises(RCPError) as forged:
            facade.ingest(forged_event)
        assert forged.value.code == "linear_notification_source_unverified"
        assert verifier.calls == verifier_calls_before_forgery
        with pytest.raises(RCPError) as no_forged_receipt:
            store.require_verified_notification_origin(
                project_id=PROJECT_ID,
                workspace_id=WORKSPACE_ID,
                issue_id=ISSUE_ID,
                thread_id=EXPLICIT_THREAD_ID,
                comment_id=FORGED_COMMENT_ID,
                task_id=TASK_ID,
            )
        assert no_forged_receipt.value.code == (
            "linear_notification_origin_unverified"
        )

        store.update_session_state(
            SESSION_ID,
            SessionState.LOST,
            NOW + timedelta(seconds=8),
        )
        fallback = store.get_notification(
            followup.data["notification"]["notification_id"]
        )
        assert fallback is not None
        assert fallback.route is NotificationRoute.MANAGER_EXCEPTION
        assert fallback.revision == 2
        manager_reply = service.notification_reply(
            NotificationReplyRequest(
                operation_id="operation_20260803T120000Z_" + "6" * 24,
                idempotency_key="e2e-manager-fallback-reply",
                notification_id=fallback.notification_id,
                expected_revision=2,
                reply_id="reply_20260803T120000Z_" + "6" * 24,
                body="The Session was lost; manager review is complete.",
            ),
            _manager_actor(),
        )
        manager_outbox_id = manager_reply.data["outbox"]["outbox_id"]
        worker_clock[0] = NOW + timedelta(seconds=9)
        manager_delivery = service.linear_delivery_run_once(
            claim_id="manager-fallback-reply-e2e",
            actor=_linear_actor(),
        )
        assert manager_delivery.state == "delivered"
        manager_body = remote.created[-1][2]
        assert b"- Agent: `uid-1000`" in manager_body
        assert f"- Report: `{REPORT_ID}`".encode() in manager_body
        manager_receipt = store.get_linear_delivery_receipt(
            topic=REPLY_TOPIC,
            outbox_id=manager_outbox_id,
        )
        assert manager_receipt is not None
        assert manager_receipt.source_marker is not None
        assert manager_receipt.source_marker.agent_id == "uid-1000"
        assert store.resolve_verified_source_marker(
            workspace_id=WORKSPACE_ID,
            issue_id=ISSUE_ID,
            thread_id=THREAD_ID,
        ) == manager_receipt.source_marker

        manager_followup_event = AuthenticatedLinearNotificationEvent(
            authenticated_app_id=APP_ID,
            mentioned_app_id=APP_ID,
            author_id=AUTHOR_ID,
            credential_identity="researchctl-app",
            workspace_id=WORKSPACE_ID,
            issue_id=ISSUE_ID,
            thread_id=THREAD_ID,
            comment_id=MANAGER_FOLLOWUP_COMMENT_ID,
            webhook_event_id="linear-webhook-e2e-5",
            command_text=(
                f"reply commit:{'b' * 40}\n"
                "Continue the same thread after manager fallback."
            ),
            observed_payload_digest="sha256:" + "5" * 64,
            observed_at=NOW + timedelta(seconds=10),
        )
        manager_followup = facade.ingest(manager_followup_event)
        routed = store.get_notification(
            manager_followup.data["notification"]["notification_id"]
        )
        assert routed is not None
        assert routed.route is NotificationRoute.MANAGER_EXCEPTION
        assert routed.session_id == SESSION_ID
        assert routed.origin.source_marker == manager_receipt.source_marker
