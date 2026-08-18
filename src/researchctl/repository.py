from __future__ import annotations

import os
import stat
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit, urlunsplit

from researchctl.constants import PROJECT_DIR_NAME
from researchctl.errors import (
    RCPError,
    RepositoryNotFoundError,
    UnsafeRepositoryPathError,
)


def safe_repository_path(
    root: Path,
    relative: str,
    *,
    managed_only: bool = False,
) -> Path:
    """Resolve a lexical repository path while rejecting symlink traversal."""
    if not relative or "\\" in relative or "\x00" in relative:
        raise UnsafeRepositoryPathError(relative, reason="invalid path characters")

    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts:
        raise UnsafeRepositoryPathError(relative, reason="path escapes repository root")

    normalized = pure.as_posix()
    if normalized in {"", "."}:
        raise UnsafeRepositoryPathError(relative, reason="path does not identify a file")
    if managed_only and pure.parts[0] != PROJECT_DIR_NAME:
        raise UnsafeRepositoryPathError(
            relative,
            reason=f"managed path must be inside {PROJECT_DIR_NAME}",
        )

    candidate = root.resolve()
    for part in pure.parts:
        candidate = candidate / part
        try:
            mode = candidate.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise UnsafeRepositoryPathError(
                normalized,
                reason="path contains a symbolic link",
            )
    return candidate


def sanitize_remote_url(remote_url: str | None) -> str | None:
    """Remove URL credentials and query tokens before persisting repository identity."""
    if remote_url is None:
        return None
    value = remote_url.strip()
    if not value:
        return None

    if "://" in value:
        parsed = urlsplit(value)
        if not parsed.scheme or parsed.hostname is None:
            return None
        hostname = parsed.hostname
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        try:
            port = f":{parsed.port}" if parsed.port is not None else ""
        except ValueError:
            return None
        return urlunsplit((parsed.scheme, f"{hostname}{port}", parsed.path, "", ""))

    colon = value.find(":")
    at = value.find("@")
    if 0 <= at < colon:
        value = value[at + 1 :]
    if "?" in value or "#" in value:
        value = value.split("?", maxsplit=1)[0].split("#", maxsplit=1)[0]
    return value


@dataclass(frozen=True, slots=True)
class GitRepository:
    root: Path
    default_branch: str
    remote_url: str | None
    default_branch_source: str


_GIT_CONTEXT_ENVIRONMENT = {
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_WORK_TREE",
}


def _git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for key in tuple(environment):
        if key in _GIT_CONTEXT_ENVIRONMENT or key.startswith("GIT_CONFIG_"):
            environment.pop(key, None)
    environment.pop("GIT_CONFIG_PARAMETERS", None)
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    return environment


def _git(
    path: Path,
    *args: str,
    check: bool = True,
    timeout_seconds: float = 10.0,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "-C", str(path), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=_git_environment(),
        )
    except FileNotFoundError as exc:
        raise RCPError(
            code="git_not_found",
            message="git executable was not found",
            remediation="Install Git and ensure it is available on PATH.",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RCPError(
            code="git_timeout",
            message=f"Git command timed out after {timeout_seconds:g} seconds.",
            context={"path": str(path), "args": list(args)},
        ) from exc

    if check and result.returncode != 0:
        raise RCPError(
            code="git_command_failed",
            message=result.stderr.strip() or "Git command failed.",
            context={
                "path": str(path),
                "args": list(args),
                "returncode": result.returncode,
            },
        )
    return result


def discover_repository(
    path: Path,
    *,
    default_branch: str | None = None,
) -> GitRepository:
    candidate = path.expanduser().resolve()
    if candidate.is_file():
        candidate = candidate.parent
    result = _git(candidate, "rev-parse", "--show-toplevel", check=False)
    if result.returncode != 0:
        raise RepositoryNotFoundError(str(candidate))
    root = Path(result.stdout.strip()).resolve()
    if candidate != root and root not in candidate.parents:
        raise RCPError(
            code="git_repository_mismatch",
            message="Git returned a worktree that does not contain the requested path.",
            context={"path": str(candidate), "root": str(root)},
        )

    remote = _git(root, "remote", "get-url", "origin", check=False)
    remote_url = sanitize_remote_url(remote.stdout if remote.returncode == 0 else None)

    selected = default_branch
    source = "explicit" if selected is not None else "unknown"
    if selected is not None:
        valid = _git(root, "check-ref-format", "--branch", selected, check=False)
        if valid.returncode != 0:
            raise RCPError(
                code="invalid_default_branch",
                message=f"Invalid Git default branch: {selected}",
                context={"default_branch": selected},
            )

    if selected is None:
        origin_head = _git(
            root,
            "symbolic-ref",
            "--quiet",
            "--short",
            "refs/remotes/origin/HEAD",
            check=False,
        )
        if origin_head.returncode == 0 and origin_head.stdout.strip().startswith("origin/"):
            selected = origin_head.stdout.strip().removeprefix("origin/")
            source = "origin_head"

    if selected is None:
        current = _git(root, "branch", "--show-current", check=False)
        if current.returncode == 0 and current.stdout.strip():
            selected = current.stdout.strip()
            source = "current_branch"

    if selected is None:
        configured = _git(root, "config", "--get", "init.defaultBranch", check=False)
        if configured.returncode == 0 and configured.stdout.strip():
            selected = configured.stdout.strip()
            source = "configured_default"

    return GitRepository(
        root=root,
        default_branch=selected or "main",
        remote_url=remote_url,
        default_branch_source=source if selected is not None else "fallback_main",
    )


def status_porcelain(repository: GitRepository) -> tuple[str, ...]:
    result = _git(
        repository.root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    )
    return tuple(line for line in result.stdout.splitlines() if line)


def current_head(repository: GitRepository) -> str | None:
    result = _git(repository.root, "rev-parse", "HEAD", check=False)
    return result.stdout.strip() if result.returncode == 0 else None


@dataclass(frozen=True, slots=True)
class PathHistory:
    """When Git last recorded a change to one exact path.

    ``present`` is false for a path Git has never committed -- a new file, or a
    file that exists only in the working tree. There is deliberately no
    filesystem fallback: an mtime records when a checkout happened, not when
    anyone edited the document, so a missing history is reported as missing
    rather than approximated.
    """

    present: bool
    last_edited_at: datetime | None = None


def last_commit_timestamp(
    repository: GitRepository,
    relative_path: str,
) -> PathHistory:
    """Return the latest commit time for one exact repository-relative path.

    Only a successful, empty ``git log`` means the path has no history. A Git
    invocation that fails, or output this function cannot parse, is an error
    and is raised: reporting it as "no history" would let a broken environment
    quietly erase every edit time a caller is about to publish.
    """

    # Validate before Git sees it: a caller must not be able to turn a document
    # path into an arbitrary pathspec or walk out of the repository.
    safe_repository_path(repository.root, relative_path)
    if current_head(repository) is None:
        # A repository with no commits has no history for any path. That is an
        # answer, not a failure -- but an unresolvable HEAD looks exactly the
        # same from ``rev-parse`` alone. A checked, read-only status separates
        # an unborn branch from a Git state broken enough to report every
        # document as never edited.
        _git(repository.root, "status", "--porcelain=v1", "--untracked-files=no")
        return PathHistory(present=False)
    result = _git(
        repository.root,
        "log",
        "-1",
        # Renames are the normal way a document moves, and the edit history of
        # the file before its move is still its edit history.
        "--follow",
        # %cI is committer date in strict ISO 8601; it never depends on a
        # configured log.date or on the caller's locale.
        "--format=%cI",
        "--",
        # Literal pathspec magic: a document path is a path, never a glob.
        f":(literal){relative_path}",
    )
    recorded = result.stdout.strip()
    if not recorded:
        return PathHistory(present=False)
    try:
        stamp = datetime.fromisoformat(recorded.splitlines()[0].strip())
    except ValueError as error:
        raise RCPError(
            code="git_history_unreadable",
            message=f"Git returned an unparseable commit date: {recorded!r}",
            context={"path": relative_path},
        ) from error
    if stamp.tzinfo is None:
        raise RCPError(
            code="git_history_unreadable",
            message="Git returned a commit date with no time zone.",
            context={"path": relative_path, "recorded": recorded},
        )
    return PathHistory(present=True, last_edited_at=stamp.astimezone(UTC))
