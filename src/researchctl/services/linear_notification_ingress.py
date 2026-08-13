from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC
from typing import Literal, Protocol

from pydantic import TypeAdapter, ValidationError

from researchctl.domain.models import (
    SessionNotificationOrigin,
    SessionNotificationSourceMarker,
    StrictModel,
    TaskRecord,
)
from researchctl.domain.types import (
    GitObjectId,
    LinearUuid,
    NonEmptyStr,
    SessionId,
    Sha256Digest,
    ShortText,
    TaskId,
    UtcDateTime,
)
from researchctl.errors import RCPError
from researchctl.runtime import RuntimeStore, VerifiedLinearIngressReceipt
from researchctl.serialization import canonical_digest
from researchctl.services.actor import ActorContext, ActorRole
from researchctl.services.requests import (
    NotificationSendRequest,
    linear_notification_request_digest,
)

_EXPLICIT = re.compile(
    r"^notify session:(?P<session>session_\d{8}T\d{6}Z_[0-9a-f]{24}) "
    r"commit:(?P<commit>(?:[0-9a-f]{40}|[0-9a-f]{64}))$"
)
_CONTEXTUAL = re.compile(
    r"^reply commit:(?P<commit>(?:[0-9a-f]{40}|[0-9a-f]{64}))$"
)
_MAX_COMMAND_BYTES = 8 * 1024

_LINEAR_UUID = TypeAdapter(LinearUuid)
_SESSION_ID = TypeAdapter(SessionId)
_TASK_ID = TypeAdapter(TaskId)
_GIT_OBJECT_ID = TypeAdapter(GitObjectId)
_MESSAGE = TypeAdapter(NonEmptyStr)


@dataclass(frozen=True, slots=True)
class LinearNotificationDirective:
    kind: Literal["notify", "reply"]
    commit_sha: str
    message: str
    session_id: str | None = None


class AuthenticatedLinearNotificationEvent(StrictModel):
    """Exact comment data emitted by a signature-verifying Linear adapter.

    ``observed_at`` must be the stable Linear comment timestamp, not the local
    webhook delivery time, so retries derive the same operation identities.
    """

    provider: Literal["linear"] = "linear"
    authenticated_app_id: ShortText
    mentioned_app_id: ShortText
    author_id: LinearUuid
    credential_identity: ShortText
    workspace_id: LinearUuid
    issue_id: LinearUuid
    thread_id: LinearUuid
    comment_id: LinearUuid
    webhook_event_id: ShortText | None = None
    command_text: str
    observed_payload_digest: Sha256Digest
    observed_at: UtcDateTime


@dataclass(frozen=True, slots=True)
class ResolvedLinearNotification:
    directive: LinearNotificationDirective
    task_id: str
    session_id: str
    source_marker: SessionNotificationSourceMarker | None


@dataclass(frozen=True, slots=True)
class LinearCommentEnvelope:
    """Authenticated Linear comment data supplied by a trusted adapter."""

    workspace_id: str
    issue_id: str
    thread_id: str
    comment_id: str
    command_text: str

    def __post_init__(self) -> None:
        try:
            _LINEAR_UUID.validate_python(self.workspace_id)
            _LINEAR_UUID.validate_python(self.issue_id)
            _LINEAR_UUID.validate_python(self.thread_id)
            _LINEAR_UUID.validate_python(self.comment_id)
        except ValidationError as error:
            raise RCPError(
                code="linear_notification_origin_invalid",
                message="Linear notification origin IDs must be exact UUIDs.",
            ) from error


class LinearTaskBindingResolver(Protocol):
    def require_task_id(self, workspace_id: str, issue_id: str) -> str: ...


class LinearSourceMarkerResolver(Protocol):
    def require_source_marker(
        self,
        *,
        workspace_id: str,
        issue_id: str,
        thread_id: str,
    ) -> SessionNotificationSourceMarker: ...


class LinearTaskRecordReader(Protocol):
    def list(self) -> tuple[TaskRecord, ...]: ...


class LinearNotificationApplication(Protocol):
    project_id: str
    runtime: RuntimeStore
    tasks: LinearTaskRecordReader

    def notification_send(
        self,
        request: NotificationSendRequest,
        actor: ActorContext,
    ) -> object: ...


class CanonicalLinearTaskBindingResolver:
    """Resolve an issue only through manager-owned canonical Task records."""

    def __init__(
        self,
        *,
        tasks: LinearTaskRecordReader,
        workspace_id: str,
    ) -> None:
        try:
            self._workspace_id = _LINEAR_UUID.validate_python(workspace_id)
        except ValidationError as error:
            raise RCPError(
                code="linear_notification_workspace_invalid",
                message="Configured Linear workspace must be an exact UUID.",
            ) from error
        self._tasks = tasks

    def require_task_id(self, workspace_id: str, issue_id: str) -> str:
        if workspace_id != self._workspace_id:
            raise RCPError(
                code="linear_notification_workspace_mismatch",
                message="Linear notification came from another workspace.",
                context={"workspace_id": workspace_id},
            )
        matches = [
            task.task_id
            for task in self._tasks.list()
            if task.linear_issue_id == issue_id
        ]
        if not matches:
            raise RCPError(
                code="linear_notification_task_unbound",
                message="No canonical Task is bound to this Linear issue.",
                context={"issue_id": issue_id},
            )
        if len(matches) != 1:
            raise RCPError(
                code="linear_notification_task_ambiguous",
                message="Multiple canonical Tasks are bound to this Linear issue.",
                context={"issue_id": issue_id},
            )
        return matches[0]


class RuntimeLinearSourceMarkerResolver:
    """Resolve context only from immutable accepted-result delivery receipts."""

    def __init__(self, runtime: RuntimeStore) -> None:
        self._runtime = runtime

    def require_source_marker(
        self,
        *,
        workspace_id: str,
        issue_id: str,
        thread_id: str,
    ) -> SessionNotificationSourceMarker:
        marker = self._runtime.resolve_verified_source_marker(
            workspace_id=workspace_id,
            issue_id=issue_id,
            thread_id=thread_id,
        )
        if marker is None:
            raise RCPError(
                code="linear_notification_source_unverified",
                message="Linear thread has no accepted-result delivery receipt.",
                remediation=(
                    "Reply in the accepted-result thread or use an explicit full "
                    "Session ID."
                ),
                context={"issue_id": issue_id, "thread_id": thread_id},
            )
        return marker


def parse_linear_notification_command(text: str) -> LinearNotificationDirective:
    """Parse text after the trusted app mention; never interpret it as a shell."""

    if not isinstance(text, str):
        raise _command_error()
    if len(text.encode("utf-8")) > _MAX_COMMAND_BYTES:
        raise RCPError(
            code="linear_notification_command_too_large",
            message="Linear notification command exceeds 8 KiB.",
        )
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    first_line, separator, remainder = normalized.partition("\n")
    if not separator:
        raise _command_error()
    try:
        message = _MESSAGE.validate_python(remainder)
    except ValidationError as error:
        raise _command_error("A notification command requires a message body.") from error

    explicit = _EXPLICIT.fullmatch(first_line)
    if explicit is not None:
        return LinearNotificationDirective(
            kind="notify",
            session_id=_SESSION_ID.validate_python(explicit.group("session")),
            commit_sha=_GIT_OBJECT_ID.validate_python(explicit.group("commit")),
            message=message,
        )
    contextual = _CONTEXTUAL.fullmatch(first_line)
    if contextual is not None:
        return LinearNotificationDirective(
            kind="reply",
            commit_sha=_GIT_OBJECT_ID.validate_python(contextual.group("commit")),
            message=message,
        )
    raise _command_error()


class LinearNotificationIngress:
    """Build a strict send request from authenticated, pre-parsed Linear input."""

    def __init__(
        self,
        *,
        tasks: LinearTaskBindingResolver,
        markers: LinearSourceMarkerResolver,
    ) -> None:
        self._tasks = tasks
        self._markers = markers

    def resolve(
        self,
        envelope: LinearCommentEnvelope,
    ) -> ResolvedLinearNotification:
        directive = parse_linear_notification_command(envelope.command_text)
        task_id = _TASK_ID.validate_python(
            self._tasks.require_task_id(envelope.workspace_id, envelope.issue_id)
        )
        source_marker: SessionNotificationSourceMarker | None = None
        if directive.kind == "reply":
            source_marker = self._markers.require_source_marker(
                issue_id=envelope.issue_id,
                thread_id=envelope.thread_id,
                workspace_id=envelope.workspace_id,
            )
            if source_marker.task_id != task_id:
                raise RCPError(
                    code="linear_notification_source_task_mismatch",
                    message="Verified source thread belongs to another Task.",
                )
            session_id = source_marker.session_id
        else:
            assert directive.session_id is not None
            session_id = directive.session_id
        return ResolvedLinearNotification(
            directive=directive,
            task_id=task_id,
            session_id=session_id,
            source_marker=source_marker,
        )

    def build_request(
        self,
        envelope: LinearCommentEnvelope,
        *,
        operation_id: str,
        idempotency_key: str,
        notification_id: str,
    ) -> NotificationSendRequest:
        resolved = self.resolve(envelope)

        return NotificationSendRequest(
            operation_id=operation_id,
            idempotency_key=idempotency_key,
            notification_id=notification_id,
            directive_kind=resolved.directive.kind,
            task_id=resolved.task_id,
            session_id=resolved.session_id,
            commit_sha=resolved.directive.commit_sha,
            message=resolved.directive.message,
            origin=SessionNotificationOrigin(
                workspace_id=envelope.workspace_id,
                issue_id=envelope.issue_id,
                thread_id=envelope.thread_id,
                comment_id=envelope.comment_id,
                source_marker=resolved.source_marker,
            ),
        )


class LinearNotificationIngressFacade:
    """Authenticated Linear event to durable SessionNotification boundary."""

    def __init__(
        self,
        *,
        application: LinearNotificationApplication,
        runtime: RuntimeStore,
        workspace_id: str,
        app_id: str,
        notification_author_ids: tuple[str, ...],
        credential_identity: str,
        actor: ActorContext,
    ) -> None:
        if application.runtime is not runtime:
            raise ValueError("ingress and ApplicationService must share one RuntimeStore")
        if not app_id.strip() or not credential_identity.strip():
            raise ValueError("Linear app and credential identities must be non-empty")
        actor.require_role(
            "linear.notification.ingress.configure",
            ActorRole.TRUSTED_AUTOMATION,
        )
        if actor.actor_id != credential_identity:
            raise ValueError(
                "trusted Linear actor must match the credential identity"
            )
        resolver = CanonicalLinearTaskBindingResolver(
            tasks=application.tasks,
            workspace_id=workspace_id,
        )
        self._application = application
        self._runtime = runtime
        self._workspace_id = workspace_id
        self._app_id = app_id
        try:
            self._notification_author_ids = frozenset(
                _LINEAR_UUID.validate_python(author_id)
                for author_id in notification_author_ids
            )
        except ValidationError as error:
            raise ValueError(
                "Linear notification authors must be exact UUIDs"
            ) from error
        self._credential_identity = credential_identity
        self._actor = actor
        self._ingress = LinearNotificationIngress(
            tasks=resolver,
            markers=RuntimeLinearSourceMarkerResolver(runtime),
        )

    def ingest(
        self,
        event: AuthenticatedLinearNotificationEvent,
    ) -> object:
        self._require_expected_identity(event)
        envelope = LinearCommentEnvelope(
            workspace_id=event.workspace_id,
            issue_id=event.issue_id,
            thread_id=event.thread_id,
            comment_id=event.comment_id,
            command_text=event.command_text,
        )
        resolved = self._ingress.resolve(envelope)
        source_marker = resolved.source_marker
        session_id = resolved.session_id
        existing_receipt = self._existing_receipt(event, resolved.task_id)
        if existing_receipt is not None:
            expected_contextual = resolved.directive.kind == "reply"
            if expected_contextual != (existing_receipt.source_marker is not None):
                raise RCPError(
                    code="linear_notification_source_receipt_mismatch",
                    message=(
                        "Verified Linear ingress directive and historical thread "
                        "binding differ."
                    ),
                )
            source_marker = existing_receipt.source_marker
            if source_marker is not None:
                session_id = source_marker.session_id
        semantic_digest = canonical_digest(
            {
                "kind": "linear.notification-semantics.v1",
                "directive_kind": resolved.directive.kind,
                "task_id": resolved.task_id,
                "session_id": session_id,
                "commit_sha": resolved.directive.commit_sha,
                "message": resolved.directive.message,
                "source_marker": (
                    source_marker.model_dump(mode="json", exclude_none=True)
                    if source_marker is not None
                    else None
                ),
            }
        )
        identity_digest = canonical_digest(
            {
                "kind": "linear.notification-ingress.v1",
                "project_id": self._application.project_id,
                "workspace_id": event.workspace_id,
                "issue_id": event.issue_id,
                "thread_id": event.thread_id,
                "comment_id": event.comment_id,
                "webhook_event_id": event.webhook_event_id,
                "task_id": resolved.task_id,
                "semantic_digest": semantic_digest,
            }
        )
        suffix = identity_digest.removeprefix("sha256:")[:24]
        timestamp = event.observed_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
        request = NotificationSendRequest(
            operation_id=f"operation_{timestamp}_{suffix}",
            idempotency_key=(
                f"linear-comment:{event.workspace_id}:{event.comment_id}"
            ),
            notification_id=f"notification_{timestamp}_{suffix}",
            directive_kind=resolved.directive.kind,
            task_id=resolved.task_id,
            session_id=session_id,
            commit_sha=resolved.directive.commit_sha,
            message=resolved.directive.message,
            origin=SessionNotificationOrigin(
                workspace_id=event.workspace_id,
                issue_id=event.issue_id,
                thread_id=event.thread_id,
                comment_id=event.comment_id,
                source_marker=source_marker,
            ),
        )
        request_digest = linear_notification_request_digest(request)
        receipt = self._runtime.record_verified_linear_ingress(
            project_id=self._application.project_id,
            authenticated_app_id=event.authenticated_app_id,
            mentioned_app_id=event.mentioned_app_id,
            author_id=event.author_id,
            credential_identity=self._credential_identity,
            workspace_id=event.workspace_id,
            issue_id=event.issue_id,
            thread_id=event.thread_id,
            comment_id=event.comment_id,
            webhook_event_id=event.webhook_event_id,
            task_id=resolved.task_id,
            source_marker=source_marker,
            command_digest=request_digest,
            observed_payload_digest=event.observed_payload_digest,
            verified_at=event.observed_at,
        )
        if (
            receipt.source_marker != request.origin.source_marker
            or receipt.command_digest != request_digest
        ):
            raise RCPError(
                code="linear_notification_source_receipt_mismatch",
                message="Verified Linear ingress does not bind this notification.",
            )
        return self._application.notification_send(request, self._actor)

    def _existing_receipt(
        self,
        event: AuthenticatedLinearNotificationEvent,
        task_id: str,
    ) -> VerifiedLinearIngressReceipt | None:
        try:
            return self._runtime.require_verified_notification_origin(
                project_id=self._application.project_id,
                workspace_id=event.workspace_id,
                issue_id=event.issue_id,
                thread_id=event.thread_id,
                comment_id=event.comment_id,
                task_id=task_id,
            )
        except RCPError as error:
            if error.code == "linear_notification_origin_unverified":
                return None
            raise

    def _require_expected_identity(
        self,
        event: AuthenticatedLinearNotificationEvent,
    ) -> None:
        if event.credential_identity != self._credential_identity:
            raise RCPError(
                code="linear_notification_credential_mismatch",
                message="Linear event was authenticated under another credential identity.",
            )
        if (
            event.authenticated_app_id != self._app_id
            or event.mentioned_app_id != self._app_id
        ):
            raise RCPError(
                code="linear_notification_app_identity_mismatch",
                message="Linear event does not target the configured authenticated app.",
            )
        if event.workspace_id != self._workspace_id:
            raise RCPError(
                code="linear_notification_workspace_mismatch",
                message="Linear event came from another workspace.",
                context={"workspace_id": event.workspace_id},
            )
        if event.author_id not in self._notification_author_ids:
            raise RCPError(
                code="linear_notification_author_not_allowed",
                message="Linear comment author is not allowed to notify Sessions.",
            )


def _command_error(
    message: str = "Linear notification command has an invalid fixed format.",
) -> RCPError:
    return RCPError(
        code="linear_notification_command_invalid",
        message=message,
        remediation=(
            "Use `notify session:<full-session-id> commit:<full-sha>` or "
            "`reply commit:<full-sha>` on the first line, followed by a message."
        ),
    )
