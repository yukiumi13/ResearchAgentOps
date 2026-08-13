from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, replace

import pytest
from typer.testing import CliRunner

import researchctl.github_cli as github_cli
from researchctl.adapters.github_protection import (
    BytesCommandResult,
    GitHubProtectionManager,
)
from researchctl.cli import app
from researchctl.domain.models import GitHubGovernancePolicy
from researchctl.errors import RCPError
from researchctl.services.github_governance import (
    GitHubGovernanceApplyReceipt,
    GitHubGovernanceObservation,
    preview_github_governance_apply,
)

REPOSITORY = "owner/project"


@dataclass(frozen=True)
class _Call:
    argv: tuple[str, ...]
    env: dict[str, str]
    input_data: bytes


class _Runner:
    def __init__(self, responses: list[BytesCommandResult | Exception]) -> None:
        self.responses = list(responses)
        self.calls: list[_Call] = []

    def run(self, argv, *, cwd, env, timeout_seconds, input_data):
        assert cwd is None
        assert timeout_seconds == 3
        self.calls.append(_Call(argv, dict(env), input_data))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class _Observer:
    def __init__(self, observations: list[GitHubGovernanceObservation | Exception]):
        self.observations = list(observations)
        self.calls: list[dict[str, str]] = []

    def observe(self, **kwargs):
        self.calls.append(kwargs)
        value = self.observations.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def _observation(*, protected: bool) -> GitHubGovernanceObservation:
    return GitHubGovernanceObservation(
        repository=REPOSITORY,
        default_branch="main",
        branch="main",
        protection_sources=("classic",) if protected else (),
        pull_request_required=protected,
        required_approvals=1 if protected else 0,
        code_owner_review_required=protected,
        dismiss_stale_reviews=protected,
        last_push_approval_required=protected,
        required_status_checks=(
            "researchctl/exact-head",
            "researchctl/source-tests",
        )
        if protected
        else (),
        strict_status_checks=protected,
        force_push_blocked=protected,
        deletion_blocked=protected,
        classic_admins_enforced=True if protected else None,
        bypass_actors=(),
    )


def _policy(**updates: object) -> GitHubGovernancePolicy:
    content: dict[str, object] = {
        "repository": REPOSITORY,
        "default_branch": "main",
        "agent_app": {
            "app_id": 12,
            "installation_id": 34,
            "login": "researchctl-agent[bot]",
        },
        "managers": [{"kind": "user", "login": "yukiumi13"}],
    }
    content.update(updates)
    return GitHubGovernancePolicy.model_validate(content)


def _json_result(value: object, *, returncode: int = 0) -> BytesCommandResult:
    return BytesCommandResult(returncode, stdout=json.dumps(value).encode("utf-8"))


def _manager(
    observations: list[GitHubGovernanceObservation | Exception],
    responses: list[BytesCommandResult | Exception],
    *,
    environment: dict[str, str] | None = None,
) -> tuple[GitHubProtectionManager, _Observer, _Runner]:
    observer = _Observer(observations)
    runner = _Runner(responses)
    return (
        GitHubProtectionManager(
            observer=observer,
            runner=runner,
            environment=environment or {},
            timeout_seconds=3,
        ),
        observer,
        runner,
    )


def _digests(
    observation: GitHubGovernanceObservation,
    policy: GitHubGovernancePolicy,
) -> tuple[str, str]:
    preview = preview_github_governance_apply(observation, policy)
    return preview.policy_digest, preview.observation_digest


def test_preview_is_read_only_and_emits_both_digests() -> None:
    policy = _policy()
    observation = _observation(protected=False)
    manager, observer, runner = _manager([observation], [])

    receipt = manager.preview(policy)

    assert receipt.terminal_result == "preview"
    assert receipt.preview.mutation_required is True
    assert receipt.preview.policy_digest.startswith("sha256:")
    assert receipt.preview.observation_digest.startswith("sha256:")
    assert len(observer.calls) == 1
    assert runner.calls == []


def test_healthy_apply_authenticates_manager_and_returns_no_change() -> None:
    policy = _policy()
    observation = _observation(protected=True)
    manager, observer, runner = _manager(
        [observation],
        [_json_result({"login": "YukiUmi13"})],
    )
    policy_digest, observation_digest = _digests(observation, policy)

    receipt = manager.apply(
        policy,
        expected_policy_digest=policy_digest,
        expected_observation_digest=observation_digest,
    )

    assert receipt.terminal_result == "no_change"
    assert receipt.manager_login == "YukiUmi13"
    assert len(observer.calls) == 1
    assert [call.argv[-1] for call in runner.calls] == ["/user"]


@pytest.mark.parametrize(
    ("expected_policy", "expected_observation", "code"),
    [
        ("sha256:" + "0" * 64, None, "github_governance_policy_changed"),
        (None, "sha256:" + "0" * 64, "github_governance_observation_changed"),
    ],
)
def test_stale_digest_fails_before_manager_authentication(
    expected_policy: str | None,
    expected_observation: str | None,
    code: str,
) -> None:
    policy = _policy()
    observation = _observation(protected=False)
    policy_digest, observation_digest = _digests(observation, policy)
    manager, _, runner = _manager([observation], [])

    with pytest.raises(RCPError) as caught:
        manager.apply(
            policy,
            expected_policy_digest=expected_policy or policy_digest,
            expected_observation_digest=expected_observation or observation_digest,
        )

    assert caught.value.code == code
    assert runner.calls == []


@pytest.mark.parametrize("login", ["someone-else", "researchctl-agent[bot]"])
def test_non_manager_and_agent_app_cannot_apply(login: str) -> None:
    policy = _policy()
    observation = _observation(protected=False)
    manager, _, runner = _manager([observation], [_json_result({"login": login})])
    policy_digest, observation_digest = _digests(observation, policy)

    with pytest.raises(RCPError) as caught:
        manager.apply(
            policy,
            expected_policy_digest=policy_digest,
            expected_observation_digest=observation_digest,
        )

    assert caught.value.code == "github_governance_manager_required"
    assert len(runner.calls) == 1


@pytest.mark.parametrize(("state", "accepted"), [("active", True), ("pending", False)])
def test_active_manager_team_membership_is_required(
    state: str,
    accepted: bool,
) -> None:
    policy = _policy(
        managers=[
            {
                "kind": "team",
                "organization": "owner",
                "slug": "research-managers",
            }
        ]
    )
    observation = _observation(protected=True)
    manager, _, runner = _manager(
        [observation],
        [
            _json_result({"login": "human-manager"}),
            _json_result({"state": state}),
        ],
    )
    policy_digest, observation_digest = _digests(observation, policy)

    if accepted:
        receipt = manager.apply(
            policy,
            expected_policy_digest=policy_digest,
            expected_observation_digest=observation_digest,
        )
        assert receipt.terminal_result == "no_change"
    else:
        with pytest.raises(RCPError) as caught:
            manager.apply(
                policy,
                expected_policy_digest=policy_digest,
                expected_observation_digest=observation_digest,
            )
        assert caught.value.code == "github_governance_manager_required"
    assert runner.calls[1].argv[-1] == (
        "/orgs/owner/teams/research-managers/memberships/human-manager"
    )


@pytest.mark.parametrize(
    ("policy", "observation", "code"),
    [
        (
            _policy(),
            replace(
                _observation(protected=False),
                protection_sources=("ruleset:41",),
            ),
            "github_governance_ruleset_conflict",
        ),
        (
            _policy(
                bypass_actors=[
                    {
                        "actor_type": "Integration",
                        "actor_id": 7,
                        "bypass_mode": "pull_request",
                        "rationale": "Audited emergency access.",
                    }
                ]
            ),
            _observation(protected=False),
            "github_governance_bypass_unsupported",
        ),
    ],
)
def test_unrepresentable_classic_policy_fails_before_mutation(
    policy: GitHubGovernancePolicy,
    observation: GitHubGovernanceObservation,
    code: str,
) -> None:
    manager, _, runner = _manager(
        [observation],
        [_json_result({"login": "yukiumi13"})],
    )
    policy_digest, observation_digest = _digests(observation, policy)

    with pytest.raises(RCPError) as caught:
        manager.apply(
            policy,
            expected_policy_digest=policy_digest,
            expected_observation_digest=observation_digest,
        )

    assert caught.value.code == code
    assert len(runner.calls) == 1


def test_apply_uses_exact_classic_payload_and_secret_filtered_environment() -> None:
    policy = _policy()
    before = _observation(protected=False)
    after = _observation(protected=True)
    manager, observer, runner = _manager(
        [before, after],
        [
            _json_result({"login": "yukiumi13"}),
            BytesCommandResult(0, stdout=b'{"ignored":"response"}'),
        ],
        environment={
            "PATH": "/usr/bin",
            "GH_TOKEN": "secret-token",
            "UNRELATED_SECRET": "must-not-pass",
        },
    )
    policy_digest, observation_digest = _digests(before, policy)

    receipt = manager.apply(
        policy,
        expected_policy_digest=policy_digest,
        expected_observation_digest=observation_digest,
    )

    assert receipt.terminal_result == "applied"
    assert len(observer.calls) == 2
    assert all(
        call.env == {"PATH": "/usr/bin", "GH_TOKEN": "secret-token"}
        for call in runner.calls
    )
    mutation = runner.calls[1]
    assert mutation.argv == (
        "gh",
        "api",
        "--hostname",
        "github.com",
        "--method",
        "PUT",
        "--input",
        "-",
        "/repos/owner/project/branches/main/protection",
    )
    assert json.loads(mutation.input_data) == {
        "required_status_checks": {
            "strict": True,
            "contexts": ["researchctl/exact-head", "researchctl/source-tests"],
        },
        "enforce_admins": True,
        "required_pull_request_reviews": {
            "dismiss_stale_reviews": True,
            "require_code_owner_reviews": True,
            "required_approving_review_count": 1,
            "require_last_push_approval": True,
        },
        "restrictions": None,
        "allow_force_pushes": False,
        "allow_deletions": False,
    }


@pytest.mark.parametrize(
    ("mutation", "after", "expected"),
    [
        (
            subprocess.TimeoutExpired(("gh",), 3),
            _observation(protected=True),
            "applied",
        ),
        (
            subprocess.TimeoutExpired(("gh",), 3),
            _observation(protected=False),
            "github_governance_apply_uncertain",
        ),
        (
            BytesCommandResult(1, stderr=b"not exposed"),
            _observation(protected=False),
            "github_governance_apply_failed",
        ),
        (
            BytesCommandResult(0),
            _observation(protected=False),
            "github_governance_apply_incomplete",
        ),
    ],
)
def test_apply_always_decides_from_readback(
    mutation: BytesCommandResult | Exception,
    after: GitHubGovernanceObservation,
    expected: str,
) -> None:
    policy = _policy()
    before = _observation(protected=False)
    manager, _, _ = _manager(
        [before, after],
        [_json_result({"login": "yukiumi13"}), mutation],
    )
    policy_digest, observation_digest = _digests(before, policy)

    if expected == "applied":
        receipt = manager.apply(
            policy,
            expected_policy_digest=policy_digest,
            expected_observation_digest=observation_digest,
        )
        assert receipt.terminal_result == "applied"
    else:
        with pytest.raises(RCPError) as caught:
            manager.apply(
                policy,
                expected_policy_digest=policy_digest,
                expected_observation_digest=observation_digest,
            )
        assert caught.value.code == expected
        assert "not exposed" not in str(caught.value.context)


def test_readback_failure_after_put_is_uncertain() -> None:
    policy = _policy()
    before = _observation(protected=False)
    manager, _, _ = _manager(
        [
            before,
            RCPError(code="github_governance_unavailable", message="unavailable"),
        ],
        [_json_result({"login": "yukiumi13"}), BytesCommandResult(0)],
    )
    policy_digest, observation_digest = _digests(before, policy)

    with pytest.raises(RCPError) as caught:
        manager.apply(
            policy,
            expected_policy_digest=policy_digest,
            expected_observation_digest=observation_digest,
        )

    assert caught.value.code == "github_governance_apply_uncertain"


def test_apply_cli_preview_emits_stable_json_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _policy()
    observation = _observation(protected=False)

    class _Manager:
        def __init__(self, *, observer):
            assert observer is not None

        def preview(self, supplied, *, hostname):
            assert supplied == policy
            assert hostname == "github.com"
            return GitHubGovernanceApplyReceipt(
                terminal_result="preview",
                preview=preview_github_governance_apply(observation, policy),
            )

    monkeypatch.setattr(github_cli, "_accepted_github_policy", lambda project: policy)
    monkeypatch.setattr(github_cli, "GitHubGovernanceClient", object)
    monkeypatch.setattr(github_cli, "GitHubProtectionManager", _Manager)

    result = CliRunner().invoke(
        app,
        ["github", "apply-governance", "--project", ".", "--json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["command"] == "github.apply-governance"
    assert payload["success"] is True
    assert payload["data"]["terminal_result"] == "preview"
    assert payload["data"]["preview"]["mutation_required"] is True


def test_apply_cli_blocks_session_capability_before_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _policy()

    class _Manager:
        def __init__(self, *, observer):
            pass

        def apply(self, *args, **kwargs):
            raise AssertionError("Session capability reached the mutation adapter")

    monkeypatch.setattr(github_cli, "_accepted_github_policy", lambda project: policy)
    monkeypatch.setattr(github_cli, "GitHubGovernanceClient", object)
    monkeypatch.setattr(github_cli, "GitHubProtectionManager", _Manager)

    result = CliRunner().invoke(
        app,
        [
            "github",
            "apply-governance",
            "--apply",
            "--expected-policy-digest",
            "sha256:" + "1" * 64,
            "--expected-observation-digest",
            "sha256:" + "2" * 64,
            "--json",
        ],
        env={
            "RESEARCHCTL_SESSION_ID": "session_20260805T120000Z_" + "a" * 24,
            "RESEARCHCTL_SESSION_TOKEN": "capability",
        },
    )

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["errors"][0]["code"] == "authorization_denied"
