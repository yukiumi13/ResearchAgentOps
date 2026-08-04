from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from researchctl.adapters._subprocess import (
    CommandResult,
    CommandRunner,
    SubprocessCommandRunner,
)
from researchctl.adapters.git_worktree import GitWorktreeAdapter, WorktreeSpec
from researchctl.errors import RCPError


_SUBMISSION_ID = re.compile(r"^submission_\d{8}T\d{6}Z_[0-9a-f]{24}$")
_OPERATION_ID = re.compile(r"^operation_\d{8}T\d{6}Z_[0-9a-f]{24}$")
_DECISION_ID = re.compile(r"^decision_\d{8}T\d{6}Z_[0-9a-f]{24}$")
_REPORT_ID = re.compile(r"^report_\d{8}T\d{6}Z_[0-9a-f]{24}$")
_RUN_ID = re.compile(r"^run_\d{8}T\d{6}Z_[0-9a-f]{24}$")
_GIT_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


@dataclass(frozen=True, slots=True)
class SubmissionCommitReceipt:
    command: str
    branch: str
    worktree: Path
    commit: str
    parent_commit: str
    changed: bool
    paths: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "command": self.command,
            "branch": self.branch,
            "worktree": str(self.worktree),
            "commit": self.commit,
            "parent_commit": self.parent_commit,
            "changed": self.changed,
            "paths": list(self.paths),
            "delivery": "local_submission_change",
        }


class GitSubmissionAdapter:
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
        submission_id: str,
        base_commit: str,
        expected_head: str | None = None,
    ) -> tuple[str, Path]:
        self._validate_identity(submission_id, base_commit)
        branch = self._branch_for(submission_id)
        worktree = worktrees_directory / f"submission-{submission_id}"
        selected = base_commit
        existing = self._resolve(
            repository_root,
            f"refs/heads/{branch}",
            required=False,
        )
        if expected_head is not None:
            if not _GIT_OBJECT_ID.fullmatch(expected_head):
                self._invalid_identity()
            if existing != expected_head:
                raise RCPError(
                    code="stale_submission_head",
                    message="Submission branch no longer matches the reviewed head.",
                    context={
                        "expected_head": expected_head,
                        "observed_head": existing,
                    },
                )
            selected = expected_head
        elif existing is not None:
            selected = existing
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
        submission_id: str,
        operation_id: str,
        expected_parent: str,
        paths: tuple[str, ...],
    ) -> SubmissionCommitReceipt:
        root = f".research/submissions/{submission_id}/"
        for path in paths:
            suffix = path.removeprefix(root)
            valid = path.startswith(root) and suffix in {
                "submission.yaml",
                "proposed-report.yaml",
                "review.md",
                "report-preview.md",
            }
            match = re.fullmatch(
                r"evidence/(run_\d{8}T\d{6}Z_[0-9a-f]{24})/"
                r"(spec|result)\.yaml",
                suffix,
            )
            if match is not None and _RUN_ID.fullmatch(match.group(1)):
                valid = True
            if not valid:
                raise RCPError(
                    code="submission_proposal_path_invalid",
                    message="Submission proposal contains an unexpected path.",
                    context={"path": path},
                )
        return self._commit(
            worktree=worktree,
            branch=branch,
            submission_id=submission_id,
            operation_id=operation_id,
            command="submission.create",
            expected_parent=expected_parent,
            paths=paths,
        )

    def commit_acceptance(
        self,
        *,
        worktree: Path,
        branch: str,
        submission_id: str,
        decision_id: str,
        report_id: str,
        report_revision: int,
        operation_id: str,
        expected_parent: str,
        paths: tuple[str, ...],
    ) -> SubmissionCommitReceipt:
        if (
            not _DECISION_ID.fullmatch(decision_id)
            or not _REPORT_ID.fullmatch(report_id)
            or report_revision < 1
        ):
            self._invalid_identity()
        expected = {
            f".research/submissions/{submission_id}/submission.yaml",
            f".research/decisions/{decision_id}.yaml",
            f".research/reports/{report_id}/{report_revision}.md",
            f".research/reports/{report_id}/{report_revision}.yaml",
        }
        if set(paths) != expected or len(paths) != len(expected):
            raise RCPError(
                code="review_acceptance_path_invalid",
                message="Review acceptance must change the closed generated path set.",
            )
        return self._commit(
            worktree=worktree,
            branch=branch,
            submission_id=submission_id,
            operation_id=operation_id,
            command="review.accept",
            expected_parent=expected_parent,
            paths=paths,
        )

    def _commit(
        self,
        *,
        worktree: Path,
        branch: str,
        submission_id: str,
        operation_id: str,
        command: str,
        expected_parent: str,
        paths: tuple[str, ...],
    ) -> SubmissionCommitReceipt:
        self._validate_identity(submission_id, expected_parent)
        if not _OPERATION_ID.fullmatch(operation_id):
            self._invalid_identity()
        expected_branch = self._branch_for(submission_id)
        if branch != expected_branch:
            self._invalid_identity()
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
                code="submission_branch_mismatch",
                message="Submission worktree is on an unexpected branch.",
            )
        expected_paths = tuple(sorted(paths))
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
                code="submission_worktree_dirty",
                message="Submission worktree contains an unexpected change.",
                context={"observed_paths": list(changed_paths)},
            )
        changed = False
        if changed_paths:
            self._git(root, "add", "--", *expected_paths)
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
                f"researchctl: {command} {submission_id} {operation_id}",
                "--",
                *expected_paths,
            )
            changed = True
        head = self._resolve(root, "HEAD", required=True)
        assert head is not None
        self._verify_commit(
            root=root,
            commit=head,
            expected_parent=expected_parent,
            expected_message=(
                f"researchctl: {command} {submission_id} {operation_id}"
            ),
            expected_paths=expected_paths,
        )
        return SubmissionCommitReceipt(
            command=command,
            branch=branch,
            worktree=root,
            commit=head,
            parent_commit=expected_parent,
            changed=changed,
            paths=expected_paths,
        )

    @staticmethod
    def _branch_for(submission_id: str) -> str:
        return f"research/submission/{submission_id}"

    def _verify_commit(
        self,
        *,
        root: Path,
        commit: str,
        expected_parent: str,
        expected_message: str,
        expected_paths: tuple[str, ...],
    ) -> None:
        parent = self._git(root, "rev-parse", f"{commit}^").stdout.strip()
        if parent != expected_parent:
            raise RCPError(
                code="submission_commit_parent_mismatch",
                message="Submission commit does not extend the expected exact head.",
            )
        message = self._git(root, "show", "-s", "--format=%B", commit).stdout
        if message.rstrip("\n") != expected_message:
            raise RCPError(
                code="submission_commit_marker_mismatch",
                message="Submission commit marker does not bind the Operation.",
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
                code="submission_commit_paths_mismatch",
                message="Submission commit changed an unexpected path set.",
            )
        for path in expected_paths:
            committed = self._git(root, "show", f"{commit}:{path}").stdout
            candidate = root / path
            if (
                candidate.is_symlink()
                or not candidate.is_file()
                or committed != candidate.read_text(encoding="utf-8")
            ):
                raise RCPError(
                    code="submission_commit_content_mismatch",
                    message="Submission commit content differs from generated output.",
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
                message="Required Submission Git revision was not found.",
            )
        value = result.stdout.strip()
        if not _GIT_OBJECT_ID.fullmatch(value):
            raise RCPError(
                code="git_output_invalid",
                message="Git returned an invalid object ID.",
            )
        return value

    @staticmethod
    def _validate_identity(submission_id: str, commit: str) -> None:
        if (
            not _SUBMISSION_ID.fullmatch(submission_id)
            or not _GIT_OBJECT_ID.fullmatch(commit)
        ):
            GitSubmissionAdapter._invalid_identity()

    @staticmethod
    def _invalid_identity() -> None:
        raise RCPError(
            code="submission_identity_invalid",
            message="Submission Git mutation requires canonical identities.",
        )

    def _git(
        self,
        root: Path,
        *arguments: str,
        check: bool = True,
    ) -> CommandResult:
        try:
            result = self._runner.run(
                ("git", "-c", "core.fsmonitor=false", "-C", str(root), *arguments),
                cwd=None,
                env={
                    "GIT_OPTIONAL_LOCKS": "0",
                    "PATH": os.defpath,
                },
                timeout_seconds=self._timeout_seconds,
            )
        except (FileNotFoundError, TimeoutError) as error:
            raise RCPError(
                code="git_command_failed",
                message="Git command could not be executed.",
            ) from error
        if check and result.returncode != 0:
            raise RCPError(
                code="git_command_failed",
                message=result.stderr.strip() or "Git command failed.",
                context={"args": list(arguments), "returncode": result.returncode},
            )
        return result
