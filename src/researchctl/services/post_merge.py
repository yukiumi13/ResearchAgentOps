from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, Never

from pydantic import StrictInt, model_validator

from researchctl.constants import __version__
from researchctl.domain.models import StrictModel
from researchctl.domain.types import (
    GitObjectId,
    NonEmptyStr,
    Sha256Digest,
    ShortText,
)
from researchctl.errors import RCPError
from researchctl.runtime import RuntimeStore
from researchctl.serialization import canonical_json_bytes
from researchctl.services.actor import ActorContext, ActorRole
from researchctl.services.ci_dispatch import (
    CI_DISPATCHER_ID,
    load_ci_dispatch_artifact,
)
from researchctl.services.ci_validation import (
    CI_CHECK_IDENTITY,
    CI_WORKFLOW_ID,
)
from researchctl.services.linear_delivery import (
    LinearAcceptedResultDeliveryService,
    LinearAcceptedResultEvent,
    linear_event_payload,
)


PostMergeMode = Literal["shadow", "enqueue"]
PostMergeProvenance = Literal["local_shadow", "github_authenticated"]
PostMergeState = Literal[
    "shadow_validated",
    "queued",
    "already_queued",
    "disabled",
]
_MAX_POST_MERGE_ARTIFACT_BYTES = 8 * 1024 * 1024
_GITHUB_RESOURCE_ID = re.compile(r"^[1-9][0-9]*$")


class PostMergeRequest(StrictModel):
    mode: PostMergeMode
    provenance: PostMergeProvenance
    merge_commit: GitObjectId
    artifact_digest: Sha256Digest
    repository: ShortText
    pull_request_number: StrictInt
    base_ref: ShortText
    base_commit: GitObjectId
    subject_head: GitObjectId
    workflow_run_id: NonEmptyStr | None = None
    check_run_id: NonEmptyStr | None = None
    artifact_id: NonEmptyStr | None = None

    @model_validator(mode="after")
    def require_authenticated_enqueue(self) -> PostMergeRequest:
        github_ids = (
            self.workflow_run_id,
            self.check_run_id,
            self.artifact_id,
        )
        if self.provenance == "github_authenticated" and not all(github_ids):
            raise ValueError("authenticated GitHub provenance requires run/check/artifact IDs")
        if self.provenance == "github_authenticated" and not all(
            _GITHUB_RESOURCE_ID.fullmatch(value)
            for value in github_ids
            if value is not None
        ):
            raise ValueError("authenticated GitHub provenance IDs must be positive integers")
        if self.provenance == "local_shadow" and any(github_ids):
            raise ValueError("local shadow provenance cannot assert GitHub IDs")
        if self.mode == "enqueue" and self.provenance != "github_authenticated":
            raise ValueError("live enqueue requires authenticated GitHub provenance")
        if self.mode == "shadow" and self.provenance != "local_shadow":
            raise ValueError("shadow validation requires local shadow provenance")
        if self.pull_request_number < 1:
            raise ValueError("pull_request_number must be positive")
        return self


@dataclass(frozen=True, slots=True)
class PostMergeResult:
    state: PostMergeState
    request: PostMergeRequest
    event: LinearAcceptedResultEvent | None
    outbox_state: str | None

    def as_dict(self) -> dict[str, object]:
        event = self.event
        return {
            "schema_version": "1",
            "kind": "researchctl.post-merge.v1",
            "mode": self.request.mode,
            "provenance": self.request.provenance,
            "state": self.state,
            "remote_mutation_performed": False,
            "repository": self.request.repository,
            "pull_request_number": self.request.pull_request_number,
            "base_ref": self.request.base_ref,
            "base_commit": self.request.base_commit,
            "subject_head": self.request.subject_head,
            "merge_commit": self.request.merge_commit,
            "artifact_digest": self.request.artifact_digest,
            "workflow_run_id": self.request.workflow_run_id,
            "check_run_id": self.request.check_run_id,
            "artifact_id": self.request.artifact_id,
            "outbox_state": self.outbox_state,
            "event": _event_observation(event) if event is not None else None,
        }


@dataclass(frozen=True, slots=True)
class PostMergeArtifactReceipt:
    path: Path
    content_digest: str
    size_bytes: int
    created: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "content_digest": self.content_digest,
            "size_bytes": self.size_bytes,
            "created": self.created,
        }


class TrustedPostMergeService:
    """Validate an accepted merge before a shadow observation or outbox write."""

    def __init__(
        self,
        *,
        runtime: RuntimeStore,
        accepted: LinearAcceptedResultDeliveryService,
    ) -> None:
        self.runtime = runtime
        self.accepted = accepted

    def process(
        self,
        *,
        request: PostMergeRequest,
        dispatch_artifact: bytes,
        actor: ActorContext,
    ) -> PostMergeResult:
        actor.require_role("post-merge.process", ActorRole.TRUSTED_AUTOMATION)
        observed_digest = _sha256(dispatch_artifact)
        if observed_digest != request.artifact_digest:
            raise RCPError(
                code="post_merge_artifact_digest_mismatch",
                message="Post-merge request does not bind the supplied artifact bytes.",
            )
        artifact = load_ci_dispatch_artifact(dispatch_artifact)
        self._require_artifact_binding(request, artifact)
        ci = artifact.submission_attestation
        assert ci is not None
        event = self.accepted.enqueue(
            actor=actor,
            project_id=ci.project_id,
            merge_commit=request.merge_commit,
            ci=ci,
        )
        if event is None:
            return PostMergeResult(
                state="disabled",
                request=request,
                event=None,
                outbox_state=None,
            )
        if request.provenance == "github_authenticated":
            assert request.workflow_run_id is not None
            assert request.check_run_id is not None
            assert request.artifact_id is not None
            event = replace(
                event,
                workflow_run_id=request.workflow_run_id,
                check_run_id=request.check_run_id,
                artifact_id=request.artifact_id,
            )
        if request.mode == "shadow":
            return PostMergeResult(
                state="shadow_validated",
                request=request,
                event=event,
                outbox_state=None,
            )

        existing = self.runtime.get_linear_projection_outbox(event.event_id)
        stored = self.runtime.enqueue_linear_projection(
            project_id=event.project_id,
            event_id=event.event_id,
            aggregate_id=f"{event.report_id}:{event.report_revision}",
            payload=linear_event_payload(event),
            created_at=ci.generated_at,
        )
        return PostMergeResult(
            state="already_queued" if existing is not None else "queued",
            request=request,
            event=event,
            outbox_state=stored.state,
        )

    @staticmethod
    def _require_artifact_binding(request, artifact) -> None:
        nested = artifact.submission_attestation
        accepted_fields = (
            nested.decision_digest if nested is not None else None,
            nested.report_id if nested is not None else None,
            nested.report_revision if nested is not None else None,
            nested.report_digest if nested is not None else None,
        )
        valid = (
            artifact.dispatcher_id == CI_DISPATCHER_ID
            and artifact.dispatcher_version == __version__
            and artifact.workflow_id == CI_WORKFLOW_ID
            and artifact.check_identity == CI_CHECK_IDENTITY
            and artifact.pr_type == "submission"
            and artifact.applicability == "validated"
            and artifact.overall_result == "passed"
            and nested is not None
            and nested.validator_version == __version__
            and nested.overall_result == "passed"
            and all(value is not None for value in accepted_fields)
            and artifact.repository == request.repository
            and artifact.pull_request_number == request.pull_request_number
            and artifact.base_ref == request.base_ref
            and artifact.base_commit == request.base_commit
            and artifact.subject_head == request.subject_head
        )
        if not valid:
            raise RCPError(
                code="post_merge_artifact_binding_invalid",
                message=(
                    "Post-merge observation does not match one passing accepted "
                    "Submission artifact from the fixed required check."
                ),
            )


def post_merge_request_from_artifact(
    *,
    dispatch_artifact: bytes,
    merge_commit: str,
    mode: PostMergeMode = "shadow",
    provenance: PostMergeProvenance = "local_shadow",
    workflow_run_id: str | None = None,
    check_run_id: str | None = None,
    artifact_id: str | None = None,
) -> PostMergeRequest:
    artifact = load_ci_dispatch_artifact(dispatch_artifact)
    request = PostMergeRequest(
        mode=mode,
        provenance=provenance,
        merge_commit=merge_commit,
        artifact_digest=_sha256(dispatch_artifact),
        repository=artifact.repository,
        pull_request_number=artifact.pull_request_number,
        base_ref=artifact.base_ref,
        base_commit=artifact.base_commit,
        subject_head=artifact.subject_head,
        workflow_run_id=workflow_run_id,
        check_run_id=check_run_id,
        artifact_id=artifact_id,
    )
    if request.mode != "shadow" or request.provenance != "local_shadow":
        raise ValueError(
            "artifact-derived requests are shadow-only; authenticated enqueue "
            "must use the GitHub post-merge bridge"
        )
    return request


def write_post_merge_artifact(
    result: PostMergeResult,
    path: Path,
) -> PostMergeArtifactReceipt:
    content = canonical_json_bytes(result.as_dict()) + b"\n"
    if len(content) > _MAX_POST_MERGE_ARTIFACT_BYTES:
        raise RCPError(
            code="post_merge_output_too_large",
            message="Post-merge observation exceeds the output size limit.",
        )
    destination = Path(os.path.abspath(os.fspath(path)))
    parent = destination.parent
    try:
        resolved_parent = parent.resolve(strict=True)
    except OSError as error:
        raise RCPError(
            code="post_merge_output_path_invalid",
            message="Post-merge output parent does not exist.",
        ) from error
    if parent.is_symlink() or resolved_parent != parent or not parent.is_dir():
        raise RCPError(
            code="post_merge_output_path_invalid",
            message="Post-merge output parent must be a non-symlink directory.",
        )
    if _identical_file(destination, content):
        return _artifact_receipt(destination, content, created=False)
    if destination.exists() or destination.is_symlink():
        _output_conflict(destination)

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.researchctl-",
        dir=parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, destination, follow_symlinks=False)
        except FileExistsError:
            if not _identical_file(destination, content):
                _output_conflict(destination)
            created = False
        else:
            created = True
            directory = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        return _artifact_receipt(destination, content, created=created)
    finally:
        temporary.unlink(missing_ok=True)


def _event_observation(event: LinearAcceptedResultEvent) -> dict[str, object]:
    return {
        "event_id": event.event_id,
        "project_id": event.project_id,
        "task_id": event.task_id,
        "session_id": event.session_id,
        "agent_id": event.agent_id,
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
        "record_digests": {
            "task": event.task_digest,
            "submission": event.submission_digest,
            "decision": event.decision_digest,
            "report": event.report_digest,
        },
        "target": {
            "workspace_id": event.target.workspace_id,
            "team_id": event.target.team_id,
            "project_id": event.target.project_id,
            "issue_id": event.target.issue_id,
        },
        "renderer_id": event.renderer_id,
        "renderer_version": event.renderer_version,
        "payload_digest": event.payload_digest,
        "renderer_payload": event.renderer_payload.decode("utf-8"),
        "marker": event.marker,
        "transport_digest": _sha256(event.transport_body),
    }


def _sha256(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _identical_file(path: Path, expected: bytes) -> bool:
    return path.is_file() and not path.is_symlink() and path.read_bytes() == expected


def _artifact_receipt(
    path: Path,
    content: bytes,
    *,
    created: bool,
) -> PostMergeArtifactReceipt:
    return PostMergeArtifactReceipt(
        path=path,
        content_digest=_sha256(content),
        size_bytes=len(content),
        created=created,
    )


def _output_conflict(path: Path) -> Never:
    raise RCPError(
        code="post_merge_output_conflict",
        message="Post-merge output path already contains different content.",
        context={"path": str(path)},
    )
