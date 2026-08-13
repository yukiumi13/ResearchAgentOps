from __future__ import annotations

import json
from pathlib import Path

from researchctl.adapters._subprocess import CommandResult
from researchctl.adapters.github_impact_decision import (
    GitHubImpactDecisionDelivery,
)
from researchctl.adapters.github_submission import GhSubmissionCommandResult
from researchctl.domain.models import GitHubGovernancePolicy

REMOTE_URL = "git@github.example.invalid:owner/project.git"
DECISION_ID = "decision_20260803T150000Z_" + "a" * 24
BRANCH = f"research/impact-decision/{DECISION_ID}"
COMMIT = "b" * 40
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

    def run(self, argv, *, cwd, env, timeout_seconds):
        del cwd, env, timeout_seconds
        assert argv[1:8] == (
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "credential.helper=",
            "-C",
        )
        arguments = argv[9:]
        if arguments in {
            ("remote", "get-url", "origin"),
            ("remote", "get-url", "--push", "origin"),
        }:
            return CommandResult(0, stdout=f"{REMOTE_URL}\n")
        if arguments[:3] == ("ls-remote", "--refs", "origin"):
            return CommandResult(
                0,
                stdout=(
                    ""
                    if self.remote_head is None
                    else f"{self.remote_head}\trefs/heads/{BRANCH}\n"
                ),
            )
        if arguments[:3] == ("push", "--porcelain", "origin"):
            self.remote_head = COMMIT
            return CommandResult(0)
        raise AssertionError(f"unexpected Git call: {arguments!r}")


class _GhRunner:
    def __init__(self) -> None:
        self.pulls: list[dict[str, object]] = []

    def run(self, argv, *, env, input_bytes, timeout_seconds):
        del env, timeout_seconds
        method = argv[argv.index("--method") + 1]
        if method == "GET":
            return GhSubmissionCommandResult(
                0,
                json.dumps(self.pulls, separators=(",", ":")).encode("utf-8"),
            )
        payload = json.loads(input_bytes or b"")
        self.pulls = [
            {
                "number": 19,
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


def test_manager_decision_delivery_uses_only_derived_branch(
    tmp_path: Path,
) -> None:
    delivery = GitHubImpactDecisionDelivery(
        accepted_remote_url=REMOTE_URL,
        governance=_governance(),
        git_runner=_GitRunner(),
        gh_runner=_GhRunner(),
        environment={"GH_TOKEN": "test-token"},
    )

    branch = delivery.push_exact(
        repository_root=tmp_path,
        branch=BRANCH,
        commit=COMMIT,
    )
    pull = delivery.open_or_observe(
        decision_id=DECISION_ID,
        branch=branch,
        base_branch="main",
        title="researchctl: keep stale",
        body="# Impact decision\n",
    )

    assert branch.branch == BRANCH
    assert pull.number == 19
    assert pull.head_branch == BRANCH
    assert pull.head_commit == COMMIT
