from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field, StrictInt

from researchctl.domain.models import SessionNotificationSourceMarker, StrictModel
from researchctl.domain.types import NonEmptyStr, ProjectId, UtcDateTime
from researchctl.runtime.models import LinearDeliveryRecord
from researchctl.services.requests import LinearDeliveryState, LinearDeliveryTopic


class LinearDeliveryClaimView(StrictModel):
    claim_id: NonEmptyStr
    claimed_at: UtcDateTime
    expires_at: UtcDateTime


class LinearDeliveryReceiptView(StrictModel):
    receipt_id: NonEmptyStr
    credential_identity: NonEmptyStr
    workspace_id: NonEmptyStr
    issue_id: NonEmptyStr
    thread_id: NonEmptyStr
    comment_id: NonEmptyStr
    event_id: NonEmptyStr
    task_id: NonEmptyStr
    source_marker: SessionNotificationSourceMarker | None = None
    payload_digest: NonEmptyStr
    transport_digest: NonEmptyStr
    created_at: UtcDateTime


class LinearDeliveryView(StrictModel):
    project_id: ProjectId
    topic: LinearDeliveryTopic
    outbox_id: NonEmptyStr
    aggregate_id: NonEmptyStr
    state: LinearDeliveryState
    created_at: UtcDateTime
    age_seconds: StrictInt = Field(ge=0)
    pending_age_seconds: StrictInt | None = Field(default=None, ge=0)
    status_updated_at: UtcDateTime | None = None
    attempt_count: StrictInt = Field(ge=0)
    last_error_code: NonEmptyStr | None = None
    last_claim_id: NonEmptyStr | None = None
    active_claim: LinearDeliveryClaimView | None = None
    receipt: LinearDeliveryReceiptView | None = None
    lineage: dict[str, Any]


class LinearDeliveryListResult(StrictModel):
    topic: LinearDeliveryTopic | None = None
    state: LinearDeliveryState | None = None
    limit: StrictInt = Field(ge=1, le=1000)
    count: StrictInt = Field(ge=0)
    items: tuple[LinearDeliveryView, ...]


class LinearDeliveryShowResult(StrictModel):
    delivery: LinearDeliveryView


def linear_delivery_view(
    record: LinearDeliveryRecord,
    *,
    observed_at: datetime,
) -> LinearDeliveryView:
    age_seconds = max(0, int((observed_at - record.created_at).total_seconds()))
    claim = record.active_claim
    receipt = record.receipt
    return LinearDeliveryView(
        project_id=record.project_id,
        topic=record.topic,
        outbox_id=record.outbox_id,
        aggregate_id=record.aggregate_id,
        state=record.state,
        created_at=record.created_at,
        age_seconds=age_seconds,
        pending_age_seconds=age_seconds if record.state == "pending" else None,
        status_updated_at=record.status_updated_at,
        attempt_count=record.attempt_count,
        last_error_code=record.last_error_code,
        last_claim_id=record.last_claim_id,
        active_claim=(
            None
            if claim is None
            else LinearDeliveryClaimView(
                claim_id=claim.claim_id,
                claimed_at=claim.claimed_at,
                expires_at=claim.expires_at,
            )
        ),
        receipt=(
            None
            if receipt is None
            else LinearDeliveryReceiptView(
                receipt_id=receipt.receipt_id,
                credential_identity=receipt.credential_identity,
                workspace_id=receipt.workspace_id,
                issue_id=receipt.issue_id,
                thread_id=receipt.thread_id,
                comment_id=receipt.comment_id,
                event_id=receipt.event_id,
                task_id=receipt.task_id,
                source_marker=receipt.source_marker,
                payload_digest=receipt.payload_digest,
                transport_digest=receipt.transport_digest,
                created_at=receipt.created_at,
            )
        ),
        lineage=dict(record.lineage),
    )
