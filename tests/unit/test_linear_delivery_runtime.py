from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from researchctl.domain.models import SessionNotificationSourceMarker
from researchctl.errors import RCPError
from researchctl.runtime import RuntimeStore

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
PROJECT_ID = "project_20260803T120000Z_" + "a" * 24
TASK_ID = "task_20260803T120000Z_" + "b" * 24
SESSION_ID = "session_20260803T120000Z_" + "c" * 24
REPORT_ID = "report_20260803T120000Z_" + "d" * 24
WORKSPACE_ID = "11111111-1111-4111-8111-111111111111"
ISSUE_ID = "22222222-2222-4222-8222-222222222222"
THREAD_ID = "33333333-3333-4333-8333-333333333333"
COMMENT_ID = "44444444-4444-4444-8444-444444444444"
EVENT_ID = "linear-event-" + "5" * 64
RECEIPT_ID = "linear-receipt-" + "6" * 64


def _event_payload() -> dict[str, object]:
    return {
        "version": 1,
        "event_id": EVENT_ID,
        "task_id": TASK_ID,
        "report_id": REPORT_ID,
    }


def _source_marker() -> SessionNotificationSourceMarker:
    return SessionNotificationSourceMarker(
        agent_id=f"agent-{SESSION_ID}",
        task_id=TASK_ID,
        session_id=SESSION_ID,
        report_id=REPORT_ID,
        marker_digest="sha256:" + "7" * 64,
    )


def _receipt() -> dict[str, object]:
    return {
        "version": 1,
        "receipt_id": RECEIPT_ID,
        "credential_identity": "researchctl-app",
        "event_id": EVENT_ID,
        "task_id": TASK_ID,
        "workspace_id": WORKSPACE_ID,
        "issue_id": ISSUE_ID,
        "thread_id": THREAD_ID,
        "comment_id": COMMENT_ID,
        "payload_digest": "sha256:" + "8" * 64,
        "transport_digest": "sha256:" + "9" * 64,
        "marker": "<!-- stable marker -->",
        "source_marker": _source_marker().model_dump(mode="json"),
    }


def test_projection_outbox_claim_retry_and_receipt_are_durable(tmp_path) -> None:
    database = tmp_path / "runtime.sqlite3"
    with RuntimeStore(database) as store:
        queued = store.enqueue_linear_projection(
            project_id=PROJECT_ID,
            event_id=EVENT_ID,
            aggregate_id=f"{REPORT_ID}:1",
            payload=_event_payload(),
            created_at=NOW,
        )
        replay = store.enqueue_linear_projection(
            project_id=PROJECT_ID,
            event_id=EVENT_ID,
            aggregate_id=f"{REPORT_ID}:1",
            payload=_event_payload(),
            created_at=NOW + timedelta(seconds=1),
        )
        assert replay == queued
        assert queued.state == "pending"

        first = store.claim_linear_delivery(
            project_id=PROJECT_ID,
            claim_id="claim-1",
            claimed_at=NOW,
            lease_seconds=30,
        )
        same_claim = store.claim_linear_delivery(
            project_id=PROJECT_ID,
            claim_id="claim-1",
            claimed_at=NOW + timedelta(seconds=5),
            lease_seconds=30,
        )
        assert same_claim == first
        assert first is not None
        assert store.claim_linear_delivery(
            project_id=PROJECT_ID,
            claim_id="claim-2",
            claimed_at=NOW + timedelta(seconds=5),
            lease_seconds=30,
        ) is None

        pending = store.finish_linear_delivery(
            claim_id="claim-1",
            state="retryable",
            error_code="linear_delivery_unavailable",
            receipt=None,
            finished_at=NOW + timedelta(seconds=6),
        )
        assert pending.state == "pending"
        status = store.get_linear_delivery_status(
            topic="linear.accepted-result.v1",
            outbox_id=EVENT_ID,
        )
        assert status is not None
        assert status["attempt_count"] == 1

        second = store.claim_linear_delivery(
            project_id=PROJECT_ID,
            claim_id="claim-2",
            claimed_at=NOW + timedelta(seconds=7),
        )
        assert second is not None
        delivered = store.finish_linear_delivery(
            claim_id="claim-2",
            state="delivered",
            error_code=None,
            receipt=_receipt(),
            finished_at=NOW + timedelta(seconds=8),
        )
        assert delivered.state == "delivered"
        stored_receipt = store.get_linear_delivery_receipt(
            topic="linear.accepted-result.v1",
            outbox_id=EVENT_ID,
        )
        assert stored_receipt is not None
        assert stored_receipt.credential_identity == "researchctl-app"
        assert stored_receipt.source_marker == _source_marker()
        assert store.list_linear_projection_outbox(
            PROJECT_ID,
            state="pending",
        ) == ()

    with RuntimeStore(database) as reopened:
        receipt = reopened.get_linear_delivery_receipt(
            topic="linear.accepted-result.v1",
            outbox_id=EVENT_ID,
        )
        assert receipt is not None
        assert receipt.receipt_id == RECEIPT_ID
        assert reopened.resolve_verified_source_marker(
            workspace_id=WORKSPACE_ID,
            issue_id=ISSUE_ID,
            thread_id=THREAD_ID,
        ) == _source_marker()


def test_verified_ingress_binds_app_credential_command_and_exact_origin(
    tmp_path,
) -> None:
    with RuntimeStore(tmp_path / "runtime.sqlite3") as store:
        store.enqueue_linear_projection(
            project_id=PROJECT_ID,
            event_id=EVENT_ID,
            aggregate_id=f"{REPORT_ID}:1",
            payload=_event_payload(),
            created_at=NOW,
        )
        store.claim_linear_delivery(
            project_id=PROJECT_ID,
            claim_id="claim-receipt",
            claimed_at=NOW,
        )
        store.finish_linear_delivery(
            claim_id="claim-receipt",
            state="delivered",
            error_code=None,
            receipt=_receipt(),
            finished_at=NOW,
        )

        ingress_comment = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        receipt = store.record_verified_linear_ingress(
            project_id=PROJECT_ID,
            authenticated_app_id="researchctl-linear-app",
            mentioned_app_id="researchctl-linear-app",
            author_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
            credential_identity="researchctl-app",
            workspace_id=WORKSPACE_ID,
            issue_id=ISSUE_ID,
            thread_id=THREAD_ID,
            comment_id=ingress_comment,
            webhook_event_id="linear-webhook-event-17",
            task_id=TASK_ID,
            source_marker=_source_marker(),
            command_digest="sha256:" + "a" * 64,
            observed_payload_digest="sha256:" + "b" * 64,
            verified_at=NOW,
        )
        replay = store.record_verified_linear_ingress(
            project_id=PROJECT_ID,
            authenticated_app_id="researchctl-linear-app",
            mentioned_app_id="researchctl-linear-app",
            author_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
            credential_identity="researchctl-app",
            workspace_id=WORKSPACE_ID,
            issue_id=ISSUE_ID,
            thread_id=THREAD_ID,
            comment_id=ingress_comment,
            webhook_event_id="linear-webhook-event-17",
            task_id=TASK_ID,
            source_marker=_source_marker(),
            command_digest="sha256:" + "a" * 64,
            observed_payload_digest="sha256:" + "b" * 64,
            verified_at=NOW + timedelta(seconds=10),
        )
        assert replay == receipt
        assert receipt.authenticated_app_id == "researchctl-linear-app"
        assert receipt.mentioned_app_id == "researchctl-linear-app"
        assert receipt.author_id == "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        assert receipt.credential_identity == "researchctl-app"
        assert receipt.source_marker == _source_marker()
        assert store.require_verified_notification_origin(
            project_id=PROJECT_ID,
            workspace_id=WORKSPACE_ID,
            issue_id=ISSUE_ID,
            thread_id=THREAD_ID,
            comment_id=ingress_comment,
            task_id=TASK_ID,
        ) == receipt

        with pytest.raises(RCPError) as unverified:
            store.require_verified_notification_origin(
                project_id=PROJECT_ID,
                workspace_id=WORKSPACE_ID,
                issue_id=ISSUE_ID,
                thread_id=THREAD_ID,
                comment_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                task_id=TASK_ID,
            )
        assert unverified.value.code == "linear_notification_origin_unverified"

        with pytest.raises(RCPError) as conflict:
            store.record_verified_linear_ingress(
                project_id=PROJECT_ID,
                authenticated_app_id="researchctl-linear-app",
                mentioned_app_id="researchctl-linear-app",
                author_id="dddddddd-dddd-4ddd-8ddd-dddddddddddd",
                credential_identity="researchctl-app",
                workspace_id=WORKSPACE_ID,
                issue_id=ISSUE_ID,
                thread_id=THREAD_ID,
                comment_id=ingress_comment,
                webhook_event_id="linear-webhook-event-17",
                task_id=TASK_ID,
                source_marker=_source_marker(),
                command_digest="sha256:" + "a" * 64,
                observed_payload_digest="sha256:" + "b" * 64,
                verified_at=NOW,
            )
        assert conflict.value.code == "linear_ingress_receipt_conflict"
