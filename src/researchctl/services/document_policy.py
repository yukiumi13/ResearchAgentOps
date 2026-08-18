"""Select the effective standalone document policy version.

One repository has one policy file. Which contract that file expresses is an
explicit ``version`` discriminator, not an inference from which keys happen to
be present: a v1 policy keeps working untouched, a v2 policy opts in by saying
so, and anything else fails closed rather than being read under a guessed
contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from researchctl.domain.models import DocumentLayoutPolicy, SimpleDocumentLayoutPolicy
from researchctl.errors import RCPError
from researchctl.serialization import load_yaml

LEGACY_POLICY_VERSION = 1
SIMPLE_POLICY_VERSION = 2
SUPPORTED_POLICY_VERSIONS: tuple[int, ...] = (
    LEGACY_POLICY_VERSION,
    SIMPLE_POLICY_VERSION,
)


@dataclass(frozen=True, slots=True)
class EffectiveDocumentPolicy:
    """The one document policy in force, tagged with the version that produced it."""

    version: int
    source: Path | None = None
    legacy: DocumentLayoutPolicy | None = None
    simple: SimpleDocumentLayoutPolicy | None = None

    @property
    def is_simple(self) -> bool:
        return self.version == SIMPLE_POLICY_VERSION

    @property
    def root(self) -> str:
        if self.simple is not None:
            return self.simple.root
        assert self.legacy is not None
        return self.legacy.root

    def require_legacy(self, *, command: str) -> DocumentLayoutPolicy:
        if self.legacy is None:
            raise RCPError(
                code="document_policy_version_unsupported_command",
                message=(
                    f"`{command}` is not implemented for a version {self.version} "
                    "document policy."
                ),
                remediation=(
                    "Use the commands this policy version supports, or keep the "
                    "repository on the version 1 document policy."
                ),
                context={"command": command, "policy_version": self.version},
            )
        return self.legacy

    def require_simple(self, *, command: str) -> SimpleDocumentLayoutPolicy:
        if self.simple is None:
            raise RCPError(
                code="document_policy_version_unsupported_command",
                message=(
                    f"`{command}` requires a version {SIMPLE_POLICY_VERSION} "
                    "document policy."
                ),
                context={"command": command, "policy_version": self.version},
            )
        return self.simple


def select_policy_version(payload: dict[str, Any], *, path: Path | None = None) -> int:
    """Read the explicit policy version, defaulting to the original contract."""

    if "version" not in payload:
        return LEGACY_POLICY_VERSION
    declared = payload["version"]
    if isinstance(declared, bool) or not isinstance(declared, int):
        raise _unsupported_version(declared, path)
    if declared not in SUPPORTED_POLICY_VERSIONS:
        raise _unsupported_version(declared, path)
    return declared


def _unsupported_version(declared: object, path: Path | None) -> RCPError:
    return RCPError(
        code="document_policy_version_unsupported",
        message=f"Document policy version {declared!r} is not supported.",
        remediation=(
            "Declare `version: 2` for the simple directory-first policy, or omit "
            "`version` for the original classification-route policy."
        ),
        context={
            "declared_version": declared,
            "supported_versions": list(SUPPORTED_POLICY_VERSIONS),
            **({"path": str(path)} if path is not None else {}),
        },
    )


def build_effective_policy(
    payload: dict[str, Any],
    *,
    path: Path | None = None,
) -> EffectiveDocumentPolicy:
    """Validate a raw policy mapping under the contract its version selects."""

    version = select_policy_version(payload, path=path)
    if version == SIMPLE_POLICY_VERSION:
        return EffectiveDocumentPolicy(
            version=version,
            source=path,
            simple=SimpleDocumentLayoutPolicy.model_validate(payload),
        )
    legacy_payload = {key: value for key, value in payload.items() if key != "version"}
    return EffectiveDocumentPolicy(
        version=version,
        source=path,
        legacy=DocumentLayoutPolicy.model_validate(legacy_payload),
    )


def load_effective_policy(path: Path) -> EffectiveDocumentPolicy:
    """Read one policy file and validate it under its declared version."""

    return build_effective_policy(
        load_yaml(path.read_text(encoding="utf-8")),
        path=path,
    )
