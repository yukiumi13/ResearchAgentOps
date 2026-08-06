from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from researchctl.adapters._subprocess import (
    CommandResult,
    CommandRunner,
    SubprocessCommandRunner,
)
from researchctl.domain.models import GitHubGovernancePolicy
from researchctl.errors import RCPError
from researchctl.services.submission_delivery import (
    SubmissionBranchDelivery,
    SubmissionPullRequestReceipt,
)


_GIT_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SUBMISSION_BRANCH = re.compile(
    r"^research/submission/(submission_\d{8}T\d{6}Z_[0-9a-f]{24})$"
)
_REPOSITORY = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,99})/"
    r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,99})$"
)
_HOST = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$"
)
_REMOTE_NAME = "origin"
_MAX_GITHUB_JSON_BYTES = 2 * 1024 * 1024
_GIT_ENVIRONMENT_KEYS = frozenset({"HOME", "PATH", "SSH_AUTH_SOCK"})
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
class GitHubRepositoryIdentity:
    host: str
    repository: str

    @property
    def owner(self) -> str:
        return self.repository.split("/", maxsplit=1)[0]


@dataclass(frozen=True, slots=True)
class GhSubmissionCommandResult:
    returncode: int
    stdout: bytes = b""


class GhSubmissionCommandRunner(Protocol):
    def run(
        self,
        argv: tuple[str, ...],
        *,
        env: Mapping[str, str],
        input_bytes: bytes | None,
        timeout_seconds: float,
    ) -> GhSubmissionCommandResult: ...


class SubprocessGhSubmissionCommandRunner:
    def run(
        self,
        argv: tuple[str, ...],
        *,
        env: Mapping[str, str],
        input_bytes: bytes | None,
        timeout_seconds: float,
    ) -> GhSubmissionCommandResult:
        completed = subprocess.run(
            argv,
            check=False,
            env=dict(env),
            shell=False,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout_seconds,
        )
        return GhSubmissionCommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
        )


class GitHubSubmissionDelivery:
    """Push and open one derived same-repository Submission proposal."""

    def __init__(
        self,
        *,
        accepted_remote_url: str | None,
        governance: GitHubGovernancePolicy | None,
        git_runner: CommandRunner | None = None,
        gh_runner: GhSubmissionCommandRunner | None = None,
        environment: Mapping[str, str] | None = None,
        timeout_seconds: float = 20.0,
        max_json_bytes: int = _MAX_GITHUB_JSON_BYTES,
    ) -> None:
        if timeout_seconds <= 0 or max_json_bytes < 1:
            raise ValueError("Submission delivery limits must be positive")
        source = os.environ if environment is None else environment
        self._git_environment = {
            key: value
            for key, value in source.items()
            if key in _GIT_ENVIRONMENT_KEYS and value
        }
        self._git_environment.update(
            {
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_TERMINAL_PROMPT": "0",
            }
        )
        self._gh_environment = {
            key: value
            for key, value in source.items()
            if key in _GH_ENVIRONMENT_KEYS and value
        }
        self._accepted_remote_url = accepted_remote_url
        self._governance = governance
        self._git_runner = git_runner or SubprocessCommandRunner()
        self._gh_runner = gh_runner or SubprocessGhSubmissionCommandRunner()
        self._timeout_seconds = timeout_seconds
        self._max_json_bytes = max_json_bytes

    def push_exact(
        self,
        *,
        repository_root: Path,
        branch: str,
        commit: str,
    ) -> SubmissionBranchDelivery:
        self._require_branch_commit(branch, commit)
        root = repository_root.resolve()
        self._require_remote_identity(root)
        before = self._remote_head(root, branch)
        if before is not None and before != commit:
            raise RCPError(
                code="submission_remote_head_conflict",
                message="Submission remote branch identifies a different commit.",
                context={"branch": branch},
            )
        if before == commit:
            return SubmissionBranchDelivery(
                remote=_REMOTE_NAME,
                branch=branch,
                ref=f"refs/heads/{branch}",
                commit=commit,
                pushed=False,
            )

        push_result: CommandResult | None = None
        push_uncertain = False
        try:
            push_result = self._git(
                root,
                "push",
                "--porcelain",
                _REMOTE_NAME,
                f"{commit}:refs/heads/{branch}",
            )
        except (subprocess.TimeoutExpired, TimeoutError):
            push_uncertain = True
        except FileNotFoundError as error:
            raise RCPError(
                code="git_not_found",
                message="git executable was not found for Submission delivery.",
            ) from error

        observed = self._remote_head(root, branch)
        if observed == commit:
            return SubmissionBranchDelivery(
                remote=_REMOTE_NAME,
                branch=branch,
                ref=f"refs/heads/{branch}",
                commit=commit,
                pushed=True,
            )
        if observed is not None:
            raise RCPError(
                code="submission_remote_head_conflict",
                message="Submission remote branch identifies a different commit.",
                context={"branch": branch},
            )
        if push_uncertain:
            self._uncertain("branch_push")
        assert push_result is not None
        raise RCPError(
            code="submission_branch_push_failed",
            message="The exact Submission branch was not present after push.",
            context={"returncode": push_result.returncode},
        )

    def open_or_observe(
        self,
        *,
        submission_id: str,
        branch: SubmissionBranchDelivery,
        base_branch: str,
        title: str,
        body: str,
    ) -> SubmissionPullRequestReceipt:
        if (
            not self._pull_identity_matches(submission_id, branch.branch)
            or branch.ref != f"refs/heads/{branch.branch}"
            or not _GIT_OBJECT_ID.fullmatch(branch.commit)
            or branch.remote != _REMOTE_NAME
            or not base_branch
            or any(character in base_branch for character in "\x00\r\n")
            or not title
            or not body
        ):
            self._invalid_request()
        identity = self._identity()
        governance = self._require_governance(identity)
        if base_branch != governance.default_branch:
            raise RCPError(
                code="submission_github_policy_mismatch",
                message="Submission base branch differs from accepted GitHub policy.",
            )
        observed = self._observe_pull_request(
            identity=identity,
            branch=branch,
            base_branch=base_branch,
            title=title,
            body=body,
            created=False,
        )
        if observed is not None:
            return observed

        payload = json.dumps(
            {
                "title": title,
                "head": branch.branch,
                "base": base_branch,
                "body": body,
            },
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        create_result: GhSubmissionCommandResult | None = None
        create_uncertain = False
        try:
            create_result = self._gh(
                identity,
                "--method",
                "POST",
                "--input",
                "-",
                f"/repos/{identity.repository}/pulls",
                input_bytes=payload,
            )
        except (subprocess.TimeoutExpired, TimeoutError):
            create_uncertain = True
        except FileNotFoundError as error:
            raise RCPError(
                code="github_cli_not_found",
                message="gh executable was not found for Submission delivery.",
            ) from error

        observed = self._observe_pull_request(
            identity=identity,
            branch=branch,
            base_branch=base_branch,
            title=title,
            body=body,
            created=True,
        )
        if observed is not None:
            return observed
        if create_uncertain:
            self._uncertain("pull_request_create")
        assert create_result is not None
        raise RCPError(
            code="submission_pr_create_failed",
            message="GitHub did not expose the exact Submission pull request after create.",
            context={"returncode": create_result.returncode},
        )

    def _require_remote_identity(self, root: Path) -> None:
        accepted = self._accepted_remote_url
        if accepted is None:
            raise RCPError(
                code="submission_github_not_configured",
                message="Accepted Project configuration has no GitHub remote.",
            )
        self._require_governance(self._identity())
        for arguments in (
            ("remote", "get-url", _REMOTE_NAME),
            ("remote", "get-url", "--push", _REMOTE_NAME),
        ):
            try:
                result = self._git(root, *arguments)
            except (subprocess.TimeoutExpired, TimeoutError) as error:
                raise RCPError(
                    code="submission_remote_observation_failed",
                    message="Submission Git remote could not be observed.",
                ) from error
            except FileNotFoundError as error:
                raise RCPError(
                    code="git_not_found",
                    message="git executable was not found for Submission delivery.",
                ) from error
            if result.returncode != 0 or result.stdout.rstrip("\n") != accepted:
                raise RCPError(
                    code="submission_remote_identity_mismatch",
                    message="Local origin does not match the accepted Project remote.",
                )

    def _remote_head(self, root: Path, branch: str) -> str | None:
        try:
            result = self._git(
                root,
                "ls-remote",
                "--refs",
                _REMOTE_NAME,
                f"refs/heads/{branch}",
            )
        except (subprocess.TimeoutExpired, TimeoutError) as error:
            raise RCPError(
                code="submission_delivery_uncertain",
                message="Submission remote branch observation is uncertain.",
                context={"stage": "branch_observation"},
            ) from error
        except FileNotFoundError as error:
            raise RCPError(
                code="git_not_found",
                message="git executable was not found for Submission delivery.",
            ) from error
        if result.returncode != 0:
            raise RCPError(
                code="submission_remote_observation_failed",
                message="Submission remote branch could not be observed.",
                context={"returncode": result.returncode},
            )
        if not result.stdout:
            return None
        lines = result.stdout.splitlines()
        if len(lines) != 1:
            self._invalid_response("Git returned ambiguous Submission branch data.")
        object_id, separator, ref = lines[0].partition("\t")
        if (
            not separator
            or not _GIT_OBJECT_ID.fullmatch(object_id)
            or ref != f"refs/heads/{branch}"
        ):
            self._invalid_response("Git returned invalid Submission branch data.")
        return object_id

    def _observe_pull_request(
        self,
        *,
        identity: GitHubRepositoryIdentity,
        branch: SubmissionBranchDelivery,
        base_branch: str,
        title: str,
        body: str,
        created: bool,
    ) -> SubmissionPullRequestReceipt | None:
        try:
            result = self._gh(
                identity,
                "--method",
                "GET",
                "-f",
                "state=all",
                "-f",
                f"head={identity.owner}:{branch.branch}",
                "-f",
                f"base={base_branch}",
                "-f",
                "per_page=100",
                f"/repos/{identity.repository}/pulls",
                input_bytes=None,
            )
        except (subprocess.TimeoutExpired, TimeoutError) as error:
            raise RCPError(
                code="submission_delivery_uncertain",
                message="Submission pull request observation is uncertain.",
                context={"stage": "pull_request_observation"},
            ) from error
        except FileNotFoundError as error:
            raise RCPError(
                code="github_cli_not_found",
                message="gh executable was not found for Submission delivery.",
            ) from error
        if result.returncode != 0:
            raise RCPError(
                code="submission_pr_observation_failed",
                message="GitHub pull requests could not be observed.",
                context={"returncode": result.returncode},
            )
        values = self._json_array(result.stdout)
        if not values:
            return None
        if len(values) != 1:
            raise RCPError(
                code="submission_pr_ambiguous",
                message="GitHub returned multiple pull requests for one Submission branch.",
                context={"count": len(values)},
            )
        pull = values[0]
        if not isinstance(pull, dict):
            self._invalid_response("GitHub pull request response is invalid.")
        number = pull.get("number")
        state = pull.get("state")
        observed_title = pull.get("title")
        observed_body = pull.get("body")
        author = pull.get("user")
        head = pull.get("head")
        base = pull.get("base")
        if (
            isinstance(number, bool)
            or not isinstance(number, int)
            or number < 1
            or state not in {"open", "closed"}
            or not isinstance(author, dict)
            or not isinstance(head, dict)
            or not isinstance(base, dict)
        ):
            self._invalid_response("GitHub pull request identity is invalid.")
        author_login = author.get("login")
        if not isinstance(author_login, str) or not author_login:
            self._invalid_response("GitHub pull request author identity is invalid.")
        governance = self._require_governance(identity)
        if author_login.lower() != governance.agent_app.login.lower():
            raise RCPError(
                code="submission_pr_author_mismatch",
                message="Submission pull request was not authored by the accepted Agent App.",
                context={"number": number},
            )
        head_repo = head.get("repo")
        base_repo = base.get("repo")
        if not isinstance(head_repo, dict) or not isinstance(base_repo, dict):
            self._invalid_response("GitHub pull request repository identity is invalid.")
        exact_identity = (
            head.get("ref") == branch.branch
            and head.get("sha") == branch.commit
            and head_repo.get("full_name") == identity.repository
            and base.get("ref") == base_branch
            and base_repo.get("full_name") == identity.repository
        )
        if not exact_identity:
            raise RCPError(
                code="submission_pr_identity_conflict",
                message="GitHub pull request does not bind the exact Submission lineage.",
                context={"number": number},
            )
        if observed_title != title or observed_body != body:
            raise RCPError(
                code="submission_pr_metadata_conflict",
                message="Submission pull request title or body differs from generated records.",
                context={"number": number},
            )
        if state != "open" or pull.get("merged_at") is not None:
            raise RCPError(
                code="submission_pr_not_open",
                message="The exact Submission pull request is no longer open.",
                context={"number": number, "state": state},
            )
        return SubmissionPullRequestReceipt(
            host=identity.host,
            repository=identity.repository,
            number=number,
            url=f"https://{identity.host}/{identity.repository}/pull/{number}",
            state="open",
            base_branch=base_branch,
            head_branch=branch.branch,
            head_commit=branch.commit,
            author_login=author_login,
            created=created,
        )

    def _require_governance(
        self,
        identity: GitHubRepositoryIdentity,
    ) -> GitHubGovernancePolicy:
        policy = self._governance
        if policy is None:
            raise RCPError(
                code="submission_github_governance_not_configured",
                message="Accepted ProjectPolicy has no GitHub proposal identity policy.",
                remediation="Accept a protected GitHub governance policy before submitting.",
            )
        if identity.repository.lower() != policy.repository.lower():
            raise RCPError(
                code="submission_github_policy_mismatch",
                message="Accepted Git remote differs from GitHub governance policy.",
            )
        return policy

    def _identity(self) -> GitHubRepositoryIdentity:
        if self._accepted_remote_url is None:
            raise RCPError(
                code="submission_github_not_configured",
                message="Accepted Project configuration has no GitHub remote.",
            )
        try:
            return parse_github_remote(self._accepted_remote_url)
        except ValueError as error:
            raise RCPError(
                code="submission_github_remote_invalid",
                message="Accepted Project remote is not a supported GitHub repository URL.",
            ) from error

    def _git(self, root: Path, *arguments: str) -> CommandResult:
        return self._git_runner.run(
            (
                "git",
                "-c",
                "core.fsmonitor=false",
                "-C",
                str(root),
                *arguments,
            ),
            cwd=None,
            env=self._git_environment,
            timeout_seconds=self._timeout_seconds,
        )

    def _gh(
        self,
        identity: GitHubRepositoryIdentity,
        *arguments: str,
        input_bytes: bytes | None,
    ) -> GhSubmissionCommandResult:
        return self._gh_runner.run(
            ("gh", "api", "--hostname", identity.host, *arguments),
            env=self._gh_environment,
            input_bytes=input_bytes,
            timeout_seconds=self._timeout_seconds,
        )

    def _json_array(self, content: bytes) -> list[object]:
        if len(content) > self._max_json_bytes:
            self._invalid_response("GitHub response exceeds the configured bound.")
        try:
            value = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
            raise RCPError(
                code="submission_github_response_invalid",
                message="GitHub returned invalid bounded JSON.",
            ) from error
        if not isinstance(value, list):
            self._invalid_response("GitHub pull request response is not an array.")
        return value

    @staticmethod
    def _require_branch_commit(branch: str, commit: str) -> None:
        if _SUBMISSION_BRANCH.fullmatch(branch) is None or _GIT_OBJECT_ID.fullmatch(
            commit
        ) is None:
            GitHubSubmissionDelivery._invalid_request()

    @staticmethod
    def _pull_identity_matches(submission_id: str, branch: str) -> bool:
        matched = _SUBMISSION_BRANCH.fullmatch(branch)
        return matched is not None and matched.group(1) == submission_id

    @staticmethod
    def _invalid_request() -> None:
        raise RCPError(
            code="submission_delivery_request_invalid",
            message="Submission delivery requires canonical derived identities.",
        )

    @staticmethod
    def _invalid_response(message: str) -> None:
        raise RCPError(code="submission_github_response_invalid", message=message)

    @staticmethod
    def _uncertain(stage: str) -> None:
        raise RCPError(
            code="submission_delivery_uncertain",
            message="Submission delivery outcome is uncertain and must be observed.",
            context={"stage": stage},
        )


def parse_github_remote(remote_url: str) -> GitHubRepositoryIdentity:
    if not isinstance(remote_url, str) or not remote_url or "\x00" in remote_url:
        raise ValueError("remote URL is empty or invalid")
    scp = re.fullmatch(
        r"git@(?P<host>[A-Za-z0-9.-]+):(?P<path>[^/?#]+/[^/?#]+)",
        remote_url,
    )
    if scp is not None:
        host = scp.group("host")
        path = scp.group("path")
    else:
        parsed = urlsplit(remote_url)
        if (
            parsed.scheme not in {"https", "ssh"}
            or not parsed.hostname
            or parsed.port is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("unsupported remote URL")
        if parsed.scheme == "https" and parsed.username is not None:
            raise ValueError("credential-bearing HTTPS remote is forbidden")
        if parsed.scheme == "ssh" and parsed.username not in {None, "git"}:
            raise ValueError("SSH remote user is invalid")
        host = parsed.hostname
        path = parsed.path.removeprefix("/")
    if path.endswith(".git"):
        path = path[:-4]
    if _HOST.fullmatch(host) is None or _REPOSITORY.fullmatch(path) is None:
        raise ValueError("remote repository identity is invalid")
    return GitHubRepositoryIdentity(host=host.lower(), repository=path)
