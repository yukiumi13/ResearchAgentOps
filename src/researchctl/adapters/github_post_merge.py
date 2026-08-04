from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import subprocess
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

from researchctl.errors import RCPError
from researchctl.services.ci_validation import CI_CHECK_IDENTITY, CI_WORKFLOW_ID
from researchctl.services.github_post_merge import (
    GITHUB_ARTIFACT_MEMBER,
    GITHUB_WORKFLOW_EVENT,
    GITHUB_WORKFLOW_PATH,
    AuthenticatedGitHubPostMergeObservation,
    github_artifact_name,
)


_API_VERSION = "2022-11-28"
_GITHUB_ACTIONS_APP = "github-actions"
_MAX_JSON_BYTES = 2 * 1024 * 1024
_MAX_ARCHIVE_BYTES = 10 * 1024 * 1024
_MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
_GIT_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_REPOSITORY = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,99})/"
    r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,99})$"
)
_ALLOWED_ENVIRONMENT = frozenset(
    {
        "PATH",
        "HOME",
        "GH_CONFIG_DIR",
        "GH_HOST",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GH_ENTERPRISE_TOKEN",
        "GITHUB_ENTERPRISE_TOKEN",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
    }
)


@dataclass(frozen=True, slots=True)
class GhApiCommandResult:
    returncode: int
    stdout: bytes


class GhApiCommandRunner(Protocol):
    def run(
        self,
        argv: tuple[str, ...],
        *,
        env: Mapping[str, str],
        timeout_seconds: float,
    ) -> GhApiCommandResult: ...


class SubprocessGhApiCommandRunner:
    def run(
        self,
        argv: tuple[str, ...],
        *,
        env: Mapping[str, str],
        timeout_seconds: float,
    ) -> GhApiCommandResult:
        completed = subprocess.run(
            argv,
            check=False,
            env=dict(env),
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout_seconds,
        )
        return GhApiCommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
        )


class GhApiPostMergeClient:
    """Observe one merged PR using only authenticated, bounded ``gh api`` calls."""

    def __init__(
        self,
        *,
        runner: GhApiCommandRunner | None = None,
        environment: Mapping[str, str] | None = None,
        timeout_seconds: float = 20.0,
        max_json_bytes: int = _MAX_JSON_BYTES,
        max_archive_bytes: int = _MAX_ARCHIVE_BYTES,
        max_artifact_bytes: int = _MAX_ARTIFACT_BYTES,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if min(max_json_bytes, max_archive_bytes, max_artifact_bytes) < 1:
            raise ValueError("GitHub response limits must be positive")
        source = os.environ if environment is None else environment
        self._environment = {
            key: value
            for key, value in source.items()
            if key in _ALLOWED_ENVIRONMENT and value
        }
        self._runner = runner or SubprocessGhApiCommandRunner()
        self._timeout_seconds = timeout_seconds
        self._max_json_bytes = max_json_bytes
        self._max_archive_bytes = max_archive_bytes
        self._max_artifact_bytes = max_artifact_bytes

    def observe(
        self,
        *,
        repository: str,
        pull_request_number: int,
    ) -> AuthenticatedGitHubPostMergeObservation:
        _require_request(repository, pull_request_number)
        pull = self._json_object(
            f"/repos/{repository}/pulls/{pull_request_number}",
            operation="pull_request",
        )
        pull_number = _positive_int(pull.get("number"), "pull request number")
        merged = _boolean(pull.get("merged"), "pull request merged state")
        if pull_number != pull_request_number:
            _invalid("github_api_identity_mismatch", "GitHub returned a different PR.")
        if not merged:
            _invalid(
                "github_post_merge_not_merged",
                "GitHub does not report this pull request as merged.",
            )
        base = _object(pull.get("base"), "pull request base")
        head = _object(pull.get("head"), "pull request head")
        base_repository = _repository_name(base, "pull request base")
        if base_repository != repository:
            _invalid(
                "github_api_identity_mismatch",
                "Pull request base repository does not match the requested repository.",
            )
        base_ref = _nonempty_string(base.get("ref"), "pull request base ref")
        base_sha = _git_object_id(base.get("sha"), "pull request base SHA")
        subject_head = _git_object_id(head.get("sha"), "pull request head SHA")
        merge_commit = _git_object_id(
            pull.get("merge_commit_sha"),
            "pull request merge commit",
        )

        runs = self._json_object(
            (
                f"/repos/{repository}/actions/workflows/{CI_WORKFLOW_ID}.yml/runs"
                f"?event={GITHUB_WORKFLOW_EVENT}&status=completed&per_page=100"
            ),
            operation="workflow_runs",
        )
        run = _select_workflow_run(
            runs,
            repository=repository,
            pull_request_number=pull_request_number,
            base_ref=base_ref,
            base_sha=base_sha,
            subject_head=subject_head,
        )
        workflow_run_id = _positive_id(run.get("id"), "workflow run ID")
        workflow_status = _nonempty_string(run.get("status"), "workflow status")
        workflow_conclusion = _nonempty_string(
            run.get("conclusion"),
            "workflow conclusion",
        )
        if workflow_status != "completed" or workflow_conclusion != "success":
            _invalid(
                "github_post_merge_workflow_invalid",
                "The exact validation workflow did not complete successfully.",
            )
        run_head_sha = _git_object_id(run.get("head_sha"), "workflow run head SHA")
        check_suite_id = _positive_int(
            run.get("check_suite_id"),
            "workflow check suite ID",
        )

        jobs = self._json_object(
            (
                f"/repos/{repository}/actions/runs/{workflow_run_id}/jobs"
                "?filter=latest&per_page=100"
            ),
            operation="workflow_jobs",
        )
        job = _select_exact_job(jobs)
        if _positive_id(job.get("run_id"), "job workflow run ID") != workflow_run_id:
            _invalid("github_api_lineage_mismatch", "Job belongs to a different run.")
        if _git_object_id(job.get("head_sha"), "job head SHA") != run_head_sha:
            _invalid("github_api_lineage_mismatch", "Job head differs from its run.")
        if _nonempty_string(job.get("workflow_name"), "job workflow name") != CI_WORKFLOW_ID:
            _invalid("github_api_identity_mismatch", "Job workflow identity is invalid.")
        job_status = _nonempty_string(job.get("status"), "job status")
        job_conclusion = _nonempty_string(job.get("conclusion"), "job conclusion")
        if job_status != "completed" or job_conclusion != "success":
            _invalid(
                "github_post_merge_check_invalid",
                "The exact validation job did not complete successfully.",
            )
        check_run_id = _check_run_id(
            job.get("check_run_url"),
            repository=repository,
        )

        check = self._json_object(
            f"/repos/{repository}/check-runs/{check_run_id}",
            operation="check_run",
        )
        _require_check_run(
            check,
            check_run_id=check_run_id,
            run_head_sha=run_head_sha,
            check_suite_id=check_suite_id,
        )
        check_status = _nonempty_string(check.get("status"), "check status")
        check_conclusion = _nonempty_string(check.get("conclusion"), "check conclusion")

        artifacts = self._json_object(
            f"/repos/{repository}/actions/runs/{workflow_run_id}/artifacts?per_page=100",
            operation="workflow_artifacts",
        )
        expected_artifact_name = github_artifact_name(
            pull_request_number,
            subject_head,
        )
        artifact = _select_artifact(artifacts, expected_artifact_name)
        artifact_id = _positive_id(artifact.get("id"), "artifact ID")
        artifact_expired = _boolean(artifact.get("expired"), "artifact expired state")
        if artifact_expired:
            _invalid("github_post_merge_artifact_invalid", "GitHub artifact has expired.")
        artifact_size = _positive_int(artifact.get("size_in_bytes"), "artifact size")
        if artifact_size > self._max_archive_bytes:
            _invalid("github_api_response_too_large", "GitHub artifact is too large.")
        _require_artifact_run(artifact, workflow_run_id, run_head_sha)
        archive = self._call(
            f"/repos/{repository}/actions/artifacts/{artifact_id}/zip",
            operation="artifact_download",
            max_bytes=self._max_archive_bytes,
        )
        artifact_bytes = _extract_artifact(
            archive,
            max_artifact_bytes=self._max_artifact_bytes,
        )
        digest = f"sha256:{hashlib.sha256(artifact_bytes).hexdigest()}"
        return AuthenticatedGitHubPostMergeObservation(
            repository=repository,
            pull_request_number=pull_request_number,
            merged=merged,
            base_ref=base_ref,
            base_sha=base_sha,
            subject_head=subject_head,
            merge_commit=merge_commit,
            workflow_id=CI_WORKFLOW_ID,
            workflow_path=GITHUB_WORKFLOW_PATH,
            workflow_event=GITHUB_WORKFLOW_EVENT,
            workflow_run_id=workflow_run_id,
            workflow_status=workflow_status,
            workflow_conclusion=workflow_conclusion,
            check_identity=CI_CHECK_IDENTITY,
            check_run_id=check_run_id,
            check_status=check_status,
            check_conclusion=check_conclusion,
            artifact_id=artifact_id,
            artifact_name=expected_artifact_name,
            artifact_expired=artifact_expired,
            artifact_bytes=artifact_bytes,
            artifact_digest=digest,
        )

    def _json_object(self, endpoint: str, *, operation: str) -> dict[str, object]:
        content = self._call(
            endpoint,
            operation=operation,
            max_bytes=self._max_json_bytes,
        )
        try:
            payload = json.loads(
                content.decode("utf-8", errors="strict"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise RCPError(
                code="github_api_response_invalid",
                message="GitHub API returned invalid strict JSON.",
                context={"operation": operation},
            ) from error
        if not isinstance(payload, dict):
            _invalid(
                "github_api_response_invalid",
                "GitHub API response must be a JSON object.",
                operation=operation,
            )
        return payload

    def _call(self, endpoint: str, *, operation: str, max_bytes: int) -> bytes:
        argv = (
            "gh",
            "api",
            "--method",
            "GET",
            "-H",
            "Accept: application/vnd.github+json",
            "-H",
            f"X-GitHub-Api-Version: {_API_VERSION}",
            endpoint,
        )
        try:
            result = self._runner.run(
                argv,
                env=self._environment,
                timeout_seconds=self._timeout_seconds,
            )
        except FileNotFoundError as error:
            raise RCPError(
                code="github_cli_not_found",
                message="GitHub CLI executable was not found.",
            ) from error
        except subprocess.TimeoutExpired as error:
            raise RCPError(
                code="github_api_timeout",
                message="Authenticated GitHub API observation timed out.",
                context={"operation": operation},
            ) from error
        except OSError as error:
            raise RCPError(
                code="github_api_execution_failed",
                message="GitHub CLI could not be executed.",
                context={
                    "operation": operation,
                    "error_type": type(error).__name__,
                },
            ) from error
        if result.returncode != 0:
            _invalid(
                "github_api_request_failed",
                "Authenticated GitHub API request failed.",
                operation=operation,
                returncode=result.returncode,
            )
        if not isinstance(result.stdout, bytes):
            _invalid(
                "github_api_port_contract_invalid",
                "GitHub command runner returned non-byte output.",
                operation=operation,
            )
        if not result.stdout:
            _invalid(
                "github_api_response_invalid",
                "GitHub API returned an empty response.",
                operation=operation,
            )
        if len(result.stdout) > max_bytes:
            _invalid(
                "github_api_response_too_large",
                "GitHub API response exceeds the configured size limit.",
                operation=operation,
            )
        return result.stdout


def _select_workflow_run(
    payload: dict[str, object],
    *,
    repository: str,
    pull_request_number: int,
    base_ref: str,
    base_sha: str,
    subject_head: str,
) -> dict[str, object]:
    runs = _array(payload.get("workflow_runs"), "workflow runs")
    for value in runs:
        run = _object(value, "workflow run")
        if not _run_binds_pull_request(
            run,
            repository=repository,
            pull_request_number=pull_request_number,
            base_ref=base_ref,
            base_sha=base_sha,
            subject_head=subject_head,
        ):
            continue
        identity = (
            _nonempty_string(run.get("name"), "workflow name") == CI_WORKFLOW_ID
            and _nonempty_string(run.get("path"), "workflow path")
            == GITHUB_WORKFLOW_PATH
            and _nonempty_string(run.get("event"), "workflow event")
            == GITHUB_WORKFLOW_EVENT
            and _repository_name(run, "workflow run") == repository
        )
        if not identity:
            _invalid(
                "github_api_identity_mismatch",
                "Workflow run does not have the fixed validation identity.",
            )
        return run
    _invalid(
        "github_post_merge_workflow_not_found",
        "No completed validation workflow binds the exact pull request lineage.",
    )


def _run_binds_pull_request(
    run: dict[str, object],
    *,
    repository: str,
    pull_request_number: int,
    base_ref: str,
    base_sha: str,
    subject_head: str,
) -> bool:
    pull_requests = _array(run.get("pull_requests"), "workflow pull requests")
    for value in pull_requests:
        pull = _object(value, "workflow pull request")
        head = _object(pull.get("head"), "workflow pull request head")
        base = _object(pull.get("base"), "workflow pull request base")
        if (
            _positive_int(pull.get("number"), "workflow pull request number")
            == pull_request_number
            and _git_object_id(head.get("sha"), "workflow pull request head SHA")
            == subject_head
            and _nonempty_string(base.get("ref"), "workflow pull request base ref")
            == base_ref
            and _git_object_id(base.get("sha"), "workflow pull request base SHA")
            == base_sha
            and _repository_name(base, "workflow pull request base") == repository
        ):
            return True
    return False


def _select_exact_job(payload: dict[str, object]) -> dict[str, object]:
    jobs = [
        _object(value, "workflow job")
        for value in _array(payload.get("jobs"), "workflow jobs")
    ]
    matches = [
        job
        for job in jobs
        if _nonempty_string(job.get("name"), "job name") == CI_CHECK_IDENTITY
    ]
    if len(matches) != 1:
        _invalid(
            "github_post_merge_check_invalid",
            "GitHub must return exactly one fixed validation job.",
        )
    return matches[0]


def _require_check_run(
    check: dict[str, object],
    *,
    check_run_id: str,
    run_head_sha: str,
    check_suite_id: int,
) -> None:
    suite = _object(check.get("check_suite"), "check suite")
    app = _object(check.get("app"), "check application")
    valid = (
        _positive_id(check.get("id"), "check run ID") == check_run_id
        and _nonempty_string(check.get("name"), "check name") == CI_CHECK_IDENTITY
        and _nonempty_string(check.get("status"), "check status") == "completed"
        and _nonempty_string(check.get("conclusion"), "check conclusion") == "success"
        and _git_object_id(check.get("head_sha"), "check head SHA") == run_head_sha
        and _positive_int(suite.get("id"), "check suite ID") == check_suite_id
        and _nonempty_string(app.get("slug"), "check application")
        == _GITHUB_ACTIONS_APP
    )
    if not valid:
        _invalid(
            "github_post_merge_check_invalid",
            "Check run identity, lineage, or conclusion is invalid.",
        )


def _select_artifact(
    payload: dict[str, object],
    expected_name: str,
) -> dict[str, object]:
    artifacts = [
        _object(value, "workflow artifact")
        for value in _array(payload.get("artifacts"), "workflow artifacts")
    ]
    matches = [
        artifact
        for artifact in artifacts
        if _nonempty_string(artifact.get("name"), "artifact name") == expected_name
    ]
    if len(matches) != 1:
        _invalid(
            "github_post_merge_artifact_invalid",
            "GitHub must return exactly one exact-head attestation artifact.",
        )
    return matches[0]


def _require_artifact_run(
    artifact: dict[str, object],
    workflow_run_id: str,
    run_head_sha: str,
) -> None:
    run = _object(artifact.get("workflow_run"), "artifact workflow run")
    if (
        _positive_id(run.get("id"), "artifact workflow run ID") != workflow_run_id
        or _git_object_id(run.get("head_sha"), "artifact workflow head SHA")
        != run_head_sha
    ):
        _invalid(
            "github_api_lineage_mismatch",
            "Artifact belongs to a different workflow run.",
        )


def _extract_artifact(archive: bytes, *, max_artifact_bytes: int) -> bytes:
    try:
        with zipfile.ZipFile(io.BytesIO(archive), mode="r") as bundle:
            entries = bundle.infolist()
            if len(entries) != 1:
                _invalid(
                    "github_post_merge_artifact_invalid",
                    "Attestation archive must contain exactly one file.",
                )
            entry = entries[0]
            file_type = (entry.external_attr >> 16) & 0o170000
            if (
                entry.filename != GITHUB_ARTIFACT_MEMBER
                or entry.is_dir()
                or entry.flag_bits & 0x1
                or file_type not in (0, stat.S_IFREG)
                or entry.file_size < 1
                or entry.file_size > max_artifact_bytes
            ):
                _invalid(
                    "github_post_merge_artifact_invalid",
                    "Attestation archive contains an unsafe or invalid member.",
                )
            content = bundle.read(entry)
    except RCPError:
        raise
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile) as error:
        raise RCPError(
            code="github_post_merge_artifact_invalid",
            message="GitHub artifact is not a valid attestation ZIP archive.",
        ) from error
    if not content or len(content) > max_artifact_bytes:
        _invalid(
            "github_post_merge_artifact_invalid",
            "Extracted attestation exceeds the configured size limit.",
        )
    return content


def _check_run_id(value: object, *, repository: str) -> str:
    url = _nonempty_string(value, "check run URL")
    parsed = urlsplit(url)
    prefix = f"/repos/{repository}/check-runs/"
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith(prefix)
    ):
        _invalid("github_api_identity_mismatch", "Check run URL is invalid.")
    check_id = parsed.path.removeprefix(prefix)
    if "/" in check_id or not check_id.isascii() or not check_id.isdigit():
        _invalid("github_api_identity_mismatch", "Check run URL is invalid.")
    return _positive_id(int(check_id), "check run ID")


def _repository_name(container: dict[str, object], label: str) -> str:
    repository = _object(container.get("repo") or container.get("repository"), label)
    return _nonempty_string(repository.get("full_name"), f"{label} repository")


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


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        _invalid("github_api_response_invalid", f"GitHub {label} must be an object.")
    return value


def _array(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        _invalid("github_api_response_invalid", f"GitHub {label} must be an array.")
    return value


def _nonempty_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        _invalid("github_api_response_invalid", f"GitHub {label} is invalid.")
    return value


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        _invalid("github_api_response_invalid", f"GitHub {label} must be boolean.")
    return value


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        _invalid("github_api_response_invalid", f"GitHub {label} is invalid.")
    return value


def _positive_id(value: object, label: str) -> str:
    return str(_positive_int(value, label))


def _git_object_id(value: object, label: str) -> str:
    text = _nonempty_string(value, label)
    if _GIT_OBJECT_ID.fullmatch(text) is None:
        _invalid("github_api_response_invalid", f"GitHub {label} is not a full SHA.")
    return text


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _invalid(code: str, message: str, **context: object) -> None:
    raise RCPError(code=code, message=message, context=context)
