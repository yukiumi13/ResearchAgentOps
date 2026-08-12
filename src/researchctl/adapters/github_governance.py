from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Mapping
from urllib.parse import quote

from researchctl.adapters._subprocess import CommandRunner, SubprocessCommandRunner
from researchctl.errors import RCPError
from researchctl.services.github_governance import GitHubGovernanceObservation

_REPOSITORY = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,99})/"
    r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,99})$"
)
_HOST = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$"
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


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise RCPError(
            code="github_governance_response_invalid",
            message=f"GitHub returned an invalid {label} object.",
        )
    return value


def _array(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise RCPError(
            code="github_governance_response_invalid",
            message=f"GitHub returned an invalid {label} array.",
        )
    return value


def _enabled(value: object, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, dict) and isinstance(value.get("enabled"), bool):
        return bool(value["enabled"])
    raise RCPError(
        code="github_governance_response_invalid",
        message="GitHub returned an invalid branch protection boolean.",
    )


def _positive_or_zero(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RCPError(
            code="github_governance_response_invalid",
            message=f"GitHub returned an invalid {label} value.",
        )
    return value


def _boolean_field(
    payload: Mapping[str, object],
    name: str,
    *,
    default: bool = False,
) -> bool:
    if name not in payload:
        return default
    value = payload[name]
    if not isinstance(value, bool):
        raise RCPError(
            code="github_governance_response_invalid",
            message=f"GitHub returned an invalid {name} boolean.",
        )
    return value


def _actor_label(value: object) -> str:
    actor = _object(value, "ruleset bypass actor")
    actor_type = actor.get("actor_type")
    actor_id = actor.get("actor_id")
    bypass_mode = actor.get("bypass_mode")
    if (
        not isinstance(actor_type, str)
        or not actor_type
        or isinstance(actor_id, bool)
        or not isinstance(actor_id, int)
        or actor_id < 1
        or not isinstance(bypass_mode, str)
        or not bypass_mode
    ):
        raise RCPError(
            code="github_governance_response_invalid",
            message="GitHub returned an invalid ruleset bypass actor.",
        )
    return f"{actor_type}:{actor_id}:{bypass_mode}"


def _classic_bypass_labels(reviews: dict[str, object]) -> tuple[str, ...]:
    allowances = reviews.get("bypass_pull_request_allowances")
    if allowances is None:
        return ()
    payload = _object(allowances, "classic bypass allowances")
    labels: list[str] = []
    for kind in ("apps", "teams", "users"):
        for value in _array(payload.get(kind, []), f"classic bypass {kind}"):
            actor = _object(value, f"classic bypass {kind} entry")
            identity = actor.get("slug") or actor.get("login") or actor.get("name")
            if not isinstance(identity, str) or not identity:
                actor_id = actor.get("id")
                if isinstance(actor_id, bool) or not isinstance(actor_id, int):
                    raise RCPError(
                        code="github_governance_response_invalid",
                        message="GitHub returned an invalid classic bypass actor.",
                    )
                identity = str(actor_id)
            labels.append(f"classic-{kind[:-1]}:{identity}:always")
    return tuple(labels)


def _ref_pattern_matches(pattern: str, *, branch: str, default_branch: str) -> bool:
    if pattern == "~ALL":
        return True
    if pattern == "~DEFAULT_BRANCH":
        return branch == default_branch
    expression: list[str] = ["^"]
    index = 0
    while index < len(pattern):
        character = pattern[index]
        if character == "*":
            if pattern[index : index + 3] == "**/":
                expression.append("(?:.*/)?")
                index += 3
                continue
            if pattern[index : index + 2] == "**":
                expression.append(".*")
                index += 2
                continue
            expression.append("[^/]*")
        elif character == "?":
            expression.append("[^/]")
        elif character == "[":
            raise RCPError(
                code="github_governance_response_invalid",
                message="GitHub returned an unsupported ruleset ref pattern.",
                remediation="Use literal, *, **, or ? branch rules for bounded audit.",
            )
        else:
            expression.append(re.escape(character))
        index += 1
    expression.append("$")
    return re.fullmatch("".join(expression), f"refs/heads/{branch}") is not None


def _ruleset_applies(
    ruleset: dict[str, object],
    *,
    branch: str,
    default_branch: str,
) -> bool:
    conditions = ruleset.get("conditions")
    if conditions is None:
        return True
    ref_name = _object(_object(conditions, "ruleset conditions").get("ref_name"), "ref")
    includes = _array(ref_name.get("include", []), "ruleset include")
    excludes = _array(ref_name.get("exclude", []), "ruleset exclude")
    if not all(isinstance(pattern, str) and pattern for pattern in (*includes, *excludes)):
        raise RCPError(
            code="github_governance_response_invalid",
            message="GitHub returned an invalid ruleset ref condition.",
        )
    included = not includes or any(
        _ref_pattern_matches(pattern, branch=branch, default_branch=default_branch)
        for pattern in includes
    )
    excluded = any(
        _ref_pattern_matches(pattern, branch=branch, default_branch=default_branch)
        for pattern in excludes
    )
    return included and not excluded


class GitHubGovernanceClient:
    """Read and normalize GitHub merge governance without mutating the repository."""

    def __init__(
        self,
        *,
        runner: CommandRunner | None = None,
        environment: Mapping[str, str] | None = None,
        timeout_seconds: float = 20.0,
        max_json_bytes: int = _MAX_GITHUB_JSON_BYTES,
    ) -> None:
        if timeout_seconds <= 0 or max_json_bytes < 1:
            raise ValueError("GitHub governance limits must be positive")
        source = os.environ if environment is None else environment
        self._environment = {
            key: value
            for key, value in source.items()
            if key in _GH_ENVIRONMENT_KEYS and value
        }
        self._runner = runner or SubprocessCommandRunner()
        self._timeout_seconds = timeout_seconds
        self._max_json_bytes = max_json_bytes

    def observe(
        self,
        *,
        repository: str,
        branch: str | None = None,
        hostname: str = "github.com",
    ) -> GitHubGovernanceObservation:
        if _REPOSITORY.fullmatch(repository) is None or _HOST.fullmatch(hostname) is None:
            raise RCPError(
                code="github_governance_request_invalid",
                message="GitHub governance requires a canonical OWNER/REPOSITORY and host.",
            )
        repository_payload = _object(
            self._get(hostname, f"/repos/{repository}"),
            "repository",
        )
        default_branch = repository_payload.get("default_branch")
        canonical_repository = repository_payload.get("full_name")
        if (
            not isinstance(default_branch, str)
            or not default_branch
            or not isinstance(canonical_repository, str)
            or _REPOSITORY.fullmatch(canonical_repository) is None
        ):
            raise RCPError(
                code="github_governance_response_invalid",
                message="GitHub returned no canonical repository or default branch.",
            )
        target_branch = branch or default_branch
        if not target_branch or any(character in target_branch for character in "\x00\r\n"):
            raise RCPError(
                code="github_governance_request_invalid",
                message="GitHub governance requires a valid target branch.",
            )
        encoded_branch = quote(target_branch, safe="")
        protection = self._get(
            hostname,
            f"/repos/{repository}/branches/{encoded_branch}/protection",
            absent_on_404=True,
        )
        summaries = _array(
            self._get(hostname, f"/repos/{repository}/rulesets?includes_parents=true"),
            "rulesets",
        )
        rulesets: list[dict[str, object]] = []
        for value in summaries:
            summary = _object(value, "ruleset summary")
            ruleset_id = summary.get("id")
            if isinstance(ruleset_id, bool) or not isinstance(ruleset_id, int) or ruleset_id < 1:
                raise RCPError(
                    code="github_governance_response_invalid",
                    message="GitHub returned an invalid ruleset identity.",
                )
            detail = _object(
                self._get(hostname, f"/repos/{repository}/rulesets/{ruleset_id}"),
                "ruleset",
            )
            if (
                detail.get("target") == "branch"
                and detail.get("enforcement") == "active"
                and _ruleset_applies(
                    detail,
                    branch=target_branch,
                    default_branch=default_branch,
                )
            ):
                rulesets.append(detail)
        return self._normalize(
            repository=canonical_repository,
            default_branch=default_branch,
            branch=target_branch,
            protection=protection,
            rulesets=rulesets,
        )

    def _get(
        self,
        hostname: str,
        endpoint: str,
        *,
        absent_on_404: bool = False,
    ) -> object | None:
        try:
            result = self._runner.run(
                ("gh", "api", "--hostname", hostname, "--method", "GET", endpoint),
                cwd=None,
                env=self._environment,
                timeout_seconds=self._timeout_seconds,
            )
        except (subprocess.TimeoutExpired, TimeoutError) as error:
            raise RCPError(
                code="github_governance_unavailable",
                message="GitHub governance observation timed out.",
                remediation="Retry the read-only audit after GitHub connectivity recovers.",
            ) from error
        except FileNotFoundError as error:
            raise RCPError(
                code="github_cli_not_found",
                message="The gh executable was not found for GitHub governance audit.",
            ) from error
        if result.returncode == 0:
            return self._json(result.stdout)
        if absent_on_404 and self._is_not_found(result.stdout, result.stderr):
            return None
        raise RCPError(
            code="github_governance_observation_failed",
            message="GitHub governance state could not be observed.",
            remediation="Check gh authentication and repository administration visibility.",
            context={"endpoint": endpoint, "returncode": result.returncode},
        )

    def _is_not_found(self, stdout: str, stderr: str) -> bool:
        for content in (stdout, stderr):
            if len(content.encode("utf-8")) > self._max_json_bytes:
                continue
            try:
                payload = json.loads(content)
            except (json.JSONDecodeError, RecursionError, UnicodeError):
                continue
            if isinstance(payload, dict) and payload.get("status") in {404, "404"}:
                return True
        bounded_stderr = stderr[: min(len(stderr), self._max_json_bytes)]
        return re.search(r"\(HTTP 404\)(?:\s|$)", bounded_stderr) is not None

    def _json(self, content: str) -> object:
        encoded = content.encode("utf-8")
        if len(encoded) > self._max_json_bytes:
            raise RCPError(
                code="github_governance_response_invalid",
                message="GitHub governance response exceeds the configured bound.",
            )
        try:
            return json.loads(content)
        except (json.JSONDecodeError, RecursionError, UnicodeError) as error:
            raise RCPError(
                code="github_governance_response_invalid",
                message="GitHub returned invalid bounded JSON for governance audit.",
            ) from error

    @staticmethod
    def _normalize(
        *,
        repository: str,
        default_branch: str,
        branch: str,
        protection: object | None,
        rulesets: list[dict[str, object]],
    ) -> GitHubGovernanceObservation:
        sources: list[str] = []
        pull_request_required = False
        approvals = 0
        code_owner = False
        dismiss_stale = False
        last_push = False
        status_checks: set[str] = set()
        strict_checks = False
        force_push_blocked = False
        deletion_blocked = False
        admins_enforced: bool | None = None
        bypass: list[str] = []

        if protection is not None:
            classic = _object(protection, "classic branch protection")
            sources.append("classic")
            reviews_value = classic.get("required_pull_request_reviews")
            if reviews_value is not None:
                reviews = _object(reviews_value, "required pull-request reviews")
                pull_request_required = True
                approvals = max(
                    approvals,
                    _positive_or_zero(
                        reviews.get("required_approving_review_count", 0),
                        "required approval count",
                    ),
                )
                code_owner = code_owner or _boolean_field(
                    reviews,
                    "require_code_owner_reviews",
                )
                dismiss_stale = dismiss_stale or _boolean_field(
                    reviews,
                    "dismiss_stale_reviews",
                )
                last_push = last_push or _boolean_field(
                    reviews,
                    "require_last_push_approval",
                )
                bypass.extend(_classic_bypass_labels(reviews))
            statuses_value = classic.get("required_status_checks")
            if statuses_value is not None:
                statuses = _object(statuses_value, "required status checks")
                strict_checks = strict_checks or _boolean_field(statuses, "strict")
                for context in _array(statuses.get("contexts", []), "status contexts"):
                    if not isinstance(context, str) or not context:
                        raise RCPError(
                            code="github_governance_response_invalid",
                            message="GitHub returned an invalid status check context.",
                        )
                    status_checks.add(context)
                for value in _array(statuses.get("checks", []), "status checks"):
                    check = _object(value, "status check")
                    context = check.get("context")
                    if not isinstance(context, str) or not context:
                        raise RCPError(
                            code="github_governance_response_invalid",
                            message="GitHub returned an invalid status check identity.",
                        )
                    status_checks.add(context)
            force_push_blocked = not _enabled(classic.get("allow_force_pushes"))
            deletion_blocked = not _enabled(classic.get("allow_deletions"))
            admins_enforced = _enabled(classic.get("enforce_admins"))

        for ruleset in rulesets:
            ruleset_id = _positive_or_zero(ruleset.get("id"), "ruleset ID")
            if ruleset_id < 1:
                raise RCPError(
                    code="github_governance_response_invalid",
                    message="GitHub returned an invalid ruleset ID value.",
                )
            sources.append(f"ruleset:{ruleset_id}")
            bypass.extend(
                _actor_label(value)
                for value in _array(ruleset.get("bypass_actors", []), "ruleset bypass")
            )
            for value in _array(ruleset.get("rules", []), "ruleset rules"):
                rule = _object(value, "ruleset rule")
                rule_type = rule.get("type")
                if rule_type == "pull_request":
                    parameters = _object(rule.get("parameters"), "pull-request parameters")
                    pull_request_required = True
                    approvals = max(
                        approvals,
                        _positive_or_zero(
                            parameters.get("required_approving_review_count", 0),
                            "ruleset approval count",
                        ),
                    )
                    code_owner = code_owner or _boolean_field(
                        parameters,
                        "require_code_owner_review",
                    )
                    dismiss_stale = dismiss_stale or _boolean_field(
                        parameters,
                        "dismiss_stale_reviews_on_push",
                    )
                    last_push = last_push or _boolean_field(
                        parameters,
                        "require_last_push_approval",
                    )
                elif rule_type == "required_status_checks":
                    parameters = _object(rule.get("parameters"), "status-check parameters")
                    strict_checks = strict_checks or _boolean_field(
                        parameters,
                        "strict_required_status_checks_policy",
                    )
                    for item in _array(
                        parameters.get("required_status_checks", []),
                        "ruleset status checks",
                    ):
                        context = _object(item, "ruleset status check").get("context")
                        if not isinstance(context, str) or not context:
                            raise RCPError(
                                code="github_governance_response_invalid",
                                message="GitHub returned an invalid ruleset status check.",
                            )
                        status_checks.add(context)
                elif rule_type == "non_fast_forward":
                    force_push_blocked = True
                elif rule_type == "deletion":
                    deletion_blocked = True

        return GitHubGovernanceObservation(
            repository=repository,
            default_branch=default_branch,
            branch=branch,
            protection_sources=tuple(sorted(set(sources))),
            pull_request_required=pull_request_required,
            required_approvals=approvals,
            code_owner_review_required=code_owner,
            dismiss_stale_reviews=dismiss_stale,
            last_push_approval_required=last_push,
            required_status_checks=tuple(sorted(status_checks)),
            strict_status_checks=strict_checks,
            force_push_blocked=force_push_blocked,
            deletion_blocked=deletion_blocked,
            classic_admins_enforced=admins_enforced,
            bypass_actors=tuple(sorted(set(bypass))),
        )
