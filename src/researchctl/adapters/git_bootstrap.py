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
from researchctl.errors import RCPError


_OPERATION_ID = re.compile(r"^operation_\d{8}T\d{6}Z_[0-9a-f]{24}$")
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
class BootstrapAcceptanceReceipt:
    branch: str
    worktree: Path
    proposal_commit: str
    commit: str
    manifest_digest: str
    changed_paths: tuple[str, ...]
    changed: bool
    effect_applied: bool = True

    def as_dict(self) -> dict[str, object]:
        return {
            "branch": self.branch,
            "worktree": str(self.worktree),
            "proposal_commit": self.proposal_commit,
            "commit": self.commit,
            "manifest_digest": self.manifest_digest,
            "changed_paths": list(self.changed_paths),
            "changed": self.changed,
            "effect_applied": self.effect_applied,
            "transition_prepared": "bootstrapping_to_managed",
            "accepted": False,
            "requires_merge": True,
            "delivery": "local_acceptance_proposal",
        }


class GitBootstrapCommitAdapter:
    """Creates or verifies the one local commit for bootstrap acceptance."""

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

    @staticmethod
    def validate_identity(*, operation_id: str, branch: str) -> None:
        if not isinstance(operation_id, str) or not _OPERATION_ID.fullmatch(
            operation_id
        ):
            raise RCPError(
                code="bootstrap_operation_id_invalid",
                message="Bootstrap acceptance requires a canonical Operation ID.",
            )
        expected = f"research/control/{operation_id}"
        if branch != expected:
            raise RCPError(
                code="bootstrap_branch_invalid",
                message="Bootstrap acceptance branch does not match its Operation ID.",
                context={"expected_branch": expected},
            )

    @staticmethod
    def validate_commit(commit: str, *, label: str = "Proposal commit") -> None:
        if not isinstance(commit, str) or not _GIT_OBJECT_ID.fullmatch(commit):
            raise RCPError(
                code="bootstrap_proposal_commit_invalid",
                message=f"{label} must be a full lowercase Git object ID.",
            )

    def read_tree_file(
        self,
        *,
        root: Path,
        commit: str,
        relative: str,
    ) -> bytes | None:
        repository = self._existing_root(root)
        self.validate_commit(commit)
        normalized = self._managed_path(relative)
        listed = self._git(
            repository,
            "ls-tree",
            "--name-only",
            "-z",
            commit,
            "--",
            normalized,
        )
        if listed.stdout == "":
            return None
        if listed.stdout != f"{normalized}\x00":
            raise RCPError(
                code="bootstrap_tree_invalid",
                message="Git returned an unexpected managed tree entry.",
                context={"path": normalized},
            )
        shown = self._git(repository, "show", f"{commit}:{normalized}")
        try:
            return shown.stdout.encode("utf-8")
        except UnicodeError as error:
            raise RCPError(
                code="bootstrap_tree_invalid",
                message="A managed bootstrap file is not valid UTF-8.",
                context={"path": normalized},
            ) from error

    def controlled_tree_paths(self, *, root: Path, commit: str) -> tuple[str, ...]:
        repository = self._existing_root(root)
        self.validate_commit(commit)
        result = self._git(
            repository,
            "ls-tree",
            "-r",
            "-z",
            commit,
            "--",
            ".research",
            ".researchctl.toml",
        )
        paths: list[str] = []
        for entry in result.stdout.split("\x00"):
            if not entry:
                continue
            metadata, separator, path = entry.partition("\t")
            fields = metadata.split(" ")
            if (
                not separator
                or len(fields) != 3
                or fields[0] != "100644"
                or fields[1] != "blob"
                or not _GIT_OBJECT_ID.fullmatch(fields[2])
            ):
                raise RCPError(
                    code="bootstrap_tree_invalid",
                    message="Bootstrap managed tree entry is not a regular file.",
                )
            paths.append(path)
        if len(paths) != len(set(paths)):
            raise RCPError(
                code="bootstrap_tree_invalid",
                message="Git returned duplicate managed tree paths.",
            )
        for path in paths:
            self._managed_path(path)
        return tuple(sorted(paths))

    def attach_existing_branch(
        self,
        *,
        repository_root: Path,
        worktree: Path,
        branch: str,
        expected_head: str,
        operation_id: str,
    ) -> None:
        self.validate_identity(operation_id=operation_id, branch=branch)
        self.validate_commit(expected_head, label="Control branch head")
        repository = self._existing_root(repository_root)
        target = Path(os.path.abspath(os.fspath(worktree)))
        if (
            not target.is_absolute()
            or target.is_symlink()
            or os.path.lexists(target)
            or not target.parent.is_dir()
            or target.parent.is_symlink()
        ):
            raise RCPError(
                code="bootstrap_worktree_invalid",
                message="Recovered bootstrap worktree path is not safely absent.",
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
                code="bootstrap_branch_mismatch",
                message="Recovered bootstrap branch head changed unexpectedly.",
            )
        self._git(
            repository,
            "worktree",
            "add",
            "--",
            str(target),
            branch,
        )
        recovered = self._existing_root(
            target,
            code="bootstrap_worktree_invalid",
            label="Recovered bootstrap worktree",
        )
        self._require_branch(recovered, branch)
        if self._head(recovered) != expected_head:
            raise RCPError(
                code="bootstrap_branch_mismatch",
                message="Recovered bootstrap worktree has the wrong HEAD.",
            )

    def is_ancestor(self, *, root: Path, ancestor: str, descendant: str) -> bool:
        repository = self._existing_root(root)
        self.validate_commit(ancestor, label="Default-branch commit")
        self.validate_commit(descendant)
        result = self._git(
            repository,
            "merge-base",
            "--is-ancestor",
            ancestor,
            descendant,
            check=False,
        )
        if result.returncode not in {0, 1}:
            self._raise_failed(result, "verify proposal ancestry")
        return result.returncode == 0

    def observe_prepared(
        self,
        *,
        worktree: Path,
        branch: str,
        proposal_commit: str,
        operation_id: str,
        manifest_digest: str,
        desired_files: Mapping[str, bytes],
    ) -> BootstrapAcceptanceReceipt | None:
        root, normalized_files = self._validate_request(
            worktree=worktree,
            branch=branch,
            proposal_commit=proposal_commit,
            operation_id=operation_id,
            manifest_digest=manifest_digest,
            desired_files=desired_files,
        )
        self._require_branch(root, branch)
        changed_paths = self._status_paths(root, frozenset(normalized_files))
        head = self._head(root)
        if head == proposal_commit:
            return None
        if changed_paths:
            raise RCPError(
                code="bootstrap_worktree_dirty",
                message="A completed bootstrap acceptance worktree must be clean.",
            )
        return self._verify_commit(
            root=root,
            branch=branch,
            proposal_commit=proposal_commit,
            operation_id=operation_id,
            manifest_digest=manifest_digest,
            desired_files=normalized_files,
            commit=head,
            changed=False,
        )

    def commit_or_observe(
        self,
        *,
        worktree: Path,
        branch: str,
        proposal_commit: str,
        operation_id: str,
        manifest_digest: str,
        desired_files: Mapping[str, bytes],
    ) -> BootstrapAcceptanceReceipt:
        root, normalized_files = self._validate_request(
            worktree=worktree,
            branch=branch,
            proposal_commit=proposal_commit,
            operation_id=operation_id,
            manifest_digest=manifest_digest,
            desired_files=desired_files,
        )
        self._require_branch(root, branch)
        allowed = frozenset(normalized_files)
        changed_paths = self._status_paths(root, allowed)
        head = self._head(root)
        if head != proposal_commit:
            if changed_paths:
                raise RCPError(
                    code="bootstrap_worktree_dirty",
                    message="A completed bootstrap acceptance worktree must be clean.",
                )
            return self._verify_commit(
                root=root,
                branch=branch,
                proposal_commit=proposal_commit,
                operation_id=operation_id,
                manifest_digest=manifest_digest,
                desired_files=normalized_files,
                commit=head,
                changed=False,
            )
        if not changed_paths or _PROJECT_RECORD_PATH not in changed_paths:
            raise RCPError(
                code="bootstrap_transition_missing",
                message="Bootstrap acceptance did not change the Project state record.",
            )

        paths = tuple(sorted(normalized_files))
        self._git(root, "add", "--", *paths)
        staged = self._git(root, "diff", "--cached", "--quiet", check=False)
        if staged.returncode not in {0, 1}:
            self._raise_failed(staged, "inspect staged bootstrap acceptance")
        if staged.returncode == 0:
            raise RCPError(
                code="bootstrap_transition_missing",
                message="Bootstrap acceptance has no staged Project transition.",
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
            f"researchctl: bootstrap.accept {operation_id}",
            "-m",
            f"manifest-digest: {manifest_digest}",
        )
        commit = self._head(root)
        return self._verify_commit(
            root=root,
            branch=branch,
            proposal_commit=proposal_commit,
            operation_id=operation_id,
            manifest_digest=manifest_digest,
            desired_files=normalized_files,
            commit=commit,
            changed=True,
        )

    def _validate_request(
        self,
        *,
        worktree: Path,
        branch: str,
        proposal_commit: str,
        operation_id: str,
        manifest_digest: str,
        desired_files: Mapping[str, bytes],
    ) -> tuple[Path, dict[str, bytes]]:
        self.validate_identity(operation_id=operation_id, branch=branch)
        self.validate_commit(proposal_commit)
        if not isinstance(manifest_digest, str) or not _SHA256_DIGEST.fullmatch(
            manifest_digest
        ):
            raise RCPError(
                code="bootstrap_manifest_digest_invalid",
                message="Bootstrap manifest digest must be a canonical SHA-256 digest.",
            )
        root = self._existing_root(
            worktree,
            code="bootstrap_worktree_invalid",
            label="Bootstrap acceptance worktree",
        )
        normalized: dict[str, bytes] = {}
        for relative, content in desired_files.items():
            path = self._managed_path(relative)
            if path in normalized or not isinstance(content, bytes):
                raise RCPError(
                    code="bootstrap_manifest_invalid",
                    message="Bootstrap manifest paths and bytes must be canonical.",
                )
            normalized[path] = content
        if _PROJECT_RECORD_PATH not in normalized:
            raise RCPError(
                code="bootstrap_manifest_invalid",
                message="Bootstrap manifest must include the Project state record.",
            )
        return root, normalized

    def _verify_commit(
        self,
        *,
        root: Path,
        branch: str,
        proposal_commit: str,
        operation_id: str,
        manifest_digest: str,
        desired_files: Mapping[str, bytes],
        commit: str,
        changed: bool,
    ) -> BootstrapAcceptanceReceipt:
        parents = self._git(root, "show", "-s", "--format=%P", commit)
        if parents.stdout.strip() != proposal_commit:
            raise RCPError(
                code="bootstrap_commit_invalid",
                message="Bootstrap acceptance must be one commit over the proposal head.",
            )
        message = self._git(root, "show", "-s", "--format=%B", commit)
        expected_message = (
            f"researchctl: bootstrap.accept {operation_id}\n\n"
            f"manifest-digest: {manifest_digest}"
        )
        if message.stdout.rstrip("\n") != expected_message:
            raise RCPError(
                code="bootstrap_commit_invalid",
                message="Bootstrap acceptance commit has the wrong operation marker.",
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
        allowed = frozenset(desired_files)
        if (
            not changed_paths
            or _PROJECT_RECORD_PATH not in changed_paths
            or any(path not in allowed for path in changed_paths)
        ):
            raise RCPError(
                code="bootstrap_commit_invalid",
                message="Bootstrap acceptance commit changed an unexpected path.",
            )

        tree_paths = self.controlled_tree_paths(root=root, commit=commit)
        if tree_paths != tuple(sorted(desired_files)):
            raise RCPError(
                code="bootstrap_commit_invalid",
                message="Bootstrap acceptance tree has unexpected managed content.",
            )
        for relative, expected in desired_files.items():
            observed = self.read_tree_file(
                root=root,
                commit=commit,
                relative=relative,
            )
            if observed != expected:
                raise RCPError(
                    code="bootstrap_commit_invalid",
                    message="Bootstrap acceptance commit differs from its manifest.",
                    context={"path": relative},
                )
        if self._status_paths(root, frozenset(desired_files)):
            raise RCPError(
                code="bootstrap_worktree_dirty",
                message="Bootstrap acceptance worktree is dirty after commit.",
            )
        return BootstrapAcceptanceReceipt(
            branch=branch,
            worktree=root,
            proposal_commit=proposal_commit,
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
                code="bootstrap_branch_mismatch",
                message="Bootstrap worktree is not on the operation branch.",
                context={"branch": branch},
            )

    def _head(self, root: Path) -> str:
        result = self._git(root, "rev-parse", "--verify", "HEAD^{commit}")
        commit = result.stdout.strip()
        if not _GIT_OBJECT_ID.fullmatch(commit):
            raise RCPError(
                code="git_output_invalid",
                message="Git returned an invalid bootstrap commit object ID.",
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
                    code="bootstrap_worktree_dirty",
                    message="Bootstrap worktree has an unsupported Git status entry.",
                )
            path = record[3:]
            if path not in allowed:
                raise RCPError(
                    code="bootstrap_worktree_dirty",
                    message="Bootstrap worktree contains a change outside its manifest.",
                    context={"path": path},
                )
            paths.append(path)
        if len(paths) != len(set(paths)):
            raise RCPError(
                code="bootstrap_worktree_dirty",
                message="Bootstrap worktree returned duplicate changed paths.",
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
                code="bootstrap_path_invalid",
                message="Bootstrap manifest contains an unsafe path.",
            )
        pure = PurePosixPath(relative)
        normalized = pure.as_posix()
        if (
            pure.is_absolute()
            or ".." in pure.parts
            or normalized in {"", "."}
            or normalized != relative
            or ".git" in pure.parts
            or not (
                normalized == ".researchctl.toml"
                or normalized.startswith(".research/")
            )
        ):
            raise RCPError(
                code="bootstrap_path_invalid",
                message="Bootstrap manifest contains an unsafe path.",
                context={"path": relative},
            )
        return normalized

    @staticmethod
    def _existing_root(
        root: Path,
        *,
        code: str = "bootstrap_repository_invalid",
        label: str = "Bootstrap repository",
    ) -> Path:
        normalized = Path(os.path.abspath(os.fspath(root)))
        if normalized.is_symlink() or not normalized.is_dir():
            raise RCPError(
                code=code,
                message=f"{label} must be an existing non-symlink directory.",
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
                message="Git bootstrap acceptance timed out.",
            ) from error
        except OSError as error:
            raise RCPError(
                code="git_execution_failed",
                message="Git bootstrap acceptance could not execute.",
            ) from error
        except UnicodeError as error:
            raise RCPError(
                code="git_output_invalid",
                message="Git returned non-UTF-8 bootstrap metadata.",
            ) from error
        if check and result.returncode != 0:
            self._raise_failed(result, args[0] if args else "run Git")
        return result

    @staticmethod
    def _raise_failed(result: CommandResult, operation: str) -> None:
        raise RCPError(
            code="git_bootstrap_command_failed",
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
