from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from researchctl.domain.models import GitHubGovernancePolicy
from researchctl.serialization import canonical_digest

CheckStatus = Literal["pass", "warn", "error"]
REQUIRED_GITHUB_CHECKS = (
    "researchctl/source-tests",
    "researchctl/exact-head",
)


@dataclass(frozen=True, slots=True)
class GitHubGovernanceObservation:
    repository: str
    default_branch: str
    branch: str
    protection_sources: tuple[str, ...]
    pull_request_required: bool
    required_approvals: int
    code_owner_review_required: bool
    dismiss_stale_reviews: bool
    last_push_approval_required: bool
    required_status_checks: tuple[str, ...]
    strict_status_checks: bool
    force_push_blocked: bool
    deletion_blocked: bool
    classic_admins_enforced: bool | None
    bypass_actors: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "repository": self.repository,
            "default_branch": self.default_branch,
            "branch": self.branch,
            "protection_sources": list(self.protection_sources),
            "pull_request_required": self.pull_request_required,
            "required_approvals": self.required_approvals,
            "code_owner_review_required": self.code_owner_review_required,
            "dismiss_stale_reviews": self.dismiss_stale_reviews,
            "last_push_approval_required": self.last_push_approval_required,
            "required_status_checks": list(self.required_status_checks),
            "strict_status_checks": self.strict_status_checks,
            "force_push_blocked": self.force_push_blocked,
            "deletion_blocked": self.deletion_blocked,
            "classic_admins_enforced": self.classic_admins_enforced,
            "bypass_actors": list(self.bypass_actors),
        }


@dataclass(frozen=True, slots=True)
class GitHubGovernanceCheck:
    name: str
    status: CheckStatus
    message: str
    remediation: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "name": self.name,
            "status": self.status,
            "message": self.message,
            "remediation": self.remediation,
        }


@dataclass(frozen=True, slots=True)
class GitHubGovernanceReport:
    observation: GitHubGovernanceObservation
    checks: tuple[GitHubGovernanceCheck, ...]

    @property
    def healthy(self) -> bool:
        return not any(check.status == "error" for check in self.checks)

    def as_dict(self) -> dict[str, object]:
        return {
            "healthy": self.healthy,
            "observation": self.observation.as_dict(),
            "checks": [check.as_dict() for check in self.checks],
        }


@dataclass(frozen=True, slots=True)
class GitHubGovernanceApplyPreview:
    policy_digest: str
    observation_digest: str
    report: GitHubGovernanceReport
    required_changes: tuple[str, ...]

    @property
    def mutation_required(self) -> bool:
        return not self.report.healthy

    def as_dict(self) -> dict[str, object]:
        return {
            "policy_digest": self.policy_digest,
            "observation_digest": self.observation_digest,
            "mutation_required": self.mutation_required,
            "required_changes": list(self.required_changes),
            "report": self.report.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class GitHubGovernanceApplyReceipt:
    terminal_result: Literal["preview", "no_change", "applied"]
    preview: GitHubGovernanceApplyPreview
    manager_login: str | None = None
    final_observation_digest: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "terminal_result": self.terminal_result,
            "preview": self.preview.as_dict(),
            "manager_login": self.manager_login,
            "final_observation_digest": self.final_observation_digest,
        }


def _boolean_check(
    *,
    name: str,
    passed: bool,
    pass_message: str,
    error_message: str,
    remediation: str,
) -> GitHubGovernanceCheck:
    if passed:
        return GitHubGovernanceCheck(name=name, status="pass", message=pass_message)
    return GitHubGovernanceCheck(
        name=name,
        status="error",
        message=error_message,
        remediation=remediation,
    )


def audit_github_governance(
    observation: GitHubGovernanceObservation,
    *,
    policy: GitHubGovernancePolicy | None = None,
    required_checks: tuple[str, ...] | None = None,
) -> GitHubGovernanceReport:
    configured_checks = (
        required_checks
        if required_checks is not None
        else (
            policy.required_status_checks
            if policy is not None
            else REQUIRED_GITHUB_CHECKS
        )
    )
    expected_checks = set(configured_checks)
    observed_checks = set(observation.required_status_checks)
    missing_checks = tuple(sorted(expected_checks - observed_checks))
    checks: list[GitHubGovernanceCheck] = []
    if policy is not None:
        checks.extend(
            [
                _boolean_check(
                    name="policy-repository",
                    passed=observation.repository.lower() == policy.repository.lower(),
                    pass_message="Observed repository matches the accepted GitHub policy.",
                    error_message=(
                        "Observed repository does not match accepted policy: "
                        f"expected {policy.repository}, observed {observation.repository}."
                    ),
                    remediation="Audit the repository bound by accepted ProjectPolicy.",
                ),
                _boolean_check(
                    name="policy-branch",
                    passed=observation.branch == policy.default_branch,
                    pass_message="Observed branch matches the accepted GitHub policy.",
                    error_message=(
                        "Observed branch does not match accepted policy: "
                        f"expected {policy.default_branch}, observed {observation.branch}."
                    ),
                    remediation="Audit the protected default branch bound by ProjectPolicy.",
                ),
            ]
        )
    checks.extend([
        _boolean_check(
            name="branch-protection",
            passed=bool(observation.protection_sources),
            pass_message=(
                "The target branch has an active classic protection or applicable ruleset."
            ),
            error_message="The target branch has no active merge protection source.",
            remediation="Install active protection or a branch ruleset for the default branch.",
        ),
        _boolean_check(
            name="pull-request-required",
            passed=observation.pull_request_required,
            pass_message="Changes must enter the target branch through a pull request.",
            error_message="The target branch does not require a pull request before update.",
            remediation="Require a pull request before merging or updating the branch.",
        ),
        _boolean_check(
            name="approving-review",
            passed=observation.required_approvals
            >= (policy.required_approvals if policy is not None else 1),
            pass_message=(
                f"The merge gate requires {observation.required_approvals} approving review(s)."
            ),
            error_message="The merge gate does not require an approving review.",
            remediation="Require at least one approving review.",
        ),
        _boolean_check(
            name="code-owner-review",
            passed=observation.code_owner_review_required,
            pass_message="A CODEOWNER review is required.",
            error_message="CODEOWNER review is not required.",
            remediation="Require review from Code Owners and protect the CODEOWNERS file.",
        ),
        _boolean_check(
            name="stale-review-dismissal",
            passed=observation.dismiss_stale_reviews,
            pass_message="Approvals are dismissed when the proposal head changes.",
            error_message="An approval can remain current after the proposal head changes.",
            remediation="Dismiss stale pull-request approvals on new commits.",
        ),
        _boolean_check(
            name="latest-push-approval",
            passed=observation.last_push_approval_required,
            pass_message="The most recent reviewable push requires approval.",
            error_message="The most recent reviewable push does not require approval.",
            remediation="Require approval of the most recent reviewable push.",
        ),
        _boolean_check(
            name="required-status-checks",
            passed=not missing_checks,
            pass_message="All fixed researchctl status checks are required.",
            error_message=(
                "The merge gate is missing required status checks: "
                + ", ".join(missing_checks)
            ),
            remediation="Require researchctl/source-tests and researchctl/exact-head.",
        ),
        _boolean_check(
            name="strict-status-checks",
            passed=observation.strict_status_checks,
            pass_message="The proposal must be current with its protected base.",
            error_message="Required checks do not require a current protected base.",
            remediation="Require branches to be up to date before merging.",
        ),
        _boolean_check(
            name="force-push-blocked",
            passed=observation.force_push_blocked,
            pass_message="Force pushes are blocked on the target branch.",
            error_message="Force pushes are not blocked on the target branch.",
            remediation="Block force pushes on the protected default branch.",
        ),
        _boolean_check(
            name="deletion-blocked",
            passed=observation.deletion_blocked,
            pass_message="Deletion is blocked for the target branch.",
            error_message="Deletion is not blocked for the target branch.",
            remediation="Block deletion of the protected default branch.",
        ),
    ])
    if observation.classic_admins_enforced is False:
        ruleset_constrains_admins = any(
            source.startswith("ruleset:")
            for source in observation.protection_sources
        )
        checks.append(
            GitHubGovernanceCheck(
                name="classic-admin-enforcement",
                status=(
                    "warn"
                    if policy is None or ruleset_constrains_admins
                    else "error"
                ),
                message=(
                    "Classic branch protection permits administrator bypass."
                    if policy is None or ruleset_constrains_admins
                    else "Accepted policy is not enforced for repository administrators."
                ),
                remediation="Enforce classic protection for administrators or audit break-glass.",
            )
        )
    expected_bypass = (
        {
            f"{item.actor_type}:{item.actor_id}:{item.bypass_mode}"
            for item in policy.bypass_actors
        }
        if policy is not None
        else set()
    )
    observed_bypass = set(observation.bypass_actors)
    if policy is not None and observed_bypass != expected_bypass:
        checks.append(
            GitHubGovernanceCheck(
                name="bypass-policy",
                status="error",
                message=(
                    "Observed bypass actors differ from accepted policy: expected "
                    f"{', '.join(sorted(expected_bypass)) or 'none'}; observed "
                    f"{', '.join(sorted(observed_bypass)) or 'none'}."
                ),
                remediation="Remove unexpected bypass or accept an explicit audited policy.",
            )
        )
    elif observation.bypass_actors:
        checks.append(
            GitHubGovernanceCheck(
                name="ruleset-bypass",
                status="warn",
                message=(
                    "Applicable rulesets declare bypass actors: "
                    + ", ".join(observation.bypass_actors)
                ),
                remediation="Remove bypass actors or document an audited break-glass policy.",
            )
        )
    if policy is None:
        checks.append(
            GitHubGovernanceCheck(
                name="proposal-identity",
                status="warn",
                message=(
                    "No accepted ProjectPolicy was supplied to bind the Agent App "
                    "author and authorized human Managers."
                ),
                remediation="Configure protected GitHub governance policy and audit by project.",
            )
        )
    else:
        checks.append(
            GitHubGovernanceCheck(
                name="proposal-identity-policy",
                status="pass",
                message=(
                    f"Accepted policy binds proposal author {policy.agent_app.login} "
                    f"and {len(policy.managers)} human Manager principal(s)."
                ),
            )
        )
    return GitHubGovernanceReport(observation=observation, checks=tuple(checks))


def preview_github_governance_apply(
    observation: GitHubGovernanceObservation,
    policy: GitHubGovernancePolicy,
) -> GitHubGovernanceApplyPreview:
    report = audit_github_governance(observation, policy=policy)
    return GitHubGovernanceApplyPreview(
        policy_digest=canonical_digest(policy),
        observation_digest=canonical_digest(observation.as_dict()),
        report=report,
        required_changes=tuple(
            check.name for check in report.checks if check.status == "error"
        ),
    )
