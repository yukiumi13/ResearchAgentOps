from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Mapping

from researchctl.adapters._subprocess import CommandRunner, SubprocessCommandRunner
from researchctl.errors import RCPError
from researchctl.services.github_pr_gate import (
    GitHubPullRequestGateObservation,
    GitHubRequiredCheck,
    GitHubRunnerCapacityEvidence,
    GitHubWorkflowRun,
)

_REPOSITORY = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,99})/"
    r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,99})$"
)
_HOST = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_GIT_OBJECT_ID = re.compile(r"^[0-9a-f]{40}$")
_MAX_GITHUB_JSON_BYTES = 4 * 1024 * 1024
_CAPACITY_MESSAGE = "not acquired by runner"
_GH_ENVIRONMENT_KEYS = frozenset(
    {
        "PATH",
        "HOME",
        "GH_CONFIG_DIR",
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


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise _invalid_response(label)
    return value


def _array(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise _invalid_response(label)
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise _invalid_response(label)
    return value


def _optional_string(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _string(value, label)


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise _invalid_response(label)
    return value


def _invalid_response(label: str) -> RCPError:
    return RCPError(
        code="github_pr_status_response_invalid",
        message=f"GitHub returned an invalid {label} value.",
        remediation="Retry the read-only observation and inspect the GitHub API response.",
    )


class GitHubPullRequestGateClient:
    """Observe one exact PR head, its required checks, and runner acquisition evidence."""

    def __init__(
        self,
        *,
        runner: CommandRunner | None = None,
        environment: Mapping[str, str] | None = None,
        timeout_seconds: float = 20.0,
        max_json_bytes: int = _MAX_GITHUB_JSON_BYTES,
        max_workflow_runs: int = 5,
    ) -> None:
        if timeout_seconds <= 0 or max_json_bytes < 1 or max_workflow_runs < 1:
            raise ValueError("GitHub PR status limits must be positive")
        source = os.environ if environment is None else environment
        self._environment = {
            key: value
            for key, value in source.items()
            if key in _GH_ENVIRONMENT_KEYS and value
        }
        self._runner = runner or SubprocessCommandRunner()
        self._timeout_seconds = timeout_seconds
        self._max_json_bytes = max_json_bytes
        self._max_workflow_runs = max_workflow_runs

    def observe(
        self,
        *,
        repository: str,
        pull_request_number: int,
        required_checks: tuple[str, ...],
        hostname: str = "github.com",
    ) -> GitHubPullRequestGateObservation:
        if (
            _REPOSITORY.fullmatch(repository) is None
            or _HOST.fullmatch(hostname) is None
            or isinstance(pull_request_number, bool)
            or pull_request_number < 1
            or not all(isinstance(item, str) and item for item in required_checks)
        ):
            raise RCPError(
                code="github_pr_status_request_invalid",
                message="PR status requires a canonical repository, pull request, and checks.",
            )

        pull = _object(
            self._get(hostname, f"/repos/{repository}/pulls/{pull_request_number}"),
            "pull request",
        )
        if pull.get("state") != "open":
            raise RCPError(
                code="github_pr_status_not_open",
                message=f"Pull request {pull_request_number} is not open.",
                remediation="Select an open pull request whose merge gate is still active.",
            )
        head_sha = _string(_object(pull.get("head"), "pull request head").get("sha"), "head SHA")
        if _GIT_OBJECT_ID.fullmatch(head_sha) is None:
            raise _invalid_response("pull request head SHA")
        base = _object(pull.get("base"), "pull request base")
        base_branch = _string(base.get("ref"), "base branch")
        base_repository = _string(
            _object(base.get("repo"), "base repository").get("full_name"),
            "base repository name",
        )
        if base_repository.lower() != repository.lower():
            raise RCPError(
                code="github_pr_status_repository_mismatch",
                message="Pull request base repository differs from the requested repository.",
            )
        draft = pull.get("draft")
        if not isinstance(draft, bool):
            raise _invalid_response("pull request draft")
        mergeable_state = _string(pull.get("mergeable_state"), "mergeable state")

        checks = self._checks(hostname, repository, head_sha, required_checks)
        reviewers = self._approved_reviewers(
            hostname,
            repository,
            pull_request_number,
        )
        workflows, capacity = self._workflow_evidence(
            hostname,
            repository,
            head_sha,
            required_checks,
        )
        return GitHubPullRequestGateObservation(
            repository=base_repository,
            pull_request_number=pull_request_number,
            head_sha=head_sha,
            base_branch=base_branch,
            draft=draft,
            mergeable_state=mergeable_state,
            approved_reviewers=reviewers,
            required_checks=tuple(sorted(set(required_checks))),
            checks=checks,
            workflow_runs=workflows,
            capacity_evidence=capacity,
        )

    def _checks(
        self,
        hostname: str,
        repository: str,
        head_sha: str,
        required_checks: tuple[str, ...],
    ) -> tuple[GitHubRequiredCheck, ...]:
        required = set(required_checks)
        selected: dict[str, tuple[str, GitHubRequiredCheck]] = {}
        payload = _object(
            self._get(
                hostname,
                f"/repos/{repository}/commits/{head_sha}/check-runs?filter=all&per_page=100",
            ),
            "check runs",
        )
        for value in _array(payload.get("check_runs"), "check runs"):
            item = _object(value, "check run")
            name = _string(item.get("name"), "check run name")
            if name not in required:
                continue
            status = _string(item.get("status"), "check run status")
            conclusion = _optional_string(item.get("conclusion"), "check run conclusion")
            timestamp = str(item.get("completed_at") or item.get("started_at") or "")
            check = GitHubRequiredCheck(
                name=name,
                status=status,
                conclusion=conclusion,
                details_url=_optional_string(item.get("details_url"), "check run URL"),
            )
            if name not in selected or timestamp > selected[name][0]:
                selected[name] = (timestamp, check)

        statuses = _object(
            self._get(hostname, f"/repos/{repository}/commits/{head_sha}/status"),
            "combined commit status",
        )
        for value in _array(statuses.get("statuses"), "commit statuses"):
            item = _object(value, "commit status")
            name = _string(item.get("context"), "commit status context")
            if name not in required or name in selected:
                continue
            state = _string(item.get("state"), "commit status state")
            selected[name] = (
                str(item.get("updated_at") or item.get("created_at") or ""),
                GitHubRequiredCheck(
                    name=name,
                    status="completed" if state != "pending" else "pending",
                    conclusion=state if state != "pending" else None,
                    details_url=_optional_string(item.get("target_url"), "commit status URL"),
                ),
            )
        return tuple(selected[name][1] for name in sorted(selected))

    def _approved_reviewers(
        self,
        hostname: str,
        repository: str,
        pull_request_number: int,
    ) -> tuple[str, ...]:
        reviews = _array(
            self._get(
                hostname,
                f"/repos/{repository}/pulls/{pull_request_number}/reviews?per_page=100",
            ),
            "pull request reviews",
        )
        decisions: dict[str, str] = {}
        for value in reviews:
            review = _object(value, "pull request review")
            login = _string(_object(review.get("user"), "review user").get("login"), "reviewer")
            state = _string(review.get("state"), "review state").upper()
            if state in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
                decisions[login] = state
        return tuple(sorted(login for login, state in decisions.items() if state == "APPROVED"))

    def _workflow_evidence(
        self,
        hostname: str,
        repository: str,
        head_sha: str,
        required_checks: tuple[str, ...],
    ) -> tuple[tuple[GitHubWorkflowRun, ...], tuple[GitHubRunnerCapacityEvidence, ...]]:
        payload = _object(
            self._get(
                hostname,
                (
                    f"/repos/{repository}/actions/runs?head_sha={head_sha}"
                    f"&per_page={self._max_workflow_runs}"
                ),
            ),
            "workflow runs",
        )
        runs: list[GitHubWorkflowRun] = []
        capacity: list[GitHubRunnerCapacityEvidence] = []
        required = set(required_checks)
        workflow_runs = _array(payload.get("workflow_runs"), "workflow runs")
        for value in workflow_runs[: self._max_workflow_runs]:
            run = _object(value, "workflow run")
            run_id = _positive_int(run.get("id"), "workflow run ID")
            observed = GitHubWorkflowRun(
                run_id=run_id,
                name=_string(run.get("name"), "workflow run name"),
                status=_string(run.get("status"), "workflow run status"),
                conclusion=_optional_string(run.get("conclusion"), "workflow run conclusion"),
                attempt=_positive_int(run.get("run_attempt", 1), "workflow run attempt"),
                url=_optional_string(run.get("html_url"), "workflow run URL"),
            )
            runs.append(observed)
            jobs_payload = _object(
                self._get(
                    hostname,
                    f"/repos/{repository}/actions/runs/{run_id}/jobs?filter=all&per_page=100",
                ),
                "workflow jobs",
            )
            for job_value in _array(jobs_payload.get("jobs"), "workflow jobs"):
                job = _object(job_value, "workflow job")
                name = _string(job.get("name"), "workflow job name")
                if name not in required or job.get("conclusion") != "cancelled":
                    continue
                runner_id = job.get("runner_id")
                steps = job.get("steps", [])
                if runner_id not in (None, 0) or not isinstance(steps, list) or steps:
                    continue
                check_run_url = _string(job.get("check_run_url"), "workflow job check URL")
                check_run_id = check_run_url.rsplit("/", maxsplit=1)[-1]
                if not check_run_id.isdigit():
                    raise _invalid_response("workflow job check run ID")
                annotations = _array(
                    self._get(
                        hostname,
                        f"/repos/{repository}/check-runs/{check_run_id}/annotations?per_page=100",
                    ),
                    "check run annotations",
                )
                for annotation_value in annotations:
                    annotation = _object(annotation_value, "check run annotation")
                    message = _string(annotation.get("message"), "check run annotation message")
                    if _CAPACITY_MESSAGE not in message.lower():
                        continue
                    labels = _array(job.get("labels", []), "workflow job labels")
                    if not all(isinstance(label, str) and label for label in labels):
                        raise _invalid_response("workflow job labels")
                    capacity.append(
                        GitHubRunnerCapacityEvidence(
                            workflow_run_id=run_id,
                            job_id=_positive_int(job.get("id"), "workflow job ID"),
                            check_name=name,
                            runner_labels=tuple(labels),
                            message=message,
                        )
                    )
        return tuple(runs), tuple(capacity)

    def _get(self, hostname: str, endpoint: str) -> object:
        try:
            result = self._runner.run(
                ("gh", "api", "--hostname", hostname, "--method", "GET", endpoint),
                cwd=None,
                env=self._environment,
                timeout_seconds=self._timeout_seconds,
            )
        except (subprocess.TimeoutExpired, TimeoutError) as error:
            raise RCPError(
                code="github_pr_status_unavailable",
                message="GitHub PR status observation timed out.",
                remediation="Retry after GitHub connectivity recovers.",
            ) from error
        except FileNotFoundError as error:
            raise RCPError(
                code="github_cli_not_found",
                message="The gh executable was not found for PR status observation.",
            ) from error
        if result.returncode != 0:
            raise RCPError(
                code="github_pr_status_observation_failed",
                message="GitHub pull request state could not be observed.",
                remediation="Check gh authentication and repository visibility.",
                context={"endpoint": endpoint, "returncode": result.returncode},
            )
        encoded = result.stdout.encode("utf-8")
        if len(encoded) > self._max_json_bytes:
            raise _invalid_response("oversized GitHub response")
        try:
            return json.loads(result.stdout)
        except (json.JSONDecodeError, RecursionError, UnicodeError) as error:
            raise _invalid_response("GitHub JSON response") from error
