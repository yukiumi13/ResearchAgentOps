from __future__ import annotations

import io

import pytest
from pydantic import BaseModel, ConfigDict, StrictInt, ValidationError

from researchctl.errors import RCPError
from researchctl.request_io import MAX_JSON_REQUEST_BYTES, read_json_request


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    count: StrictInt


def test_machine_request_reads_one_strict_json_object() -> None:
    request = read_json_request(io.BytesIO(b'{"count": 3}\n'), Request)

    assert request.count == 3


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"[]",
        b'{"count": 1, "count": 2}',
        b'{"count": NaN}',
        b"\xff",
        b'{"count":',
    ],
)
def test_machine_request_rejects_noncanonical_or_ambiguous_input(content: bytes) -> None:
    with pytest.raises(RCPError):
        read_json_request(io.BytesIO(content), Request)


def test_machine_request_rejects_unknown_or_coercible_fields() -> None:
    with pytest.raises(ValidationError):
        read_json_request(io.BytesIO(b'{"count": "3"}'), Request)

    with pytest.raises(ValidationError):
        read_json_request(io.BytesIO(b'{"count": 3, "actor_role": "manager"}'), Request)


def test_machine_request_has_a_hard_size_limit() -> None:
    oversized = b"{" + b" " * MAX_JSON_REQUEST_BYTES + b"}"

    with pytest.raises(RCPError) as error:
        read_json_request(io.BytesIO(oversized), Request)

    assert error.value.code == "json_request_too_large"
