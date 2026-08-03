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
