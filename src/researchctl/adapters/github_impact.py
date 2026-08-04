from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from researchctl.adapters.github_submission import GitHubSubmissionDelivery
from researchctl.errors import RCPError
from researchctl.services.impact_delivery import (
    ImpactBranchDelivery,
    ImpactPullRequestReceipt,
)


_GIT_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_IMPACT_BRANCH = re.compile(
    r"^research/impact/(impact_\d{8}T\d{6}Z_[0-9a-f]{24})$"
)


class GitHubImpactDelivery(GitHubSubmissionDelivery):
    """Push and open one fixed same-repository Impact proposal."""

    def push_exact(
        self,
        *,
        repository_root: Path,
        branch: str,
        commit: str,
    ) -> ImpactBranchDelivery:
        delivered = super().push_exact(
            repository_root=repository_root,
            branch=branch,
            commit=commit,
        )
        return ImpactBranchDelivery(
            remote=delivered.remote,
            branch=delivered.branch,
            ref=delivered.ref,
            commit=delivered.commit,
            pushed=delivered.pushed,
        )

    def open_or_observe(
        self,
        *,
        impact_id: str,
        branch: ImpactBranchDelivery,
        base_branch: str,
        title: str,
        body: str,
    ) -> ImpactPullRequestReceipt:
        matched = _IMPACT_BRANCH.fullmatch(branch.branch)
        if (
            matched is None
            or matched.group(1) != impact_id
            or branch.ref != f"refs/heads/{branch.branch}"
            or not _GIT_OBJECT_ID.fullmatch(branch.commit)
            or branch.remote != "origin"
            or not base_branch
            or any(character in base_branch for character in "\x00\r\n")
            or not title
            or not body
        ):
            self._invalid_request()
        identity = self._identity()
        observed = self._observe_pull_request(
            identity=identity,
            branch=branch,  # type: ignore[arg-type]
            base_branch=base_branch,
            title=title,
            body=body,
            created=False,
        )
        if observed is None:
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
            create_result = None
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
                    message="gh executable was not found for Impact delivery.",
                ) from error
            observed = self._observe_pull_request(
                identity=identity,
                branch=branch,  # type: ignore[arg-type]
                base_branch=base_branch,
                title=title,
                body=body,
                created=True,
            )
            if observed is None:
                if create_uncertain:
                    self._uncertain("pull_request_create")
                assert create_result is not None
                raise RCPError(
                    code="impact_pr_create_failed",
                    message="GitHub did not expose the exact Impact pull request after create.",
                    context={"returncode": create_result.returncode},
                )
        return ImpactPullRequestReceipt(
            host=observed.host,
            repository=observed.repository,
            number=observed.number,
            url=observed.url,
            state=observed.state,
            base_branch=observed.base_branch,
            head_branch=observed.head_branch,
            head_commit=observed.head_commit,
            created=observed.created,
        )

    @staticmethod
    def _require_branch_commit(branch: str, commit: str) -> None:
        if (
            _IMPACT_BRANCH.fullmatch(branch) is None
            or _GIT_OBJECT_ID.fullmatch(commit) is None
        ):
            GitHubImpactDelivery._invalid_request()

    @staticmethod
    def _invalid_request() -> None:
        raise RCPError(
            code="impact_delivery_request_invalid",
            message="Impact delivery requires canonical derived identities.",
        )

    @staticmethod
    def _uncertain(stage: str) -> None:
        raise RCPError(
            code="impact_delivery_uncertain",
            message="Impact delivery outcome is uncertain and must be observed.",
            context={"stage": stage},
        )
