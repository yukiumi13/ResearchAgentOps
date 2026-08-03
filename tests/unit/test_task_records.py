from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from researchctl.domain.models import TaskRecord
from researchctl.errors import RCPError
from researchctl.serialization import canonical_digest, dump_yaml
from researchctl.services.task_records import TaskRecordRepository


def _repository(tmp_path: Path) -> TaskRecordRepository:
    (tmp_path / ".research" / "tasks").mkdir(parents=True)
    return TaskRecordRepository(tmp_path)


def test_create_is_deterministic_and_identical_retry_is_idempotent(
    tmp_path: Path,
    task_payload,
) -> None:
    repository = _repository(tmp_path)
    task = TaskRecord.model_validate(task_payload())

    created = repository.create(task)
    repeated = repository.create(task)

    assert created.changed is True
    assert repeated.changed is False
    assert created.digest == repeated.digest == canonical_digest(task)
    assert created.path.read_text(encoding="utf-8") == dump_yaml(task)
    assert repository.list() == (task,)
    assert repository.find_by_key(task.key) == task


def test_create_rejects_id_and_human_key_conflicts(tmp_path: Path, task_payload) -> None:
    repository = _repository(tmp_path)
    first = TaskRecord.model_validate(task_payload())
    repository.create(first)

    different_content = TaskRecord.model_validate(task_payload(title="Different"))
    with pytest.raises(RCPError) as id_error:
        repository.create(different_content)
    assert id_error.value.code == "task_id_conflict"

    other_payload = task_payload(
        task_id="task_20260802T123456Z_" + "b" * 24,
    )
    with pytest.raises(RCPError) as key_error:
        repository.create(TaskRecord.model_validate(other_payload))
    assert key_error.value.code == "task_key_conflict"


def test_replace_uses_digest_cas_and_preserves_identity(tmp_path: Path, task_payload) -> None:
    repository = _repository(tmp_path)
    current = TaskRecord.model_validate(task_payload())
    repository.create(current)
    replacement = TaskRecord.model_validate(task_payload(title="Updated title"))

    with pytest.raises(RCPError) as stale:
        repository.replace(
            current.task_id,
            "sha256:" + "0" * 64,
            replacement,
        )
    assert stale.value.code == "stale_task"
    assert repository.load(current.task_id) == current

    changed = repository.replace(
        current.task_id,
        canonical_digest(current),
        replacement,
    )
    assert changed.changed is True
    assert repository.load(current.task_id) == replacement

    renamed = TaskRecord.model_validate(task_payload(key="RENAMED"))
    with pytest.raises(RCPError) as identity:
        repository.replace(
            current.task_id,
            canonical_digest(replacement),
            renamed,
        )
    assert identity.value.code == "immutable_task_identity"


def test_cancel_is_explicit_and_terminal(tmp_path: Path, task_payload) -> None:
    repository = _repository(tmp_path)
    current = TaskRecord.model_validate(task_payload())
    repository.create(current)

    canceled = repository.cancel(
        current.task_id,
        canonical_digest(current),
        updated_at=current.updated_at + timedelta(seconds=1),
    )
    repeated = repository.cancel(
        current.task_id,
        canonical_digest(current),
        updated_at=current.updated_at + timedelta(seconds=2),
    )

    assert canceled.record.state.value == "canceled"
    assert repeated.changed is False
    assert repeated.record == canceled.record


def test_scan_fails_closed_for_malformed_or_symlinked_records(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    malformed = repository.directory / "task_20260802T123456Z_aaaaaaaaaaaaaaaaaaaaaaaa.yaml"
    malformed.write_text("not: a-task\n", encoding="utf-8")

    with pytest.raises(RCPError) as invalid:
        repository.list()
    assert invalid.value.code == "invalid_task_record"

    malformed.unlink()
    target = tmp_path / "outside.yaml"
    target.write_text("outside\n", encoding="utf-8")
    malformed.symlink_to(target)
    with pytest.raises(RCPError) as unsafe:
        repository.list()
    assert unsafe.value.code == "unsafe_repository_path"


def test_task_writes_do_not_touch_other_worktree_files(tmp_path: Path, task_payload) -> None:
    repository = _repository(tmp_path)
    existing = tmp_path / "README.md"
    existing.write_bytes(b"unchanged\n")

    repository.create(TaskRecord.model_validate(task_payload()))

    assert existing.read_bytes() == b"unchanged\n"
