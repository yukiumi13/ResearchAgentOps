from __future__ import annotations

import hashlib
import json

from researchctl.constants import PROTOCOL_VERSION
from researchctl.schema import SCHEMA_MODELS, generate_schema_files


EXPECTED_FILE_SHA256 = {
    "ci-validation-attestation.schema.json": (
        "28545c955f8732f6f07b0ace2655888c410a64bb6745d078de0fc8bf57165100"
    ),
    "dependency-change-receipt.schema.json": (
        "9c84ab2efb3a14e72194aea7dc724d733492f44a111ed9da648c09ea9259517a"
    ),
    "experiment-plan.schema.json": (
        "ea9a9e6605cfeed351cb603ade19dab4c2a2e0050fad77742e1fa1c14e1eabd5"
    ),
    "impact-decision.schema.json": (
        "c4b0de7410fdd75e3e14fbec15199cfab7b6e5940dce08d8687bf4e36ec4d311"
    ),
    "linear-projection-policy.schema.json": (
        "1fe26641a13487c156e897b61dc57e728ecdb3e9ea933e96f6084710d11b2646"
    ),
    "plan-review.schema.json": (
        "f5ee98589661badf473e4f5b47bdee6b83432a89ca501934316d9b02070e2b9d"
    ),
    "policy.schema.json": "9f50cdae7e15d3ff7ad43269a45d72ee1070064ed9e482e51c84602613c23c69",
    "manifest.json": "4025543d5e1205375aef5dbb361bacef99cee4074e61aa82ac676714952737e0",
    "project.schema.json": "14f86275ae17891280548b32cff9cb3998fea09424933525b5152efcd3ea0235",
    "report-proposal.schema.json": (
        "4208f055cfc0429fb10f6d33b9b82d5b9bd6716eeae712974023bb8c52be75c6"
    ),
    "report-impact.schema.json": (
        "5372761260e39a0897ebb2ec4b9999d5a0a3c6b5e71bfeca2599fc689cd72250"
    ),
    "report-impact-batch.schema.json": (
        "50e2fe243da5cbb24ec3a49578afccc9974edfdd880e16428be4a84d0e4b6d16"
    ),
    "report.schema.json": "6c2ba291f310a5c0e4f17683b41ebc4337b8ad40c61852e016717f4e7fdf62ae",
    "research-submission.schema.json": (
        "9194a5383e9323596ee51a900c5633376f48bfaf589424027f9ab236244d0c39"
    ),
    "review-decision.schema.json": (
        "e5de29722548b47f2f005399911caa5d60a2dca9d393ddb58305ac621b635cad"
    ),
    "run-attempt.schema.json": (
        "55d32c1c70b5e5a4abba2954b7f59bcfae8d0a9bb694c3408b59a52f524067c3"
    ),
    "run-result.schema.json": (
        "c4bda290dc1eba38d8b4fdd72829743b7d4a247b7b02743dc21d1c43c51269ed"
    ),
    "run-spec.schema.json": (
        "51cf40b156c485ed4c0dea1c2f6eca7a4e1ff3de6d32f70b78c5e2a2e5cbfa4b"
    ),
    "status-update.schema.json": (
        "17401b3348a639ae1fb86502653a4ec74aa87c07b740ca325ce8ba6c636e5251"
    ),
    "task.schema.json": "87da25858db35847cb250d63a344d58170da1d66c010b4ab716a3b0b26148ef2",
}


def test_generated_schema_files_match_the_frozen_byte_fingerprints() -> None:
    first = generate_schema_files()
    second = generate_schema_files()

    assert first == second
    assert {
        name: hashlib.sha256(content).hexdigest() for name, content in first.items()
    } == EXPECTED_FILE_SHA256


def test_schema_manifest_covers_and_authenticates_every_schema() -> None:
    files = generate_schema_files()
    manifest = json.loads(files["manifest.json"])
    expected_paths = {f"{name}.schema.json" for name in SCHEMA_MODELS}

    assert manifest["protocol_version"] == PROTOCOL_VERSION
    assert set(manifest["schemas"]) == expected_paths
    assert set(files) == expected_paths | {"manifest.json"}

    for path, recorded_digest in manifest["schemas"].items():
        assert recorded_digest == "sha256:" + hashlib.sha256(files[path]).hexdigest()


def test_each_schema_has_a_stable_id_and_json_file_format() -> None:
    files = generate_schema_files()

    for name in SCHEMA_MODELS:
        path = f"{name}.schema.json"
        content = files[path]
        schema = json.loads(content)
        assert content.endswith(b"\n")
        assert schema["$id"] == f"urn:researchctl:schema:{PROTOCOL_VERSION}:{name}"


def test_input_identity_schema_requires_a_non_null_version_or_digest() -> None:
    task_schema = json.loads(generate_schema_files()["task.schema.json"])
    input_identity = task_schema["$defs"]["InputIdentity"]

    assert input_identity["anyOf"] == [
        {
            "properties": {"version": {"type": "string"}},
            "required": ["version"],
        },
        {
            "properties": {"digest": {"type": "string"}},
            "required": ["digest"],
        },
    ]
