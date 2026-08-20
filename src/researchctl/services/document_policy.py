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

from researchctl.domain.models import (
    SIMPLE_MARKDOWN_CONTRACT,
    AgentGuideFormat,
    DocumentLayoutPolicy,
    SimpleDocumentLayoutPolicy,
)
from researchctl.errors import RCPError
from researchctl.serialization import dump_yaml, load_yaml

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


#: Comments that belong beside the field they explain, not in a preamble the
#: adopter scrolls past. ``sections`` carries the longest one because it is the
#: only field this template deliberately refuses to fill.
_SIMPLE_TEMPLATE_HEADER = (
    "# researchctl standalone document policy candidate (version 2)\n"
    "# Directory-first: a section directory under the document root is the\n"
    "# document type. There is no classification to invent and no route table to\n"
    "# keep in sync. Adopting this policy, and every later change to its\n"
    "# sections, is a governance change needing manager/CODEOWNER review.\n"
)

_SIMPLE_TEMPLATE_COMMENTS: dict[str, str] = {
    "root": "# Every governed document lives under this directory.\n",
    "sections": (
        "# Inventory this repository before filling this in. Directory names are\n"
        "# facts about this project, so the list ships empty and\n"
        "# `researchctl doc policy-lint` refuses the candidate until you write\n"
        "# down the sections this repository actually has. Copying another\n"
        "# project's layout is the failure this gate exists to prevent.\n"
        "# Every section accepts ordinary Markdown under the\n"
        f"# {SIMPLE_MARKDOWN_CONTRACT} contract. Add `structured` only where a\n"
        "# section really keeps canonical YAML sources:\n"
        "#\n"
        "#   sections:\n"
        "#     - path: runbooks\n"
        "#     - path: experiments\n"
        "#       structured:\n"
        "#         contract: analysis-brief\n"
    ),
    "root_pages": "# Markdown pages published directly from the document root.\n",
    "max_depth": "# Directory depth allowed below a section directory.\n",
    "ownership": (
        "# CODEOWNERS is the only review authority. `required` refuses a document\n"
        "# that no CODEOWNERS rule matches.\n"
    ),
    "agent_guides": (
        "# Files whose managed block `researchctl doc agent-guide` writes. A guide\n"
        "# must live outside the document root.\n"
    ),
}


def simple_document_policy_template(
    guide_format: AgentGuideFormat,
) -> dict[str, Any]:
    """The version 2 adoption candidate as data, with no section chosen yet."""

    return {
        "version": SIMPLE_POLICY_VERSION,
        "root": "docs",
        "sections": [],
        "root_pages": ["README.md"],
        "max_depth": 3,
        "ownership": {"source": "codeowners", "required": True},
        "agent_guides": [
            {
                "path": "CLAUDE.md" if guide_format == "claude" else "AGENTS.md",
                "format": guide_format,
            }
        ],
    }


def render_simple_document_policy_template(guide_format: AgentGuideFormat) -> bytes:
    """Render the candidate one field at a time so each keeps its comment."""

    blocks = [_SIMPLE_TEMPLATE_HEADER]
    for key, value in simple_document_policy_template(guide_format).items():
        blocks.append(_SIMPLE_TEMPLATE_COMMENTS.get(key, ""))
        blocks.append(dump_yaml({key: value}))
    return "".join(blocks).encode("utf-8")
