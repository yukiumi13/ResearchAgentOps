from __future__ import annotations

import os
import stat
import subprocess
from dataclasses import dataclass
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
