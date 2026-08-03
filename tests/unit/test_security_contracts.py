from __future__ import annotations

import json

import pytest
from pydantic import TypeAdapter, ValidationError

from researchctl.cli import _known_error
from researchctl.config import ProjectConfig
from researchctl.domain.models import TaskRecord
from researchctl.domain.types import ShortText
from researchctl.serialization import SerializationError, load_yaml


PROJECT_ID = "project_20260802T123456Z_" + "a" * 24
MANIFEST_DIGEST = "sha256:" + "b" * 64


@pytest.mark.parametrize(
    "project_file",
    [
        ".git/config",
        "README.md",
        ".research/tasks/project.yaml",
        "/tmp/project.yaml",
        "../project.yaml",
    ],
)
def test_project_config_accepts_only_the_canonical_project_record_path(
    project_file: str,
) -> None:
    with pytest.raises(ValidationError):
        ProjectConfig(
            project_id=PROJECT_ID,
            project_file=project_file,
            schema_manifest_digest=MANIFEST_DIGEST,
        )


def test_project_config_accepts_the_canonical_project_record_path() -> None:
    config = ProjectConfig(
        project_id=PROJECT_ID,
        project_file=".research/project.yaml",
        schema_manifest_digest=MANIFEST_DIGEST,
    )

    assert config.project_file == ".research/project.yaml"


def test_project_config_rejects_a_non_project_record_id() -> None:
    with pytest.raises(ValidationError):
        ProjectConfig(
            project_id="task_20260802T123456Z_" + "a" * 24,
            schema_manifest_digest=MANIFEST_DIGEST,
        )


@pytest.mark.parametrize(
    "document",
    [
        "record: [unterminated\n",
        "loop: &loop\n  self: *loop\n",
    ],
)
def test_yaml_parser_and_recursive_alias_failures_are_serialization_errors(
    document: str,
) -> None:
    with pytest.raises(SerializationError):
        load_yaml(document)


def test_yaml_rejects_documents_beyond_the_nesting_limit() -> None:
    lines = [f"{'  ' * index}level_{index}:" for index in range(66)]
    lines.append(f"{'  ' * 66}value: terminal")
    document = "\n".join(lines) + "\n"

    with pytest.raises(SerializationError, match="maximum nesting depth"):
        load_yaml(document)


def test_yaml_rejects_documents_beyond_the_byte_limit() -> None:
    document = "payload: " + "x" * (2 * 1024 * 1024) + "\n"

    with pytest.raises(SerializationError, match="byte limit"):
        load_yaml(document)


def test_validation_error_context_is_json_serializable_and_omits_raw_input() -> None:
    secret_input = b"secret-that-must-not-be-rendered"
    with pytest.raises(ValidationError) as raised:
        TypeAdapter(ShortText).validate_python(secret_input)

    error = _known_error(raised.value)

    assert error is not None
    assert error.code == "validation_error"
    encoded = json.dumps(error.context, allow_nan=False)
    assert "secret-that-must-not-be-rendered" not in encoded
    assert "input" not in error.context["details"][0]


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("title", b"Evaluate a stopping policy"),
        ("execution.gpu_count", "1"),
    ],
)
def test_protocol_models_reject_coercible_scalar_values(
    task_payload,
    field: str,
    invalid_value: object,
) -> None:
    payload = task_payload()
    if field == "execution.gpu_count":
        payload["execution"]["gpu_count"] = invalid_value
    else:
        payload[field] = invalid_value

    with pytest.raises(ValidationError):
        TaskRecord.model_validate(payload)
