from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from researchctl.domain import ids
from researchctl.domain.types import RecordId, RepositoryPath, UtcDateTime


def test_new_id_has_canonical_shape_and_uses_96_bits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_bytes: list[int] = []

    def deterministic_token_hex(size: int) -> str:
        requested_bytes.append(size)
        return "ab" * size

    monkeypatch.setattr(ids.secrets, "token_hex", deterministic_token_hex)

    generated = ids.new_id(
        "run_attempt",
        now=datetime(2026, 8, 2, 12, 34, 56, tzinfo=UTC),
    )

    assert generated == "run_attempt_20260802T123456Z_" + "ab" * 12
    assert requested_bytes == [12]
    assert TypeAdapter(RecordId).validate_python(generated) == generated


@pytest.mark.parametrize(
    "kind",
    ["", "Task", "1task", "task-name", "task.name", "task name", "_task"],
)
def test_new_id_rejects_noncanonical_kinds(kind: str) -> None:
    with pytest.raises(ValueError, match="ID kind"):
        ids.new_id(kind)


def test_new_id_normalizes_an_aware_timestamp_to_utc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ids.secrets, "token_hex", lambda size: "0" * (size * 2))
    east_of_utc = timezone(timedelta(hours=5, minutes=30))

    generated = ids.new_id(
        "task",
        now=datetime(2026, 8, 2, 18, 4, 56, tzinfo=east_of_utc),
    )

    assert generated == "task_20260802T123456Z_" + "0" * 24


def test_new_id_rejects_naive_timestamps() -> None:
    with pytest.raises(ValueError, match="timezone aware"):
        ids.new_id("task", now=datetime(2026, 8, 2, 12, 34, 56))


@pytest.mark.parametrize(
    "value",
    [
        "Task_20260802T123456Z_" + "a" * 24,
        "task-20260802T123456Z_" + "a" * 24,
        "1task_20260802T123456Z_" + "a" * 24,
        "task_20260802T123456_" + "a" * 24,
        "task_20260802T123456Z_" + "A" * 24,
        "task_20260802T123456Z_" + "a" * 23,
        "task_20260802T123456Z_" + "a" * 25,
    ],
)
def test_record_id_rejects_noncanonical_values(value: str) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(RecordId).validate_python(value)


def test_utc_datetime_normalizes_offsets_and_serializes_with_z() -> None:
    adapter = TypeAdapter(UtcDateTime)

    value = adapter.validate_python("2026-08-02T14:34:56.123456+02:00")

    assert value == datetime(2026, 8, 2, 12, 34, 56, 123456, tzinfo=UTC)
    assert adapter.dump_python(value, mode="json") == "2026-08-02T12:34:56.123456Z"


def test_utc_datetime_omits_fraction_when_microseconds_are_zero() -> None:
    adapter = TypeAdapter(UtcDateTime)
    value = adapter.validate_python("2026-08-02T12:34:56Z")

    assert adapter.dump_python(value, mode="json") == "2026-08-02T12:34:56Z"


@pytest.mark.parametrize(
    "value",
    [datetime(2026, 8, 2, 12, 34, 56), "2026-08-02T12:34:56"],
)
def test_utc_datetime_rejects_values_without_a_timezone(value: Any) -> None:
    with pytest.raises(ValidationError, match="timezone"):
        TypeAdapter(UtcDateTime).validate_python(value)


@pytest.mark.parametrize("value", [0, 1_750_000_000, 1_750_000_000.25])
def test_utc_datetime_rejects_numeric_unix_timestamps(value: int | float) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(UtcDateTime).validate_python(value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (".", "."),
        ("./", "."),
        ("./src//training/./model.py", "src/training/model.py"),
        ("artifacts/run-1/", "artifacts/run-1"),
    ],
)
def test_repository_path_normalizes_relative_posix_paths(value: str, expected: str) -> None:
    assert TypeAdapter(RepositoryPath).validate_python(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "/etc/passwd",
        "../secrets",
        "artifacts/../../secrets",
        "src/../secrets",
        "src\\model.py",
        "src/model.py\x00ignored",
    ],
)
def test_repository_path_rejects_unsafe_values(value: str) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(RepositoryPath).validate_python(value)


@pytest.mark.parametrize("value", [42, b"src/model.py", ["src", "model.py"]])
def test_repository_path_rejects_non_string_values(value: Any) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(RepositoryPath).validate_python(value)
