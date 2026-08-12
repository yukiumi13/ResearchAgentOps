from __future__ import annotations

import hashlib
import io
import json
import zipfile
from dataclasses import replace

import pytest

from researchctl.adapters.github_post_merge import (
    GhApiCommandResult,
    GhApiPostMergeClient,
)
from researchctl.errors import RCPError
from researchctl.services.ci_validation import CI_CHECK_IDENTITY, CI_WORKFLOW_ID
from researchctl.services.github_post_merge import (
    GITHUB_ARTIFACT_MEMBER,
    GITHUB_WORKFLOW_EVENT,
    GITHUB_WORKFLOW_PATH,
    AuthenticatedGitHubPostMergeBridge,
    AuthenticatedGitHubPostMergeObservation,
    github_artifact_name,
)

REPOSITORY = "owner/repository"
PULL_REQUEST = 17
BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40
MERGE_SHA = "c" * 40
WORKFLOW_RUN_ID = 101
CHECK_SUITE_ID = 202
CHECK_RUN_ID = 303
ARTIFACT_ID = 404
ARTIFACT_BYTES = b"strict dispatch artifact\n"


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


def _zip_artifact(
    content: bytes = ARTIFACT_BYTES,
    *,
    member: str = GITHUB_ARTIFACT_MEMBER,
) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, mode="w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(member, content)
    return stream.getvalue()


def _responses(*, archive: bytes | None = None) -> dict[str, bytes]:
    artifact_archive = archive or _zip_artifact()
    artifact_name = github_artifact_name(PULL_REQUEST, HEAD_SHA)
    return {
        f"/repos/{REPOSITORY}/pulls/{PULL_REQUEST}": _json_bytes(
            {
                "number": PULL_REQUEST,
                "merged": True,
                "base": {
                    "ref": "main",
                    "sha": BASE_SHA,
                    "repo": {"full_name": REPOSITORY},
                },
                "head": {
                    "ref": "research/submission/example",
                    "sha": HEAD_SHA,
                    "repo": {"full_name": "contributor/fork"},
                },
                "merge_commit_sha": MERGE_SHA,
            }
        ),
        (
            f"/repos/{REPOSITORY}/actions/workflows/{CI_WORKFLOW_ID}.yml/runs"
            f"?event={GITHUB_WORKFLOW_EVENT}&status=completed&per_page=100"
        ): _json_bytes(
            {
                "workflow_runs": [
                    {
                        "id": WORKFLOW_RUN_ID,
                        "name": CI_WORKFLOW_ID,
                        "path": GITHUB_WORKFLOW_PATH,
                        "event": GITHUB_WORKFLOW_EVENT,
                        "repository": {"full_name": REPOSITORY},
                        "status": "completed",
                        "conclusion": "success",
                        "head_sha": HEAD_SHA,
                        "check_suite_id": CHECK_SUITE_ID,
                        "pull_requests": [
                            {
                                "number": PULL_REQUEST,
                                "head": {"sha": HEAD_SHA},
                                "base": {
                                    "ref": "main",
                                    "sha": BASE_SHA,
                                    "repo": {"full_name": REPOSITORY},
                                },
                            }
                        ],
                    }
                ]
            }
        ),
        (
            f"/repos/{REPOSITORY}/actions/runs/{WORKFLOW_RUN_ID}/jobs"
            "?filter=latest&per_page=100"
        ): _json_bytes(
            {
                "jobs": [
                    {
                        "name": CI_CHECK_IDENTITY,
                        "run_id": WORKFLOW_RUN_ID,
                        "head_sha": HEAD_SHA,
                        "workflow_name": CI_WORKFLOW_ID,
                        "status": "completed",
                        "conclusion": "success",
                        "check_run_url": (
                            f"https://api.github.com/repos/{REPOSITORY}/"
                            f"check-runs/{CHECK_RUN_ID}"
                        ),
                    }
                ]
            }
        ),
        f"/repos/{REPOSITORY}/check-runs/{CHECK_RUN_ID}": _json_bytes(
            {
                "id": CHECK_RUN_ID,
                "name": CI_CHECK_IDENTITY,
                "status": "completed",
                "conclusion": "success",
                "head_sha": HEAD_SHA,
                "check_suite": {"id": CHECK_SUITE_ID},
                "app": {"slug": "github-actions"},
            }
        ),
        (
            f"/repos/{REPOSITORY}/actions/runs/{WORKFLOW_RUN_ID}/artifacts"
            "?per_page=100"
        ): _json_bytes(
            {
                "artifacts": [
                    {
                        "id": ARTIFACT_ID,
                        "name": artifact_name,
                        "expired": False,
                        "size_in_bytes": len(artifact_archive),
                        "workflow_run": {
                            "id": WORKFLOW_RUN_ID,
                            "head_sha": HEAD_SHA,
                        },
                    }
                ]
            }
        ),
        f"/repos/{REPOSITORY}/actions/artifacts/{ARTIFACT_ID}/zip": artifact_archive,
    }


class RecordingRunner:
    def __init__(
        self,
        responses: dict[str, bytes],
        *,
        returncode: int = 0,
    ) -> None:
        self.responses = responses
        self.returncode = returncode
        self.calls: list[tuple[tuple[str, ...], dict[str, str], float]] = []

    def run(self, argv, *, env, timeout_seconds):
        self.calls.append((argv, dict(env), timeout_seconds))
        return GhApiCommandResult(
            returncode=self.returncode,
            stdout=self.responses[argv[-1]],
        )


def test_gh_api_client_binds_exact_pr_workflow_check_and_artifact() -> None:
    runner = RecordingRunner(_responses())
    client = GhApiPostMergeClient(
        runner=runner,
        environment={
            "PATH": "/usr/bin",
            "GH_TOKEN": "not-logged",
            "UNRELATED_SECRET": "must-not-be-forwarded",
        },
        timeout_seconds=7,
    )

    observation = client.observe(
        repository=REPOSITORY,
        pull_request_number=PULL_REQUEST,
    )

    assert observation.repository == REPOSITORY
    assert observation.pull_request_number == PULL_REQUEST
    assert observation.base_sha == BASE_SHA
    assert observation.subject_head == HEAD_SHA
    assert observation.merge_commit == MERGE_SHA
    assert observation.workflow_run_id == str(WORKFLOW_RUN_ID)
    assert observation.check_run_id == str(CHECK_RUN_ID)
    assert observation.artifact_id == str(ARTIFACT_ID)
    assert observation.artifact_bytes == ARTIFACT_BYTES
    assert observation.artifact_digest == (
        "sha256:" + hashlib.sha256(ARTIFACT_BYTES).hexdigest()
    )
    assert len(runner.calls) == 6
    for argv, environment, timeout in runner.calls:
        assert argv[:3] == ("gh", "api", "--method")
        assert argv[3] == "GET"
        assert environment == {"PATH": "/usr/bin", "GH_TOKEN": "not-logged"}
        assert timeout == 7


def test_gh_api_client_rejects_duplicate_json_and_unsafe_artifact_member() -> None:
    duplicate = _responses()
    duplicate[f"/repos/{REPOSITORY}/pulls/{PULL_REQUEST}"] = (
        b'{"number":17,"number":17}'
    )
    with pytest.raises(RCPError) as duplicate_error:
        GhApiPostMergeClient(runner=RecordingRunner(duplicate)).observe(
            repository=REPOSITORY,
            pull_request_number=PULL_REQUEST,
        )
    assert duplicate_error.value.code == "github_api_response_invalid"

    unsafe = _responses(archive=_zip_artifact(member="../attestation.yaml"))
    with pytest.raises(RCPError) as unsafe_error:
        GhApiPostMergeClient(runner=RecordingRunner(unsafe)).observe(
            repository=REPOSITORY,
            pull_request_number=PULL_REQUEST,
        )
    assert unsafe_error.value.code == "github_post_merge_artifact_invalid"


def test_gh_api_failure_does_not_expose_response_content() -> None:
    responses = _responses()
    first = f"/repos/{REPOSITORY}/pulls/{PULL_REQUEST}"
    responses[first] = b"credential-shaped remote diagnostic"
    runner = RecordingRunner(responses, returncode=1)

    with pytest.raises(RCPError) as caught:
        GhApiPostMergeClient(runner=runner).observe(
            repository=REPOSITORY,
            pull_request_number=PULL_REQUEST,
        )

    assert caught.value.code == "github_api_request_failed"
    assert "credential-shaped" not in str(caught.value.context)
    assert caught.value.context == {"operation": "pull_request", "returncode": 1}


def _observation() -> AuthenticatedGitHubPostMergeObservation:
    return AuthenticatedGitHubPostMergeObservation(
        repository=REPOSITORY,
        pull_request_number=PULL_REQUEST,
        merged=True,
        base_ref="main",
        base_sha=BASE_SHA,
        subject_head=HEAD_SHA,
        merge_commit=MERGE_SHA,
        workflow_id=CI_WORKFLOW_ID,
        workflow_path=GITHUB_WORKFLOW_PATH,
        workflow_event=GITHUB_WORKFLOW_EVENT,
        workflow_run_id=str(WORKFLOW_RUN_ID),
        workflow_status="completed",
        workflow_conclusion="success",
        check_identity=CI_CHECK_IDENTITY,
        check_run_id=str(CHECK_RUN_ID),
        check_status="completed",
        check_conclusion="success",
        artifact_id=str(ARTIFACT_ID),
        artifact_name=github_artifact_name(PULL_REQUEST, HEAD_SHA),
        artifact_expired=False,
        artifact_bytes=ARTIFACT_BYTES,
        artifact_digest="sha256:" + hashlib.sha256(ARTIFACT_BYTES).hexdigest(),
    )


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"merged": False}, "github_post_merge_not_merged"),
        ({"workflow_conclusion": "failure"}, "github_post_merge_workflow_invalid"),
        ({"artifact_digest": "sha256:" + "0" * 64}, "github_post_merge_artifact_invalid"),
    ],
)
def test_bridge_rejects_untrusted_observation_before_application_call(
    change: dict[str, object],
    code: str,
) -> None:
    observation = replace(_observation(), **change)

    class GitHub:
        def observe(self, *, repository: str, pull_request_number: int):
            return observation

    class Application:
        def __init__(self) -> None:
            self.calls: list[object] = []

        def post_merge_process(self, **kwargs):
            self.calls.append(kwargs)
            raise AssertionError("application must not be called")

    application = Application()
    bridge = AuthenticatedGitHubPostMergeBridge(
        github=GitHub(),
        application=application,
        actor=object(),
    )

    with pytest.raises(RCPError) as caught:
        bridge.enqueue(repository=REPOSITORY, pull_request_number=PULL_REQUEST)

    assert caught.value.code == code
    assert application.calls == []
