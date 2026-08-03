from __future__ import annotations

import io
import json

import pytest
from pydantic import ValidationError

from researchctl.domain.models import TaskRecord
from researchctl.errors import RCPError
from researchctl.request_io import read_json_request
from researchctl.services.actor import ActorContext, ActorRole, CredentialKind
from researchctl.services.requests import TaskCreateRequest


def _id(kind: str, fill: str) -> str:
    return f"{kind}_20260802T123456Z_{fill * 24}"


def test_actor_role_and_credential_binding_are_consistent() -> None:
    manager = ActorContext(
        actor_id="uid-1000",
        role=ActorRole.MANAGER,
        credential_kind=CredentialKind.LOCAL_OS,
    )
    agent = ActorContext(
        actor_id="agent-session-a",
        role=ActorRole.AGENT,
        credential_kind=CredentialKind.SESSION_CAPABILITY,
        bound_session_id=_id("session", "a"),
    )

    assert manager.bound_session_id is None
    assert agent.bound_session_id == _id("session", "a")

    with pytest.raises(ValidationError):
        ActorContext(
            actor_id="spoofed-manager",
            role=ActorRole.MANAGER,
            credential_kind=CredentialKind.SESSION_CAPABILITY,
        )

    with pytest.raises(ValidationError):
        ActorContext(
            actor_id="unbound-agent",
            role=ActorRole.AGENT,
            credential_kind=CredentialKind.SESSION_CAPABILITY,
        )


def test_role_and_session_authorization_fail_with_stable_errors() -> None:
    session_a = _id("session", "a")
    session_b = _id("session", "b")
    actor = ActorContext(
        actor_id="agent-session-a",
        role=ActorRole.AGENT,
        credential_kind=CredentialKind.SESSION_CAPABILITY,
        bound_session_id=session_a,
    )

    with pytest.raises(RCPError) as role_error:
        actor.require_role("task.create", ActorRole.MANAGER)
    assert role_error.value.code == "authorization_denied"

    with pytest.raises(RCPError) as session_error:
        actor.require_session_scope(session_b, command="status.publish")
    assert session_error.value.code == "session_scope_denied"
    assert "bound_session_id" not in session_error.value.context


def test_manager_may_access_a_session_without_becoming_session_scoped() -> None:
    manager = ActorContext(
        actor_id="uid-1000",
        role=ActorRole.MANAGER,
        credential_kind=CredentialKind.LOCAL_OS,
    )

    manager.require_session_scope(_id("session", "a"), command="session.attach")


@pytest.mark.parametrize("field", ["actor_id", "role", "actor_role"])
def test_business_request_rejects_actor_authority_fields(
    field: str,
    task_payload,
) -> None:
    payload = {
        "operation_id": _id("operation", "c"),
        "idempotency_key": "task-create-test",
        "task": TaskRecord.model_validate(task_payload()).model_dump(mode="json"),
        field: "manager",
    }

    with pytest.raises(ValidationError):
        read_json_request(
            io.BytesIO(json.dumps(payload).encode("utf-8")),
            TaskCreateRequest,
        )
