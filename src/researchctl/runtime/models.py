from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from researchctl.domain.enums import NotificationRoute, NotificationState, SessionState
from researchctl.domain.models import SessionNotificationOrigin, StatusUpdate


@dataclass(frozen=True, slots=True)
class OperationEvent:
    operation_id: str
    sequence: int
    kind: str
    observed_at: datetime
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class OperationRecord:
    operation_id: str
    project_id: str
    command: str
    idempotency_key: str
    request_digest: str
    state: str
    started_at: datetime
    finished_at: datetime | None = None
    terminal_result: str | None = None
    result: dict[str, Any] | None = None
    events: tuple[OperationEvent, ...] = ()


@dataclass(frozen=True, slots=True)
class RuntimeSession:
    session_id: str
    project_id: str
    task_id: str
    state: SessionState
    created_at: datetime
    updated_at: datetime
    host: str | None = None
    branch: str | None = None
    worktree_path: str | None = None
    continued_from: str | None = None
    actor_token_digest: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class OutboxRecord:
    outbox_id: str
    project_id: str
    topic: str
    aggregate_id: str
    payload: dict[str, Any]
    created_at: datetime
    state: str


@dataclass(frozen=True, slots=True)
class AttentionItem:
    dedupe_key: str
    project_id: str
    task_id: str
    session_id: str
    kind: str
    identity: str
    priority: int
    state: str
    generation: int
    evidence_digest: str
    current_update: StatusUpdate
    first_seen_at: datetime
    last_seen_at: datetime
    acknowledged_by: str | None = None
    acknowledged_at: datetime | None = None
    snoozed_by: str | None = None
    snoozed_until: datetime | None = None
    resolved_by: str | None = None
    resolved_at: datetime | None = None
    resolution_reason: str | None = None


@dataclass(frozen=True, slots=True)
class PublishedStatus:
    update: StatusUpdate
    outbox: OutboxRecord
    attention: AttentionItem


@dataclass(frozen=True, slots=True)
class SessionNotification:
    notification_id: str
    project_id: str
    task_id: str
    session_id: str
    commit_sha: str
    message: str
    origin: SessionNotificationOrigin
    route: NotificationRoute
    state: NotificationState
    revision: int
    created_at: datetime
    routed_at: datetime
    fallback_reason: str | None = None
    acknowledged_by: str | None = None
    acknowledged_at: datetime | None = None
    reply_id: str | None = None
    replied_by: str | None = None
    replied_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class SessionNotificationReply:
    reply_id: str
    notification_id: str
    project_id: str
    task_id: str
    session_id: str
    actor_id: str
    body: str
    payload_digest: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class PublishedNotificationReply:
    notification: SessionNotification
    reply: SessionNotificationReply
    outbox: OutboxRecord


@dataclass(frozen=True, slots=True)
class LinearDeliveryClaim:
    claim_id: str
    outbox: OutboxRecord
    claimed_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class LinearDeliveryReceiptRecord:
    receipt_id: str
    project_id: str
    credential_identity: str
    topic: str
    outbox_id: str
    workspace_id: str
    issue_id: str
    thread_id: str
    comment_id: str
    event_id: str
    task_id: str
    source_marker: SessionNotificationSourceMarker | None
    payload_digest: str
    transport_digest: str
    marker: str
    payload: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class VerifiedLinearIngressReceipt:
    receipt_id: str
    project_id: str
    provider: str
    authenticated_app_id: str
    mentioned_app_id: str
    author_id: str
    credential_identity: str
    workspace_id: str
    issue_id: str
    thread_id: str
    comment_id: str
    webhook_event_id: str | None
    task_id: str
    source_marker: SessionNotificationSourceMarker | None
    command_digest: str
    observed_payload_digest: str
    binding_digest: str
    verified_at: datetime
