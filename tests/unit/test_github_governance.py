from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, replace
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import researchctl.github_cli as github_cli
from researchctl.adapters._subprocess import CommandResult
from researchctl.adapters.github_governance import (
    GitHubGovernanceClient,
    _ref_pattern_matches,
)
from researchctl.cli import app
from researchctl.errors import RCPError
from researchctl.domain.models import GitHubGovernancePolicy
from researchctl.services.github_governance import (
    GitHubGovernanceObservation,
    audit_github_governance,
)


REPOSITORY = "owner/project"


@dataclass(frozen=True)
class _Call:
    argv: tuple[str, ...]
    env: dict[str, str]
    timeout_seconds: float


class _Runner:
    def __init__(self, responses: dict[str, CommandResult | Exception]) -> None:
        self.responses = responses
        self.calls: list[_Call] = []

    def run(self, argv, *, cwd, env, timeout_seconds):
        assert cwd is None
        self.calls.append(_Call(argv, dict(env), timeout_seconds))
        endpoint = argv[-1]
        response = self.responses[endpoint]
        if isinstance(response, Exception):
            raise response
        return response


def _result(value: object) -> CommandResult:
    return CommandResult(0, stdout=json.dumps(value))


def _classic_protection(
    *,
    checks: tuple[str, ...] = (
        "researchctl/source-tests",
        "researchctl/exact-head",
    ),
    admins: bool = True,
    bypass: bool = False,
) -> dict[str, object]:
    reviews: dict[str, object] = {
        "required_approving_review_count": 1,
        "require_code_owner_reviews": True,
        "dismiss_stale_reviews": True,
        "require_last_push_approval": True,
    }
    if bypass:
        reviews["bypass_pull_request_allowances"] = {
            "apps": [{"slug": "break-glass"}],
            "teams": [],
            "users": [],
        }
    return {
        "required_pull_request_reviews": reviews,
        "required_status_checks": {
            "strict": True,
            "contexts": list(checks),
            "checks": [],
        },
        "allow_force_pushes": {"enabled": False},
        "allow_deletions": {"enabled": False},
        "enforce_admins": {"enabled": admins},
    }


def _client(
    protection: CommandResult,
    *,
    ruleset_summaries: list[dict[str, object]] | None = None,
    rulesets: dict[int, dict[str, object]] | None = None,
    environment: dict[str, str] | None = None,
    max_json_bytes: int = 2 * 1024 * 1024,
) -> tuple[GitHubGovernanceClient, _Runner]:
    responses: dict[str, CommandResult | Exception] = {
        f"/repos/{REPOSITORY}": _result(
            {"full_name": REPOSITORY, "default_branch": "main"}
        ),
        f"/repos/{REPOSITORY}/branches/main/protection": protection,
        f"/repos/{REPOSITORY}/rulesets?includes_parents=true": _result(
            ruleset_summaries or []
        ),
    }
    for ruleset_id, payload in (rulesets or {}).items():
        responses[f"/repos/{REPOSITORY}/rulesets/{ruleset_id}"] = _result(payload)
    runner = _Runner(responses)
    return (
        GitHubGovernanceClient(
            runner=runner,
            environment=environment or {},
            timeout_seconds=3,
            max_json_bytes=max_json_bytes,
        ),
        runner,
    )


def _unprotected() -> CommandResult:
    return CommandResult(1, stderr="gh: Branch not protected (HTTP 404)\n")


def test_fully_protected_classic_branch_is_healthy_with_identity_warning() -> None:
    client, _ = _client(_result(_classic_protection()))

    observation = client.observe(repository=REPOSITORY)
    report = audit_github_governance(observation)

    assert report.healthy is True
    assert observation.protection_sources == ("classic",)
    assert observation.required_status_checks == (
        "researchctl/exact-head",
        "researchctl/source-tests",
    )
    assert [(item.name, item.status) for item in report.checks if item.status != "pass"] == [
        ("proposal-identity", "warn")
    ]


def test_unprotected_branch_fails_required_governance_checks() -> None:
    client, _ = _client(_unprotected())

    report = audit_github_governance(client.observe(repository=REPOSITORY))

    assert report.healthy is False
    assert report.observation.protection_sources == ()
    assert {item.name for item in report.checks if item.status == "error"} == {
        "branch-protection",
        "pull-request-required",
        "approving-review",
        "code-owner-review",
        "stale-review-dismissal",
        "latest-push-approval",
        "required-status-checks",
        "strict-status-checks",
        "force-push-blocked",
        "deletion-blocked",
    }


def test_missing_fixed_status_check_is_reported() -> None:
    client, _ = _client(
        _result(_classic_protection(checks=("researchctl/source-tests",)))
    )

    report = audit_github_governance(client.observe(repository=REPOSITORY))

    check = next(item for item in report.checks if item.name == "required-status-checks")
    assert check.status == "error"
    assert "researchctl/exact-head" in check.message


def test_classic_admin_and_pull_request_bypass_are_visible_warnings() -> None:
    client, _ = _client(_result(_classic_protection(admins=False, bypass=True)))

    report = audit_github_governance(client.observe(repository=REPOSITORY))

    warnings = {item.name: item.message for item in report.checks if item.status == "warn"}
    assert "classic-admin-enforcement" in warnings
    assert "classic-app:break-glass:always" in warnings["ruleset-bypass"]


def test_active_applicable_ruleset_is_normalized_as_merge_protection() -> None:
    ruleset = {
        "id": 41,
        "target": "branch",
        "enforcement": "active",
        "conditions": {
            "ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}
        },
        "bypass_actors": [
            {"actor_id": 7, "actor_type": "Integration", "bypass_mode": "pull_request"}
        ],
        "rules": [
            {
                "type": "pull_request",
                "parameters": {
                    "required_approving_review_count": 1,
                    "require_code_owner_review": True,
                    "dismiss_stale_reviews_on_push": True,
                    "require_last_push_approval": True,
                },
            },
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": True,
                    "required_status_checks": [
                        {"context": "researchctl/source-tests"},
                        {"context": "researchctl/exact-head"},
                    ],
                },
            },
            {"type": "non_fast_forward"},
            {"type": "deletion"},
        ],
    }
    client, _ = _client(
        _unprotected(),
        ruleset_summaries=[{"id": 41}],
        rulesets={41: ruleset},
    )

    report = audit_github_governance(client.observe(repository=REPOSITORY))

    assert report.healthy is True
    assert report.observation.protection_sources == ("ruleset:41",)
    assert report.observation.bypass_actors == ("Integration:7:pull_request",)
    assert "ruleset-bypass" in {
        item.name for item in report.checks if item.status == "warn"
    }


def test_ruleset_excluded_from_target_branch_does_not_count_as_protection() -> None:
    client, _ = _client(
        _unprotected(),
        ruleset_summaries=[{"id": 41}],
        rulesets={
            41: {
                "id": 41,
                "target": "branch",
                "enforcement": "active",
                "conditions": {
                    "ref_name": {
                        "include": ["~ALL"],
                        "exclude": ["refs/heads/main"],
                    }
                },
                "bypass_actors": [],
                "rules": [{"type": "deletion"}],
            }
        },
    )

    observation = client.observe(repository=REPOSITORY)

    assert observation.protection_sources == ()
    assert observation.deletion_blocked is False


def test_ruleset_star_does_not_cross_branch_path_segments() -> None:
    assert _ref_pattern_matches(
        "refs/heads/release/*",
        branch="release/v1",
        default_branch="main",
    ) is True
    assert _ref_pattern_matches(
        "refs/heads/release/*",
        branch="release/team/v1",
        default_branch="main",
    ) is False
    assert _ref_pattern_matches(
        "refs/heads/release/**/*",
        branch="release/team/v1",
        default_branch="main",
    ) is True


@pytest.mark.parametrize(
    ("response", "expected_code"),
    [
        (CommandResult(0, stdout="not-json"), "github_governance_response_invalid"),
        (CommandResult(1, stderr="network unavailable"), "github_governance_observation_failed"),
    ],
)
def test_invalid_and_failed_responses_have_distinct_errors(
    response: CommandResult,
    expected_code: str,
) -> None:
    runner = _Runner({f"/repos/{REPOSITORY}": response})
    client = GitHubGovernanceClient(runner=runner, environment={})

    with pytest.raises(RCPError) as caught:
        client.observe(repository=REPOSITORY)

    assert caught.value.code == expected_code


def test_malformed_boolean_is_rejected_instead_of_becoming_truthy() -> None:
    payload = _classic_protection()
    reviews = payload["required_pull_request_reviews"]
    assert isinstance(reviews, dict)
    reviews["require_code_owner_reviews"] = "false"
    client, _ = _client(_result(payload))

    with pytest.raises(RCPError) as caught:
        client.observe(repository=REPOSITORY)

    assert caught.value.code == "github_governance_response_invalid"


def test_oversized_success_response_is_rejected() -> None:
    runner = _Runner(
        {f"/repos/{REPOSITORY}": CommandResult(0, stdout='{"padding":"12345"}')}
    )
    client = GitHubGovernanceClient(
        runner=runner,
        environment={},
        max_json_bytes=8,
    )

    with pytest.raises(RCPError) as caught:
        client.observe(repository=REPOSITORY)

    assert caught.value.code == "github_governance_response_invalid"


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        (subprocess.TimeoutExpired(("gh",), 3), "github_governance_unavailable"),
        (FileNotFoundError("gh"), "github_cli_not_found"),
    ],
)
def test_gh_timeout_and_missing_executable_are_bounded(
    failure: Exception,
    expected_code: str,
) -> None:
    runner = _Runner({f"/repos/{REPOSITORY}": failure})
    client = GitHubGovernanceClient(runner=runner, environment={})

    with pytest.raises(RCPError) as caught:
        client.observe(repository=REPOSITORY)

    assert caught.value.code == expected_code


def test_only_explicit_transport_environment_is_forwarded() -> None:
    client, runner = _client(
        _result(_classic_protection()),
        environment={
            "PATH": "/usr/bin",
            "HOME": "/home/test",
            "GH_TOKEN": "token",
            "UNRELATED_SECRET": "do-not-forward",
        },
    )

    client.observe(repository=REPOSITORY)

    assert runner.calls
    assert all(call.env == {
        "PATH": "/usr/bin",
        "HOME": "/home/test",
        "GH_TOKEN": "token",
    } for call in runner.calls)


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
        ) if protected else (),
        strict_status_checks=protected,
        force_push_blocked=protected,
        deletion_blocked=protected,
        classic_admins_enforced=True if protected else None,
        bypass_actors=(),
    )


def _policy() -> GitHubGovernancePolicy:
    return GitHubGovernancePolicy.model_validate(
        {
            "repository": REPOSITORY,
            "default_branch": "main",
            "agent_app": {
                "app_id": 12,
                "installation_id": 34,
                "login": "researchctl-agent[bot]",
            },
            "managers": [{"kind": "user", "login": "yukiumi13"}],
        }
    )


def test_accepted_policy_binds_target_gates_and_principals() -> None:
    report = audit_github_governance(_observation(protected=True), policy=_policy())

    assert report.healthy is True
    assert not [item for item in report.checks if item.status == "warn"]
    assert next(
        item for item in report.checks if item.name == "proposal-identity-policy"
    ).status == "pass"


def test_accepted_policy_rejects_repository_and_bypass_drift() -> None:
    observation = replace(
        _observation(protected=True),
        repository="owner/renamed",
        bypass_actors=("Integration:99:always",),
    )

    report = audit_github_governance(observation, policy=_policy())

    assert report.healthy is False
    assert {item.name for item in report.checks if item.status == "error"} >= {
        "policy-repository",
        "bypass-policy",
    }


def test_accepted_policy_rejects_unconstrained_classic_admin_bypass() -> None:
    observation = replace(
        _observation(protected=True),
        classic_admins_enforced=False,
    )

    report = audit_github_governance(observation, policy=_policy())

    assert report.healthy is False
    assert next(
        item for item in report.checks if item.name == "classic-admin-enforcement"
    ).status == "error"


@pytest.mark.parametrize(("protected", "exit_code"), [(True, 0), (False, 2)])
def test_github_doctor_json_envelope_and_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    protected: bool,
    exit_code: int,
) -> None:
    class _Client:
        def observe(self, **kwargs):
            assert kwargs == {
                "repository": REPOSITORY,
                "branch": None,
                "hostname": "github.com",
            }
            return _observation(protected=protected)

    monkeypatch.setattr(github_cli, "GitHubGovernanceClient", _Client)

    result = CliRunner().invoke(
        app,
        ["github", "doctor", "--repository", REPOSITORY, "--json"],
    )

    assert result.exit_code == exit_code
    payload = json.loads(result.stdout)
    assert payload["command"] == "github.doctor"
    assert payload["success"] is protected
    assert payload["data"]["healthy"] is protected
    assert len(payload["warnings"]) == 1
    assert bool(payload["errors"]) is (not protected)


def test_github_doctor_can_derive_target_from_accepted_project_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _policy()

    class _Runtime:
        def discover(self, project):
            assert str(project) == "."
            return SimpleNamespace(policy=SimpleNamespace(github=policy))

    class _Client:
        def observe(self, **kwargs):
            assert kwargs == {
                "repository": REPOSITORY,
                "branch": "main",
                "hostname": "github.com",
            }
            return _observation(protected=True)

    monkeypatch.setattr(github_cli, "ProjectRuntimeService", _Runtime)
    monkeypatch.setattr(github_cli, "GitHubGovernanceClient", _Client)

    result = CliRunner().invoke(app, ["github", "doctor", "--project", ".", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["success"] is True
    assert payload["warnings"] == []
