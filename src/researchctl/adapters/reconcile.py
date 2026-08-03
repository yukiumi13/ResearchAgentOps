from __future__ import annotations

import os
import re
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from researchctl.adapters._subprocess import (
    CommandResult,
    CommandRunner,
    SubprocessCommandRunner,
)
from researchctl.errors import RCPError


_GIT_CONTEXT_ENVIRONMENT = {
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_WORK_TREE",
}
_GIT_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SESSION_ID_TEXT = r"session_\d{8}T\d{6}Z_[0-9a-f]{24}"
_TASK_KEY_TEXT = r"[A-Za-z][A-Za-z0-9._-]{0,63}"
_MANAGED_BRANCH = re.compile(
    rf"^research/task/(?P<task_key>{_TASK_KEY_TEXT})/"
    rf"(?P<session_id>{_SESSION_ID_TEXT})$"
)
_MANAGED_REF = re.compile(
    rf"^refs/heads/research/task/(?P<task_key>{_TASK_KEY_TEXT})/"
    rf"(?P<session_id>{_SESSION_ID_TEXT})$"
)
_TMUX_SESSION = re.compile(rf"^research-(?P<session_id>{_SESSION_ID_TEXT})$")
_RUN_ID_TEXT = r"run_\d{8}T\d{6}Z_[0-9a-f]{24}"
_RUN_BRANCH_REF = re.compile(
    rf"^refs/heads/research/run/(?P<run_id>{_RUN_ID_TEXT})$"
)
_RUN_TAG_REF = re.compile(
    rf"^refs/tags/research-run/(?P<run_id>{_RUN_ID_TEXT})$"
)
_MAX_RUN_MARKER_BYTES = 256 * 1024


@dataclass(frozen=True, slots=True)
class ObservationFailure:
    component: str
    code: str
    message: str
    returncode: int | None = None


@dataclass(frozen=True, slots=True)
class ManagedBranchObservation:
    name: str
    ref: str
    task_key: str
    session_id: str
    commit: str


@dataclass(frozen=True, slots=True)
class WorktreeInventoryEntry:
    path: Path
    head: str | None
    branch_ref: str | None
    prunable: bool = False


@dataclass(frozen=True, slots=True)
class RunRefObservation:
    kind: str
    ref: str
    run_id: str | None
    commit: str | None
    object_type: str
    parents: tuple[str, ...]
    spec_text: str | None
    result_text: str | None
    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RunMarkerObservation:
    path: Path
    content: bytes | None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class LocalReconcileObservation:
    branches: tuple[ManagedBranchObservation, ...]
    worktrees: tuple[WorktreeInventoryEntry, ...]
    tmux_sessions: tuple[str, ...]
    failures: tuple[ObservationFailure, ...]
    git_branches_complete: bool
    git_worktrees_complete: bool
    tmux_complete: bool
    run_refs: tuple[RunRefObservation, ...]
    run_markers: tuple[RunMarkerObservation, ...]
    run_refs_complete: bool
    run_records_complete: bool
    run_markers_complete: bool


class LocalReconcileObserver:
    """Bounded, read-only inventory of local Session and Run evidence."""

    def __init__(
        self,
        repository_root: Path,
        *,
        runner: CommandRunner | None = None,
        timeout_seconds: float = 5.0,
        max_records: int = 500,
        max_output_bytes: int = 1024 * 1024,
        run_marker_directory: Path | None = None,
        max_run_observation_seconds: float = 15.0,
    ) -> None:
        root = Path(os.path.abspath(os.fspath(repository_root)))
        if root.is_symlink() or not root.is_dir():
            raise RCPError(
                code="reconcile_repository_invalid",
                message="Reconcile requires an existing non-symlink repository root.",
                context={"root": str(root)},
            )
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if not 1 <= max_records <= 10_000:
            raise ValueError("max_records must be between 1 and 10000")
        if not 1024 <= max_output_bytes <= 16 * 1024 * 1024:
            raise ValueError("max_output_bytes must be between 1024 and 16777216")
        if max_run_observation_seconds <= 0:
            raise ValueError("max_run_observation_seconds must be positive")
        self._root = root
        self._runner = runner or SubprocessCommandRunner()
        self._timeout_seconds = timeout_seconds
        self._max_records = max_records
        self._max_output_bytes = max_output_bytes
        self._max_run_observation_seconds = max_run_observation_seconds
        self._run_marker_directory = (
            Path(os.path.abspath(os.fspath(run_marker_directory)))
            if run_marker_directory is not None
            else None
        )

    def observe(
        self,
        *,
        run_marker_directory: Path | None = None,
    ) -> LocalReconcileObservation:
        branches, branch_failure = self._observe_branches()
        worktrees, worktree_failure = self._observe_worktrees()
        tmux_sessions, tmux_failure = self._observe_tmux()
        run_deadline = time.monotonic() + self._max_run_observation_seconds
        run_refs, run_ref_failure = self._observe_run_refs(run_deadline)
        run_markers, run_marker_failure = self._observe_run_markers(
            run_marker_directory,
            deadline=run_deadline,
        )
        failures = tuple(
            failure
            for failure in (
                branch_failure,
                worktree_failure,
                tmux_failure,
                run_ref_failure,
                run_marker_failure,
            )
            if failure is not None
        )
        return LocalReconcileObservation(
            branches=branches,
            worktrees=worktrees,
            tmux_sessions=tmux_sessions,
            failures=failures,
            git_branches_complete=branch_failure is None,
            git_worktrees_complete=worktree_failure is None,
            tmux_complete=tmux_failure is None,
            run_refs=run_refs,
            run_markers=run_markers,
            run_refs_complete=(
                run_ref_failure is None or run_ref_failure.component == "run_records"
            ),
            run_records_complete=run_ref_failure is None,
            run_markers_complete=run_marker_failure is None,
        )

    def _observe_run_refs(
        self,
        deadline: float,
    ) -> tuple[tuple[RunRefObservation, ...], ObservationFailure | None]:
        result, failure = self._run(
            "run_refs",
            (
                "git",
                "-c",
                "core.fsmonitor=false",
                "-C",
                str(self._root),
                "for-each-ref",
                "--format=%(refname)%00%(objectname)%00%(objecttype)%00%(parent)",
                "refs/heads/research/run/",
                "refs/tags/research-run/",
            ),
            env=self._git_environment(),
            deadline=deadline,
        )
        if failure is not None or result is None:
            return (), failure
        if result.returncode != 0:
            return (), self._command_failure("run_refs", result)
        try:
            self._check_output_bound(result.stdout)
            refs = self._parse_run_refs(result.stdout)
            self._check_record_bound(len(refs))
        except ValueError:
            return (), self._invalid_output("run_refs")

        cache: dict[
            tuple[str, str], tuple[str | None, str | None, tuple[str, ...]]
        ] = {}
        observed: list[RunRefObservation] = []
        for ref in refs:
            spec_text: str | None = None
            result_text: str | None = None
            errors = list(ref.errors)
            if (
                ref.run_id is not None
                and ref.commit is not None
                and ref.object_type == "commit"
            ):
                key = (ref.commit, ref.run_id)
                records = cache.get(key)
                if records is None:
                    records = self._observe_run_tree(
                        ref.commit,
                        ref.run_id,
                        deadline=deadline,
                    )
                    cache[key] = records
                spec_text, result_text, record_errors = records
                errors.extend(record_errors)
            observed.append(
                RunRefObservation(
                    kind=ref.kind,
                    ref=ref.ref,
                    run_id=ref.run_id,
                    commit=ref.commit,
                    object_type=ref.object_type,
                    parents=ref.parents,
                    spec_text=spec_text,
                    result_text=result_text,
                    errors=tuple(dict.fromkeys(errors)),
                )
            )
        if any(
            "run_record_observation_incomplete" in ref.errors for ref in observed
        ):
            return tuple(observed), ObservationFailure(
                component="run_records",
                code="run_record_observation_incomplete",
                message="One or more authoritative Run records could not be observed.",
            )
        return tuple(observed), None

    def _observe_run_tree(
        self,
        commit: str,
        run_id: str,
        *,
        deadline: float,
    ) -> tuple[str | None, str | None, tuple[str, ...]]:
        directory = f".research/runs/{run_id}"
        spec_path = f"{directory}/spec.yaml"
        result_path = f"{directory}/result.yaml"
        listed, failure = self._run(
            "run_records",
            (
                "git",
                "-c",
                "core.fsmonitor=false",
                "-C",
                str(self._root),
                "ls-tree",
                "-r",
                "-z",
                "--name-only",
                commit,
                "--",
                directory,
            ),
            env=self._git_environment(),
            deadline=deadline,
        )
        if failure is not None or listed is None:
            error = (
                "run_record_tree_invalid"
                if failure is not None and failure.code.endswith("_output_invalid")
                else "run_record_observation_incomplete"
            )
            return None, None, (error,)
        if listed.returncode != 0:
            return None, None, ("run_record_observation_incomplete",)
        try:
            self._check_output_bound(listed.stdout)
            paths = tuple(path for path in listed.stdout.split("\x00") if path)
            if len(paths) != len(set(paths)):
                raise ValueError("duplicate Run record path")
        except ValueError:
            return None, None, ("run_record_tree_invalid",)

        allowed = {spec_path, result_path}
        errors: list[str] = []
        if any(path not in allowed for path in paths):
            errors.append("run_record_paths_unexpected")
        spec_text = (
            self._observe_run_blob(commit, spec_path, errors, deadline=deadline)
            if spec_path in paths
            else None
        )
        result_text = (
            self._observe_run_blob(commit, result_path, errors, deadline=deadline)
            if result_path in paths
            else None
        )
        return spec_text, result_text, tuple(dict.fromkeys(errors))

    def _observe_run_blob(
        self,
        commit: str,
        path: str,
        errors: list[str],
        *,
        deadline: float,
    ) -> str | None:
        result, failure = self._run(
            "run_records",
            (
                "git",
                "-c",
                "core.fsmonitor=false",
                "-C",
                str(self._root),
                "show",
                f"{commit}:{path}",
            ),
            env=self._git_environment(),
            deadline=deadline,
        )
        if failure is not None or result is None:
            errors.append(
                "run_record_blob_invalid"
                if failure is not None and failure.code.endswith("_output_invalid")
                else "run_record_observation_incomplete"
            )
            return None
        if result.returncode != 0:
            errors.append("run_record_observation_incomplete")
            return None
        try:
            self._check_output_bound(result.stdout)
        except ValueError:
            errors.append("run_record_blob_unbounded")
            return None
        return result.stdout

    def _observe_run_markers(
        self,
        fallback_directory: Path | None,
        *,
        deadline: float,
    ) -> tuple[tuple[RunMarkerObservation, ...], ObservationFailure | None]:
        directory = self._run_marker_directory
        if directory is None and fallback_directory is not None:
            directory = Path(os.path.abspath(os.fspath(fallback_directory)))
        if directory is None:
            return (), None
        try:
            info = directory.lstat()
        except FileNotFoundError:
            return (), None
        except OSError:
            return (), ObservationFailure(
                component="run_markers",
                code="run_marker_directory_unreadable",
                message="The local Run marker directory could not be observed safely.",
            )
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            return (), ObservationFailure(
                component="run_markers",
                code="run_marker_directory_unsafe",
                message="The local Run marker directory is not private and locally owned.",
            )
        try:
            entries: list[os.DirEntry[str]] = []
            with os.scandir(directory) as iterator:
                for entry in iterator:
                    if time.monotonic() >= deadline:
                        return (), ObservationFailure(
                            component="run_markers",
                            code="run_marker_observation_timeout",
                            message="The local Run marker observation exceeded its deadline.",
                        )
                    entries.append(entry)
                    if len(entries) > self._max_records:
                        break
        except OSError:
            return (), ObservationFailure(
                component="run_markers",
                code="run_marker_directory_unreadable",
                message="The local Run marker directory could not be observed safely.",
            )
        if len(entries) > self._max_records:
            return (), ObservationFailure(
                component="run_markers",
                code="run_marker_inventory_unbounded",
                message="The local Run marker inventory exceeded the record bound.",
            )
        entries.sort(key=lambda entry: entry.name)

        markers: list[RunMarkerObservation] = []
        for entry in entries:
            if time.monotonic() >= deadline:
                return tuple(markers), ObservationFailure(
                    component="run_markers",
                    code="run_marker_observation_timeout",
                    message="The local Run marker observation exceeded its deadline.",
                )
            path = directory / entry.name
            try:
                content = self._read_run_marker(path)
            except ValueError as error:
                markers.append(
                    RunMarkerObservation(
                        path=path,
                        content=None,
                        error=str(error),
                    )
                )
            else:
                markers.append(RunMarkerObservation(path=path, content=content))
        return tuple(markers), None

    @staticmethod
    def _read_run_marker(path: Path) -> bytes:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as error:
            raise ValueError("run_marker_file_unreadable") from error
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_size > _MAX_RUN_MARKER_BYTES
                or stat.S_IMODE(before.st_mode) & 0o077
            ):
                raise ValueError("run_marker_file_unsafe")
            content = bytearray()
            while len(content) <= _MAX_RUN_MARKER_BYTES:
                chunk = os.read(
                    descriptor,
                    min(64 * 1024, _MAX_RUN_MARKER_BYTES + 1 - len(content)),
                )
                if not chunk:
                    break
                content.extend(chunk)
            after = os.fstat(descriptor)
            if (
                len(content) > _MAX_RUN_MARKER_BYTES
                or before.st_dev != after.st_dev
                or before.st_ino != after.st_ino
                or before.st_size != after.st_size
                or len(content) != after.st_size
            ):
                raise ValueError("run_marker_file_changed_or_unbounded")
            return bytes(content)
        except OSError as error:
            raise ValueError("run_marker_file_unreadable") from error
        finally:
            os.close(descriptor)

    def _observe_branches(
        self,
    ) -> tuple[tuple[ManagedBranchObservation, ...], ObservationFailure | None]:
        result, failure = self._run(
            "git_branches",
            (
                "git",
                "-c",
                "core.fsmonitor=false",
                "-C",
                str(self._root),
                "for-each-ref",
                "--format=%(refname)%00%(objectname)",
                "refs/heads/research/task/",
            ),
            env=self._git_environment(),
        )
        if failure is not None or result is None:
            return (), failure
        if result.returncode != 0:
            return (), self._command_failure("git_branches", result)
        try:
            self._check_output_bound(result.stdout)
            branches = self._parse_branches(result.stdout)
            self._check_record_bound(len(branches))
        except ValueError:
            return (), self._invalid_output("git_branches")
        return branches, None

    def _observe_worktrees(
        self,
    ) -> tuple[tuple[WorktreeInventoryEntry, ...], ObservationFailure | None]:
        result, failure = self._run(
            "git_worktrees",
            (
                "git",
                "-c",
                "core.fsmonitor=false",
                "-C",
                str(self._root),
                "worktree",
                "list",
                "--porcelain",
                "-z",
            ),
            env=self._git_environment(),
        )
        if failure is not None or result is None:
            return (), failure
        if result.returncode != 0:
            return (), self._command_failure("git_worktrees", result)
        try:
            self._check_output_bound(result.stdout)
            worktrees = self._parse_worktrees(result.stdout)
            self._check_record_bound(len(worktrees))
        except ValueError:
            return (), self._invalid_output("git_worktrees")
        return worktrees, None

    def _observe_tmux(
        self,
    ) -> tuple[tuple[str, ...], ObservationFailure | None]:
        result, failure = self._run(
            "tmux",
            ("tmux", "list-sessions", "-F", "#{session_name}"),
            env=None,
        )
        if failure is not None or result is None:
            return (), failure
        if result.returncode == 1:
            detail = result.stderr.strip().casefold()
            no_server = detail.startswith("no server running on ") or (
                detail.startswith("error connecting to ")
                and detail.endswith("(no such file or directory)")
            )
            if not result.stdout and no_server:
                return (), None
            return (), self._command_failure("tmux", result)
        if result.returncode != 0:
            return (), self._command_failure("tmux", result)
        try:
            self._check_output_bound(result.stdout)
            sessions = self._parse_tmux_sessions(result.stdout)
            self._check_record_bound(len(sessions))
        except ValueError:
            return (), self._invalid_output("tmux")
        return sessions, None

    def _run(
        self,
        component: str,
        argv: tuple[str, ...],
        *,
        env: dict[str, str] | None,
        deadline: float | None = None,
    ) -> tuple[CommandResult | None, ObservationFailure | None]:
        timeout_seconds = self._timeout_seconds
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None, ObservationFailure(
                    component=component,
                    code=f"{component}_timeout",
                    message=f"{component} observation exceeded its local deadline.",
                )
            if remaining < timeout_seconds:
                timeout_seconds = remaining
        try:
            result = self._runner.run(
                argv,
                cwd=None,
                env=env,
                timeout_seconds=timeout_seconds,
            )
        except FileNotFoundError:
            return None, ObservationFailure(
                component=component,
                code=f"{component}_not_found",
                message=f"{component} executable was not found during observation.",
            )
        except subprocess.TimeoutExpired:
            return None, ObservationFailure(
                component=component,
                code=f"{component}_timeout",
                message=f"{component} observation exceeded its local deadline.",
            )
        except OSError:
            return None, ObservationFailure(
                component=component,
                code=f"{component}_observation_failed",
                message=f"{component} could not be observed.",
            )
        except UnicodeError:
            return None, ObservationFailure(
                component=component,
                code=f"{component}_output_invalid",
                message=f"{component} returned non-UTF-8 observation data.",
            )
        return result, None

    def _check_output_bound(self, output: str) -> None:
        if len(output.encode("utf-8")) > self._max_output_bytes:
            raise ValueError("observation output exceeds byte bound")

    def _check_record_bound(self, count: int) -> None:
        if count > self._max_records:
            raise ValueError("observation output exceeds record bound")

    @staticmethod
    def _parse_branches(output: str) -> tuple[ManagedBranchObservation, ...]:
        branches: list[ManagedBranchObservation] = []
        seen: set[str] = set()
        for line in output.splitlines():
            ref, separator, commit = line.partition("\x00")
            match = _MANAGED_REF.fullmatch(ref)
            if (
                not separator
                or match is None
                or not _GIT_OBJECT_ID.fullmatch(commit)
                or ref in seen
            ):
                raise ValueError("malformed managed branch inventory")
            seen.add(ref)
            branches.append(
                ManagedBranchObservation(
                    name=ref.removeprefix("refs/heads/"),
                    ref=ref,
                    task_key=match.group("task_key"),
                    session_id=match.group("session_id"),
                    commit=commit,
                )
            )
        return tuple(sorted(branches, key=lambda item: item.ref))

    @staticmethod
    def _parse_run_refs(output: str) -> tuple[RunRefObservation, ...]:
        observations: list[RunRefObservation] = []
        seen: set[str] = set()
        for line in output.splitlines():
            fields = line.split("\x00")
            if len(fields) != 4:
                raise ValueError("malformed Run ref inventory")
            ref, object_name, object_type, parent_text = fields
            if not ref or ref in seen or not object_type:
                raise ValueError("malformed Run ref inventory")
            seen.add(ref)
            branch_match = _RUN_BRANCH_REF.fullmatch(ref)
            tag_match = _RUN_TAG_REF.fullmatch(ref)
            if ref.startswith("refs/heads/research/run/"):
                kind = "branch"
                run_id = (
                    branch_match.group("run_id") if branch_match is not None else None
                )
            elif ref.startswith("refs/tags/research-run/"):
                kind = "tag"
                run_id = tag_match.group("run_id") if tag_match is not None else None
            else:
                raise ValueError("unexpected Run ref namespace")

            errors: list[str] = []
            commit = object_name if _GIT_OBJECT_ID.fullmatch(object_name) else None
            if commit is None or object_type != "commit":
                errors.append("run_ref_object_invalid")
            parents = tuple(parent_text.split()) if parent_text else ()
            if any(_GIT_OBJECT_ID.fullmatch(parent) is None for parent in parents):
                parents = ()
                errors.append("run_ref_parent_invalid")
            if run_id is None:
                errors.append("run_ref_identity_invalid")
            observations.append(
                RunRefObservation(
                    kind=kind,
                    ref=ref,
                    run_id=run_id,
                    commit=commit,
                    object_type=object_type,
                    parents=parents,
                    spec_text=None,
                    result_text=None,
                    errors=tuple(errors),
                )
            )
        return tuple(sorted(observations, key=lambda item: item.ref))

    @staticmethod
    def _parse_worktrees(output: str) -> tuple[WorktreeInventoryEntry, ...]:
        entries: list[WorktreeInventoryEntry] = []
        current: dict[str, str] = {}
        valued = {"HEAD", "branch", "worktree"}
        optional = {"locked", "prunable"}
        flags = {"bare", "detached"}
        for field in output.split("\x00"):
            if not field:
                if current:
                    entries.append(LocalReconcileObserver._worktree_entry(current))
                    current = {}
                continue
            key, separator, value = field.partition(" ")
            if key == "worktree" and current:
                entries.append(LocalReconcileObserver._worktree_entry(current))
                current = {}
            valid = (
                (key in valued and bool(separator))
                or key in optional
                or (key in flags and not separator)
            )
            if not valid or key in current:
                raise ValueError("malformed worktree inventory")
            current[key] = value
        if current:
            entries.append(LocalReconcileObserver._worktree_entry(current))
        paths = [entry.path for entry in entries]
        if len(paths) != len(set(paths)):
            raise ValueError("duplicate worktree path")
        return tuple(sorted(entries, key=lambda item: str(item.path)))

    @staticmethod
    def _worktree_entry(fields: dict[str, str]) -> WorktreeInventoryEntry:
        path_value = fields.get("worktree")
        if not path_value:
            raise ValueError("worktree path missing")
        path = Path(path_value)
        if not path.is_absolute() or "\x00" in path_value:
            raise ValueError("worktree path invalid")
        head = fields.get("HEAD")
        if head is not None and _GIT_OBJECT_ID.fullmatch(head) is None:
            raise ValueError("worktree head invalid")
        branch_ref = fields.get("branch")
        if branch_ref is not None and any(char in branch_ref for char in "\x00\r\n"):
            raise ValueError("worktree branch invalid")
        return WorktreeInventoryEntry(
            path=path,
            head=head,
            branch_ref=branch_ref,
            prunable="prunable" in fields,
        )

    @staticmethod
    def _parse_tmux_sessions(output: str) -> tuple[str, ...]:
        managed: list[str] = []
        for name in output.splitlines():
            if not name or any(char in name for char in "\x00\r"):
                raise ValueError("malformed tmux inventory")
            if not name.startswith("research-"):
                continue
            if _TMUX_SESSION.fullmatch(name) is None:
                raise ValueError("noncanonical research tmux session")
            managed.append(name)
        if len(managed) != len(set(managed)):
            raise ValueError("duplicate tmux session")
        return tuple(sorted(managed))

    @staticmethod
    def _command_failure(component: str, result: CommandResult) -> ObservationFailure:
        return ObservationFailure(
            component=component,
            code=f"{component}_command_failed",
            message=f"{component} observation command failed.",
            returncode=result.returncode,
        )

    @staticmethod
    def _invalid_output(component: str) -> ObservationFailure:
        return ObservationFailure(
            component=component,
            code=f"{component}_output_invalid",
            message=f"{component} returned invalid or unbounded observation data.",
        )

    @staticmethod
    def _git_environment() -> dict[str, str]:
        environment = os.environ.copy()
        for key in tuple(environment):
            if key in _GIT_CONTEXT_ENVIRONMENT or key.startswith("GIT_CONFIG_"):
                environment.pop(key, None)
        environment.pop("GIT_CONFIG_PARAMETERS", None)
        return environment


def parse_managed_branch(value: str | None) -> tuple[str, str] | None:
    if not isinstance(value, str):
        return None
    match = _MANAGED_BRANCH.fullmatch(value)
    if match is None:
        return None
    return match.group("task_key"), match.group("session_id")


def parse_managed_branch_ref(value: str | None) -> tuple[str, str] | None:
    if not isinstance(value, str):
        return None
    match = _MANAGED_REF.fullmatch(value)
    if match is None:
        return None
    return match.group("task_key"), match.group("session_id")


def parse_tmux_session(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    match = _TMUX_SESSION.fullmatch(value)
    return match.group("session_id") if match is not None else None
