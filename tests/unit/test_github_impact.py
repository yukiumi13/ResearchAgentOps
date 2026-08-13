from __future__ import annotations

import json
from pathlib import Path

import pytest

from researchctl.adapters._subprocess import CommandResult
from researchctl.adapters.github_impact import GitHubImpactDelivery
from researchctl.adapters.github_submission import GhSubmissionCommandResult
from researchctl.domain.models import GitHubGovernancePolicy
from researchctl.errors import RCPError

REMOTE_URL = "git@github.example.invalid:owner/project.git"
IMPACT_ID = "impact_20260803T120000Z_" + "a" * 24
BRANCH = f"research/impact/{IMPACT_ID}"
COMMIT = "b" * 40
TITLE = "researchctl: overlap impact"
BODY = "# Impact review\n"
AUTHOR = "researchctl-agent[bot]"


def _governance() -> GitHubGovernancePolicy:
    return GitHubGovernancePolicy.model_validate(
        {
            "repository": "owner/project",
            "default_branch": "main",
            "agent_app": {
                "app_id": 12,
                "installation_id": 34,
                "login": AUTHOR,
            },
            "managers": [{"kind": "user", "login": "manager"}],
        }
    )


class _GitRunner:
    def __init__(self) -> None:
        self.remote_head: str | None = None
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv, *, cwd, env, timeout_seconds):
        del cwd, env, timeout_seconds
        arguments = argv[5:]
        self.calls.append(arguments)
        if arguments in {
            ("remote", "get-url", "origin"),
            ("remote", "get-url", "--push", "origin"),
        }:
            return CommandResult(0, stdout=f"{REMOTE_URL}\n")
        if arguments[:3] == ("ls-remote", "--refs", "origin"):
            if self.remote_head is None:
                return CommandResult(0)
            return CommandResult(
                0,
                stdout=f"{self.remote_head}\trefs/heads/{BRANCH}\n",
            )
        if arguments[:3] == ("push", "--porcelain", "origin"):
            self.remote_head = COMMIT
            return CommandResult(0)
        raise AssertionError(f"unexpected Git call: {arguments!r}")


class _GhRunner:
    def __init__(self) -> None:
        self.pulls: list[dict[str, object]] = []
        self.posts = 0

    def run(self, argv, *, env, input_bytes, timeout_seconds):
        del env, timeout_seconds
        method = argv[argv.index("--method") + 1]
        if method == "GET":
            return GhSubmissionCommandResult(
                0,
                json.dumps(self.pulls, separators=(",", ":")).encode("utf-8"),
            )
        assert method == "POST"
        self.posts += 1
        payload = json.loads(input_bytes or b"")
        self.pulls = [
            {
                "number": 18,
                "state": "open",
                "title": payload["title"],
                "body": payload["body"],
                "merged_at": None,
                "user": {"login": AUTHOR},
                "head": {
                    "ref": BRANCH,
                    "sha": COMMIT,
                    "repo": {"full_name": "owner/project"},
                },
                "base": {
                    "ref": "main",
                    "repo": {"full_name": "owner/project"},
                },
            }
        ]
        return GhSubmissionCommandResult(0, b"{}")


def test_impact_delivery_pushes_only_fixed_branch_and_observes_exact_pr(
    tmp_path: Path,
) -> None:
    git = _GitRunner()
    gh = _GhRunner()
    delivery = GitHubImpactDelivery(
        accepted_remote_url=REMOTE_URL,
        governance=_governance(),
        git_runner=git,
        gh_runner=gh,
        environment={"PATH": "/usr/bin", "GH_TOKEN": "test-token"},
    )

    branch = delivery.push_exact(
        repository_root=tmp_path,
        branch=BRANCH,
        commit=COMMIT,
    )
    pull = delivery.open_or_observe(
        impact_id=IMPACT_ID,
        branch=branch,
        base_branch="main",
        title=TITLE,
        body=BODY,
    )
    observed_again = delivery.open_or_observe(
        impact_id=IMPACT_ID,
        branch=branch,
        base_branch="main",
        title=TITLE,
        body=BODY,
    )

    assert branch.ref == f"refs/heads/{BRANCH}"
    assert (
        "push",
        "--porcelain",
        "origin",
        f"{COMMIT}:refs/heads/{BRANCH}",
    ) in git.calls
    assert pull.number == 18
    assert pull.created is True
    assert observed_again.created is False
    assert gh.posts == 1


def test_impact_delivery_rejects_caller_selected_branch_before_remote_calls(
    tmp_path: Path,
) -> None:
    git = _GitRunner()
    delivery = GitHubImpactDelivery(
        accepted_remote_url=REMOTE_URL,
        governance=_governance(),
        git_runner=git,
        gh_runner=_GhRunner(),
        environment={},
    )

    with pytest.raises(RCPError) as raised:
        delivery.push_exact(
            repository_root=tmp_path,
            branch="research/impact/caller-selected",
            commit=COMMIT,
        )

    assert raised.value.code == "impact_delivery_request_invalid"
    assert git.calls == []
