from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from researchctl.adapters._subprocess import (
    CommandResult,
    CommandRunner,
    SubprocessCommandRunner,
)
from researchctl.adapters.git_bootstrap import GitBootstrapCommitAdapter
from researchctl.errors import RCPError


_OPERATION_ID = re.compile(r"^operation_\d{8}T\d{6}Z_[0-9a-f]{24}$")
_BOOTSTRAP_ID = re.compile(r"^bootstrap_\d{8}T\d{6}Z_[0-9a-f]{24}$")
_GIT_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SHA256_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_PROJECT_RECORD_PATH = ".research/project.yaml"
_GIT_ENVIRONMENT_KEYS = {
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_WORK_TREE",
}


@dataclass(frozen=True, slots=True)
class BootstrapProposalReceipt:
    operation_id: str
    bootstrap_id: str
    branch: str
    worktree: Path
    base_commit: str
    commit: str
    manifest_digest: str
    changed_paths: tuple[str, ...]
    changed: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "operation_id": self.operation_id,
            "bootstrap_id": self.bootstrap_id,
            "branch": self.branch,
            "worktree": str(self.worktree),
            "base_commit": self.base_commit,
            "commit": self.commit,
            "manifest_digest": self.manifest_digest,
            "changed_paths": list(self.changed_paths),
            "changed": self.changed,
            "project_state": "bootstrapping",
            "proposal_only": True,
            "accepted": False,
            "pushed": False,
            "pr_created": False,
            "delivery": "local_bootstrap_proposal",
        }


class GitBootstrapProposalAdapter:
    """Commits or verifies one init-only bootstrap proposal."""

    def __init__(
        self,
        *,
        runner: CommandRunner | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._runner = runner or SubprocessCommandRunner()
        self._timeout_seconds = timeout_seconds
        self._tree = GitBootstrapCommitAdapter(
            runner=self._runner,
            timeout_seconds=timeout_seconds,
        )

    @staticmethod
    def validate_identity(
        *,
        operation_id: str,
        bootstrap_id: str,
        branch: str,
    ) -> None:
        if not isinstance(operation_id, str) or not _OPERATION_ID.fullmatch(
            operation_id
        ):
            raise RCPError(
                code="bootstrap_proposal_operation_id_invalid",
                message="Bootstrap proposal requires a canonical Operation ID.",
            )
        if not isinstance(bootstrap_id, str) or not _BOOTSTRAP_ID.fullmatch(
            bootstrap_id
        ):
            raise RCPError(
                code="bootstrap_id_invalid",
                message="Bootstrap proposal requires a canonical Bootstrap ID.",
            )
        expected = f"research/bootstrap/{bootstrap_id}"
        if branch != expected:
            raise RCPError(
                code="bootstrap_proposal_branch_invalid",
                message="Bootstrap proposal branch does not match its Bootstrap ID.",
                context={"expected_branch": expected},
            )

    @staticmethod
    def validate_commit(commit: str) -> None:
        if not isinstance(commit, str) or not _GIT_OBJECT_ID.fullmatch(commit):
            raise RCPError(
                code="bootstrap_proposal_base_invalid",
                message="Bootstrap proposal base must be a full lowercase Git object ID.",
            )

    def controlled_tree_paths(self, *, root: Path, commit: str) -> tuple[str, ...]:
        return self._tree.controlled_tree_paths(root=root, commit=commit)

    def observe(
        self,
        *,
        worktree: Path,
        branch: str,
        base_commit: str,
        operation_id: str,
        bootstrap_id: str,
        manifest_digest: str,
        desired_files: Mapping[str, bytes],
    ) -> BootstrapProposalReceipt | None:
        root, files = self._validate_request(
            worktree=worktree,
            branch=branch,
            base_commit=base_commit,
            operation_id=operation_id,
            bootstrap_id=bootstrap_id,
            manifest_digest=manifest_digest,
            desired_files=desired_files,
        )
        self._require_branch(root, branch)
        changed_paths = self._status_paths(root, frozenset(files))
        head = self._head(root)
        if head == base_commit:
            return None
        if changed_paths:
            raise RCPError(
                code="bootstrap_proposal_worktree_dirty",
                message="A completed bootstrap proposal worktree must be clean.",
            )
        return self._verify_commit(
            root=root,
            branch=branch,
            base_commit=base_commit,
            operation_id=operation_id,
            bootstrap_id=bootstrap_id,
            manifest_digest=manifest_digest,
            desired_files=files,
            commit=head,
            changed=False,
        )

    def commit_or_observe(
        self,
        *,
        worktree: Path,
        branch: str,
        base_commit: str,
        operation_id: str,
        bootstrap_id: str,
        manifest_digest: str,
        desired_files: Mapping[str, bytes],
    ) -> BootstrapProposalReceipt:
        root, files = self._validate_request(
            worktree=worktree,
            branch=branch,
            base_commit=base_commit,
            operation_id=operation_id,
            bootstrap_id=bootstrap_id,
            manifest_digest=manifest_digest,
            desired_files=desired_files,
        )
        self._require_branch(root, branch)
        changed_paths = self._status_paths(root, frozenset(files))
        head = self._head(root)
        if head != base_commit:
            if changed_paths:
                raise RCPError(
                    code="bootstrap_proposal_worktree_dirty",
                    message="A completed bootstrap proposal worktree must be clean.",
                )
            return self._verify_commit(
                root=root,
                branch=branch,
                base_commit=base_commit,
                operation_id=operation_id,
                bootstrap_id=bootstrap_id,
                manifest_digest=manifest_digest,
                desired_files=files,
                commit=head,
                changed=False,
            )
        expected_paths = tuple(sorted(files))
        if changed_paths != expected_paths:
            raise RCPError(
                code="bootstrap_proposal_manifest_incomplete",
                message="Bootstrap proposal worktree does not contain the complete manifest.",
            )

        self._git(root, "add", "--", *expected_paths)
        staged = self._git(root, "diff", "--cached", "--quiet", check=False)
        if staged.returncode not in {0, 1}:
            self._raise_failed(staged, "inspect staged bootstrap proposal")
        if staged.returncode == 0:
            raise RCPError(
                code="bootstrap_proposal_manifest_incomplete",
                message="Bootstrap proposal has no staged init manifest.",
            )
        self._git(
            root,
            "-c",
            "user.name=Research Control Plane",
            "-c",
            "user.email=researchctl@localhost",
            "-c",
            "commit.gpgSign=false",
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "--no-gpg-sign",
            "-m",
            f"researchctl: bootstrap proposal {bootstrap_id}",
            "-m",
            f"operation-id: {operation_id}",
            "-m",
            f"manifest-digest: {manifest_digest}",
        )
        return self._verify_commit(
            root=root,
            branch=branch,
            base_commit=base_commit,
            operation_id=operation_id,
            bootstrap_id=bootstrap_id,
            manifest_digest=manifest_digest,
            desired_files=files,
            commit=self._head(root),
            changed=True,
        )

    def attach_existing_branch(
        self,
        *,
        repository_root: Path,
        worktree: Path,
        branch: str,
        expected_head: str,
        operation_id: str,
        bootstrap_id: str,
    ) -> None:
        self.validate_identity(
            operation_id=operation_id,
            bootstrap_id=bootstrap_id,
            branch=branch,
        )
        self.validate_commit(expected_head)
        repository = self._existing_root(repository_root)
        target = Path(os.path.abspath(os.fspath(worktree)))
        if (
            target.is_symlink()
            or os.path.lexists(target)
            or not target.parent.is_dir()
            or target.parent.is_symlink()
        ):
            raise RCPError(
                code="bootstrap_proposal_worktree_invalid",
                message="Recovered bootstrap proposal worktree path is not safely absent.",
            )
        observed = self._git(
            repository,
            "rev-parse",
            "--verify",
            "--quiet",
            f"refs/heads/{branch}^{{commit}}",
            check=False,
        )
        if observed.returncode != 0 or observed.stdout.strip() != expected_head:
            raise RCPError(
                code="bootstrap_proposal_branch_mismatch",
                message="Recovered bootstrap proposal branch changed unexpectedly.",
            )
        self._git(
            repository,
            "worktree",
            "add",
            "--",
            str(target),
            branch,
        )
        recovered = self._existing_root(target)
        self._require_branch(recovered, branch)
        if self._head(recovered) != expected_head:
            raise RCPError(
                code="bootstrap_proposal_branch_mismatch",
                message="Recovered bootstrap proposal worktree has the wrong HEAD.",
            )

    def _validate_request(
        self,
        *,
        worktree: Path,
        branch: str,
        base_commit: str,
        operation_id: str,
        bootstrap_id: str,
        manifest_digest: str,
        desired_files: Mapping[str, bytes],
    ) -> tuple[Path, dict[str, bytes]]:
        self.validate_identity(
            operation_id=operation_id,
            bootstrap_id=bootstrap_id,
            branch=branch,
        )
        self.validate_commit(base_commit)
        if not isinstance(manifest_digest, str) or not _SHA256_DIGEST.fullmatch(
            manifest_digest
        ):
            raise RCPError(
                code="bootstrap_proposal_manifest_digest_invalid",
                message="Bootstrap proposal manifest digest is not canonical.",
            )
        root = self._existing_root(worktree)
        files: dict[str, bytes] = {}
        for relative, content in desired_files.items():
            path = self._managed_path(relative)
            if path in files or not isinstance(content, bytes):
                raise RCPError(
                    code="bootstrap_proposal_manifest_invalid",
                    message="Bootstrap proposal manifest paths and bytes must be canonical.",
                )
            files[path] = content
        if _PROJECT_RECORD_PATH not in files:
            raise RCPError(
                code="bootstrap_proposal_manifest_invalid",
                message="Bootstrap proposal manifest must include the ProjectRecord.",
            )
        return root, files

    def _verify_commit(
        self,
        *,
        root: Path,
        branch: str,
        base_commit: str,
        operation_id: str,
        bootstrap_id: str,
        manifest_digest: str,
        desired_files: Mapping[str, bytes],
        commit: str,
        changed: bool,
    ) -> BootstrapProposalReceipt:
        parents = self._git(root, "show", "-s", "--format=%P", commit)
        if parents.stdout.strip() != base_commit:
            raise RCPError(
                code="bootstrap_proposal_commit_invalid",
                message="Bootstrap proposal must be one commit over its exact base.",
            )
        message = self._git(root, "show", "-s", "--format=%B", commit)
        expected_message = (
            f"researchctl: bootstrap proposal {bootstrap_id}\n\n"
            f"operation-id: {operation_id}\n\n"
            f"manifest-digest: {manifest_digest}"
        )
        if message.stdout.rstrip("\n") != expected_message:
            raise RCPError(
                code="bootstrap_proposal_commit_invalid",
                message="Bootstrap proposal commit has the wrong identity marker.",
            )
        diff = self._git(
            root,
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "-r",
            "-z",
            commit,
        )
        changed_paths = tuple(sorted(path for path in diff.stdout.split("\x00") if path))
        expected_paths = tuple(sorted(desired_files))
        if changed_paths != expected_paths:
            raise RCPError(
                code="bootstrap_proposal_commit_invalid",
                message="Bootstrap proposal commit differs from its exact manifest paths.",
            )
        tree_paths = self._tree.controlled_tree_paths(root=root, commit=commit)
        if tree_paths != expected_paths:
            raise RCPError(
                code="bootstrap_proposal_commit_invalid",
                message="Bootstrap proposal tree has unexpected managed content.",
            )
        for relative, expected in desired_files.items():
            observed = self._tree.read_tree_file(
                root=root,
                commit=commit,
                relative=relative,
            )
            if observed != expected:
                raise RCPError(
                    code="bootstrap_proposal_commit_invalid",
                    message="Bootstrap proposal content differs from its manifest.",
                    context={"path": relative},
                )
        if self._status_paths(root, frozenset(desired_files)):
            raise RCPError(
                code="bootstrap_proposal_worktree_dirty",
                message="Bootstrap proposal worktree is dirty after commit.",
            )
        return BootstrapProposalReceipt(
            operation_id=operation_id,
            bootstrap_id=bootstrap_id,
            branch=branch,
            worktree=root,
            base_commit=base_commit,
            commit=commit,
            manifest_digest=manifest_digest,
            changed_paths=changed_paths,
            changed=changed,
        )

    def _require_branch(self, root: Path, branch: str) -> None:
        observed = self._git(
            root,
            "symbolic-ref",
            "--quiet",
            "--short",
            "HEAD",
            check=False,
        )
        if observed.returncode != 0 or observed.stdout.removesuffix("\n") != branch:
            raise RCPError(
                code="bootstrap_proposal_branch_mismatch",
                message="Bootstrap proposal worktree is on the wrong branch.",
            )

    def _head(self, root: Path) -> str:
        result = self._git(root, "rev-parse", "--verify", "HEAD^{commit}")
        commit = result.stdout.strip()
        if not _GIT_OBJECT_ID.fullmatch(commit):
            raise RCPError(
                code="git_output_invalid",
                message="Git returned an invalid bootstrap proposal object ID.",
            )
        return commit

    def _status_paths(self, root: Path, allowed: frozenset[str]) -> tuple[str, ...]:
        result = self._git(
            root,
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        )
        paths: list[str] = []
        for record in result.stdout.split("\x00"):
            if not record:
                continue
            if (
                len(record) < 4
                or record[2] != " "
                or "R" in record[:2]
                or "C" in record[:2]
            ):
                raise RCPError(
                    code="bootstrap_proposal_worktree_dirty",
                    message="Bootstrap proposal has an unsupported Git status entry.",
                )
            path = record[3:]
            if path not in allowed:
                raise RCPError(
                    code="bootstrap_proposal_worktree_dirty",
                    message="Bootstrap proposal changed a path outside its init manifest.",
                    context={"path": path},
                )
            paths.append(path)
        if len(paths) != len(set(paths)):
            raise RCPError(
                code="bootstrap_proposal_worktree_dirty",
                message="Bootstrap proposal returned duplicate changed paths.",
            )
        return tuple(sorted(paths))

    @staticmethod
    def _managed_path(relative: str) -> str:
        if (
            not isinstance(relative, str)
            or not relative
            or "\\" in relative
            or any(ord(character) < 32 for character in relative)
        ):
            raise RCPError(
                code="bootstrap_proposal_path_invalid",
                message="Bootstrap proposal manifest contains an unsafe path.",
            )
        pure = PurePosixPath(relative)
        normalized = pure.as_posix()
        if (
            pure.is_absolute()
            or ".." in pure.parts
            or ".git" in pure.parts
            or normalized != relative
            or not (
                normalized == ".researchctl.toml"
                or normalized.startswith(".research/")
            )
        ):
            raise RCPError(
                code="bootstrap_proposal_path_invalid",
                message="Bootstrap proposal manifest contains an unsafe path.",
                context={"path": relative},
            )
        return normalized

    @staticmethod
    def _existing_root(root: Path) -> Path:
        normalized = Path(os.path.abspath(os.fspath(root)))
        if normalized.is_symlink() or not normalized.is_dir():
            raise RCPError(
                code="bootstrap_proposal_worktree_invalid",
                message="Bootstrap proposal path must be a non-symlink directory.",
            )
        return normalized

    def _git(
        self,
        root: Path,
        *args: str,
        check: bool = True,
    ) -> CommandResult:
        try:
            result = self._runner.run(
                ("git", "-c", "core.fsmonitor=false", "-C", str(root), *args),
                cwd=None,
                env=_git_environment(),
                timeout_seconds=self._timeout_seconds,
            )
        except FileNotFoundError as error:
            raise RCPError(
                code="git_not_found",
                message="git executable was not found.",
            ) from error
        except subprocess.TimeoutExpired as error:
            raise RCPError(
                code="git_timeout",
                message="Git bootstrap proposal timed out.",
            ) from error
        except OSError as error:
            raise RCPError(
                code="git_execution_failed",
                message="Git bootstrap proposal could not execute.",
            ) from error
        except UnicodeError as error:
            raise RCPError(
                code="git_output_invalid",
                message="Git returned non-UTF-8 bootstrap proposal metadata.",
            ) from error
        if check and result.returncode != 0:
            self._raise_failed(result, args[0] if args else "run Git")
        return result

    @staticmethod
    def _raise_failed(result: CommandResult, operation: str) -> None:
        raise RCPError(
            code="git_bootstrap_proposal_command_failed",
            message=f"Git failed to {operation}.",
            context={"returncode": result.returncode},
        )


def _git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for key in tuple(environment):
        if key in _GIT_ENVIRONMENT_KEYS or key.startswith("GIT_CONFIG_"):
            environment.pop(key, None)
    environment.pop("GIT_CONFIG_PARAMETERS", None)
    return environment
