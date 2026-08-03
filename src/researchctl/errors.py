from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class RCPError(Exception):
    code: str
    message: str
    remediation: str | None = None
    context: dict[str, Any] = field(default_factory=dict)
    exit_code: int = 2

    def __str__(self) -> str:
        return self.message


class RepositoryNotFoundError(RCPError):
    def __init__(self, path: str) -> None:
        super().__init__(
            code="repository_not_found",
            message=f"Not inside a Git repository: {path}",
            remediation="Run the command inside a Git repository.",
            context={"path": path},
        )


class ConflictError(RCPError):
    def __init__(self, message: str, *, paths: list[str] | None = None) -> None:
        super().__init__(
            code="managed_file_conflict",
            message=message,
            remediation="Review the conflicting managed files; RCP did not overwrite them.",
            context={"paths": paths or []},
        )
class UnsafeRepositoryPathError(RCPError):
    def __init__(self, path: str, *, reason: str) -> None:
        super().__init__(
            code="unsafe_repository_path",
            message=f"Repository path is unsafe: {path}",
            remediation=(
                "Keep protocol files inside their managed directory and replace "
                "symlinks with regular repository paths."
            ),
            context={"path": path, "reason": reason},
        )




class ProtocolCompatibilityError(RCPError):
    def __init__(self, found: str, supported: str) -> None:
        super().__init__(
            code="unsupported_protocol",
            message=f"Project protocol {found!r} is not supported by CLI protocol {supported!r}.",
            remediation="Use a compatible researchctl version or review an explicit migration.",
            context={"found": found, "supported": supported},
        )


class ProtocolLockError(RCPError):
    def __init__(self, component: str, *, found: str, expected: str) -> None:
        super().__init__(
            code="protocol_lock_mismatch",
            message=f"Pinned {component} does not match this researchctl build.",
            remediation=(
                "Use the pinned researchctl environment or review an explicit "
                "protocol upgrade."
            ),
            context={
                "component": component,
                "found": found,
                "expected": expected,
            },
        )
