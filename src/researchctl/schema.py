from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from pydantic import BaseModel

from researchctl.constants import (
    PROTOCOL_VERSION,
    SCHEMA_GENERATOR_VERSION,
    __version__,
)
from researchctl.domain.types import Sha256Digest
from researchctl.domain.models import (
    CIValidationAttestation,
    LinearProjectionPolicy,
    ProjectRecord,
    ProjectPolicy,
    ReportRecord,
    ReportProposal,
    ResearchSubmission,
    ReviewDecision,
    RunAttempt,
    RunResult,
    RunSpec,
    StatusUpdate,
    TaskRecord,
)

SCHEMA_MODELS: Mapping[str, type[BaseModel]] = {
    "ci-validation-attestation": CIValidationAttestation,
    "linear-projection-policy": LinearProjectionPolicy,
    "project": ProjectRecord,
    "policy": ProjectPolicy,
    "report": ReportRecord,
    "report-proposal": ReportProposal,
    "research-submission": ResearchSubmission,
    "review-decision": ReviewDecision,
    "run-attempt": RunAttempt,
    "run-result": RunResult,
    "run-spec": RunSpec,
    "status-update": StatusUpdate,
    "task": TaskRecord,
}


def _json_file_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def generate_schema_files() -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    hashes: dict[str, str] = {}
    for name, model_type in sorted(SCHEMA_MODELS.items()):
        schema = model_type.model_json_schema()
        schema["$id"] = f"urn:researchctl:schema:{PROTOCOL_VERSION}:{name}"
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        path = f"{name}.schema.json"
        content = _json_file_bytes(schema)
        files[path] = content
        hashes[path] = f"sha256:{hashlib.sha256(content).hexdigest()}"

    manifest = {
        "protocol_version": PROTOCOL_VERSION,
        "researchctl_version": __version__,
        "schema_generator_version": SCHEMA_GENERATOR_VERSION,
        "schemas": hashes,
    }
    files["manifest.json"] = _json_file_bytes(manifest)
    return files


def schema_manifest_digest(files: Mapping[str, bytes] | None = None) -> Sha256Digest:
    generated = files if files is not None else generate_schema_files()
    digest = hashlib.sha256(generated["manifest.json"]).hexdigest()
    return f"sha256:{digest}"  # type: ignore[return-value]
