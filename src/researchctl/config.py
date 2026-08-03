from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from researchctl.constants import (
    PROJECT_RECORD_PATH,
    PROTOCOL_VERSION,
    SCHEMA_GENERATOR_VERSION,
    YAML_RENDERER_VERSION,
    __version__,
)
from researchctl.domain.types import ProjectId, Sha256Digest


class ProjectConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    cli_version: Literal["0.1.0"] = __version__
    schema_generator_version: Literal[1] = SCHEMA_GENERATOR_VERSION
    yaml_renderer_version: Literal[1] = YAML_RENDERER_VERSION
    schema_manifest_digest: Sha256Digest
    schema_version: Literal[1] = 1
    protocol_version: str = PROTOCOL_VERSION
    project_id: ProjectId
    project_file: Literal[".research/project.yaml"] = PROJECT_RECORD_PATH


def dump_project_config(config: ProjectConfig) -> bytes:
    lines = [
        f"schema_version = {config.schema_version}",
        f"cli_version = {json.dumps(config.cli_version)}",
        f"schema_generator_version = {config.schema_generator_version}",
        f"yaml_renderer_version = {config.yaml_renderer_version}",
        f"schema_manifest_digest = {json.dumps(config.schema_manifest_digest)}",
        f"protocol_version = {json.dumps(config.protocol_version)}",
        f"project_id = {json.dumps(config.project_id)}",
        f"project_file = {json.dumps(config.project_file)}",
        "",
    ]
    return "\n".join(lines).encode("utf-8")


def load_project_config(path: Path) -> ProjectConfig:
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    return ProjectConfig.model_validate(data)
