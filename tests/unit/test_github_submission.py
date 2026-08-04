from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from researchctl.adapters._subprocess import CommandResult
from researchctl.adapters.github_submission import (
    GhSubmissionCommandResult,
    GitHubSubmissionDelivery,
    parse_github_remote,
)
from researchctl.errors import RCPError


REMOTE_URL = "git@github.example.invalid:owner/project.git"
BRANCH = "research/submission/submission_20260803T120000Z_" + "a" * 24
COMMIT = "b" * 40
OTHER_COMMIT = "c" * 40
BASE = "main"
TITLE = "researchctl: MAR-17 proposal"
BODY = "# Review\n\nGenerated proposal body.\n"


@dataclass(frozen=True)
class _GitCall:
    argv: tuple[str, ...]
    cwd: Path | None
    env: dict[str, str] | None
    timeout_seconds: float


class _GitRunner:
    def __init__(
        self,
        *,
        remote_url: str = REMOTE_URL,
        remote_head: str | None = None,
        push: str = "success",
    ) -> None:
        self.remote_url = remote_url
        self.remote_head = remote_head
        self.push = push
        self.calls: list[_GitCall] = []

    def run(self, argv, *, cwd, env, timeout_seconds):
        self.calls.append(_GitCall(argv, cwd, dict(env), timeout_seconds))
        arguments = argv[5:]
        if arguments == ("remote", "get-url", "origin"):
            return CommandResult(0, stdout=f"{self.remote_url}\n")
        if arguments == ("remote", "get-url", "--push", "origin"):
            return CommandResult(0, stdout=f"{self.remote_url}\n")
        if arguments[:3] == ("ls-remote", "--refs", "origin"):
            if self.remote_head is None:
                return CommandResult(0)
            return CommandResult(
                0,
                stdout=f"{self.remote_head}\trefs/heads/{BRANCH}\n",
            )
        if arguments[:3] == ("push", "--porcelain", "origin"):
            if self.push in {"success", "timeout_after_update"}:
                self.remote_head = COMMIT
            if self.push.startswith("timeout"):
                raise subprocess.TimeoutExpired(argv, timeout_seconds)
            if self.push == "failure":
                return CommandResult(1, stderr="credential SECRET")
            return CommandResult(0)
        raise AssertionError(f"unexpected git call: {arguments!r}")


@dataclass(frozen=True)
class _GhCall:
    argv: tuple[str, ...]
    env: dict[str, str]
    input_bytes: bytes | None
    timeout_seconds: float


class _GhRunner:
    def __init__(
        self,
        *,
        pulls: list[dict[str, object]] | None = None,
        create: str = "success",
    ) -> None:
        self.pulls = list(pulls or [])
        self.create = create
        self.calls: list[_GhCall] = []

    def run(self, argv, *, env, input_bytes, timeout_seconds):
        self.calls.append(_GhCall(argv, dict(env), input_bytes, timeout_seconds))
        method = argv[argv.index("--method") + 1]
        if method == "GET":
            return GhSubmissionCommandResult(
                0,
                json.dumps(self.pulls, separators=(",", ":")).encode("utf-8"),
            )
        if method == "POST":
            assert input_bytes is not None
            payload = json.loads(input_bytes)
            if self.create in {"success", "timeout_after_create"}:
                self.pulls = [
                    _pull(
                        title=payload["title"],
                        body=payload["body"],
                    )
                ]
            if self.create.startswith("timeout"):
                raise subprocess.TimeoutExpired(argv, timeout_seconds)
            if self.create == "failure":
                return GhSubmissionCommandResult(1, b'{"message":"SECRET"}')
            return GhSubmissionCommandResult(0, b"{}")
        raise AssertionError(f"unexpected gh method: {method}")


def _pull(
    *,
    number: int = 17,
    state: str = "open",
    commit: str = COMMIT,
    title: str = TITLE,
    body: str = BODY,
    merged_at: str | None = None,
) -> dict[str, object]:
    return {
        "number": number,
        "state": state,
        "title": title,
        "body": body,
        "merged_at": merged_at,
        "head": {
            "ref": BRANCH,
            "sha": commit,
            "repo": {"full_name": "owner/project"},
        },
        "base": {
            "ref": BASE,
            "repo": {"full_name": "owner/project"},
        },
    }


def _delivery(
    git: _GitRunner,
    gh: _GhRunner,
    *,
    remote_url: str = REMOTE_URL,
) -> GitHubSubmissionDelivery:
    return GitHubSubmissionDelivery(
        accepted_remote_url=remote_url,
        git_runner=git,
        gh_runner=gh,
        environment={
            "PATH": "/usr/bin",
            "HOME": "/home/test",
            "SSH_AUTH_SOCK": "/tmp/agent.sock",
            "GH_TOKEN": "top-secret-token",
            "UNRELATED_SECRET": "must-not-pass",
        },
        timeout_seconds=3,
    )


def _push(delivery: GitHubSubmissionDelivery, tmp_path: Path):
    return delivery.push_exact(
        repository_root=tmp_path,
        branch=BRANCH,
        commit=COMMIT,
    )


def _open(delivery: GitHubSubmissionDelivery, branch):
    return delivery.open_or_observe(
        submission_id=BRANCH.removeprefix("research/submission/"),
        branch=branch,
        base_branch=BASE,
        title=TITLE,
        body=BODY,
    )


def test_exact_branch_is_pushed_and_generated_pull_request_is_created(
    tmp_path: Path,
) -> None:
    git = _GitRunner()
    gh = _GhRunner()
    delivery = _delivery(git, gh)

    branch = _push(delivery, tmp_path)
    pull = _open(delivery, branch)

    assert branch.pushed is True
    assert branch.ref == f"refs/heads/{BRANCH}"
    assert pull.created is True
    assert pull.repository == "owner/project"
    assert pull.number == 17
    push = next(call for call in git.calls if call.argv[5] == "push")
    assert push.argv[5:] == (
        "push",
        "--porcelain",
        "origin",
        f"{COMMIT}:refs/heads/{BRANCH}",
    )
    assert push.env is not None
    assert push.env["SSH_AUTH_SOCK"] == "/tmp/agent.sock"
    assert "GH_TOKEN" not in push.env
    assert "UNRELATED_SECRET" not in push.env
    post = next(call for call in gh.calls if "POST" in call.argv)
    assert json.loads(post.input_bytes or b"") == {
        "base": BASE,
        "body": BODY,
        "head": BRANCH,
        "title": TITLE,
    }
    assert post.env["GH_TOKEN"] == "top-secret-token"
    assert "UNRELATED_SECRET" not in post.env


def test_existing_exact_branch_and_pr_are_observed_without_mutation(
    tmp_path: Path,
) -> None:
    git = _GitRunner(remote_head=COMMIT)
    gh = _GhRunner(pulls=[_pull()])
    delivery = _delivery(git, gh)

    branch = _push(delivery, tmp_path)
    pull = _open(delivery, branch)

    assert branch.pushed is False
    assert pull.created is False
    assert not any(call.argv[5] == "push" for call in git.calls)
    assert not any("POST" in call.argv for call in gh.calls)


def test_push_timeout_is_recovered_by_exact_remote_observation(tmp_path: Path) -> None:
    git = _GitRunner(push="timeout_after_update")
    delivery = _delivery(git, _GhRunner())

    branch = _push(delivery, tmp_path)

    assert branch.commit == COMMIT
    assert branch.pushed is True


def test_pr_create_timeout_is_recovered_by_exact_pr_observation(
    tmp_path: Path,
) -> None:
    git = _GitRunner(remote_head=COMMIT)
    gh = _GhRunner(create="timeout_after_create")
    delivery = _delivery(git, gh)

    pull = _open(delivery, _push(delivery, tmp_path))

    assert pull.number == 17
    assert pull.created is True
    assert len([call for call in gh.calls if "GET" in call.argv]) == 2


@pytest.mark.parametrize(
    ("git", "expected"),
    [
        (_GitRunner(remote_head=OTHER_COMMIT), "submission_remote_head_conflict"),
        (_GitRunner(push="timeout_without_update"), "submission_delivery_uncertain"),
        (_GitRunner(push="failure"), "submission_branch_push_failed"),
    ],
)
def test_remote_branch_conflicts_and_unresolved_pushes_fail_closed(
    tmp_path: Path,
    git: _GitRunner,
    expected: str,
) -> None:
    with pytest.raises(RCPError) as raised:
        _push(_delivery(git, _GhRunner()), tmp_path)

    assert raised.value.code == expected
    assert "SECRET" not in str(raised.value)
    assert "SECRET" not in repr(raised.value.context)


@pytest.mark.parametrize(
    ("pulls", "expected"),
    [
        ([_pull(), _pull(number=18)], "submission_pr_ambiguous"),
        ([_pull(state="closed")], "submission_pr_not_open"),
        ([_pull(commit=OTHER_COMMIT)], "submission_pr_identity_conflict"),
        ([_pull(body="REMOTE SECRET BODY")], "submission_pr_metadata_conflict"),
    ],
)
def test_conflicting_pull_requests_fail_closed_without_remote_body_in_errors(
    tmp_path: Path,
    pulls: list[dict[str, object]],
    expected: str,
) -> None:
    git = _GitRunner(remote_head=COMMIT)
    delivery = _delivery(git, _GhRunner(pulls=pulls))

    with pytest.raises(RCPError) as raised:
        _open(delivery, _push(delivery, tmp_path))

    assert raised.value.code == expected
    assert "REMOTE SECRET BODY" not in str(raised.value)
    assert "REMOTE SECRET BODY" not in repr(raised.value.context)


def test_pr_timeout_without_observed_effect_stays_uncertain(tmp_path: Path) -> None:
    git = _GitRunner(remote_head=COMMIT)
    gh = _GhRunner(create="timeout_without_create")
    delivery = _delivery(git, gh)

    with pytest.raises(RCPError) as raised:
        _open(delivery, _push(delivery, tmp_path))

    assert raised.value.code == "submission_delivery_uncertain"
    assert raised.value.context == {"stage": "pull_request_create"}


def test_remote_identity_must_match_accepted_project_configuration(
    tmp_path: Path,
) -> None:
    git = _GitRunner(remote_url="git@github.example.invalid:other/project.git")
    delivery = _delivery(git, _GhRunner())

    with pytest.raises(RCPError) as raised:
        _push(delivery, tmp_path)

    assert raised.value.code == "submission_remote_identity_mismatch"


def test_github_remote_parser_accepts_credential_free_common_forms() -> None:
    values = (
        "git@github.com:Owner/repository.git",
        "ssh://git@github.com/Owner/repository.git",
        "https://github.com/Owner/repository.git",
    )

    assert [parse_github_remote(value).repository for value in values] == [
        "Owner/repository",
        "Owner/repository",
        "Owner/repository",
    ]
    with pytest.raises(ValueError):
        parse_github_remote("https://token@github.com/owner/repository.git")
