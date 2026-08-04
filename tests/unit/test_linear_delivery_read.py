from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from researchctl.domain.enums import SessionState
from researchctl.domain.models import (
    AgentPolicy,
    ProjectPolicy,
    SessionNotificationOrigin,
)
from researchctl.errors import RCPError
from researchctl.runtime import RuntimeSession, RuntimeStore
from researchctl.services.actor import ActorContext, ActorRole, CredentialKind
from researchctl.services.application import ApplicationService
from researchctl.services.linear_delivery_read import (
    LinearDeliveryListResult,
    LinearDeliveryShowResult,
)
from researchctl.services.requests import (
    LinearDeliveryListRequest,
    LinearDeliveryShowRequest,
)
from researchctl.services.task_records import TaskRecordRepository


NOW = datetime(2026, 8, 3, 14, 0, tzinfo=UTC)
PROJECT_ID = "project_20260803T140000Z_" + "a" * 24
OTHER_PROJECT_ID = "project_20260803T140000Z_" + "b" * 24
TASK_ID = "task_20260803T140000Z_" + "c" * 24
SESSION_ID = "session_20260803T140000Z_" + "d" * 24
NOTIFICATION_ID = "notification_20260803T140000Z_" + "e" * 24
REPLY_ID = "reply_20260803T140000Z_" + "f" * 24
OPERATION_ID = "operation_20260803T140000Z_" + "1" * 24
EVENT_ID = "linear-event-" + "2" * 64
OTHER_EVENT_ID = "linear-event-" + "3" * 64
FAILED_EVENT_ID = "linear-event-" + "b" * 64
REPLY_OUTBOX_ID = f"notification-reply:{REPLY_ID}"
ACCEPTED_TOPIC = "linear.accepted-result.v1"
REPLY_TOPIC = "linear.session-reply.v1"


def _manager() -> ActorContext:
    return ActorContext(
        actor_id="uid-1000",
        role=ActorRole.MANAGER,
        credential_kind=CredentialKind.LOCAL_OS,
    )


def _agent() -> ActorContext:
    return ActorContext(
        actor_id=f"agent-{SESSION_ID}",
        role=ActorRole.AGENT,
        credential_kind=CredentialKind.SESSION_CAPABILITY,
        bound_session_id=SESSION_ID,
    )


def _receipt() -> dict[str, object]:
    return {
        "receipt_id": "linear-receipt-" + "4" * 64,
        "credential_identity": "researchctl-app",
        "workspace_id": "11111111-1111-4111-8111-111111111111",
        "issue_id": "22222222-2222-4222-8222-222222222222",
        "thread_id": "33333333-3333-4333-8333-333333333333",
        "comment_id": "44444444-4444-4444-8444-444444444444",
        "event_id": EVENT_ID,
        "task_id": TASK_ID,
        "payload_digest": "sha256:" + "5" * 64,
        "transport_digest": "sha256:" + "6" * 64,
        "marker": "<!-- accepted-result -->",
    }


def _seed(store: RuntimeStore) -> None:
    store.enqueue_linear_projection(
        project_id=PROJECT_ID,
        event_id=EVENT_ID,
        aggregate_id="report_20260803T140000Z_" + "7" * 24 + ":1",
        payload={
            "version": 1,
            "event_id": EVENT_ID,
            "task_id": TASK_ID,
            "session_id": SESSION_ID,
            "report_id": "report_20260803T140000Z_" + "7" * 24,
            "report_revision": 1,
            "accepted_merge_commit": "8" * 40,
        },
        created_at=NOW,
    )
    accepted_claim = store.claim_linear_delivery(
        project_id=PROJECT_ID,
        claim_id="claim-accepted",
        claimed_at=NOW,
    )
    assert accepted_claim is not None
    store.finish_linear_delivery(
        claim_id=accepted_claim.claim_id,
        state="delivered",
        error_code=None,
        receipt=_receipt(),
        finished_at=NOW + timedelta(seconds=1),
    )
    store.enqueue_linear_projection(
        project_id=PROJECT_ID,
        event_id=FAILED_EVENT_ID,
        aggregate_id="failed:1",
        payload={"event_id": FAILED_EVENT_ID, "task_id": TASK_ID},
        created_at=NOW + timedelta(seconds=1),
    )
    failed_claim = store.claim_linear_delivery(
        project_id=PROJECT_ID,
        claim_id="claim-failed",
        claimed_at=NOW + timedelta(seconds=1),
    )
    assert failed_claim is not None
    store.finish_linear_delivery(
        claim_id=failed_claim.claim_id,
        state="dead_letter",
        error_code="linear_target_archived",
        receipt=None,
        finished_at=NOW + timedelta(seconds=1),
    )

    store.save_session(
        RuntimeSession(
            session_id=SESSION_ID,
            project_id=PROJECT_ID,
            task_id=TASK_ID,
            state=SessionState.ACTIVE,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    store.create_notification(
        notification_id=NOTIFICATION_ID,
        project_id=PROJECT_ID,
        task_id=TASK_ID,
        session_id=SESSION_ID,
        commit_sha="9" * 40,
        message="Review the exact commit.",
        origin=SessionNotificationOrigin(
            workspace_id="11111111-1111-4111-8111-111111111111",
            issue_id="22222222-2222-4222-8222-222222222222",
            thread_id="33333333-3333-4333-8333-333333333333",
            comment_id="55555555-5555-4555-8555-555555555555",
        ),
        created_at=NOW + timedelta(seconds=2),
    )
    store.begin_operation(
        PROJECT_ID,
        "notification.reply",
        "delivery-read-fixture",
        "sha256:" + "a" * 64,
        OPERATION_ID,
        NOW + timedelta(seconds=2),
    )
    store.reply_notification(
        notification_id=NOTIFICATION_ID,
        reply_id=REPLY_ID,
        actor_id=f"agent-{SESSION_ID}",
        body="The commit is correct.",
        observed_at=NOW + timedelta(seconds=2),
        expected_revision=1,
        operation_id=OPERATION_ID,
    )
    reply_claim = store.claim_linear_delivery(
        project_id=PROJECT_ID,
        claim_id="claim-reply",
        claimed_at=NOW + timedelta(seconds=3),
        lease_seconds=30,
    )
    assert reply_claim is not None
    assert reply_claim.outbox.outbox_id == REPLY_OUTBOX_ID

    store.enqueue_linear_projection(
        project_id=OTHER_PROJECT_ID,
        event_id=OTHER_EVENT_ID,
        aggregate_id="other:1",
        payload={"event_id": OTHER_EVENT_ID, "task_id": TASK_ID},
        created_at=NOW + timedelta(seconds=4),
    )


def test_runtime_delivery_read_is_bounded_filtered_exact_and_non_mutating(
    tmp_path: Path,
) -> None:
    with RuntimeStore(tmp_path / "runtime.sqlite3") as store:
        _seed(store)
        reply_before = store.get_notification_reply_outbox(REPLY_OUTBOX_ID)
        status_before = store.get_linear_delivery_status(
            topic=REPLY_TOPIC,
            outbox_id=REPLY_OUTBOX_ID,
        )

        records = store.list_linear_deliveries(
            PROJECT_ID,
            observed_at=NOW + timedelta(seconds=5),
        )
        accepted = store.list_linear_deliveries(
            PROJECT_ID,
            topic=ACCEPTED_TOPIC,
            state="delivered",
            limit=1,
            observed_at=NOW + timedelta(seconds=5),
        )
        shown = store.get_linear_delivery(
            PROJECT_ID,
            topic=REPLY_TOPIC,
            outbox_id=REPLY_OUTBOX_ID,
            observed_at=NOW + timedelta(seconds=5),
        )

        assert [item.outbox_id for item in records] == [
            REPLY_OUTBOX_ID,
            FAILED_EVENT_ID,
            EVENT_ID,
        ]
        assert len(accepted) == 1
        assert accepted[0].receipt is not None
        assert accepted[0].receipt.credential_identity == "researchctl-app"
        assert accepted[0].attempt_count == 1
        assert accepted[0].lineage["accepted_merge_commit"] == "8" * 40
        failed = next(item for item in records if item.outbox_id == FAILED_EVENT_ID)
        assert failed.state == "dead_letter"
        assert failed.attempt_count == 1
        assert failed.last_claim_id == "claim-failed"
        assert failed.last_error_code == "linear_target_archived"
        assert shown is not None
        assert shown.active_claim is not None
        assert shown.active_claim.claim_id == "claim-reply"
        assert shown.lineage["notification_id"] == NOTIFICATION_ID
        assert shown.lineage["session_id"] == SESSION_ID
        assert store.get_linear_delivery(
            PROJECT_ID,
            topic=ACCEPTED_TOPIC,
            outbox_id=REPLY_OUTBOX_ID,
            observed_at=NOW + timedelta(seconds=5),
        ) is None
        assert store.get_linear_delivery(
            OTHER_PROJECT_ID,
            topic=ACCEPTED_TOPIC,
            outbox_id=EVENT_ID,
            observed_at=NOW + timedelta(seconds=5),
        ) is None

        expired = store.get_linear_delivery(
            PROJECT_ID,
            topic=REPLY_TOPIC,
            outbox_id=REPLY_OUTBOX_ID,
            observed_at=NOW + timedelta(seconds=40),
        )
        assert expired is not None
        assert expired.active_claim is None
        assert store.get_notification_reply_outbox(REPLY_OUTBOX_ID) == reply_before
        assert store.get_linear_delivery_status(
            topic=REPLY_TOPIC,
            outbox_id=REPLY_OUTBOX_ID,
        ) == status_before

        with pytest.raises(ValueError):
            store.list_linear_deliveries(PROJECT_ID, limit=1001)


def test_application_delivery_read_is_typed_manager_only_and_project_scoped(
    tmp_path: Path,
) -> None:
    tasks_root = tmp_path / "project"
    (tasks_root / ".research" / "tasks").mkdir(parents=True)
    runtime = RuntimeStore(tmp_path / "runtime.sqlite3")
    _seed(runtime)
    service = ApplicationService(
        project_id=PROJECT_ID,
        policy=ProjectPolicy(
            agent=AgentPolicy(
                accepted_paths_denied=(
                    ".research/decisions/**",
                    ".research/policies/**",
                    ".research/project.yaml",
                    ".research/impacts/**",
                    ".research/reports/**",
                    ".research/tasks/**",
                )
            )
        ),
        tasks=TaskRecordRepository(tasks_root),
        runtime=runtime,
        clock=lambda: NOW + timedelta(seconds=5),
    )

    listed = service.linear_delivery_list(
        LinearDeliveryListRequest(topic=REPLY_TOPIC, state="pending", limit=10),
        _manager(),
    )
    shown = service.linear_delivery_show(
        LinearDeliveryShowRequest(topic=ACCEPTED_TOPIC, outbox_id=EVENT_ID),
        _manager(),
    )

    assert isinstance(listed, LinearDeliveryListResult)
    assert listed.count == 1
    assert listed.items[0].pending_age_seconds == 3
    assert listed.items[0].active_claim is not None
    assert isinstance(shown, LinearDeliveryShowResult)
    assert shown.delivery.age_seconds == 5
    assert shown.delivery.receipt is not None
    serialized = shown.model_dump(mode="json", exclude_none=True)
    assert "marker" not in serialized["delivery"]["receipt"]
    assert "payload" not in serialized["delivery"]["receipt"]
    assert "renderer_payload" not in serialized["delivery"]["lineage"]
    assert "body" not in serialized["delivery"]["lineage"]

    with pytest.raises(RCPError) as list_denied:
        service.linear_delivery_list(LinearDeliveryListRequest(), _agent())
    assert list_denied.value.code == "authorization_denied"
    with pytest.raises(RCPError) as show_denied:
        service.linear_delivery_show(
            LinearDeliveryShowRequest(topic=ACCEPTED_TOPIC, outbox_id=EVENT_ID),
            _agent(),
        )
    assert show_denied.value.code == "authorization_denied"
    with pytest.raises(RCPError) as missing:
        service.linear_delivery_show(
            LinearDeliveryShowRequest(
                topic=ACCEPTED_TOPIC,
                outbox_id="missing-event",
            ),
            _manager(),
        )
    assert missing.value.code == "linear_delivery_not_found"
    runtime.close()
