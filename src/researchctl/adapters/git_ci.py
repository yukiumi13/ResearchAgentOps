from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Never

from researchctl.adapters._subprocess import (
    CommandResult,
    CommandRunner,
    SubprocessCommandRunner,
)
from researchctl.errors import RCPError


_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_RAW_OBJECT_ID = re.compile(
    r"^(?:[0-9a-f]{40}|[0-9a-f]{64}|0{40}|0{64})$"
)
_PROTECTED_BRANCH_REF = re.compile(
    r"^refs/heads/[A-Za-z0-9][A-Za-z0-9._/-]{0,511}$"
)
_MODE = re.compile(r"^[0-7]{6}$")
_MAX_COMMIT_BYTES = 256 * 1024
_MAX_RECORD_BYTES = 2 * 1024 * 1024
_MAX_TREE_ENTRIES = 100_000
_GIT_ENVIRONMENT_KEYS = {
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_WORK_TREE",
}


@dataclass(frozen=True, slots=True)
class GitCommitData:
    object_id: str
    tree: str
    parents: tuple[str, ...]
    message: str


@dataclass(frozen=True, slots=True)
class GitTreeEntry:
    mode: str
    object_type: str
    object_id: str
    path: str


@dataclass(frozen=True, slots=True)
class GitTreeChange:
    path: str
    old_mode: str
    new_mode: str
    old_object: str
    new_object: str
    status: str


class GitCIObjectReader:
    """Read an untrusted PR head as Git data without checking it out."""

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

    def read_commit(self, repository_root: Path, object_id: str) -> GitCommitData:
        root = self._directory(repository_root)
        self._require_object_id(object_id)
        object_type = self._git(root, "cat-file", "-t", object_id, check=False)
        if object_type.returncode != 0:
            raise RCPError(
                code="ci_git_object_missing",
                message="A commit required by exact-head validation is unavailable.",
                context={"object_id": object_id},
            )
        if object_type.stdout.removesuffix("\n") != "commit":
            raise RCPError(
                code="ci_git_object_invalid",
                message="Exact-head validation requires a Git commit object.",
                context={"object_id": object_id},
            )
        size = self._object_size(root, object_id)
        if size > _MAX_COMMIT_BYTES:
            raise RCPError(
                code="ci_git_object_invalid",
                message="Commit metadata exceeds the exact-head verification limit.",
                context={"object_id": object_id, "size_bytes": size},
            )
        content = self._git(root, "cat-file", "commit", object_id).stdout
        if len(content.encode("utf-8")) != size:
            self._invalid_output("Git returned truncated or transformed commit data.")
        headers, separator, message = content.partition("\n\n")
        if not separator:
            self._invalid_output("Git returned malformed commit data.")
        tree: str | None = None
        parents: list[str] = []
        for line in headers.splitlines():
            if line.startswith("tree "):
                candidate = line.removeprefix("tree ")
                self._require_object_id(candidate)
                if tree is not None:
                    self._invalid_output("Git commit contains duplicate tree headers.")
                tree = candidate
            elif line.startswith("parent "):
                parent = line.removeprefix("parent ")
                self._require_object_id(parent)
                parents.append(parent)
        if tree is None:
            self._invalid_output("Git commit does not contain a tree header.")
        return GitCommitData(
            object_id=object_id,
            tree=tree,
            parents=tuple(parents),
            message=message,
        )

    def object_type(
        self,
        repository_root: Path,
        object_id: str,
    ) -> Literal["blob", "commit", "tag", "tree"] | None:
        """Return the immutable Git object type without resolving a revision."""

        root = self._directory(repository_root)
        self._require_object_id(object_id)
        result = self._git(root, "cat-file", "-t", object_id, check=False)
        if result.returncode != 0:
            return None
        observed = result.stdout.removesuffix("\n")
        if observed not in {"blob", "commit", "tag", "tree"}:
            self._invalid_output("Git returned an invalid object type.")
        return observed  # type: ignore[return-value]

    def read_blob_at(
        self,
        repository_root: Path,
        *,
        commit: str,
        path: str,
        required: bool = True,
    ) -> bytes | None:
        entries = self.list_entries(
            repository_root,
            commit=commit,
            path=path,
            recursive=False,
        )
        if not entries:
            if not required:
                return None
            raise RCPError(
                code="ci_record_missing",
                message="Exact-head validation is missing a required record.",
                context={"commit": commit, "path": path},
            )
        if len(entries) != 1 or entries[0].path != path:
            self._invalid_tree_entry(commit, path)
        entry = entries[0]
        if entry.mode != "100644" or entry.object_type != "blob":
            self._invalid_tree_entry(commit, path)
        root = self._directory(repository_root)
        size = self._object_size(root, entry.object_id)
        if size > _MAX_RECORD_BYTES:
            raise RCPError(
                code="ci_record_too_large",
                message="A generated record exceeds the exact-head verification limit.",
                context={"commit": commit, "path": path, "size_bytes": size},
            )
        content = self._git(root, "cat-file", "blob", entry.object_id).stdout
        encoded = content.encode("utf-8")
        if len(encoded) != size:
            self._invalid_output("Git returned truncated or transformed blob data.")
        return encoded

    def list_entries(
        self,
        repository_root: Path,
        *,
        commit: str,
        path: str,
        recursive: bool = True,
    ) -> tuple[GitTreeEntry, ...]:
        root = self._directory(repository_root)
        self._require_object_id(commit)
        arguments = ["ls-tree", "-z", "--full-tree"]
        if recursive:
            arguments.append("-r")
        arguments.extend((commit, "--", path))
        output = self._git(root, *arguments).stdout
        entries: list[GitTreeEntry] = []
        for raw in (item for item in output.split("\x00") if item):
            metadata, separator, observed_path = raw.partition("\t")
            fields = metadata.split(" ")
            if (
                not separator
                or len(fields) != 3
                or not _MODE.fullmatch(fields[0])
                or fields[1] not in {"blob", "commit", "tree"}
                or not _OBJECT_ID.fullmatch(fields[2])
                or not observed_path
            ):
                self._invalid_output("Git returned a malformed tree entry.")
            entries.append(
                GitTreeEntry(
                    mode=fields[0],
                    object_type=fields[1],
                    object_id=fields[2],
                    path=observed_path,
                )
            )
            if len(entries) > _MAX_TREE_ENTRIES:
                raise RCPError(
                    code="ci_tree_too_large",
                    message="The inspected Git tree exceeds the validation entry limit.",
                )
        paths = [entry.path for entry in entries]
        if len(paths) != len(set(paths)):
            self._invalid_output("Git returned duplicate tree paths.")
        return tuple(sorted(entries, key=lambda item: item.path))

    def changes(
        self,
        repository_root: Path,
        *,
        old_commit: str,
        new_commit: str,
    ) -> tuple[GitTreeChange, ...]:
        root = self._directory(repository_root)
        self._require_object_id(old_commit)
        self._require_object_id(new_commit)
        output = self._git(
            root,
            "diff-tree",
            "--no-commit-id",
            "--no-ext-diff",
            "--no-renames",
            "--no-abbrev",
            "--raw",
            "-r",
            "-z",
            old_commit,
            new_commit,
        ).stdout
        chunks = output.split("\x00")
        if chunks and chunks[-1] == "":
            chunks.pop()
        if len(chunks) % 2:
            self._invalid_output("Git returned a malformed raw tree diff.")
        changes: list[GitTreeChange] = []
        for index in range(0, len(chunks), 2):
            metadata = chunks[index]
            path = chunks[index + 1]
            fields = metadata.split(" ")
            if (
                len(fields) != 5
                or not fields[0].startswith(":")
                or not _MODE.fullmatch(fields[0][1:])
                or not _MODE.fullmatch(fields[1])
                or not _RAW_OBJECT_ID.fullmatch(fields[2])
                or not _RAW_OBJECT_ID.fullmatch(fields[3])
                or fields[4] not in {"A", "D", "M", "T", "U"}
                or not path
            ):
                self._invalid_output("Git returned a malformed raw tree change.")
            changes.append(
                GitTreeChange(
                    path=path,
                    old_mode=fields[0][1:],
                    new_mode=fields[1],
                    old_object=fields[2],
                    new_object=fields[3],
                    status=fields[4],
                )
            )
        paths = [change.path for change in changes]
        if len(paths) != len(set(paths)):
            self._invalid_output("Git returned duplicate changed paths.")
        return tuple(sorted(changes, key=lambda item: item.path))

    def is_ancestor(
        self,
        repository_root: Path,
        *,
        ancestor: str,
        descendant: str,
    ) -> bool:
        """Check commit lineage without checking out or executing either tree."""

        root = self._directory(repository_root)
        self._require_object_id(ancestor)
        self._require_object_id(descendant)
        result = self._git(
            root,
            "merge-base",
            "--is-ancestor",
            ancestor,
            descendant,
            check=False,
        )
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        raise RCPError(
            code="ci_git_command_failed",
            message="Git failed while checking exact-head commit lineage.",
            context={"operation": "merge-base", "returncode": result.returncode},
        )

    def resolve_protected_branch(
        self,
        repository_root: Path,
        reference: str,
    ) -> str | None:
        """Resolve one fully qualified local protected-branch ref."""

        root = self._directory(repository_root)
        if (
            not isinstance(reference, str)
            or not _PROTECTED_BRANCH_REF.fullmatch(reference)
            or ".." in reference
            or "//" in reference
            or reference.endswith(("/", ".", ".lock"))
        ):
            raise RCPError(
                code="ci_git_ref_invalid",
                message="Protected-branch verification requires a canonical local ref.",
            )
        result = self._git(
            root,
            "rev-parse",
            "--verify",
            "--quiet",
            f"{reference}^{{commit}}",
            check=False,
        )
        if result.returncode == 1:
            return None
        if result.returncode != 0:
            raise RCPError(
                code="ci_git_command_failed",
                message="Git failed while resolving the protected branch.",
                context={"operation": "rev-parse", "returncode": result.returncode},
            )
        object_id = result.stdout.removesuffix("\n")
        if not _OBJECT_ID.fullmatch(object_id):
            self._invalid_output("Git returned an invalid protected-branch commit.")
        return object_id

    def _object_size(self, root: Path, object_id: str) -> int:
        output = self._git(root, "cat-file", "-s", object_id).stdout.removesuffix("\n")
        if not output.isascii() or not output.isdecimal():
            self._invalid_output("Git returned an invalid object size.")
        return int(output)

    @staticmethod
    def _directory(path: Path) -> Path:
        candidate = Path(os.path.abspath(os.fspath(path)))
        if candidate.is_symlink() or not candidate.is_dir():
            raise RCPError(
                code="ci_repository_invalid",
                message="Exact-head validation requires an existing non-symlink directory.",
                context={"path": str(candidate)},
            )
        return candidate

    @staticmethod
    def _require_object_id(value: str) -> None:
        if not isinstance(value, str) or not _OBJECT_ID.fullmatch(value):
            raise RCPError(
                code="ci_git_identity_invalid",
                message="Exact-head validation requires a full Git object ID.",
            )

    @staticmethod
    def _invalid_output(message: str) -> Never:
        raise RCPError(code="ci_git_output_invalid", message=message)

    @staticmethod
    def _invalid_tree_entry(commit: str, path: str) -> Never:
        raise RCPError(
            code="ci_tree_entry_invalid",
            message="Generated files must be regular non-executable Git blobs.",
            context={"commit": commit, "path": path},
        )

    def _git(
        self,
        root: Path,
        *arguments: str,
        check: bool = True,
    ) -> CommandResult:
        try:
            result = self._runner.run(
                (
                    "git",
                    "-c",
                    "core.fsmonitor=false",
                    "-C",
                    str(root),
                    *arguments,
                ),
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
                code="ci_git_timeout",
                message="Exact-head Git validation timed out.",
                context={"root": str(root)},
            ) from error
        except UnicodeError as error:
            raise RCPError(
                code="ci_git_output_invalid",
                message="Git returned non-UTF-8 exact-head data.",
            ) from error
        if check and result.returncode != 0:
            raise RCPError(
                code="ci_git_command_failed",
                message="Git failed while reading exact-head data.",
                context={
                    "operation": arguments[0] if arguments else "read",
                    "returncode": result.returncode,
                },
            )
        return result


def _git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for key in tuple(environment):
        if key in _GIT_ENVIRONMENT_KEYS or key.startswith("GIT_CONFIG_"):
            environment.pop(key, None)
    environment.pop("GIT_CONFIG_PARAMETERS", None)
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    return environment
