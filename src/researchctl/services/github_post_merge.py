from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Protocol

from researchctl.constants import __version__
from researchctl.errors import RCPError
from researchctl.services.actor import ActorContext
from researchctl.services.ci_dispatch import (
    CI_DISPATCHER_ID,
    load_ci_dispatch_artifact,
)
from researchctl.services.ci_validation import CI_CHECK_IDENTITY, CI_WORKFLOW_ID
from researchctl.services.post_merge import PostMergeRequest, PostMergeResult

GITHUB_WORKFLOW_PATH = ".github/workflows/research-validate-pr.yml"
GITHUB_WORKFLOW_EVENT = "pull_request_target"
GITHUB_ARTIFACT_MEMBER = "researchctl-ci-attestation.yaml"
_MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
_GIT_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_GITHUB_RESOURCE_ID = re.compile(r"^[1-9][0-9]*$")
_REPOSITORY = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,99})/"
    r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,99})$"
)


def github_artifact_name(pull_request_number: int, subject_head: str) -> str:
    return f"researchctl-ci-{pull_request_number}-{subject_head}"


@dataclass(frozen=True, slots=True)
class AuthenticatedGitHubPostMergeObservation:
    repository: str
    pull_request_number: int
    merged: bool
    base_ref: str
    base_sha: str
    subject_head: str
    merge_commit: str
    workflow_id: str
    workflow_path: str
    workflow_event: str
    workflow_run_id: str
    workflow_status: str
    workflow_conclusion: str
    check_identity: str
    check_run_id: str
    check_status: str
    check_conclusion: str
    artifact_id: str
    artifact_name: str
    artifact_expired: bool
    artifact_bytes: bytes
    artifact_digest: str


class GitHubPostMergeObservationPort(Protocol):
    def observe(
        self,
        *,
        repository: str,
        pull_request_number: int,
    ) -> AuthenticatedGitHubPostMergeObservation: ...


class PostMergeApplicationPort(Protocol):
    def post_merge_process(
        self,
        *,
        request: PostMergeRequest,
        dispatch_artifact: bytes,
        actor: ActorContext,
    ) -> PostMergeResult: ...


class AuthenticatedGitHubPostMergeBridge:
    """Turn authenticated GitHub observations into one trusted outbox enqueue."""

    def __init__(
        self,
        *,
        github: GitHubPostMergeObservationPort,
        application: PostMergeApplicationPort,
        actor: ActorContext,
    ) -> None:
        self._github = github
        self._application = application
        self._actor = actor

    def enqueue(
        self,
        *,
        repository: str,
        pull_request_number: int,
    ) -> PostMergeResult:
        _require_request(repository, pull_request_number)
        observation = self._github.observe(
            repository=repository,
            pull_request_number=pull_request_number,
        )
        _require_observation(
            observation,
            repository=repository,
            pull_request_number=pull_request_number,
        )
        artifact = load_ci_dispatch_artifact(observation.artifact_bytes)
        _require_artifact_binding(observation, artifact)

        request = PostMergeRequest(
            mode="enqueue",
            provenance="github_authenticated",
            merge_commit=observation.merge_commit,
            artifact_digest=observation.artifact_digest,
            repository=observation.repository,
            pull_request_number=observation.pull_request_number,
            base_ref=observation.base_ref,
            base_commit=observation.base_sha,
            subject_head=observation.subject_head,
            workflow_run_id=observation.workflow_run_id,
            check_run_id=observation.check_run_id,
            artifact_id=observation.artifact_id,
        )
        return self._application.post_merge_process(
            request=request,
            dispatch_artifact=observation.artifact_bytes,
            actor=self._actor,
        )


def _require_request(repository: str, pull_request_number: int) -> None:
    if not isinstance(repository, str) or _REPOSITORY.fullmatch(repository) is None:
        _invalid("github_post_merge_request_invalid", "Repository must be owner/name.")
    if (
        isinstance(pull_request_number, bool)
        or not isinstance(pull_request_number, int)
        or pull_request_number < 1
    ):
        _invalid(
            "github_post_merge_request_invalid",
            "Pull request number must be a positive integer.",
        )


def _require_observation(
    observation: AuthenticatedGitHubPostMergeObservation,
    *,
    repository: str,
    pull_request_number: int,
) -> None:
    if (
        observation.repository != repository
        or observation.pull_request_number != pull_request_number
    ):
        _invalid(
            "github_post_merge_identity_mismatch",
            "GitHub observation does not match the requested repository and PR.",
        )
    if observation.merged is not True:
        _invalid(
            "github_post_merge_not_merged",
            "GitHub does not report this pull request as merged.",
        )
    if not observation.base_ref.strip() or not all(
        _GIT_OBJECT_ID.fullmatch(value)
        for value in (
            observation.base_sha,
            observation.subject_head,
            observation.merge_commit,
        )
    ):
        _invalid(
            "github_post_merge_lineage_invalid",
            "GitHub returned invalid base, head, or merge lineage.",
        )
    workflow_valid = (
        observation.workflow_id == CI_WORKFLOW_ID
        and observation.workflow_path == GITHUB_WORKFLOW_PATH
        and observation.workflow_event == GITHUB_WORKFLOW_EVENT
        and observation.workflow_status == "completed"
        and observation.workflow_conclusion == "success"
        and _GITHUB_RESOURCE_ID.fullmatch(observation.workflow_run_id) is not None
    )
    if not workflow_valid:
        _invalid(
            "github_post_merge_workflow_invalid",
            "The fixed GitHub validation workflow did not complete successfully.",
        )
    check_valid = (
        observation.check_identity == CI_CHECK_IDENTITY
        and observation.check_status == "completed"
        and observation.check_conclusion == "success"
        and _GITHUB_RESOURCE_ID.fullmatch(observation.check_run_id) is not None
    )
    if not check_valid:
        _invalid(
            "github_post_merge_check_invalid",
            "The fixed GitHub validation check did not complete successfully.",
        )
    expected_name = github_artifact_name(pull_request_number, observation.subject_head)
    observed_digest = _sha256(observation.artifact_bytes)
    artifact_valid = (
        _GITHUB_RESOURCE_ID.fullmatch(observation.artifact_id) is not None
        and observation.artifact_name == expected_name
        and observation.artifact_expired is False
        and 0 < len(observation.artifact_bytes) <= _MAX_ARTIFACT_BYTES
        and observation.artifact_digest == observed_digest
    )
    if not artifact_valid:
        _invalid(
            "github_post_merge_artifact_invalid",
            "GitHub artifact identity, bytes, or digest are invalid.",
        )


def _require_artifact_binding(observation, artifact) -> None:
    nested = artifact.submission_attestation
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
        and artifact.repository == observation.repository
        and artifact.pull_request_number == observation.pull_request_number
        and artifact.base_ref == observation.base_ref
        and artifact.base_commit == observation.base_sha
        and artifact.subject_head == observation.subject_head
    )
    if not valid:
        _invalid(
            "github_post_merge_artifact_binding_invalid",
            "The downloaded artifact does not bind the authenticated GitHub facts.",
        )


def _sha256(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _invalid(code: str, message: str) -> None:
    raise RCPError(code=code, message=message)
