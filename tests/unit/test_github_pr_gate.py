from __future__ import annotations

import json
from dataclasses import dataclass, replace

from typer.testing import CliRunner

import researchctl.github_cli as github_cli
from researchctl.adapters._subprocess import CommandResult
from researchctl.adapters.github_pr_gate import GitHubPullRequestGateClient
from researchctl.cli import app
from researchctl.services.github_governance import GitHubGovernanceObservation
from researchctl.services.github_pr_gate import (
    GitHubPullRequestGateObservation,
    GitHubRequiredCheck,
    GitHubRunnerCapacityEvidence,
    GitHubWorkflowRun,
    assess_github_pull_request_gate,
)


REPOSITORY = "owner/project"
HEAD = "7" * 40


def _governance(*, checks: tuple[str, ...] = ("doc-tree",)) -> GitHubGovernanceObservation:
    return GitHubGovernanceObservation(
        repository=REPOSITORY,
        default_branch="main",
        branch="main",
        protection_sources=("ruleset:41",),
        pull_request_required=True,
        required_approvals=1,
        code_owner_review_required=True,
        dismiss_stale_reviews=True,
        last_push_approval_required=True,
        required_status_checks=checks,
        strict_status_checks=True,
        force_push_blocked=True,
        deletion_blocked=True,
        classic_admins_enforced=None,
        bypass_actors=(),
    )


def _observation(
    *,
    check: GitHubRequiredCheck | None = None,
    reviewers: tuple[str, ...] = (),
    mergeable_state: str = "blocked",
    capacity: tuple[GitHubRunnerCapacityEvidence, ...] = (),
) -> GitHubPullRequestGateObservation:
    return GitHubPullRequestGateObservation(
        repository=REPOSITORY,
        pull_request_number=6,
        head_sha=HEAD,
        base_branch="main",
        draft=False,
        mergeable_state=mergeable_state,
        approved_reviewers=reviewers,
        required_checks=("doc-tree",),
        checks=(check,) if check is not None else (),
        workflow_runs=(
            GitHubWorkflowRun(
                run_id=31119263841,
                name="documents",
                status="queued",
                conclusion=None,
                attempt=2,
            ),
        ),
        capacity_evidence=capacity,
    )


def test_capacity_is_distinct_from_validation_failure() -> None:
    capacity = GitHubRunnerCapacityEvidence(
        workflow_run_id=31119263841,
        job_id=92679154553,
        check_name="doc-tree",
        runner_labels=("ubuntu-latest",),
        message="The job was not acquired by Runner of type hosted even after multiple attempts",
    )
    observation = _observation(
        check=GitHubRequiredCheck(
            name="doc-tree",
            status="completed",
            conclusion="cancelled",
        ),
        capacity=(capacity,),
    )

    report = assess_github_pull_request_gate(observation, governance=_governance())

    assert report.terminal_result == "ci_capacity_pending"
    assert report.merge_allowed is False
    assert "do not disable" in report.recommendation


def test_failed_pending_review_and_ready_states_are_typed() -> None:
    governance = _governance()
    failed = _observation(
        check=GitHubRequiredCheck("doc-tree", "completed", "failure")
    )
    pending = _observation(check=GitHubRequiredCheck("doc-tree", "queued", None))
    passed = _observation(
        check=GitHubRequiredCheck("doc-tree", "completed", "success")
    )
    ready = replace(passed, approved_reviewers=("manager",), mergeable_state="clean")

    assert assess_github_pull_request_gate(
        failed, governance=governance
    ).terminal_result == "validation_failed"
    assert assess_github_pull_request_gate(
        pending, governance=governance
    ).terminal_result == "checks_pending"
    assert assess_github_pull_request_gate(
        passed, governance=governance
    ).terminal_result == "review_pending"
    assert assess_github_pull_request_gate(
        ready, governance=governance
    ).terminal_result == "ready"


def test_missing_merge_protection_is_governance_misconfiguration() -> None:
    governance = replace(_governance(), protection_sources=())

    report = assess_github_pull_request_gate(_observation(), governance=governance)

    assert report.terminal_result == "governance_misconfigured"


@dataclass(frozen=True)
class _Call:
    endpoint: str
    environment: dict[str, str]


class _Runner:
    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.calls: list[_Call] = []

    def run(self, argv, *, cwd, env, timeout_seconds):
        del timeout_seconds
        assert cwd is None
        endpoint = argv[-1]
        self.calls.append(_Call(endpoint, dict(env)))
        return CommandResult(0, stdout=json.dumps(self.responses[endpoint]))


def _capacity_responses() -> dict[str, object]:
    run_id = 31119263841
    check_run_id = 92679154553
    return {
        f"/repos/{REPOSITORY}/pulls/6": {
            "state": "open",
            "draft": False,
            "mergeable_state": "blocked",
            "head": {"sha": HEAD},
            "base": {"ref": "main", "repo": {"full_name": REPOSITORY}},
        },
        f"/repos/{REPOSITORY}/commits/{HEAD}/check-runs?filter=all&per_page=100": {
            "check_runs": [
                {
                    "name": "doc-tree",
                    "status": "completed",
                    "conclusion": "cancelled",
                    "started_at": "2026-08-06T16:33:13Z",
                    "completed_at": "2026-08-06T16:48:14Z",
                    "details_url": "https://example.invalid/check",
                }
            ]
        },
        f"/repos/{REPOSITORY}/commits/{HEAD}/status": {"statuses": []},
        f"/repos/{REPOSITORY}/pulls/6/reviews?per_page=100": [],
        f"/repos/{REPOSITORY}/actions/runs?head_sha={HEAD}&per_page=5": {
            "workflow_runs": [
                {
                    "id": run_id,
                    "name": "documents",
                    "status": "queued",
                    "conclusion": None,
                    "run_attempt": 2,
                    "html_url": "https://example.invalid/run",
                }
            ]
        },
        f"/repos/{REPOSITORY}/actions/runs/{run_id}/jobs?filter=all&per_page=100": {
            "jobs": [
                {
                    "id": check_run_id,
                    "name": "doc-tree",
                    "conclusion": "cancelled",
                    "runner_id": 0,
                    "labels": ["ubuntu-latest"],
                    "steps": [],
                    "check_run_url": (
                        f"https://api.github.com/repos/{REPOSITORY}/check-runs/"
                        f"{check_run_id}"
                    ),
                }
            ]
        },
        f"/repos/{REPOSITORY}/check-runs/{check_run_id}/annotations?per_page=100": [
            {
                "message": (
                    "The job was not acquired by Runner of type hosted even after "
                    "multiple attempts"
                )
            }
        ],
    }


def test_client_observes_exact_runner_capacity_annotation() -> None:
    runner = _Runner(_capacity_responses())
    client = GitHubPullRequestGateClient(
        runner=runner,
        environment={"PATH": "/usr/bin", "UNRELATED_SECRET": "hidden"},
    )

    observation = client.observe(
        repository=REPOSITORY,
        pull_request_number=6,
        required_checks=("doc-tree",),
    )

    assert observation.head_sha == HEAD
    assert observation.capacity_evidence[0].check_name == "doc-tree"
    assert observation.capacity_evidence[0].runner_labels == ("ubuntu-latest",)
    assert all(call.environment == {"PATH": "/usr/bin"} for call in runner.calls)


def test_pr_status_cli_emits_machine_readable_capacity_state(monkeypatch) -> None:
    capacity = GitHubRunnerCapacityEvidence(
        workflow_run_id=31119263841,
        job_id=92679154553,
        check_name="doc-tree",
        runner_labels=("ubuntu-latest",),
        message="The job was not acquired by Runner of type hosted",
    )
    observation = _observation(
        check=GitHubRequiredCheck("doc-tree", "completed", "cancelled"),
        capacity=(capacity,),
    )

    class _GovernanceClient:
        def observe(self, **kwargs):
            assert kwargs["repository"] == REPOSITORY
            return _governance()

    class _GateClient:
        def observe(self, **kwargs):
            assert kwargs["pull_request_number"] == 6
            assert kwargs["required_checks"] == ("doc-tree",)
            return observation

    monkeypatch.setattr(github_cli, "GitHubGovernanceClient", _GovernanceClient)
    monkeypatch.setattr(github_cli, "GitHubPullRequestGateClient", _GateClient)

    result = CliRunner().invoke(
        app,
        [
            "github",
            "pr-status",
            "--repository",
            REPOSITORY,
            "--pull-request",
            "6",
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["command"] == "github.pr-status"
    assert payload["data"]["terminal_result"] == "ci_capacity_pending"
    assert payload["data"]["merge_allowed"] is False
