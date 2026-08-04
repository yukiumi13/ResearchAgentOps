from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import TypeAdapter

from researchctl.adapters._subprocess import (
    CommandResult,
    CommandRunner,
    SubprocessCommandRunner,
)
from researchctl.adapters.git_worktree import GitWorktreeAdapter, WorktreeSpec
from researchctl.domain.types import UtcDateTime
from researchctl.errors import RCPError


_IMPACT_ID = re.compile(r"^impact_\d{8}T\d{6}Z_[0-9a-f]{24}$")
_OPERATION_ID = re.compile(r"^operation_\d{8}T\d{6}Z_[0-9a-f]{24}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_GIT_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_REPORT_REVISION_PATH = re.compile(
    r"^\.research/reports/"
    r"report_\d{8}T\d{6}Z_[0-9a-f]{24}/[1-9][0-9]*\.(?:md|yaml)$"
)
_UTC_DATETIME_ADAPTER = TypeAdapter(UtcDateTime)


@dataclass(frozen=True, slots=True)
class ImpactCommitReceipt:
    command: Literal["impact.create", "impact.batch"]
    branch: str
    worktree: Path
    commit: str
    parent_commit: str
    changed: bool
    paths: tuple[str, ...]
    manifest_digest: str

    def as_dict(self) -> dict[str, object]:
        return {
            "command": self.command,
            "branch": self.branch,
            "worktree": str(self.worktree),
            "commit": self.commit,
            "parent_commit": self.parent_commit,
            "changed": self.changed,
            "paths": list(self.paths),
            "manifest_digest": self.manifest_digest,
            "delivery": "local_impact_change",
        }


class GitImpactAdapter:
    def __init__(
        self,
        *,
        worktrees: GitWorktreeAdapter | None = None,
        runner: CommandRunner | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.worktrees = worktrees or GitWorktreeAdapter()
        self._runner = runner or SubprocessCommandRunner()
        self._timeout_seconds = timeout_seconds

    def prepare_worktree(
        self,
        *,
        repository_root: Path,
        worktrees_directory: Path,
        impact_id: str,
        target_commit: str,
    ) -> tuple[str, Path]:
        self._validate_identity(impact_id, target_commit)
        branch = f"research/impact/{impact_id}"
        worktree = worktrees_directory / f"impact-{impact_id}"
        existing = self._resolve(
            repository_root,
            f"refs/heads/{branch}",
            required=False,
        )
        selected = existing or target_commit
        self.worktrees.create_or_observe(
            WorktreeSpec(
                root=repository_root,
                base_commit=selected,
                branch=branch,
                worktree=worktree,
            )
        )
        return branch, worktree

    def commit_proposal(
        self,
        *,
        worktree: Path,
        branch: str,
        impact_id: str,
        operation_id: str,
        expected_parent: str,
        manifest_digest: str,
        paths: tuple[str, ...],
        command: Literal["impact.create", "impact.batch"] = "impact.create",
        committed_at: datetime | str | None = None,
    ) -> ImpactCommitReceipt:
        self._validate_identity(impact_id, expected_parent)
        if (
            not _OPERATION_ID.fullmatch(operation_id)
            or not _DIGEST.fullmatch(manifest_digest)
            or branch != f"research/impact/{impact_id}"
        ):
            self._invalid_identity()
        expected_paths = tuple(sorted(paths))
        if len(expected_paths) != len(set(expected_paths)):
            self._invalid_paths()
        impact_filename = (
            "impact.yaml" if command == "impact.create" else "impact-batch.yaml"
        )
        impact_path = f".research/impacts/{impact_id}/{impact_filename}"
        if impact_path not in expected_paths:
            self._invalid_paths()
        report_paths = tuple(
            path for path in expected_paths if path != impact_path
        )
        if (
            not report_paths
            or len(report_paths) % 2
            or len(report_paths) > 512
            or any(
                _REPORT_REVISION_PATH.fullmatch(path) is None
                for path in report_paths
            )
            or {
                path.removesuffix(".md")
                if path.endswith(".md")
                else path.removesuffix(".yaml")
                for path in report_paths
            }
            != {
                path.removesuffix(".md")
                for path in report_paths
                if path.endswith(".md")
            }
            or len(report_paths) != 2 * len(
                {
                    path.removesuffix(".md")
                    for path in report_paths
                    if path.endswith(".md")
                }
            )
        ):
            self._invalid_paths()
        if command == "impact.create" and len(report_paths) != 2:
            self._invalid_paths()

        root = Path(os.path.abspath(os.fspath(worktree)))
        observed_branch = self._git(
            root,
            "symbolic-ref",
            "--quiet",
            "--short",
            "HEAD",
            check=False,
        )
        if (
            observed_branch.returncode != 0
            or observed_branch.stdout.removesuffix("\n") != branch
        ):
            raise RCPError(
                code="impact_branch_mismatch",
                message="Impact worktree is on an unexpected branch.",
            )
        status = self._git(
            root,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        )
        changed_paths = tuple(
            sorted(line[3:] for line in status.stdout.splitlines() if len(line) >= 4)
        )
        if changed_paths and changed_paths != expected_paths:
            raise RCPError(
                code="impact_worktree_dirty",
                message="Impact worktree contains an unexpected change.",
                context={"observed_paths": list(changed_paths)},
            )
        message = (
            f"researchctl: {command} {impact_id} {operation_id}\n\n"
            f"manifest-digest: {manifest_digest}"
        )
        changed = False
        if changed_paths:
            self._git(root, "add", "--", *expected_paths)
            commit_environment = None
            if committed_at is not None:
                canonical_time = _UTC_DATETIME_ADAPTER.dump_python(
                    _UTC_DATETIME_ADAPTER.validate_python(committed_at),
                    mode="json",
                )
                commit_environment = {
                    "GIT_AUTHOR_DATE": canonical_time,
                    "GIT_COMMITTER_DATE": canonical_time,
                }
            self._git(
                root,
                "-c",
                "user.name=Research Control Plane",
                "-c",
                "user.email=researchctl@localhost",
                "-c",
                "commit.gpgSign=false",
                "commit",
                "--no-gpg-sign",
                "-m",
                message,
                "--",
                *expected_paths,
                environment=commit_environment,
            )
            changed = True
        head = self._resolve(root, "HEAD", required=True)
        assert head is not None
        self._verify_commit(
            root=root,
            commit=head,
            expected_parent=expected_parent,
            expected_message=message,
            expected_paths=expected_paths,
        )
        return ImpactCommitReceipt(
            command=command,
            branch=branch,
            worktree=root,
            commit=head,
            parent_commit=expected_parent,
            changed=changed,
            paths=expected_paths,
            manifest_digest=manifest_digest,
        )

    def _verify_commit(
        self,
        *,
        root: Path,
        commit: str,
        expected_parent: str,
        expected_message: str,
        expected_paths: tuple[str, ...],
    ) -> None:
        parents = self._git(root, "show", "-s", "--format=%P", commit).stdout.split()
        if parents != [expected_parent]:
            raise RCPError(
                code="impact_commit_parent_mismatch",
                message="Impact commit does not extend the exact target main commit.",
            )
        message = self._git(root, "show", "-s", "--format=%B", commit).stdout
        if message.rstrip("\n") != expected_message:
            raise RCPError(
                code="impact_commit_marker_mismatch",
                message="Impact commit marker does not bind its operation and manifest.",
            )
        changed = self._git(
            root,
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "-r",
            "-z",
            commit,
        ).stdout
        observed = tuple(sorted(item for item in changed.split("\x00") if item))
        if observed != expected_paths:
            raise RCPError(
                code="impact_commit_paths_mismatch",
                message="Impact commit changed an unexpected path set.",
            )
        for path in expected_paths:
            committed = self._git(root, "show", f"{commit}:{path}").stdout.encode(
                "utf-8"
            )
            candidate = root / path
            if (
                candidate.is_symlink()
                or not candidate.is_file()
                or committed != candidate.read_bytes()
            ):
                raise RCPError(
                    code="impact_commit_content_mismatch",
                    message="Impact commit content differs from generated output.",
                    context={"path": path},
                )

    def _resolve(self, root: Path, revision: str, *, required: bool) -> str | None:
        result = self._git(
            root,
            "rev-parse",
            "--verify",
            "--quiet",
            f"{revision}^{{commit}}",
            check=False,
        )
        if result.returncode == 1 and not required:
            return None
        if result.returncode != 0:
            raise RCPError(
                code="git_revision_not_found",
                message="Required Impact Git revision was not found.",
            )
        value = result.stdout.strip()
        if not _GIT_OBJECT_ID.fullmatch(value):
            raise RCPError(
                code="git_output_invalid",
                message="Git returned an invalid Impact object ID.",
            )
        return value

    @staticmethod
    def _validate_identity(impact_id: str, commit: str) -> None:
        if not _IMPACT_ID.fullmatch(impact_id) or not _GIT_OBJECT_ID.fullmatch(commit):
            GitImpactAdapter._invalid_identity()

    @staticmethod
    def _invalid_identity() -> None:
        raise RCPError(
            code="impact_identity_invalid",
            message="Impact Git mutation requires canonical identities.",
        )

    @staticmethod
    def _invalid_paths() -> None:
        raise RCPError(
            code="impact_proposal_path_invalid",
            message="Impact proposal must change its closed generated path set.",
        )

    def _git(
        self,
        root: Path,
        *arguments: str,
        check: bool = True,
        environment: dict[str, str] | None = None,
    ) -> CommandResult:
        try:
            result = self._runner.run(
                ("git", "-c", "core.fsmonitor=false", "-C", str(root), *arguments),
                cwd=None,
                env={
                    "GIT_OPTIONAL_LOCKS": "0",
                    "PATH": os.defpath,
                    **(environment or {}),
                },
                timeout_seconds=self._timeout_seconds,
            )
        except FileNotFoundError as error:
            raise RCPError(
                code="git_not_found",
                message="git executable was not found for Impact mutation.",
            ) from error
        except (subprocess.TimeoutExpired, TimeoutError) as error:
            raise RCPError(
                code="git_timeout",
                message="Impact Git mutation timed out.",
            ) from error
        if check and result.returncode != 0:
            raise RCPError(
                code="impact_git_command_failed",
                message="Git failed while preparing an Impact proposal.",
                context={
                    "operation": arguments[0] if arguments else "unknown",
                    "returncode": result.returncode,
                },
            )
        return result
