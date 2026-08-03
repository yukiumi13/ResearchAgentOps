from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from researchctl.adapters._subprocess import (
    CommandResult,
    CommandRunner,
    SubprocessCommandRunner,
)
from researchctl.domain.models import RunResult, RunSpec
from researchctl.errors import RCPError
from researchctl.serialization import dump_yaml, load_yaml


_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_RUN_ID = re.compile(r"^run_\d{8}T\d{6}Z_[0-9a-f]{24}$")
_MAX_COMMIT_BYTES = 256 * 1024
_MAX_RECORD_BYTES = 2 * 1024 * 1024
_RunRecordT = TypeVar("_RunRecordT", RunSpec, RunResult)
_GIT_ENVIRONMENT_KEYS = {
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_WORK_TREE",
}


@dataclass(frozen=True, slots=True)
class GitRunEvidence:
    spec: RunSpec
    result: RunResult
    spec_commit: str
    result_commit: str


@dataclass(frozen=True, slots=True)
class _CommitRecord:
    parents: tuple[str, ...]
    message: str


class GitRunEvidenceReader:
    """Read and verify one immutable Run entirely from Git objects."""

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

    def read(self, repository_root: Path, run_id: str) -> GitRunEvidence:
        root = self._directory(repository_root)
        if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
            raise RCPError(
                code="run_identity_invalid",
                message="Run evidence requires a canonical Run ID.",
            )

        spec_path = f".research/runs/{run_id}/spec.yaml"
        result_path = f".research/runs/{run_id}/result.yaml"
        spec_commit = self._resolve_required_ref(
            root,
            f"refs/tags/research-run/{run_id}",
            missing_code="run_spec_not_found",
            missing_message="The immutable RunSpec tag was not found.",
        )
        result_commit = self._resolve_required_ref(
            root,
            f"refs/heads/research/run/{run_id}",
            missing_code="run_result_not_found",
            missing_message="The Run result branch was not found.",
        )

        spec_text = self._read_record(
            root,
            commit=spec_commit,
            path=spec_path,
            missing_code="run_spec_not_found",
            record_name="RunSpec",
        )
        spec = self._parse_record(spec_text, RunSpec, code="run_spec_invalid")
        if spec.run_id != run_id:
            raise RCPError(
                code="run_tag_conflict",
                message="Immutable research-run tag identifies another RunSpec.",
                context={"expected_run_id": run_id, "observed_run_id": spec.run_id},
            )
        self._require_canonical_record(
            spec_text,
            spec,
            code="run_spec_invalid",
            record_name="RunSpec",
        )
        self._verify_record_commit(
            root,
            commit=spec_commit,
            expected_parent=spec.source_commit,
            path=spec_path,
            marker=f"researchctl: freeze run {run_id} {spec.spec_digest}",
        )
        self._verify_source_tree(root, spec)

        result_text = self._read_record(
            root,
            commit=result_commit,
            path=result_path,
            missing_code="run_result_not_found",
            record_name="RunResult",
        )
        result = self._parse_record(result_text, RunResult, code="run_result_invalid")
        self._require_canonical_record(
            result_text,
            result,
            code="run_result_invalid",
            record_name="RunResult",
        )
        self._verify_record_commit(
            root,
            commit=result_commit,
            expected_parent=spec_commit,
            path=result_path,
            marker=f"researchctl: collect run {run_id} {result.result_id}",
        )
        if result.run_id != run_id:
            raise RCPError(
                code="run_result_identity_mismatch",
                message="RunResult does not belong to the requested Run.",
                context={"expected_run_id": run_id, "observed_run_id": result.run_id},
            )
        if result.run_spec_digest != spec.spec_digest:
            raise RCPError(
                code="run_result_spec_mismatch",
                message="RunResult does not bind the immutable RunSpec digest.",
                context={
                    "expected_spec_digest": spec.spec_digest,
                    "observed_spec_digest": result.run_spec_digest,
                },
            )
        return GitRunEvidence(
            spec=spec,
            result=result,
            spec_commit=spec_commit,
            result_commit=result_commit,
        )

    def _verify_record_commit(
        self,
        root: Path,
        *,
        commit: str,
        expected_parent: str,
        path: str,
        marker: str,
    ) -> None:
        record = self._read_commit(root, commit)
        if record.parents != (expected_parent,):
            raise RCPError(
                code="run_record_parent_mismatch",
                message="Run record commit has an unexpected parent.",
                context={
                    "commit": commit,
                    "expected_parent": expected_parent,
                    "observed_parents": list(record.parents),
                },
            )
        if record.message.rstrip("\n") != marker:
            raise RCPError(
                code="run_record_commit_invalid",
                message="Run record commit has an unexpected operation marker.",
                context={"commit": commit},
            )

        changed = self._git(
            root,
            "diff-tree",
            "--quiet",
            "--no-ext-diff",
            "-r",
            expected_parent,
            commit,
            "--",
            f":(top,literal){path}",
            check=False,
        )
        if changed.returncode == 0:
            raise RCPError(
                code="run_record_commit_invalid",
                message="Run record commit did not change its canonical record.",
                context={"commit": commit, "path": path},
            )
        if changed.returncode != 1:
            self._raise_failed(changed, "inspect the canonical Run record change")

        outside = self._git(
            root,
            "diff-tree",
            "--quiet",
            "--no-ext-diff",
            "-r",
            expected_parent,
            commit,
            "--",
            f":(top,literal,exclude){path}",
            check=False,
        )
        if outside.returncode == 1:
            raise RCPError(
                code="run_record_commit_invalid",
                message="Run record commit changed a path outside its canonical record.",
                context={"commit": commit, "path": path},
            )
        if outside.returncode != 0:
            self._raise_failed(outside, "inspect Run record commit scope")

    def _verify_source_tree(self, root: Path, spec: RunSpec) -> None:
        observed = self._parse_object_id(
            self._git(
                root,
                "rev-parse",
                "--verify",
                f"{spec.source_commit}^{{tree}}",
            ).stdout
        )
        if observed != spec.source_tree:
            raise RCPError(
                code="run_source_mismatch",
                message="RunSpec source commit or tree does not match Git.",
                context={
                    "source_commit": spec.source_commit,
                    "expected_tree": spec.source_tree,
                    "observed_tree": observed,
                },
            )

    def _resolve_required_ref(
        self,
        root: Path,
        reference: str,
        *,
        missing_code: str,
        missing_message: str,
    ) -> str:
        result = self._git(
            root,
            "rev-parse",
            "--verify",
            "--quiet",
            f"{reference}^{{commit}}",
            check=False,
        )
        if result.returncode == 1:
            raise RCPError(
                code=missing_code,
                message=missing_message,
                context={"ref": reference},
            )
        if result.returncode != 0:
            self._raise_failed(result, "resolve Run evidence ref")
        return self._parse_object_id(result.stdout)

    def _read_record(
        self,
        root: Path,
        *,
        commit: str,
        path: str,
        missing_code: str,
        record_name: str,
    ) -> str:
        listed = self._git(
            root,
            "ls-tree",
            "-z",
            "--full-tree",
            commit,
            "--",
            path,
        ).stdout
        if not listed:
            raise RCPError(
                code=missing_code,
                message=f"{record_name} was not found in its canonical Git commit.",
                context={"commit": commit, "path": path},
            )
        entries = tuple(item for item in listed.split("\x00") if item)
        if len(entries) != 1:
            self._raise_record_invalid(record_name, commit, path)
        metadata, separator, observed_path = entries[0].partition("\t")
        fields = metadata.split(" ")
        if (
            not separator
            or observed_path != path
            or len(fields) != 3
            or fields[0] != "100644"
            or fields[1] != "blob"
            or not _OBJECT_ID.fullmatch(fields[2])
        ):
            self._raise_record_invalid(record_name, commit, path)
        blob = fields[2]
        size = self._object_size(root, blob)
        if size > _MAX_RECORD_BYTES:
            raise RCPError(
                code="run_record_commit_invalid",
                message=f"{record_name} Git blob exceeds the protocol size limit.",
                context={"commit": commit, "path": path, "size_bytes": size},
            )
        content = self._git(root, "cat-file", "blob", blob).stdout
        if len(content.encode("utf-8")) != size:
            self._raise_record_invalid(record_name, commit, path)
        return content

    def _read_commit(self, root: Path, commit: str) -> _CommitRecord:
        size = self._object_size(root, commit)
        if size > _MAX_COMMIT_BYTES:
            raise RCPError(
                code="run_record_commit_invalid",
                message="Run record commit metadata exceeds the verification limit.",
                context={"commit": commit, "size_bytes": size},
            )
        content = self._git(root, "cat-file", "commit", commit).stdout
        if len(content.encode("utf-8")) != size:
            raise RCPError(
                code="git_output_invalid",
                message="Git returned truncated or malformed commit data.",
            )
        headers, separator, message = content.partition("\n\n")
        if not separator:
            raise RCPError(
                code="run_record_commit_invalid",
                message="Run record commit metadata is malformed.",
                context={"commit": commit},
            )
        parents: list[str] = []
        for line in headers.splitlines():
            if not line.startswith("parent "):
                continue
            parent = line.removeprefix("parent ")
            if not _OBJECT_ID.fullmatch(parent):
                raise RCPError(
                    code="git_output_invalid",
                    message="Git returned an invalid Run record parent object ID.",
                )
            parents.append(parent)
        return _CommitRecord(parents=tuple(parents), message=message)

    def _object_size(self, root: Path, object_id: str) -> int:
        output = self._git(root, "cat-file", "-s", object_id).stdout
        value = output.removesuffix("\n")
        if not value.isascii() or not value.isdecimal():
            raise RCPError(
                code="git_output_invalid",
                message="Git returned an invalid object size.",
            )
        return int(value)

    @staticmethod
    def _parse_record(
        text: str,
        model_type: type[_RunRecordT],
        *,
        code: str,
    ) -> _RunRecordT:
        try:
            return model_type.model_validate(load_yaml(text))
        except (TypeError, ValueError) as error:
            raise RCPError(
                code=code,
                message=f"Committed {model_type.__name__} is malformed.",
            ) from error

    @staticmethod
    def _require_canonical_record(
        text: str,
        record: RunSpec | RunResult,
        *,
        code: str,
        record_name: str,
    ) -> None:
        if text != dump_yaml(record):
            raise RCPError(
                code=code,
                message=f"Committed {record_name} is not canonical protocol YAML.",
            )

    @staticmethod
    def _raise_record_invalid(record_name: str, commit: str, path: str) -> None:
        raise RCPError(
            code="run_record_commit_invalid",
            message=f"{record_name} is not a canonical regular Git record.",
            context={"commit": commit, "path": path},
        )

    @staticmethod
    def _directory(path: Path) -> Path:
        candidate = Path(os.path.abspath(os.fspath(path)))
        if candidate.is_symlink() or not candidate.is_dir():
            raise RCPError(
                code="run_repository_invalid",
                message="Run evidence requires an existing non-symlink Git directory.",
                context={"path": str(candidate)},
            )
        return candidate

    @staticmethod
    def _parse_object_id(output: str) -> str:
        value = output.strip()
        if not _OBJECT_ID.fullmatch(value):
            raise RCPError(
                code="git_output_invalid",
                message="Git returned an invalid object ID.",
            )
        return value

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
                message="Git Run evidence read timed out.",
                context={"root": str(root)},
            ) from error
        except UnicodeError as error:
            raise RCPError(
                code="git_output_invalid",
                message="Git returned non-UTF-8 Run evidence.",
            ) from error
        if check and result.returncode != 0:
            self._raise_failed(result, args[0] if args else "read Run evidence")
        return result

    @staticmethod
    def _raise_failed(result: CommandResult, operation: str) -> None:
        raise RCPError(
            code="git_run_command_failed",
            message=f"Git failed to {operation}.",
            context={"returncode": result.returncode},
        )


def _git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for key in tuple(environment):
        if key in _GIT_ENVIRONMENT_KEYS or key.startswith("GIT_CONFIG_"):
            environment.pop(key, None)
    environment.pop("GIT_CONFIG_PARAMETERS", None)
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    return environment
