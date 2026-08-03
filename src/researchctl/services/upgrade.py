from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from researchctl.config import load_project_config
from researchctl.constants import PROJECT_CONFIG_NAME, PROTOCOL_VERSION
from researchctl.errors import ProtocolCompatibilityError
from researchctl.repository import discover_repository, safe_repository_path


@dataclass(frozen=True, slots=True)
class UpgradeCheck:
    repository: Path
    current: str
    target: str
    compatible: bool
    migration_required: bool
    changes: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "repository": str(self.repository),
            "current": self.current,
            "target": self.target,
            "compatible": self.compatible,
            "migration_required": self.migration_required,
            "changes": list(self.changes),
        }


def _parts(version: str) -> tuple[int, int]:
    try:
        major_text, minor_text = version.split(".", maxsplit=1)
        return int(major_text), int(minor_text)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid protocol version: {version!r}") from exc


def check_upgrade(path: Path) -> UpgradeCheck:
    repository = discover_repository(path)
    config_path = safe_repository_path(repository.root, PROJECT_CONFIG_NAME)
    config = load_project_config(config_path)
    current = _parts(config.protocol_version)
    target = _parts(PROTOCOL_VERSION)

    if current[0] != target[0] or current > target:
        raise ProtocolCompatibilityError(config.protocol_version, PROTOCOL_VERSION)

    migration_required = current < target
    return UpgradeCheck(
        repository=repository.root,
        current=config.protocol_version,
        target=PROTOCOL_VERSION,
        compatible=True,
        migration_required=migration_required,
        changes=(
            ("A manager-reviewed protocol migration is required.",)
            if migration_required
            else ()
        ),
    )
