from __future__ import annotations

import json
from collections.abc import Callable
from typing import BinaryIO, TypeVar

from pydantic import BaseModel

from researchctl.errors import RCPError

RequestT = TypeVar("RequestT", bound=BaseModel)
MAX_JSON_REQUEST_BYTES = 1024 * 1024


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def parse_json_request(content: bytes, model: type[RequestT]) -> RequestT:
    if not content:
        raise RCPError(
            code="empty_json_request",
            message="Machine mode requires one JSON request on standard input.",
        )
    if len(content) > MAX_JSON_REQUEST_BYTES:
        raise RCPError(
            code="json_request_too_large",
            message="JSON request exceeds the 1 MiB input limit.",
        )
    try:
        text = content.decode("utf-8", errors="strict")
        payload = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RCPError(
            code="invalid_json_request",
            message="Machine request is not strict UTF-8 JSON.",
            remediation="Send one finite JSON object without duplicate keys.",
        ) from exc
    if not isinstance(payload, dict):
        raise RCPError(
            code="invalid_json_request",
            message="Machine request must be one JSON object.",
        )
    return model.model_validate(payload)


def read_json_request(stream: BinaryIO, model: type[RequestT]) -> RequestT:
    content = stream.read(MAX_JSON_REQUEST_BYTES + 1)
    return parse_json_request(content, model)


def request_loader(model: type[RequestT]) -> Callable[[BinaryIO], RequestT]:
    return lambda stream: read_json_request(stream, model)
