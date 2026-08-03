from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from researchctl.domain.models import TaskRecord
from researchctl.serialization import (
    SerializationError,
    canonical_digest,
    canonical_json_bytes,
    dump_yaml,
    load_model,
    load_yaml,
)


PayloadFactory = Callable[..., dict[str, Any]]


def test_canonical_json_is_sorted_compact_and_utf8() -> None:
    first = {
        "z": 1,
        "nested": {"z": False, "a": None},
        "a": "\u7814\u7a76",
    }
    second = {
        "a": "\u7814\u7a76",
        "nested": {"a": None, "z": False},
        "z": 1,
    }
    expected = (
        '{"a":"' + "\u7814\u7a76" + '","nested":{"a":null,"z":false},"z":1}'
    ).encode("utf-8")

    assert canonical_json_bytes(first) == expected
    assert canonical_json_bytes(second) == expected


def test_canonical_digest_is_stable_across_mapping_order() -> None:
    first = {"z": 1, "a": {"last": 2, "first": 1}}
    second = {"a": {"first": 1, "last": 2}, "z": 1}
    expected_bytes = b'{"a":{"first":1,"last":2},"z":1}'
    expected_digest = "sha256:" + hashlib.sha256(expected_bytes).hexdigest()

    assert canonical_digest(first) == expected_digest
    assert canonical_digest(second) == expected_digest


def test_canonical_model_json_uses_protocol_values(
    task_payload: PayloadFactory,
) -> None:
    task = TaskRecord.model_validate(task_payload(linear_issue_id=None))
    serialized = canonical_json_bytes(task)

    assert b'"created_at":"2026-08-02T12:34:56Z"' in serialized
    assert b'"priority":"high"' in serialized
    assert b'"required_inputs":[' in serialized
    assert b'"linear_issue_id"' not in serialized


@pytest.mark.parametrize(
    "value",
    [
        {"metric": float("nan")},
        {"metric": float("inf")},
        {"metric": float("-inf")},
        {"nested": [{"metric": float("nan")}]},
    ],
)
def test_canonical_json_rejects_nonfinite_numbers(value: dict[str, Any]) -> None:
    with pytest.raises(SerializationError, match="non-finite number"):
        canonical_json_bytes(value)


def test_yaml_dump_is_sorted_and_byte_stable() -> None:
    first = {"z": 1, "a": {"d": 4, "c": 3}}
    second = {"a": {"c": 3, "d": 4}, "z": 1}
    expected = "a:\n  c: 3\n  d: 4\nz: 1\n"

    assert dump_yaml(first) == expected
    assert dump_yaml(second) == expected
    assert dump_yaml(first) == dump_yaml(first)


@pytest.mark.parametrize(
    "document",
    [
        "key: first\nkey: second\n",
        "outer:\n  key: first\n  key: second\n",
        "base: &base\n  key: first\nmerged:\n  <<: *base\n  key: second\n",
    ],
)
def test_yaml_load_rejects_duplicate_keys(document: str) -> None:
    with pytest.raises(SerializationError, match="duplicate YAML key"):
        load_yaml(document)


@pytest.mark.parametrize(
    "document",
    [
        "metric: .nan\n",
        "metric: .inf\n",
        "metric: -.Inf\n",
        "metrics:\n  - finite: 1.0\n  - invalid: .NaN\n",
    ],
)
def test_yaml_load_rejects_nonfinite_numbers(document: str) -> None:
    with pytest.raises(SerializationError, match="non-finite number"):
        load_yaml(document)


def _document_with_aliases(alias_count: int) -> str:
    lines = ["shared: &shared", "  value: 1", "copies:"]
    lines.extend("  - *shared" for _ in range(alias_count))
    return "\n".join(lines) + "\n"


def test_yaml_load_accepts_aliases_at_the_configured_boundary() -> None:
    loaded = load_yaml(_document_with_aliases(50))

    assert len(loaded["copies"]) == 50
    assert all(item == {"value": 1} for item in loaded["copies"])


def test_yaml_load_rejects_aliases_above_the_configured_limit() -> None:
    with pytest.raises(SerializationError, match="alias count 51 exceeds limit 50"):
        load_yaml(_document_with_aliases(51))


@pytest.mark.parametrize("document", ["- one\n- two\n", "42\n", "null\n"])
def test_yaml_protocol_root_must_be_a_mapping(document: str) -> None:
    with pytest.raises(SerializationError, match="root must be a mapping"):
        load_yaml(document)


def test_yaml_loader_does_not_construct_python_objects() -> None:
    document = "payload: !!python/object/apply:builtins.str [unsafe]\n"

    with pytest.raises(SerializationError, match="ConstructorError"):
        load_yaml(document)


def test_load_model_applies_unknown_field_validation(
    tmp_path: Path,
    task_payload: PayloadFactory,
) -> None:
    record_path = tmp_path / "task.yaml"
    record_path.write_text(
        dump_yaml(task_payload(unknown_field="must-fail")),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError):
        load_model(record_path, TaskRecord)
