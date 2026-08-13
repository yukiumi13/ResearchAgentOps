from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Literal, Protocol

from researchctl.domain.enums import SubmissionState
from researchctl.domain.models import (
    CIValidationAttestation,
    LinearProjectionConfigured,
    LinearProjectionPolicy,
    ReportRecord,
    ResearchSubmission,
    ReviewDecision,
    SessionNotificationSourceMarker,
    TaskRecord,
)
from researchctl.errors import RCPError
from researchctl.serialization import canonical_digest
from researchctl.services.actor import ActorContext, ActorRole
from researchctl.services.linear_preview import (
    LINEAR_RENDERER_ID,
    LINEAR_RENDERER_VERSION,
    build_linear_preview,
)

_GIT_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SHA256_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_LINEAR_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_EVENT_KIND = "linear.accepted-result.v1"
_MARKER_PREFIX = "<!-- researchctl-linear-delivery:v1"
_DELIVERY_MARKER = re.compile(
    r"^<!-- researchctl-linear-delivery:v1 "
    r"event_id=(?P<event_id>\S+) "
    r"payload_digest=(?P<payload_digest>sha256:[0-9a-f]{64}) "
    r"agent_id=(?P<agent_id>\S+) "
    r"session_id=(?P<session_id>\S+) "
    r"task_id=(?P<task_id>\S+) "
    r"report_id=(?P<report_id>\S+) -->$"
)


@dataclass(frozen=True, slots=True)
class AcceptedMergeSnapshot:
    """Canonical accepted records read from the protected default branch.

    The adapter producing this value owns the Git checks. It must verify that
    ``merge_commit`` is reachable from ``protected_ref`` and that the exact CI
    subject was incorporated by that accepted merge. The booleans are retained
    so the core service fails closed if an adapter cannot establish either fact.
    ``policy`` and the Task issue binding are manager-owned canonical data, not
    values supplied by an Agent or by the delivery request.
    """

    project_id: str
    merge_commit: str
    subject_head: str
    default_branch: str
    protected_ref: str
    accepted_on_protected_default_branch: bool
    attested_subject_incorporated: bool
    task: TaskRecord
    submission: ResearchSubmission
    decision: ReviewDecision
    report: ReportRecord
    policy: LinearProjectionPolicy | None


class AcceptedMergeReader(Protocol):
    def read_accepted_merge(
        self,
        *,
        project_id: str,
        merge_commit: str,
        ci: CIValidationAttestation,
    ) -> AcceptedMergeSnapshot | None: ...


@dataclass(frozen=True, slots=True)
class LinearTarget:
    workspace_id: str
    team_id: str
    project_id: str | None
    issue_id: str


@dataclass(frozen=True, slots=True)
class LinearTargetObservation:
    """Read-only UUID lookup and relationship snapshot from Linear."""

    workspace_id: str
    team_id: str
    team_workspace_id: str
    project_id: str | None
    project_workspace_id: str | None
    project_team_ids: tuple[str, ...]
    issue_id: str
    issue_workspace_id: str
    issue_team_id: str
    issue_project_ids: tuple[str, ...]
    workspace_archived: bool = False
    team_archived: bool = False
    project_archived: bool = False
    issue_archived: bool = False


@dataclass(frozen=True, slots=True)
class LinearCommentObservation:
    comment_id: str
    issue_id: str
    thread_id: str
    author_app_id: str
    body: bytes


class LinearDeliveryUnavailable(Exception):
    """A retryable transport or Linear availability failure."""


class LinearDeliveryPort(Protocol):
    """Minimal trusted Linear adapter; only ``create_comment`` mutates state."""

    def preflight_target(
        self,
        target: LinearTarget,
    ) -> LinearTargetObservation | None: ...

    def observe_comment(
        self,
        *,
        issue_id: str,
        marker: str,
        expected_author_app_id: str,
        thread_id: str | None = None,
    ) -> LinearCommentObservation | None: ...

    def create_comment(
        self,
        *,
        issue_id: str,
        body: bytes,
        thread_id: str | None = None,
    ) -> LinearCommentObservation: ...


@dataclass(frozen=True, slots=True)
class LinearAcceptedResultEvent:
    event_id: str
    agent_id: str
    project_id: str
    task_id: str
    session_id: str
    submission_id: str
    decision_id: str
    report_id: str
    report_revision: int
    accepted_merge_commit: str
    ci_subject_head: str
    ci_attestation_id: str
    workflow_id: str
    check_identity: str
    task_digest: str
    submission_digest: str
    decision_digest: str
    report_digest: str
    target: LinearTarget
    ci_projection: LinearProjectionConfigured
    renderer_id: str
    renderer_version: int
    payload_digest: str
    renderer_payload: bytes
    marker: str
    transport_body: bytes
    workflow_run_id: str | None = None
    check_run_id: str | None = None
    artifact_id: str | None = None


@dataclass(frozen=True, slots=True)
class LinearProjectionReceipt:
    receipt_id: str
    event_id: str
    project_id: str
    task_id: str
    session_id: str
    submission_id: str
    report_id: str
    report_revision: int
    accepted_merge_commit: str
    ci_subject_head: str
    ci_attestation_id: str
    workflow_id: str
    check_identity: str
    task_digest: str
    submission_digest: str
    decision_digest: str
    report_digest: str
    target: LinearTarget
    comment_id: str
    thread_id: str
    renderer_id: str
    renderer_version: int
    payload_digest: str
    transport_digest: str
    marker: str
    source_marker: SessionNotificationSourceMarker
    workflow_run_id: str | None = None
    check_run_id: str | None = None
    artifact_id: str | None = None


@dataclass(frozen=True, slots=True)
class LinearDeliveryOutcome:
    state: Literal["delivered", "retryable", "dead_letter"]
    event_id: str
    receipt: LinearProjectionReceipt | None = None
    error_code: str | None = None
    retry_stage: Literal["preflight", "observe", "create"] | None = None


def linear_event_payload(event: LinearAcceptedResultEvent) -> dict[str, object]:
    return {
        "version": 1,
        "event_id": event.event_id,
        "agent_id": event.agent_id,
        "project_id": event.project_id,
        "task_id": event.task_id,
        "session_id": event.session_id,
        "submission_id": event.submission_id,
        "decision_id": event.decision_id,
        "report_id": event.report_id,
        "report_revision": event.report_revision,
        "accepted_merge_commit": event.accepted_merge_commit,
        "ci_subject_head": event.ci_subject_head,
        "ci_attestation_id": event.ci_attestation_id,
        "workflow_id": event.workflow_id,
        "check_identity": event.check_identity,
        "workflow_run_id": event.workflow_run_id,
        "check_run_id": event.check_run_id,
        "artifact_id": event.artifact_id,
        "task_digest": event.task_digest,
        "submission_digest": event.submission_digest,
        "decision_digest": event.decision_digest,
        "report_digest": event.report_digest,
        "target": {
            "workspace_id": event.target.workspace_id,
            "team_id": event.target.team_id,
            "project_id": event.target.project_id,
            "issue_id": event.target.issue_id,
        },
        "ci_projection": event.ci_projection.model_dump(mode="json"),
        "renderer_id": event.renderer_id,
        "renderer_version": event.renderer_version,
        "payload_digest": event.payload_digest,
        "renderer_payload": event.renderer_payload.decode("utf-8"),
        "marker": event.marker,
        "transport_body": event.transport_body.decode("utf-8"),
    }


def linear_event_from_payload(payload: dict[str, object]) -> LinearAcceptedResultEvent:
    expected = {
        "version",
        "event_id",
        "agent_id",
        "project_id",
        "task_id",
        "session_id",
        "submission_id",
        "decision_id",
        "report_id",
        "report_revision",
        "accepted_merge_commit",
        "ci_subject_head",
        "ci_attestation_id",
        "workflow_id",
        "check_identity",
        "workflow_run_id",
        "check_run_id",
        "artifact_id",
        "task_digest",
        "submission_digest",
        "decision_digest",
        "report_digest",
        "target",
        "ci_projection",
        "renderer_id",
        "renderer_version",
        "payload_digest",
        "renderer_payload",
        "marker",
        "transport_body",
    }
    if set(payload) != expected or payload.get("version") != 1:
        raise RCPError(
            code="linear_delivery_event_invalid",
            message="Durable Linear event has an invalid closed payload shape.",
        )
    target = payload.get("target")
    projection = payload.get("ci_projection")
    github_ids = (
        payload.get("workflow_run_id"),
        payload.get("check_run_id"),
        payload.get("artifact_id"),
    )
    if not all(value is None or isinstance(value, str) for value in github_ids):
        raise RCPError(
            code="linear_delivery_event_invalid",
            message="Durable Linear event has invalid GitHub provenance IDs.",
        )
    if any(value is None for value in github_ids) and any(
        value is not None for value in github_ids
    ):
        raise RCPError(
            code="linear_delivery_event_invalid",
            message="Durable Linear event has incomplete GitHub provenance.",
        )
    if not isinstance(target, dict) or set(target) != {
        "workspace_id",
        "team_id",
        "project_id",
        "issue_id",
    }:
        raise RCPError(
            code="linear_delivery_event_invalid",
            message="Durable Linear event has an invalid target.",
        )
    try:
        return LinearAcceptedResultEvent(
            event_id=str(payload["event_id"]),
            agent_id=str(payload["agent_id"]),
            project_id=str(payload["project_id"]),
            task_id=str(payload["task_id"]),
            session_id=str(payload["session_id"]),
            submission_id=str(payload["submission_id"]),
            decision_id=str(payload["decision_id"]),
            report_id=str(payload["report_id"]),
            report_revision=int(payload["report_revision"]),
            accepted_merge_commit=str(payload["accepted_merge_commit"]),
            ci_subject_head=str(payload["ci_subject_head"]),
            ci_attestation_id=str(payload["ci_attestation_id"]),
            workflow_id=str(payload["workflow_id"]),
            check_identity=str(payload["check_identity"]),
            task_digest=str(payload["task_digest"]),
            submission_digest=str(payload["submission_digest"]),
            decision_digest=str(payload["decision_digest"]),
            report_digest=str(payload["report_digest"]),
            target=LinearTarget(
                workspace_id=str(target["workspace_id"]),
                team_id=str(target["team_id"]),
                project_id=(
                    str(target["project_id"])
                    if target["project_id"] is not None
                    else None
                ),
                issue_id=str(target["issue_id"]),
            ),
            ci_projection=LinearProjectionConfigured.model_validate(projection),
            renderer_id=str(payload["renderer_id"]),
            renderer_version=int(payload["renderer_version"]),
            payload_digest=str(payload["payload_digest"]),
            renderer_payload=str(payload["renderer_payload"]).encode("utf-8"),
            marker=str(payload["marker"]),
            transport_body=str(payload["transport_body"]).encode("utf-8"),
            workflow_run_id=(
                str(payload["workflow_run_id"])
                if payload["workflow_run_id"] is not None
                else None
            ),
            check_run_id=(
                str(payload["check_run_id"])
                if payload["check_run_id"] is not None
                else None
            ),
            artifact_id=(
                str(payload["artifact_id"])
                if payload["artifact_id"] is not None
                else None
            ),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise RCPError(
            code="linear_delivery_event_invalid",
            message="Durable Linear event could not be reconstructed.",
        ) from error


def linear_receipt_payload(receipt: LinearProjectionReceipt) -> dict[str, object]:
    return {
        "version": 1,
        "receipt_id": receipt.receipt_id,
        "event_id": receipt.event_id,
        "project_id": receipt.project_id,
        "task_id": receipt.task_id,
        "session_id": receipt.session_id,
        "submission_id": receipt.submission_id,
        "report_id": receipt.report_id,
        "report_revision": receipt.report_revision,
        "accepted_merge_commit": receipt.accepted_merge_commit,
        "ci_subject_head": receipt.ci_subject_head,
        "ci_attestation_id": receipt.ci_attestation_id,
        "workflow_id": receipt.workflow_id,
        "check_identity": receipt.check_identity,
        "workflow_run_id": receipt.workflow_run_id,
        "check_run_id": receipt.check_run_id,
        "artifact_id": receipt.artifact_id,
        "task_digest": receipt.task_digest,
        "submission_digest": receipt.submission_digest,
        "decision_digest": receipt.decision_digest,
        "report_digest": receipt.report_digest,
        "workspace_id": receipt.target.workspace_id,
        "team_id": receipt.target.team_id,
        "linear_project_id": receipt.target.project_id,
        "issue_id": receipt.target.issue_id,
        "comment_id": receipt.comment_id,
        "thread_id": receipt.thread_id,
        "renderer_id": receipt.renderer_id,
        "renderer_version": receipt.renderer_version,
        "payload_digest": receipt.payload_digest,
        "transport_digest": receipt.transport_digest,
        "marker": receipt.marker,
        "source_marker": receipt.source_marker.model_dump(mode="json"),
    }


def linear_delivery_event_id(
    *,
    project_id: str,
    task_id: str,
    report_id: str,
    report_revision: int,
) -> str:
    """Return the stable identity of one immutable accepted Report revision."""

    digest = canonical_digest(
        {
            "kind": _EVENT_KIND,
            "project_id": project_id,
            "task_id": task_id,
            "report_id": report_id,
            "report_revision": report_revision,
        }
    )
    return f"linear-event-{digest.removeprefix('sha256:')}"


def linear_delivery_marker(
    *,
    event_id: str,
    payload_digest: str,
    agent_id: str,
    session_id: str,
    task_id: str,
    report_id: str,
) -> str:
    return (
        f"{_MARKER_PREFIX} event_id={event_id} "
        f"payload_digest={payload_digest} agent_id={agent_id} "
        f"session_id={session_id} task_id={task_id} "
        f"report_id={report_id} -->"
    )


def parse_linear_delivery_marker(marker: str) -> dict[str, str]:
    matched = _DELIVERY_MARKER.fullmatch(marker)
    if matched is None:
        raise ValueError("Linear delivery marker is not canonical")
    return matched.groupdict()


def add_linear_transport_envelope(payload: bytes, marker: str) -> bytes:
    """Append the idempotency marker without changing the CI payload boundary."""

    if not payload or not payload.endswith(b"\n"):
        raise ValueError("Linear renderer payload must end with one newline")
    encoded_marker = marker.encode("ascii")
    if encoded_marker in payload:
        raise ValueError("Linear renderer payload already contains the delivery marker")
    return payload + encoded_marker + b"\n"


def strip_linear_transport_envelope(body: bytes, marker: str) -> bytes:
    suffix = marker.encode("ascii") + b"\n"
    if not body.endswith(suffix):
        raise ValueError("Linear comment is missing the exact delivery marker")
    payload = body[: -len(suffix)]
    if not payload.endswith(b"\n"):
        raise ValueError("Linear comment contains an invalid transport envelope")
    return payload


class LinearAcceptedResultDeliveryService:
    def __init__(self, accepted_merges: AcceptedMergeReader) -> None:
        self._accepted_merges = accepted_merges

    def enqueue(
        self,
        *,
        actor: ActorContext,
        project_id: str,
        merge_commit: str,
        ci: CIValidationAttestation,
    ) -> LinearAcceptedResultEvent | None:
        """Build an outbox event only from a trusted post-merge observation.

        ``None`` means both manager policy and CI explicitly disabled projection.
        A caller cannot select a Linear target: all four UUIDs are recovered from
        the canonical policy and Task in ``AcceptedMergeSnapshot``.
        """

        actor.require_role(
            "enqueue accepted-result Linear projection",
            ActorRole.TRUSTED_AUTOMATION,
        )
        if not _GIT_OBJECT_ID.fullmatch(merge_commit):
            raise RCPError(
                code="linear_delivery_merge_identity_invalid",
                message="Post-merge delivery requires a full Git commit identity.",
            )
        snapshot = self._accepted_merges.read_accepted_merge(
            project_id=project_id,
            merge_commit=merge_commit,
            ci=ci,
        )
        self._require_accepted_merge(
            snapshot,
            project_id=project_id,
            merge_commit=merge_commit,
            ci=ci,
        )
        assert snapshot is not None

        if snapshot.policy is None:
            if ci.projection.state == "disabled":
                return None
            self._ci_mismatch("CI configured a target absent from manager policy.")
        if ci.projection.state != "configured":
            self._ci_mismatch("CI did not attest the manager-configured projection.")
        assert snapshot.policy is not None
        preview = build_linear_preview(
            policy=snapshot.policy,
            task=snapshot.task,
            submission=snapshot.submission,
            decision=snapshot.decision,
            report=snapshot.report,
        )
        if preview.body is None or preview.projection.state != "configured":
            self._ci_mismatch("Configured projection did not render a Linear payload.")
        assert isinstance(preview.projection, LinearProjectionConfigured)
        assert preview.body is not None
        event_id = linear_delivery_event_id(
            project_id=project_id,
            task_id=snapshot.task.task_id,
            report_id=snapshot.report.report_id,
            report_revision=snapshot.report.revision,
        )
        agent_id = f"agent-{snapshot.submission.session_id}"
        marker = linear_delivery_marker(
            event_id=event_id,
            payload_digest=preview.projection.payload_digest,
            agent_id=agent_id,
            session_id=snapshot.submission.session_id,
            task_id=snapshot.task.task_id,
            report_id=snapshot.report.report_id,
        )
        transport_body = add_linear_transport_envelope(preview.body, marker)
        target = LinearTarget(
            workspace_id=preview.projection.workspace_id,
            team_id=preview.projection.team_id,
            project_id=snapshot.policy.project_id,
            issue_id=preview.projection.issue_id,
        )
        return LinearAcceptedResultEvent(
            event_id=event_id,
            agent_id=agent_id,
            project_id=project_id,
            task_id=snapshot.task.task_id,
            session_id=snapshot.submission.session_id,
            submission_id=snapshot.submission.submission_id,
            decision_id=snapshot.decision.decision_id,
            report_id=snapshot.report.report_id,
            report_revision=snapshot.report.revision,
            accepted_merge_commit=merge_commit,
            ci_subject_head=ci.subject_head,
            ci_attestation_id=ci.attestation_id,
            workflow_id=ci.workflow_id,
            check_identity=ci.check_identity,
            task_digest=canonical_digest(snapshot.task),
            submission_digest=canonical_digest(snapshot.submission),
            decision_digest=canonical_digest(snapshot.decision),
            report_digest=canonical_digest(snapshot.report),
            target=target,
            ci_projection=ci.projection,
            renderer_id=preview.projection.renderer_id,
            renderer_version=preview.projection.renderer_version,
            payload_digest=preview.projection.payload_digest,
            renderer_payload=preview.body,
            marker=marker,
            transport_body=transport_body,
        )

    def deliver(
        self,
        *,
        actor: ActorContext,
        event: LinearAcceptedResultEvent,
        remote: LinearDeliveryPort,
        expected_author_app_id: str,
    ) -> LinearDeliveryOutcome:
        actor.require_role(
            "deliver accepted-result Linear projection",
            ActorRole.TRUSTED_AUTOMATION,
        )
        if not expected_author_app_id.strip():
            raise ValueError("expected_author_app_id must be non-empty")
        local_error = self._validate_event(event)
        if local_error is not None:
            return self._dead_letter(event, local_error)

        try:
            observed_target = remote.preflight_target(event.target)
        except LinearDeliveryUnavailable:
            return self._retryable(event, "preflight")
        target_error = self._validate_remote_target(event.target, observed_target)
        if target_error is not None:
            return self._dead_letter(event, target_error)

        try:
            observed = remote.observe_comment(
                issue_id=event.target.issue_id,
                marker=event.marker,
                expected_author_app_id=expected_author_app_id,
                thread_id=None,
            )
        except LinearDeliveryUnavailable:
            return self._retryable(event, "observe")
        if (
            observed is not None
            and observed.author_app_id != expected_author_app_id
        ):
            observed = None
        if observed is not None:
            comment_error = self._validate_comment(
                event,
                observed,
                expected_author_app_id,
            )
            if comment_error is not None:
                return self._dead_letter(event, comment_error)
            return self._delivered(event, observed)

        try:
            created = remote.create_comment(
                issue_id=event.target.issue_id,
                body=event.transport_body,
                thread_id=None,
            )
        except LinearDeliveryUnavailable:
            # The write may have reached Linear. A retry observes the marker first.
            return self._retryable(event, "create")
        comment_error = self._validate_comment(
            event,
            created,
            expected_author_app_id,
        )
        if comment_error is not None:
            raise RCPError(
                code="linear_delivery_port_contract_invalid",
                message="Linear create did not return the exact created comment.",
                context={"reason": comment_error},
            )
        return self._delivered(event, created)

    @staticmethod
    def _require_accepted_merge(
        snapshot: AcceptedMergeSnapshot | None,
        *,
        project_id: str,
        merge_commit: str,
        ci: CIValidationAttestation,
    ) -> None:
        if snapshot is None:
            raise RCPError(
                code="linear_delivery_not_accepted_merge",
                message="No accepted result exists at the requested protected merge.",
            )
        expected_ref = f"refs/heads/{snapshot.default_branch}"
        if (
            snapshot.project_id != project_id
            or snapshot.merge_commit != merge_commit
            or snapshot.protected_ref != expected_ref
            or not snapshot.accepted_on_protected_default_branch
            or not snapshot.attested_subject_incorporated
            or snapshot.subject_head != ci.subject_head
            or not _GIT_OBJECT_ID.fullmatch(snapshot.subject_head)
        ):
            raise RCPError(
                code="linear_delivery_not_accepted_merge",
                message=(
                    "Accepted-result delivery requires the CI subject to be incorporated "
                    "by a protected default-branch merge."
                ),
            )
        task = snapshot.task
        submission = snapshot.submission
        decision = snapshot.decision
        report = snapshot.report
        records_valid = (
            submission.state is SubmissionState.ACCEPTED
            and submission.task_id == task.task_id
            and decision.submission_id == submission.submission_id
            and report.submission_id == submission.submission_id
            and decision.report_id == report.report_id
            and report.revision == decision.expected_report_revision + 1
            and decision.accepted_submission_digest == canonical_digest(submission)
        )
        attestation_valid = (
            ci.overall_result == "passed"
            and ci.project_id == project_id
            and ci.task_id == task.task_id
            and ci.submission_id == submission.submission_id
            and ci.submission_digest == canonical_digest(submission)
            and ci.decision_digest == canonical_digest(decision)
            and ci.report_id == report.report_id
            and ci.report_revision == report.revision
            and ci.report_digest == canonical_digest(report)
        )
        if not records_valid or not attestation_valid:
            raise RCPError(
                code="linear_delivery_acceptance_mismatch",
                message=(
                    "Protected accepted records do not match the passing exact-head "
                    "CI attestation."
                ),
            )

    @staticmethod
    def _ci_mismatch(message: str) -> None:
        raise RCPError(
            code="linear_delivery_ci_projection_mismatch",
            message=message,
            remediation="Dead-letter the projection without mutating Linear.",
        )

    @staticmethod
    def _validate_event(event: LinearAcceptedResultEvent) -> str | None:
        expected_event_id = linear_delivery_event_id(
            project_id=event.project_id,
            task_id=event.task_id,
            report_id=event.report_id,
            report_revision=event.report_revision,
        )
        expected_digest = f"sha256:{hashlib.sha256(event.renderer_payload).hexdigest()}"
        expected_marker = linear_delivery_marker(
            event_id=event.event_id,
            payload_digest=event.payload_digest,
            agent_id=event.agent_id,
            session_id=event.session_id,
            task_id=event.task_id,
            report_id=event.report_id,
        )
        if event.event_id != expected_event_id:
            return "linear_event_identity_mismatch"
        if not all(
            _GIT_OBJECT_ID.fullmatch(value)
            for value in (event.accepted_merge_commit, event.ci_subject_head)
        ):
            return "linear_event_lineage_invalid"
        if not event.ci_attestation_id or not event.workflow_id or not event.check_identity:
            return "linear_event_lineage_invalid"
        github_ids = (
            event.workflow_run_id,
            event.check_run_id,
            event.artifact_id,
        )
        if any(value is None for value in github_ids) and any(
            value is not None for value in github_ids
        ):
            return "linear_event_lineage_invalid"
        if all(value is not None for value in github_ids) and not all(
            value.isascii() and value.isdigit() and not value.startswith("0")
            for value in github_ids
            if value is not None
        ):
            return "linear_event_lineage_invalid"
        if not all(
            _SHA256_DIGEST.fullmatch(value)
            for value in (
                event.task_digest,
                event.submission_digest,
                event.decision_digest,
                event.report_digest,
            )
        ):
            return "linear_event_lineage_invalid"
        if event.renderer_id != LINEAR_RENDERER_ID:
            return "linear_renderer_mismatch"
        if event.renderer_version != LINEAR_RENDERER_VERSION:
            return "linear_renderer_mismatch"
        if event.payload_digest != expected_digest:
            return "linear_payload_digest_mismatch"
        if event.marker != expected_marker:
            return "linear_delivery_marker_mismatch"
        try:
            parsed_marker = parse_linear_delivery_marker(event.marker)
        except ValueError:
            return "linear_delivery_marker_mismatch"
        if parsed_marker != {
            "event_id": event.event_id,
            "payload_digest": event.payload_digest,
            "agent_id": event.agent_id,
            "session_id": event.session_id,
            "task_id": event.task_id,
            "report_id": event.report_id,
        }:
            return "linear_delivery_marker_mismatch"
        target_ids = (
            event.target.workspace_id,
            event.target.team_id,
            event.target.issue_id,
        )
        if not all(
            _LINEAR_UUID.fullmatch(value)
            for value in target_ids
        ):
            return "linear_target_identity_invalid"
        if (
            event.target.project_id is not None
            and not _LINEAR_UUID.fullmatch(event.target.project_id)
        ):
            return "linear_target_identity_invalid"
        try:
            renderer_payload = strip_linear_transport_envelope(
                event.transport_body,
                event.marker,
            )
        except ValueError:
            return "linear_transport_envelope_mismatch"
        if renderer_payload != event.renderer_payload:
            return "linear_transport_payload_mismatch"
        ci_target = event.ci_projection
        if (
            ci_target.workspace_id != event.target.workspace_id
            or ci_target.team_id != event.target.team_id
            or ci_target.project_id != event.target.project_id
            or ci_target.issue_id != event.target.issue_id
        ):
            return "linear_ci_target_mismatch"
        if (
            ci_target.renderer_id != event.renderer_id
            or ci_target.renderer_version != event.renderer_version
        ):
            return "linear_ci_renderer_mismatch"
        if ci_target.payload_digest != event.payload_digest:
            return "linear_ci_payload_digest_mismatch"
        return None

    @staticmethod
    def _validate_remote_target(
        target: LinearTarget,
        observed: LinearTargetObservation | None,
    ) -> str | None:
        if observed is None:
            return "linear_target_not_found"
        exact_identity = (
            observed.workspace_id == target.workspace_id
            and observed.team_id == target.team_id
            and observed.issue_id == target.issue_id
        )
        exact_relationships = (
            observed.team_workspace_id == target.workspace_id
            and observed.issue_workspace_id == target.workspace_id
            and observed.issue_team_id == target.team_id
        )
        if target.project_id is not None:
            exact_identity = exact_identity and observed.project_id == target.project_id
            exact_relationships = (
                exact_relationships
                and observed.project_workspace_id == target.workspace_id
                and target.team_id in observed.project_team_ids
                and target.project_id in observed.issue_project_ids
            )
        if not exact_identity or not exact_relationships:
            return "linear_target_mismatch"
        archived = (
            observed.workspace_archived
            or observed.team_archived
            or observed.issue_archived
            or (target.project_id is not None and observed.project_archived)
        )
        if archived:
            return "linear_target_archived"
        return None

    @staticmethod
    def _validate_comment(
        event: LinearAcceptedResultEvent,
        comment: LinearCommentObservation,
        expected_author_app_id: str,
    ) -> str | None:
        if comment.author_app_id != expected_author_app_id:
            return "linear_comment_author_mismatch"
        if comment.issue_id != event.target.issue_id:
            return "linear_comment_target_mismatch"
        if not _LINEAR_UUID.fullmatch(comment.comment_id):
            return "linear_comment_identity_invalid"
        if not _LINEAR_UUID.fullmatch(comment.thread_id):
            return "linear_comment_identity_invalid"
        try:
            payload = strip_linear_transport_envelope(comment.body, event.marker)
        except ValueError:
            return "linear_comment_marker_mismatch"
        digest = f"sha256:{hashlib.sha256(payload).hexdigest()}"
        if payload != event.renderer_payload or digest != event.payload_digest:
            return "linear_comment_payload_mismatch"
        return None

    @staticmethod
    def _dead_letter(
        event: LinearAcceptedResultEvent,
        code: str,
    ) -> LinearDeliveryOutcome:
        return LinearDeliveryOutcome(
            state="dead_letter",
            event_id=event.event_id,
            error_code=code,
        )

    @staticmethod
    def _retryable(
        event: LinearAcceptedResultEvent,
        stage: Literal["preflight", "observe", "create"],
    ) -> LinearDeliveryOutcome:
        return LinearDeliveryOutcome(
            state="retryable",
            event_id=event.event_id,
            error_code="linear_delivery_unavailable",
            retry_stage=stage,
        )

    @staticmethod
    def _delivered(
        event: LinearAcceptedResultEvent,
        comment: LinearCommentObservation,
    ) -> LinearDeliveryOutcome:
        receipt_digest = canonical_digest(
            {
                "kind": "linear.projection-receipt.v1",
                "event_id": event.event_id,
                "comment_id": comment.comment_id,
                "author_app_id": comment.author_app_id,
                "payload_digest": event.payload_digest,
                "marker": event.marker,
                "accepted_merge_commit": event.accepted_merge_commit,
                "ci_subject_head": event.ci_subject_head,
                "ci_attestation_id": event.ci_attestation_id,
                "workflow_run_id": event.workflow_run_id,
                "check_run_id": event.check_run_id,
                "artifact_id": event.artifact_id,
            }
        )
        source_marker = SessionNotificationSourceMarker(
            agent_id=event.agent_id,
            task_id=event.task_id,
            session_id=event.session_id,
            report_id=event.report_id,
            marker_digest=canonical_digest(
                {
                    "marker": event.marker,
                    "agent_id": event.agent_id,
                    "task_id": event.task_id,
                    "session_id": event.session_id,
                    "report_id": event.report_id,
                }
            ),
        )
        receipt = LinearProjectionReceipt(
            receipt_id=f"linear-receipt-{receipt_digest.removeprefix('sha256:')}",
            event_id=event.event_id,
            project_id=event.project_id,
            task_id=event.task_id,
            session_id=event.session_id,
            submission_id=event.submission_id,
            report_id=event.report_id,
            report_revision=event.report_revision,
            accepted_merge_commit=event.accepted_merge_commit,
            ci_subject_head=event.ci_subject_head,
            ci_attestation_id=event.ci_attestation_id,
            workflow_id=event.workflow_id,
            check_identity=event.check_identity,
            task_digest=event.task_digest,
            submission_digest=event.submission_digest,
            decision_digest=event.decision_digest,
            report_digest=event.report_digest,
            target=event.target,
            comment_id=comment.comment_id,
            thread_id=comment.thread_id,
            renderer_id=event.renderer_id,
            renderer_version=event.renderer_version,
            payload_digest=event.payload_digest,
            transport_digest=(
                f"sha256:{hashlib.sha256(event.transport_body).hexdigest()}"
            ),
            marker=event.marker,
            source_marker=source_marker,
            workflow_run_id=event.workflow_run_id,
            check_run_id=event.check_run_id,
            artifact_id=event.artifact_id,
        )
        return LinearDeliveryOutcome(
            state="delivered",
            event_id=event.event_id,
            receipt=receipt,
        )
