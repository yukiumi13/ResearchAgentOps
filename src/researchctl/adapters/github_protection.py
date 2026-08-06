from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

from researchctl.domain.models import (
    GitHubGovernancePolicy,
    GitHubTeamManagerPrincipal,
    GitHubUserManagerPrincipal,
)
from researchctl.errors import RCPError
from researchctl.services.github_governance import (
    GitHubGovernanceApplyReceipt,
    GitHubGovernanceObservation,
    preview_github_governance_apply,
)


_MAX_GITHUB_JSON_BYTES = 2 * 1024 * 1024
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


@dataclass(frozen=True, slots=True)
class BytesCommandResult:
    returncode: int
    stdout: bytes = b""
    stderr: bytes = b""


class BytesCommandRunner(Protocol):
    def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path | None,
        env: Mapping[str, str] | None,
        timeout_seconds: float,
        input_data: bytes,
    ) -> BytesCommandResult: ...


class SubprocessBytesCommandRunner:
    def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path | None,
        env: Mapping[str, str] | None,
        timeout_seconds: float,
        input_data: bytes,
    ) -> BytesCommandResult:
        completed = subprocess.run(
            list(argv),
            check=False,
            capture_output=True,
            cwd=str(cwd) if cwd is not None else None,
            env=dict(env) if env is not None else None,
            input=input_data,
            shell=False,
            timeout=timeout_seconds,
        )
        return BytesCommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


class GitHubGovernanceObserver(Protocol):
    def observe(
        self,
        *,
        repository: str,
        branch: str | None = None,
        hostname: str = "github.com",
    ) -> GitHubGovernanceObservation: ...


class GitHubProtectionManager:
    """Apply one digest-bound classic protection policy and verify it by read-back."""

    def __init__(
        self,
        *,
        observer: GitHubGovernanceObserver,
        runner: BytesCommandRunner | None = None,
        environment: Mapping[str, str] | None = None,
        timeout_seconds: float = 20.0,
        max_json_bytes: int = _MAX_GITHUB_JSON_BYTES,
    ) -> None:
        if timeout_seconds <= 0 or max_json_bytes < 1:
            raise ValueError("GitHub protection limits must be positive")
        source = os.environ if environment is None else environment
        self._environment = {
            key: value
            for key, value in source.items()
            if key in _GH_ENVIRONMENT_KEYS and value
        }
        self._observer = observer
        self._runner = runner or SubprocessBytesCommandRunner()
        self._timeout_seconds = timeout_seconds
        self._max_json_bytes = max_json_bytes

    def preview(
        self,
        policy: GitHubGovernancePolicy,
        *,
        hostname: str = "github.com",
    ) -> GitHubGovernanceApplyReceipt:
        observation = self._observe(policy, hostname=hostname)
        return GitHubGovernanceApplyReceipt(
            terminal_result="preview",
            preview=preview_github_governance_apply(observation, policy),
        )

    def apply(
        self,
        policy: GitHubGovernancePolicy,
        *,
        expected_policy_digest: str,
        expected_observation_digest: str,
        hostname: str = "github.com",
    ) -> GitHubGovernanceApplyReceipt:
        observation = self._observe(policy, hostname=hostname)
        preview = preview_github_governance_apply(observation, policy)
        if expected_policy_digest != preview.policy_digest:
            raise RCPError(
                code="github_governance_policy_changed",
                message="Accepted GitHub governance policy changed after preview.",
                remediation="Run apply-governance without --apply and review the new digest.",
                context={
                    "expected_policy_digest": expected_policy_digest,
                    "observed_policy_digest": preview.policy_digest,
                },
            )
        if expected_observation_digest != preview.observation_digest:
            raise RCPError(
                code="github_governance_observation_changed",
                message="GitHub governance state changed after preview.",
                remediation="Run apply-governance without --apply and review the new state.",
                context={
                    "expected_observation_digest": expected_observation_digest,
                    "observed_observation_digest": preview.observation_digest,
                },
            )

        manager_login = self._require_manager(policy, hostname=hostname)
        if not preview.mutation_required:
            return GitHubGovernanceApplyReceipt(
                terminal_result="no_change",
                preview=preview,
                manager_login=manager_login,
                final_observation_digest=preview.observation_digest,
            )

        rulesets = tuple(
            source
            for source in observation.protection_sources
            if source.startswith("ruleset:")
        )
        if rulesets:
            raise RCPError(
                code="github_governance_ruleset_conflict",
                message=(
                    "Applicable active rulesets already govern the protected branch; "
                    "classic protection apply is not a second authority."
                ),
                remediation="Manage the existing ruleset explicitly or remove it under review.",
                context={"protection_sources": list(rulesets)},
            )
        if policy.bypass_actors:
            raise RCPError(
                code="github_governance_bypass_unsupported",
                message=(
                    "The accepted bypass policy cannot be represented safely by the "
                    "bounded classic protection writer."
                ),
                remediation="Use an explicitly reviewed ruleset workflow for bypass actors.",
            )

        payload = self._classic_payload(policy)
        mutation_error: Exception | None = None
        mutation_returncode: int | None = None
        try:
            result = self._run(
                hostname,
                (
                    "--method",
                    "PUT",
                    "--input",
                    "-",
                    self._protection_endpoint(policy),
                ),
                input_data=payload,
            )
            mutation_returncode = result.returncode
        except (subprocess.TimeoutExpired, TimeoutError) as error:
            mutation_error = error
        except FileNotFoundError as error:
            raise RCPError(
                code="github_cli_not_found",
                message="The gh executable was not found for GitHub governance apply.",
            ) from error

        try:
            final_observation = self._observe(policy, hostname=hostname)
            final_preview = preview_github_governance_apply(final_observation, policy)
        except RCPError as error:
            raise RCPError(
                code="github_governance_apply_uncertain",
                message="GitHub protection was invoked but its final state could not be read.",
                remediation="Rerun preview; it observes current state before any retry.",
                context={"mutation_returncode": mutation_returncode},
            ) from error

        if final_preview.report.healthy:
            return GitHubGovernanceApplyReceipt(
                terminal_result="applied",
                preview=preview,
                manager_login=manager_login,
                final_observation_digest=final_preview.observation_digest,
            )
        if mutation_error is not None:
            raise RCPError(
                code="github_governance_apply_uncertain",
                message="GitHub protection timed out and read-back is not policy-compliant.",
                remediation="Rerun preview; it observes current state before any retry.",
                context={"remaining_changes": list(final_preview.required_changes)},
            ) from mutation_error
        if mutation_returncode != 0:
            raise RCPError(
                code="github_governance_apply_failed",
                message="GitHub rejected the protection update and read-back is unchanged.",
                remediation="Check Manager administration access, then rerun preview.",
                context={
                    "returncode": mutation_returncode,
                    "remaining_changes": list(final_preview.required_changes),
                },
            )
        raise RCPError(
            code="github_governance_apply_incomplete",
            message="GitHub accepted the update call but read-back is not policy-compliant.",
            remediation="Inspect repository rules, then rerun preview before another apply.",
            context={"remaining_changes": list(final_preview.required_changes)},
        )

    def _observe(
        self,
        policy: GitHubGovernancePolicy,
        *,
        hostname: str,
    ) -> GitHubGovernanceObservation:
        return self._observer.observe(
            repository=policy.repository,
            branch=policy.default_branch,
            hostname=hostname,
        )

    def _require_manager(
        self,
        policy: GitHubGovernancePolicy,
        *,
        hostname: str,
    ) -> str:
        identity = self._get_json(hostname, "/user", label="authenticated user")
        if not isinstance(identity, dict) or not isinstance(identity.get("login"), str):
            raise RCPError(
                code="github_governance_auth_invalid",
                message="GitHub returned no bounded authenticated user identity.",
            )
        login = identity["login"]
        if not login or login.casefold() == policy.agent_app.login.casefold():
            raise RCPError(
                code="github_governance_manager_required",
                message="The Agent App cannot apply or accept GitHub governance.",
                remediation="Authenticate gh as an allowed human Manager.",
            )
        direct_users = {
            manager.login.casefold()
            for manager in policy.managers
            if isinstance(manager, GitHubUserManagerPrincipal)
        }
        if login.casefold() in direct_users:
            return login
        for manager in policy.managers:
            if not isinstance(manager, GitHubTeamManagerPrincipal):
                continue
            endpoint = (
                f"/orgs/{quote(manager.organization, safe='')}/teams/"
                f"{quote(manager.slug, safe='')}/memberships/{quote(login, safe='')}"
            )
            membership = self._get_json(
                hostname,
                endpoint,
                label="Manager team membership",
                absent_on_failure=True,
            )
            if isinstance(membership, dict) and membership.get("state") == "active":
                return login
        raise RCPError(
            code="github_governance_manager_required",
            message="The authenticated GitHub user is not an allowed human Manager.",
            remediation="Authenticate gh as a configured Manager user or active team member.",
            context={"authenticated_login": login},
        )

    def _get_json(
        self,
        hostname: str,
        endpoint: str,
        *,
        label: str,
        absent_on_failure: bool = False,
    ) -> object | None:
        try:
            result = self._run(
                hostname,
                ("--method", "GET", endpoint),
                input_data=b"",
            )
        except (subprocess.TimeoutExpired, TimeoutError) as error:
            raise RCPError(
                code="github_governance_auth_unavailable",
                message="GitHub Manager identity verification timed out.",
            ) from error
        except FileNotFoundError as error:
            raise RCPError(
                code="github_cli_not_found",
                message="The gh executable was not found for Manager verification.",
            ) from error
        if result.returncode != 0:
            if absent_on_failure:
                return None
            raise RCPError(
                code="github_governance_auth_failed",
                message="The authenticated GitHub identity could not be verified.",
                remediation="Authenticate gh as an allowed human Manager.",
                context={"returncode": result.returncode},
            )
        if len(result.stdout) > self._max_json_bytes:
            raise RCPError(
                code="github_governance_auth_invalid",
                message=f"GitHub returned an oversized {label} response.",
            )
        try:
            return json.loads(result.stdout)
        except (json.JSONDecodeError, RecursionError, UnicodeError) as error:
            raise RCPError(
                code="github_governance_auth_invalid",
                message=f"GitHub returned invalid bounded JSON for {label}.",
            ) from error

    def _run(
        self,
        hostname: str,
        arguments: tuple[str, ...],
        *,
        input_data: bytes,
    ) -> BytesCommandResult:
        return self._runner.run(
            ("gh", "api", "--hostname", hostname, *arguments),
            cwd=None,
            env=self._environment,
            timeout_seconds=self._timeout_seconds,
            input_data=input_data,
        )

    @staticmethod
    def _protection_endpoint(policy: GitHubGovernancePolicy) -> str:
        return (
            f"/repos/{policy.repository}/branches/"
            f"{quote(policy.default_branch, safe='')}/protection"
        )

    @staticmethod
    def _classic_payload(policy: GitHubGovernancePolicy) -> bytes:
        payload = {
            "required_status_checks": {
                "strict": policy.strict_status_checks,
                "contexts": list(policy.required_status_checks),
            },
            "enforce_admins": True,
            "required_pull_request_reviews": {
                "dismiss_stale_reviews": policy.dismiss_stale_reviews,
                "require_code_owner_reviews": policy.require_code_owner_review,
                "required_approving_review_count": policy.required_approvals,
                "require_last_push_approval": policy.require_last_push_approval,
            },
            "restrictions": None,
            "allow_force_pushes": not policy.block_force_pushes,
            "allow_deletions": not policy.block_deletions,
        }
        return json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
