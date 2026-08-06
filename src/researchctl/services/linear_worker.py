from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from researchctl.domain.models import (
    CIValidationAttestation,
    SessionNotificationSourceMarker,
)
from researchctl.errors import RCPError
from researchctl.runtime import RuntimeStore
from researchctl.serialization import canonical_digest
from researchctl.services.actor import ActorContext, ActorRole
from researchctl.services.linear_delivery import (
    LinearAcceptedResultDeliveryService,
    LinearCommentObservation,
    LinearDeliveryPort,
    LinearDeliveryUnavailable,
    add_linear_transport_envelope,
    linear_event_from_payload,
    linear_event_payload,
    linear_receipt_payload,
    strip_linear_transport_envelope,
)


_ACCEPTED_TOPIC = "linear.accepted-result.v1"
_REPLY_TOPIC = "linear.session-reply.v1"
LINEAR_SESSION_REPLY_RENDERER_ID = "linear.session-reply-markdown.v2"
LinearDeliveryState = Literal["delivered", "retryable", "dead_letter"]
LinearWorkerState = Literal["idle", "delivered", "retryable", "dead_letter"]
_LINEAR_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


@dataclass(frozen=True, slots=True)
class LinearReplyTarget:
    workspace_id: str
    issue_id: str
    thread_id: str
    source_comment_id: str


@dataclass(frozen=True, slots=True)
class LinearReplyTargetObservation:
    workspace_id: str
    issue_id: str
    thread_id: str
    source_comment_id: str
    workspace_archived: bool = False
    issue_archived: bool = False
    thread_archived: bool = False


class LinearWorkerPort(LinearDeliveryPort, Protocol):
    def preflight_reply_target(
        self,
        target: LinearReplyTarget,
    ) -> LinearReplyTargetObservation | None: ...


@dataclass(frozen=True, slots=True)
class LinearSessionReplyEvent:
    event_id: str
    project_id: str
    notification_id: str
    reply_id: str
    task_id: str
    session_id: str
    agent_id: str
    report_id: str | None
    target: LinearReplyTarget
    renderer_payload: bytes
    payload_digest: str
    marker: str
    transport_body: bytes
    source_marker: SessionNotificationSourceMarker


@dataclass(frozen=True, slots=True)
class LinearSessionReplyReceipt:
    receipt_id: str
    event_id: str
    task_id: str
    session_id: str
    target: LinearReplyTarget
    comment_id: str
    payload_digest: str
    transport_digest: str
    marker: str
    source_marker: SessionNotificationSourceMarker


@dataclass(frozen=True, slots=True)
class LinearWorkerResult:
    state: LinearWorkerState
    topic: str | None = None
    outbox_id: str | None = None
    error_code: str | None = None
    receipt_id: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "topic": self.topic,
            "outbox_id": self.outbox_id,
            "error_code": self.error_code,
            "receipt_id": self.receipt_id,
        }


def _escaped(value: object) -> str:
    text = html.escape(str(value), quote=False).replace("\\", "\\\\")
    for character in ("`", "*", "_", "[", "]", "#", "|"):
        text = text.replace(character, f"\\{character}")
    return "<br>\n".join(text.replace("\r\n", "\n").replace("\r", "\n").split("\n"))


def _reply_marker(
    *,
    event_id: str,
    payload_digest: str,
    agent_id: str,
    session_id: str,
    task_id: str,
    report_id: str | None,
    notification_id: str,
    reply_id: str,
) -> str:
    return (
        "<!-- researchctl-linear-delivery:v1 "
        f"topic={_REPLY_TOPIC} event_id={event_id} "
        f"payload_digest={payload_digest} agent_id={agent_id} "
        f"session_id={session_id} task_id={task_id} "
        f"report_id={report_id or 'none'} notification_id={notification_id} "
        f"reply_id={reply_id} -->"
    )


def build_linear_session_reply_event(
    *,
    project_id: str,
    outbox_id: str,
    payload: dict[str, object],
) -> LinearSessionReplyEvent:
    required = {
        "workspace_id",
        "linear_issue_id",
        "source_thread_id",
        "source_comment_id",
        "notification_id",
        "reply_id",
        "task_id",
        "session_id",
        "commit_sha",
        "body",
        "actor_id",
        "marker",
    }
    if not required.issubset(payload):
        raise RCPError(
            code="linear_session_reply_event_invalid",
            message="Durable Session reply is missing a required transport binding.",
        )
    marker_data = payload.get("marker")
    if not isinstance(marker_data, dict):
        raise RCPError(
            code="linear_session_reply_event_invalid",
            message="Durable Session reply marker is invalid.",
        )
    report = marker_data.get("report_id")
    values = {
        "workspace_id": payload.get("workspace_id"),
        "issue_id": payload.get("linear_issue_id"),
        "thread_id": payload.get("source_thread_id"),
        "source_comment_id": payload.get("source_comment_id"),
        "notification_id": payload.get("notification_id"),
        "reply_id": payload.get("reply_id"),
        "task_id": payload.get("task_id"),
        "session_id": payload.get("session_id"),
        "agent_id": payload.get("actor_id"),
        "commit_sha": payload.get("commit_sha"),
        "body": payload.get("body"),
    }
    if any(not isinstance(value, str) or not value for value in values.values()):
        raise RCPError(
            code="linear_session_reply_event_invalid",
            message="Durable Session reply contains an invalid identity or body.",
        )
    workspace_id = str(values["workspace_id"])
    issue_id = str(values["issue_id"])
    thread_id = str(values["thread_id"])
    source_comment_id = str(values["source_comment_id"])
    if not all(
        _LINEAR_UUID.fullmatch(value)
        for value in (workspace_id, issue_id, thread_id, source_comment_id)
    ):
        raise RCPError(
            code="linear_session_reply_event_invalid",
            message="Session reply requires exact Linear UUID bindings.",
        )
    task_id = str(values["task_id"])
    session_id = str(values["session_id"])
    agent_id = str(values["agent_id"])
    notification_id = str(values["notification_id"])
    reply_id = str(values["reply_id"])
    report_id = str(report) if report is not None else None
    renderer_payload = (
        f"<!-- researchctl-renderer:{LINEAR_SESSION_REPLY_RENDERER_ID} -->\n"
        f"{_escaped(values['body'])}\n\n"
        f"- Agent: `{agent_id}`\n"
        f"- Session: `{session_id}`\n"
        f"- Task: `{task_id}`\n"
        f"- Report: `{report_id or 'none'}`\n"
        f"- Commit reviewed: `{values['commit_sha']}`\n"
        f"- Reply: `{reply_id}`\n"
    ).encode("utf-8")
    payload_digest = f"sha256:{hashlib.sha256(renderer_payload).hexdigest()}"
    marker = _reply_marker(
        event_id=outbox_id,
        payload_digest=payload_digest,
        agent_id=agent_id,
        session_id=session_id,
        task_id=task_id,
        report_id=report_id,
        notification_id=notification_id,
        reply_id=reply_id,
    )
    source_marker = SessionNotificationSourceMarker(
        agent_id=agent_id,
        task_id=task_id,
        session_id=session_id,
        report_id=report_id,
        marker_digest=canonical_digest(
            {
                "marker": marker,
                "agent_id": agent_id,
                "task_id": task_id,
                "session_id": session_id,
                "report_id": report_id,
            }
        ),
    )
    return LinearSessionReplyEvent(
        event_id=outbox_id,
        project_id=project_id,
        notification_id=notification_id,
        reply_id=reply_id,
        task_id=task_id,
        session_id=session_id,
        agent_id=agent_id,
        report_id=report_id,
        target=LinearReplyTarget(
            workspace_id=workspace_id,
            issue_id=issue_id,
            thread_id=thread_id,
            source_comment_id=source_comment_id,
        ),
        renderer_payload=renderer_payload,
        payload_digest=payload_digest,
        marker=marker,
        transport_body=add_linear_transport_envelope(renderer_payload, marker),
        source_marker=source_marker,
    )


class LinearTransportWorker:
    def __init__(
        self,
        *,
        runtime: RuntimeStore,
        accepted: LinearAcceptedResultDeliveryService,
        remote: LinearWorkerPort,
        app_id: str,
        credential_identity: str,
        clock: ProtocolClock,
        lease_seconds: int = 300,
    ) -> None:
        if not app_id.strip() or not credential_identity.strip():
            raise ValueError("app_id and credential_identity must be non-empty")
        self.runtime = runtime
        self.accepted = accepted
        self.remote = remote
        self.app_id = app_id
        self.credential_identity = credential_identity
        self._clock = clock
        self.lease_seconds = lease_seconds

    def enqueue_accepted(
        self,
        *,
        actor: ActorContext,
        project_id: str,
        merge_commit: str,
        ci: CIValidationAttestation,
    ) -> str | None:
        self._require_actor(actor, "linear.enqueue")
        event = self.accepted.enqueue(
            actor=actor,
            project_id=project_id,
            merge_commit=merge_commit,
            ci=ci,
        )
        if event is None:
            return None
        self.runtime.enqueue_linear_projection(
            project_id=project_id,
            event_id=event.event_id,
            aggregate_id=f"{event.report_id}:{event.report_revision}",
            payload=linear_event_payload(event),
            created_at=self._clock(),
        )
        return event.event_id

    def run_once(
        self,
        *,
        actor: ActorContext,
        project_id: str,
        claim_id: str,
    ) -> LinearWorkerResult:
        self._require_actor(actor, "linear.deliver")
        claim = self.runtime.claim_linear_delivery(
            project_id=project_id,
            claim_id=claim_id,
            claimed_at=self._clock(),
            lease_seconds=self.lease_seconds,
        )
        if claim is None:
            return LinearWorkerResult(state="idle")
        if claim.outbox.topic == _ACCEPTED_TOPIC:
            return self._deliver_accepted(actor, claim.claim_id, claim.outbox)
        if claim.outbox.topic == _REPLY_TOPIC:
            return self._deliver_reply(claim.claim_id, claim.outbox)
        return self._finish(
            claim_id=claim.claim_id,
            topic=claim.outbox.topic,
            outbox_id=claim.outbox.outbox_id,
            state="dead_letter",
            error_code="linear_delivery_topic_invalid",
            receipt=None,
        )

    def _require_actor(self, actor: ActorContext, command: str) -> None:
        actor.require_role(command, ActorRole.TRUSTED_AUTOMATION)
        if actor.actor_id != self.credential_identity:
            raise RCPError(
                code="authorization_denied",
                message="Trusted automation actor does not own this Linear credential.",
                context={"actor_id": actor.actor_id, "command": command},
            )

    def _deliver_accepted(self, actor, claim_id, outbox) -> LinearWorkerResult:
        try:
            event = linear_event_from_payload(outbox.payload)
        except RCPError as error:
            return self._finish(
                claim_id=claim_id,
                topic=outbox.topic,
                outbox_id=outbox.outbox_id,
                state="dead_letter",
                error_code=error.code,
                receipt=None,
            )
        outcome = self.accepted.deliver(
            actor=actor,
            event=event,
            remote=self.remote,
            expected_author_app_id=self.app_id,
        )
        receipt = None
        if outcome.receipt is not None:
            receipt = linear_receipt_payload(outcome.receipt)
            receipt["credential_identity"] = self.credential_identity
        return self._finish(
            claim_id=claim_id,
            topic=outbox.topic,
            outbox_id=outbox.outbox_id,
            state=outcome.state,
            error_code=outcome.error_code,
            receipt=receipt,
        )

    def _deliver_reply(self, claim_id, outbox) -> LinearWorkerResult:
        try:
            event = build_linear_session_reply_event(
                project_id=outbox.project_id,
                outbox_id=outbox.outbox_id,
                payload=outbox.payload,
            )
        except RCPError as error:
            return self._finish(
                claim_id=claim_id,
                topic=outbox.topic,
                outbox_id=outbox.outbox_id,
                state="dead_letter",
                error_code=error.code,
                receipt=None,
            )
        state, error_code, comment = self._project_reply(event)
        receipt = None
        if comment is not None:
            reply_receipt = self._reply_receipt(event, comment)
            receipt = {
                "version": 1,
                "receipt_id": reply_receipt.receipt_id,
                "credential_identity": self.credential_identity,
                "event_id": reply_receipt.event_id,
                "task_id": reply_receipt.task_id,
                "session_id": reply_receipt.session_id,
                "workspace_id": reply_receipt.target.workspace_id,
                "issue_id": reply_receipt.target.issue_id,
                "thread_id": reply_receipt.target.thread_id,
                "comment_id": reply_receipt.comment_id,
                "payload_digest": reply_receipt.payload_digest,
                "transport_digest": reply_receipt.transport_digest,
                "marker": reply_receipt.marker,
                "source_marker": reply_receipt.source_marker.model_dump(mode="json"),
            }
        return self._finish(
            claim_id=claim_id,
            topic=outbox.topic,
            outbox_id=outbox.outbox_id,
            state=state,
            error_code=error_code,
            receipt=receipt,
        )

    def _project_reply(
        self,
        event: LinearSessionReplyEvent,
    ) -> tuple[
        LinearDeliveryState,
        str | None,
        LinearCommentObservation | None,
    ]:
        try:
            observed_target = self.remote.preflight_reply_target(event.target)
        except LinearDeliveryUnavailable:
            return "retryable", "linear_delivery_unavailable", None
        if observed_target is None:
            return "dead_letter", "linear_reply_target_not_found", None
        if (
            observed_target.workspace_id != event.target.workspace_id
            or observed_target.issue_id != event.target.issue_id
            or observed_target.thread_id != event.target.thread_id
            or observed_target.source_comment_id != event.target.source_comment_id
        ):
            return "dead_letter", "linear_reply_target_mismatch", None
        if (
            observed_target.workspace_archived
            or observed_target.issue_archived
            or observed_target.thread_archived
        ):
            return "dead_letter", "linear_reply_target_archived", None
        try:
            observed = self.remote.observe_comment(
                issue_id=event.target.issue_id,
                marker=event.marker,
                expected_author_app_id=self.app_id,
                thread_id=event.target.thread_id,
            )
        except LinearDeliveryUnavailable:
            return "retryable", "linear_delivery_unavailable", None
        if observed is not None and observed.author_app_id != self.app_id:
            observed = None
        if observed is None:
            try:
                observed = self.remote.create_comment(
                    issue_id=event.target.issue_id,
                    body=event.transport_body,
                    thread_id=event.target.thread_id,
                )
            except LinearDeliveryUnavailable:
                return "retryable", "linear_delivery_unavailable", None
        error = self._validate_reply_comment(event, observed, self.app_id)
        if error is not None:
            return "dead_letter", error, None
        return "delivered", None, observed

    @staticmethod
    def _validate_reply_comment(
        event: LinearSessionReplyEvent,
        comment: LinearCommentObservation,
        expected_author_app_id: str,
    ) -> str | None:
        if comment.author_app_id != expected_author_app_id:
            return "linear_reply_comment_author_mismatch"
        if (
            comment.issue_id != event.target.issue_id
            or comment.thread_id != event.target.thread_id
            or not _LINEAR_UUID.fullmatch(comment.comment_id)
        ):
            return "linear_reply_comment_target_mismatch"
        try:
            payload = strip_linear_transport_envelope(comment.body, event.marker)
        except ValueError:
            return "linear_reply_comment_marker_mismatch"
        if payload != event.renderer_payload:
            return "linear_reply_comment_payload_mismatch"
        digest = f"sha256:{hashlib.sha256(payload).hexdigest()}"
        if digest != event.payload_digest:
            return "linear_reply_comment_payload_mismatch"
        return None

    @staticmethod
    def _reply_receipt(
        event: LinearSessionReplyEvent,
        comment: LinearCommentObservation,
    ) -> LinearSessionReplyReceipt:
        digest = canonical_digest(
            {
                "kind": "linear.projection-receipt.v1",
                "event_id": event.event_id,
                "comment_id": comment.comment_id,
                "author_app_id": comment.author_app_id,
                "payload_digest": event.payload_digest,
                "marker": event.marker,
            }
        )
        return LinearSessionReplyReceipt(
            receipt_id=f"linear-receipt-{digest.removeprefix('sha256:')}",
            event_id=event.event_id,
            task_id=event.task_id,
            session_id=event.session_id,
            target=event.target,
            comment_id=comment.comment_id,
            payload_digest=event.payload_digest,
            transport_digest=(
                f"sha256:{hashlib.sha256(event.transport_body).hexdigest()}"
            ),
            marker=event.marker,
            source_marker=event.source_marker,
        )

    def _finish(
        self,
        *,
        claim_id: str,
        topic: str,
        outbox_id: str,
        state: LinearDeliveryState,
        error_code: str | None,
        receipt: dict[str, object] | None,
    ) -> LinearWorkerResult:
        stored = self.runtime.finish_linear_delivery(
            claim_id=claim_id,
            state=state,
            error_code=error_code,
            receipt=receipt,
            finished_at=self._clock(),
        )
        receipt_id = str(receipt["receipt_id"]) if receipt is not None else None
        return LinearWorkerResult(
            state=state,
            topic=topic,
            outbox_id=stored.outbox_id,
            error_code=error_code,
            receipt_id=receipt_id,
        )


class ProtocolClock(Protocol):
    def __call__(self) -> datetime: ...
