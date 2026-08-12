from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from researchctl.domain.models import RunSpec, TaskRecord
from researchctl.errors import RCPError
from researchctl.serialization import dump_yaml
from researchctl.services.run_preflight import (
    GPUObservation,
    LocalRunPreflight,
    StaticIdentityResolver,
)
from researchctl.services.run_profiles import LocalRunProfile

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
DIGEST = "sha256:" + "3" * 64


def _payload(*, observed_at: datetime = NOW) -> dict[str, object]:
    return {
        "version": 1,
        "host": "host-a",
        "identity_observations": [
            {
                "kind": "environment",
                "logical_id": "trainer-cu128",
                "digest": DIGEST,
                "uri": "file:///opt/environments/trainer-cu128",
            },
            {
                "kind": "dataset",
                "logical_id": "validation-split",
                "version": "2026-08-01",
                "uri": "file:///datasets/validation-split",
            },
        ],
        "gpu_observations": [
            {
                "gpu_uuid": "GPU-1",
                "gpu_type": "H100",
                "memory_gb": 80,
                "available": True,
                "observed_at": observed_at,
            }
        ],
        "minimum_free_bytes": 0,
    }


def _write_profile(path: Path, payload: dict[str, object] | None = None) -> None:
    path.write_text(dump_yaml(_payload() if payload is None else payload), encoding="utf-8")


def test_load_builds_credential_free_preflight_components(tmp_path: Path) -> None:
    path = tmp_path / "host-profile-v1.yaml"
    _write_profile(path)

    profile = LocalRunProfile.load(path, expected_host="host-a")

    identities = profile.build_identity_resolver()
    gpus = profile.build_gpu_inventory()
    preflight = profile.build_preflight(clock=lambda: NOW)
    assert isinstance(identities, StaticIdentityResolver)
    assert gpus == (GPUObservation("GPU-1", "H100", 80, True, NOW),)
    assert isinstance(preflight, LocalRunPreflight)
    assert preflight.path_environment == os.defpath
    assert preflight.minimum_free_bytes == 0


def test_missing_profile_has_stable_remediation_and_is_not_created(
    tmp_path: Path,
) -> None:
    path = tmp_path / "missing.yaml"

    with pytest.raises(RCPError) as caught:
        LocalRunProfile.load(path, expected_host="host-a")

    assert caught.value.code == "run_host_profile_missing"
    assert caught.value.remediation == (
        "Create a reviewed version 1 host profile at the explicit path before "
        "starting a local run."
    )
    assert not path.exists()


@pytest.mark.parametrize(
    "document",
    [
        "version: 1\nversion: 1\n",
        dump_yaml({**_payload(), "credentials": {"token": "not-a-real-token"}}),
        dump_yaml(_payload()).replace(
            "minimum_free_bytes: 0",
            "minimum_free_bytes: .nan",
        ),
        dump_yaml({**_payload(), "version": "1"}),
        dump_yaml(
            {
                **_payload(),
                "controlled_path": "/usr/bin:$PATH",
            }
        ),
    ],
)
def test_load_rejects_duplicate_unknown_nonfinite_and_coercible_data(
    tmp_path: Path,
    document: str,
) -> None:
    path = tmp_path / "host-profile-v1.yaml"
    path.write_text(document, encoding="utf-8")

    with pytest.raises(RCPError) as caught:
        LocalRunProfile.load(path, expected_host="host-a")

    assert caught.value.code == "run_host_profile_invalid"
    assert caught.value.remediation is not None
    assert "not-a-real-token" not in str(caught.value.context)


def test_load_rejects_invalid_utf8_and_oversized_profiles(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.yaml"
    invalid.write_bytes(b"version: 1\n\xff")
    with pytest.raises(RCPError) as malformed:
        LocalRunProfile.load(invalid, expected_host="host-a")
    assert malformed.value.code == "run_host_profile_invalid"

    oversized = tmp_path / "oversized.yaml"
    oversized.write_bytes(b"x" * (64 * 1024 + 1))
    with pytest.raises(RCPError) as too_large:
        LocalRunProfile.load(oversized, expected_host="host-a")
    assert too_large.value.code == "run_host_profile_too_large"


def test_load_rejects_symlink_and_non_regular_file(tmp_path: Path) -> None:
    target = tmp_path / "target.yaml"
    _write_profile(target)
    link = tmp_path / "linked.yaml"
    link.symlink_to(target)

    with pytest.raises(RCPError) as linked:
        LocalRunProfile.load(link, expected_host="host-a")
    assert linked.value.code == "run_host_profile_unsafe"

    with pytest.raises(RCPError) as directory:
        LocalRunProfile.load(tmp_path, expected_host="host-a")
    assert directory.value.code == "run_host_profile_unsafe"


def test_load_rejects_host_mismatch_and_noncanonical_host(tmp_path: Path) -> None:
    path = tmp_path / "host-profile-v1.yaml"
    _write_profile(path)

    with pytest.raises(RCPError) as mismatch:
        LocalRunProfile.load(path, expected_host="host-b")
    assert mismatch.value.code == "run_host_profile_host_mismatch"

    payload = _payload()
    payload["host"] = "CM04"
    _write_profile(path, payload)
    with pytest.raises(RCPError) as noncanonical:
        LocalRunProfile.load(path, expected_host="host-a")
    assert noncanonical.value.code == "run_host_profile_invalid"


@pytest.mark.parametrize("duplicate_kind", ["identity", "gpu"])
def test_load_rejects_duplicate_observations(
    tmp_path: Path,
    duplicate_kind: str,
) -> None:
    path = tmp_path / "host-profile-v1.yaml"
    payload = _payload()
    key = (
        "identity_observations"
        if duplicate_kind == "identity"
        else "gpu_observations"
    )
    observations = payload[key]
    assert isinstance(observations, list)
    observations.append(dict(observations[0]))
    _write_profile(path, payload)

    with pytest.raises(RCPError) as caught:
        LocalRunProfile.load(path, expected_host="host-a")

    assert caught.value.code == "run_host_profile_invalid"


def test_stale_gpu_timestamp_is_loaded_then_rejected_by_preflight(
    tmp_path: Path,
    run_spec_payload,
    task_payload,
) -> None:
    path = tmp_path / "host-profile-v1.yaml"
    _write_profile(path, _payload(observed_at=NOW - timedelta(seconds=31)))
    profile = LocalRunProfile.load(path, expected_host="host-a")
    preflight = profile.build_preflight(clock=lambda: NOW)

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    executable = worktree / "run.sh"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    spec = RunSpec.model_validate(
        run_spec_payload(
            requested_host="host-a",
            argv=["./run.sh"],
            environment={
                "kind": "environment",
                "logical_id": "trainer-cu128",
                "digest": DIGEST,
                "uri": "file:///opt/environments/trainer-cu128",
                "waiver_allowed": False,
            },
            inputs=[
                {
                    "kind": "dataset",
                    "logical_id": "validation-split",
                    "version": "2026-08-01",
                    "uri": "file:///datasets/validation-split",
                    "waiver_allowed": False,
                }
            ],
            resources={
                "gpu_count": 1,
                "gpu_type": "H100",
                "preferred_hosts": [],
                "preferred_pools": [],
            },
        )
    )
    task = TaskRecord.model_validate(task_payload(state="ready"))

    with pytest.raises(RCPError) as caught:
        preflight.check(
            spec=spec,
            task=task,
            execution_worktree=worktree,
            assigned_gpu_uuids=("GPU-1",),
        )

    assert caught.value.code == "run_gpu_inventory_stale"
