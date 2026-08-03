from __future__ import annotations

import pytest
from pydantic import ValidationError

from researchctl.domain.models import (
    CIValidationAttestation,
    CIValidationCheck,
    GeneratedOutputDigest,
    LinearProjectionDisabled,
)
from researchctl.serialization import canonical_digest


def _attestation(**updates):
    outputs = (
        GeneratedOutputDigest(
            path="generated/report-preview.md",
            digest="sha256:" + "1" * 64,
            size_bytes=42,
        ),
    )
    payload = {
        "attestation_id": "attestation_20260803T120000Z_" + "1" * 24,
        "project_id": "project_20260803T120000Z_" + "2" * 24,
        "task_id": "task_20260803T120000Z_" + "3" * 24,
        "submission_id": "submission_20260803T120000Z_" + "4" * 24,
        "repository": "example/research",
        "pull_request_number": 17,
        "subject_head": "a" * 40,
        "subject_tree": "b" * 40,
        "base_commit": "c" * 40,
        "validator_version": "0.1.0",
        "schema_manifest_digest": "sha256:" + "2" * 64,
        "workflow_id": "research-validate-pr",
        "check_identity": "researchctl/exact-head",
        "checks": (
            CIValidationCheck(name="generated_outputs", status="passed"),
            CIValidationCheck(name="record_linkage", status="passed"),
        ),
        "generated_outputs": outputs,
        "submission_digest": "sha256:" + "3" * 64,
        "report_proposal_digest": "sha256:" + "4" * 64,
        "report_preview_digest": "sha256:" + "5" * 64,
        "projection": LinearProjectionDisabled(
            reason="integration_not_configured"
        ),
        "generated_at": "2026-08-03T12:00:00Z",
        "artifact_digest": canonical_digest(
            {
                "generated_outputs": [
                    item.model_dump(mode="json") for item in outputs
                ]
            }
        ),
        "overall_result": "passed",
    }
    payload.update(updates)
    return CIValidationAttestation.model_validate(payload)


def test_ci_attestation_binds_exact_head_checks_outputs_and_disabled_projection() -> None:
    record = _attestation()

    assert record.subject_head == "a" * 40
    assert record.subject_tree == "b" * 40
    assert record.base_commit == "c" * 40
    assert record.projection.state == "disabled"
    assert record.overall_result == "passed"


def test_ci_attestation_rejects_self_hash_or_inconsistent_named_result() -> None:
    with pytest.raises(ValidationError):
        _attestation(artifact_digest="sha256:" + "f" * 64)

    with pytest.raises(ValidationError):
        _attestation(
            checks=(
                CIValidationCheck(name="record_linkage", status="failed"),
            ),
            overall_result="passed",
        )
