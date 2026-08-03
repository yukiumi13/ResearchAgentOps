from __future__ import annotations

import fcntl
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator

from researchctl.domain.enums import TaskState
from researchctl.domain.models import TaskRecord
from researchctl.domain.types import Sha256Digest
from researchctl.errors import RCPError
from researchctl.repository import safe_repository_path
from researchctl.serialization import canonical_digest, dump_yaml, load_model


@dataclass(frozen=True, slots=True)
class TaskWriteResult:
    record: TaskRecord
    digest: Sha256Digest
    path: Path
    changed: bool


class TaskRecordRepository:
    """Stores manager-owned Task records in an isolated control worktree."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.directory = safe_repository_path(
            self.root,
            ".research/tasks",
            managed_only=True,
        )
        if not self.directory.is_dir():
            raise RCPError(
                code="task_store_missing",
                message="Managed Task directory is missing.",
                remediation="Run researchctl init or restore .research/tasks from Git.",
            )

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        descriptor = os.open(self.directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _path(self, task_id: str) -> Path:
        return safe_repository_path(
            self.root,
            f".research/tasks/{task_id}.yaml",
            managed_only=True,
        )

    def path_for(self, task_id: str) -> Path:
        return self._path(task_id)

    def _read_path(self, path: Path) -> TaskRecord:
        try:
            record = load_model(path, TaskRecord)
        except Exception as exc:
            raise RCPError(
                code="invalid_task_record",
                message=f"Task record is malformed: {path.name}",
                remediation="Repair the record through a manager-reviewed control change.",
                context={"path": path.relative_to(self.root).as_posix()},
            ) from exc
        expected_name = f"{record.task_id}.yaml"
        if path.name != expected_name:
            raise RCPError(
                code="invalid_task_record_path",
                message="Task record filename does not match its canonical Task ID.",
                context={
                    "path": path.relative_to(self.root).as_posix(),
                    "expected_name": expected_name,
                },
            )
        return record

    def list(self) -> tuple[TaskRecord, ...]:
        records: list[TaskRecord] = []
        for discovered in sorted(self.directory.iterdir()):
            if discovered.name == ".gitkeep":
                continue
            relative = discovered.relative_to(self.root).as_posix()
            path = safe_repository_path(self.root, relative, managed_only=True)
            if path.suffix not in {".yaml", ".yml"} or not path.is_file():
                raise RCPError(
                    code="invalid_task_store_entry",
                    message="Unexpected entry in the managed Task directory.",
                    context={"path": relative},
                )
            records.append(self._read_path(path))
        return tuple(records)

    def load(self, task_id: str) -> TaskRecord:
        path = self._path(task_id)
        if not path.is_file():
            raise RCPError(
                code="task_not_found",
                message=f"Task does not exist: {task_id}",
                context={"task_id": task_id},
            )
        return self._read_path(path)

    def find_by_key(self, key: str) -> TaskRecord | None:
        matches = [record for record in self.list() if record.key == key]
        if len(matches) > 1:
            raise RCPError(
                code="duplicate_task_key",
                message=f"Multiple Task records use key {key!r}.",
                context={"key": key},
            )
        return matches[0] if matches else None

    def _write_temporary(self, content: bytes, *, prefix: str) -> Path:
        descriptor, name = tempfile.mkstemp(prefix=prefix, dir=self.directory)
        temporary = Path(name)
        try:
            os.fchmod(descriptor, 0o644)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return temporary

    def _fsync_directory(self) -> None:
        descriptor = os.open(self.directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _result(record: TaskRecord, path: Path, *, changed: bool) -> TaskWriteResult:
        return TaskWriteResult(
            record=record,
            digest=canonical_digest(record),
            path=path,
            changed=changed,
        )

    def create(self, record: TaskRecord) -> TaskWriteResult:
        content = dump_yaml(record).encode("utf-8")
        path = self._path(record.task_id)
        with self._exclusive():
            if path.exists():
                current = self._read_path(path)
                if path.read_bytes() == content:
                    return self._result(current, path, changed=False)
                raise RCPError(
                    code="task_id_conflict",
                    message="Task ID already identifies different content.",
                    context={"task_id": record.task_id},
                )
            key_owner = self.find_by_key(record.key)
            if key_owner is not None:
                raise RCPError(
                    code="task_key_conflict",
                    message=f"Task key is already in use: {record.key}",
                    context={"key": record.key, "task_id": key_owner.task_id},
                )

            temporary = self._write_temporary(content, prefix=f".{record.task_id}.")
            try:
                os.link(temporary, path, follow_symlinks=False)
                self._fsync_directory()
            except FileExistsError as exc:
                raise RCPError(
                    code="task_id_conflict",
                    message="Task ID was created concurrently with different content.",
                    context={"task_id": record.task_id},
                ) from exc
            finally:
                temporary.unlink(missing_ok=True)
        return self._result(record, path, changed=True)

    def replace(
        self,
        task_id: str,
        expected_digest: Sha256Digest,
        replacement: TaskRecord,
    ) -> TaskWriteResult:
        if replacement.task_id != task_id:
            raise RCPError(
                code="immutable_task_identity",
                message="A Task replacement cannot change its canonical ID.",
            )
        path = self._path(task_id)
        with self._exclusive():
            current = self.load(task_id)
            current_digest = canonical_digest(current)
            if current_digest != expected_digest:
                raise RCPError(
                    code="stale_task",
                    message="Task changed after the caller observed it.",
                    remediation="Reload the Task and submit a new expected digest.",
                    context={
                        "task_id": task_id,
                        "expected_digest": expected_digest,
                        "observed_digest": current_digest,
                    },
                )
            if replacement.key != current.key:
                raise RCPError(
                    code="immutable_task_identity",
                    message="Task key cannot change after creation in v0.1.",
                    context={"task_id": task_id},
                )

            content = dump_yaml(replacement).encode("utf-8")
            if path.read_bytes() == content:
                return self._result(current, path, changed=False)
            temporary = self._write_temporary(content, prefix=f".{task_id}.")
            try:
                os.replace(temporary, path)
                self._fsync_directory()
            finally:
                temporary.unlink(missing_ok=True)
        return self._result(replacement, path, changed=True)

    def cancel(
        self,
        task_id: str,
        expected_digest: Sha256Digest,
        *,
        updated_at: datetime,
    ) -> TaskWriteResult:
        current = self.load(task_id)
        if current.state is TaskState.CANCELED:
            return self._result(current, self._path(task_id), changed=False)
        payload = current.model_dump(mode="python")
        payload.update(state=TaskState.CANCELED, updated_at=updated_at)
        replacement = TaskRecord.model_validate(payload)
        return self.replace(task_id, expected_digest, replacement)
