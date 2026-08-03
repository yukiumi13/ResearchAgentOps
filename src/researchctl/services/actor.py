from __future__ import annotations

from enum import StrEnum

from pydantic import model_validator

from researchctl.domain.models import StrictModel
from researchctl.domain.types import SessionId, ShortText
from researchctl.errors import RCPError


class ActorRole(StrEnum):
    MANAGER = "manager"
    AGENT = "agent"
    RUNNER = "runner"
    TRUSTED_AUTOMATION = "trusted_automation"


class CredentialKind(StrEnum):
    LOCAL_OS = "local_os"
    SESSION_CAPABILITY = "session_capability"
    RUNNER_CREDENTIAL = "runner_credential"
    AUTOMATION_CREDENTIAL = "automation_credential"


_ROLE_CREDENTIALS = {
    ActorRole.MANAGER: CredentialKind.LOCAL_OS,
    ActorRole.AGENT: CredentialKind.SESSION_CAPABILITY,
    ActorRole.RUNNER: CredentialKind.RUNNER_CREDENTIAL,
    ActorRole.TRUSTED_AUTOMATION: CredentialKind.AUTOMATION_CREDENTIAL,
}


class ActorContext(StrictModel):
    """Authenticated authority supplied separately from every business request."""

    actor_id: ShortText
    role: ActorRole
    credential_kind: CredentialKind
    bound_session_id: SessionId | None = None

    @model_validator(mode="after")
    def require_credential_binding(self) -> ActorContext:
        expected = _ROLE_CREDENTIALS[self.role]
        if self.credential_kind is not expected:
            raise ValueError(
                f"role {self.role.value} requires credential kind {expected.value}"
            )
        session_scoped = self.role in {ActorRole.AGENT, ActorRole.RUNNER}
        if session_scoped and self.bound_session_id is None:
            raise ValueError(f"role {self.role.value} requires a Session binding")
        if not session_scoped and self.bound_session_id is not None:
            raise ValueError(f"role {self.role.value} cannot carry a Session binding")
        return self

    def require_role(self, command: str, *allowed: ActorRole) -> None:
        if self.role in allowed:
            return
        raise RCPError(
            code="authorization_denied",
            message=f"Actor role {self.role.value} cannot perform {command}.",
            remediation="Use an authenticated actor with authority for this operation.",
            context={
                "actor_id": self.actor_id,
                "actor_role": self.role.value,
                "command": command,
                "allowed_roles": [role.value for role in allowed],
            },
        )

    def require_session_scope(
        self,
        session_id: str,
        *,
        command: str,
        manager_allowed: bool = True,
    ) -> None:
        if manager_allowed and self.role is ActorRole.MANAGER:
            return
        if self.bound_session_id == session_id:
            return
        raise RCPError(
            code="session_scope_denied",
            message="The authenticated actor is not bound to the requested Session.",
            remediation="Use the capability issued for this Session.",
            context={
                "actor_id": self.actor_id,
                "actor_role": self.role.value,
                "command": command,
                "requested_session_id": session_id,
            },
        )
