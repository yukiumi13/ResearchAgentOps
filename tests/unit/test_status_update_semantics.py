from __future__ import annotations

import pytest
from pydantic import ValidationError

from researchctl.domain.models import StatusUpdate


def _id(kind: str, fill: str) -> str:
    return f"{kind}_20260802T123456Z_{fill * 24}"


def _status(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "0.1",
        "update_id": _id("update", "a"),
        "task_id": _id("task", "b"),
        "session_id": _id("session", "c"),
        "status": "running",
        "summary": "Evaluating the candidate configuration.",
        "observed_at": "2026-08-02T12:34:56Z",
    }
    payload.update(overrides)
    return payload


def test_blocked_status_requires_structured_blocker_identity_and_detail() -> None:
    with pytest.raises(ValidationError):
        StatusUpdate.model_validate(_status(status="blocked"))

    update = StatusUpdate.model_validate(
        _status(
            status="blocked",
            blocker_category="input",
            blocker_detail="Dataset digest does not match the declared input.",
        )
    )

    assert update.blocker_category == "input"


def test_needs_input_requires_a_concrete_decision_request() -> None:
    with pytest.raises(ValidationError):
        StatusUpdate.model_validate(_status(status="needs_input"))

    update = StatusUpdate.model_validate(
        _status(
            status="needs_input",
            decision_needed={
                "question": "Which validation split should this run use?",
                "options": ["frozen-v1", "rebuild-v2"],
            },
        )
    )

    assert update.decision_needed is not None


@pytest.mark.parametrize(
    "fields",
    [
        {"blocker_category": "input"},
        {"blocker_detail": "Missing typed input."},
        {
            "blocker_category": "input",
            "blocker_detail": "Missing typed input.",
        },
    ],
)
def test_nonblocked_status_rejects_blocker_fields(fields: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        StatusUpdate.model_validate(_status(**fields))
