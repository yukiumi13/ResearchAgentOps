from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from researchctl.constants import OUTPUT_SCHEMA_VERSION
from researchctl.errors import RCPError


def _observed_at() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def error_payload(error: RCPError) -> dict[str, Any]:
    return {
        "code": error.code,
        "message": error.message,
        "remediation": error.remediation,
        "context": error.context,
    }


def human_error_detail_lines(error: RCPError) -> tuple[str, ...]:
    if error.code != "validation_error":
        return ()
    details = error.context.get("details")
    if not isinstance(details, list):
        return ()
    lines: list[str] = []
    for detail in details:
        if not isinstance(detail, dict):
            continue
        location = detail.get("loc")
        if isinstance(location, (list, tuple)):
            field_path = ".".join(str(component) for component in location) or "$"
        else:
            field_path = "$"
        message = detail.get("msg")
        if not isinstance(message, str):
            message = "Field does not satisfy the schema."
        line = detail.get("line")
        column = detail.get("column")
        position = (
            f" (line {line}, column {column})"
            if isinstance(line, int) and isinstance(column, int)
            else ""
        )
        lines.append(
            f"  invalid: {field_path} [validation_error] {message}{position}"
        )
    return tuple(lines)


def envelope(
    *,
    command: str,
    success: bool,
    data: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
    errors: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "command": command,
        "success": success,
        "data": data or {},
        "warnings": warnings or [],
        "errors": errors or [],
        "observed_at": _observed_at(),
    }


def dump_envelope(value: dict[str, Any]) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
