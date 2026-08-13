from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from researchctl.domain.models import RunSpec, TaskRecord
from researchctl.errors import RCPError
from researchctl.serialization import canonical_digest
from researchctl.services.run_preflight import (
    GPUObservation,
    IdentityObservation,
    LocalRunPreflight,
    StaticIdentityResolver,
)

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


def _spec(run_spec_payload, **overrides) -> RunSpec:
    base = RunSpec.model_validate(run_spec_payload())
    values = {
        "requested_host": "host-a",
        "environment": {
            "kind": "environment",
            "logical_id": "trainer-cu128",
            "digest": "sha256:" + "3" * 64,
            "waiver_allowed": False,
        },
        "inputs": (
            {
                "kind": "dataset",
                "logical_id": "validation-split",
                "version": "2026-08-01",
                "waiver_allowed": False,
            },
        ),
    }
    values.update(overrides)
    normalized = {
        key: TypeAdapter(RunSpec.model_fields[key].annotation).validate_python(value)
        for key, value in values.items()
    }
    draft = base.model_copy(update=normalized)
    payload = draft.model_dump(
        mode="json",
        exclude={"spec_digest"},
        exclude_none=True,
    )
    payload["spec_digest"] = canonical_digest(payload)
    return RunSpec.model_validate(payload)


def _task(task_payload, **overrides) -> TaskRecord:
    return TaskRecord.model_validate(task_payload(state="ready", **overrides))


def _resolver() -> StaticIdentityResolver:
    return StaticIdentityResolver(
        (
            IdentityObservation(
                kind="environment",
                logical_id="trainer-cu128",
                digest="sha256:" + "3" * 64,
            ),
            IdentityObservation(
                kind="dataset",
                logical_id="validation-split",
                version="2026-08-01",
            ),
        )
    )


def test_local_preflight_verifies_identity_executable_paths_disk_and_gpu(
    tmp_path: Path,
    run_spec_payload,
    task_payload,
) -> None:
    worktree = tmp_path / "worktree"
    (worktree / "src" / "training").mkdir(parents=True)
    (worktree / "results" / "MAR-17").mkdir(parents=True)
    executable = worktree / "src" / "training" / "run.sh"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    spec = _spec(
        run_spec_payload,
        argv=("./src/training/run.sh",),
        resources={
            "gpu_count": 1,
            "gpu_type": "H100",
            "min_gpu_memory_gb": 70,
            "preferred_hosts": [],
            "preferred_pools": [],
        },
        artifact_declarations=(
            {
                "name": "metrics",
                "path": "results/MAR-17/metrics.json",
                "media_type": "application/json",
            },
        ),
    )
    preflight = LocalRunPreflight(
        local_host="host-a",
        identities=_resolver(),
        gpu_inventory=(GPUObservation("GPU-1", "H100", 80, True, NOW),),
        minimum_free_bytes=0,
        clock=lambda: NOW,
    )

    receipt = preflight.check(
        spec=spec,
        task=_task(task_payload),
        execution_worktree=worktree,
        assigned_gpu_uuids=("GPU-1",),
    )

    assert receipt.gpu_uuids == ("GPU-1",)
    assert receipt.artifact_paths == ("results/MAR-17/metrics.json",)
    assert receipt.executable == str(executable)
    assert receipt.allocation_backend == "local_static"
    assert receipt.global_exclusivity is False
    assert receipt.gpu_inventory_observed_at == ("2026-08-03T12:00:00Z",)
    assert receipt.receipt_digest.startswith("sha256:")


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ("missing_host", "run_host_required"),
        ("wrong_host", "run_host_mismatch"),
        ("identity", "run_input_mismatch"),
        ("gpu_count", "run_gpu_assignment_invalid"),
        ("gpu_type", "run_gpu_mismatch"),
        ("artifact_scope", "run_artifact_scope_violation"),
    ],
)
def test_local_preflight_fails_before_launch_on_mismatch(
    tmp_path: Path,
    run_spec_payload,
    task_payload,
    change: str,
    code: str,
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    executable = worktree / "run.sh"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    values = {
        "argv": ("./run.sh",),
        "resources": {
            "gpu_count": 1,
            "gpu_type": "H100" if change != "gpu_type" else "A100",
            "preferred_hosts": [],
            "preferred_pools": [],
        },
    }
    if change == "missing_host":
        values["requested_host"] = None
    elif change == "wrong_host":
        values["requested_host"] = "host-b"
    elif change == "identity":
        values["environment"] = {
            "kind": "environment",
            "logical_id": "trainer-cu128",
            "digest": "sha256:" + "4" * 64,
            "waiver_allowed": False,
        }
    elif change == "artifact_scope":
        values["artifact_declarations"] = (
            {
                "name": "outside",
                "path": "outside/result.json",
                "media_type": "application/json",
            },
        )
    spec = _spec(run_spec_payload, **values)
    assigned = () if change == "gpu_count" else ("GPU-1",)
    preflight = LocalRunPreflight(
        local_host="host-a",
        identities=_resolver(),
        gpu_inventory=(GPUObservation("GPU-1", "H100", 80, True, NOW),),
        minimum_free_bytes=0,
        clock=lambda: NOW,
    )

    with pytest.raises(RCPError) as caught:
        preflight.check(
            spec=spec,
            task=_task(task_payload),
            execution_worktree=worktree,
            assigned_gpu_uuids=assigned,
        )

    assert caught.value.code == code


def test_local_preflight_rejects_missing_required_task_input_before_path_checks(
    tmp_path: Path,
    run_spec_payload,
    task_payload,
) -> None:
    spec = _spec(run_spec_payload, inputs=(), argv=("./missing-executable",))
    preflight = LocalRunPreflight(
        local_host="host-a",
        identities=_resolver(),
        minimum_free_bytes=0,
    )

    with pytest.raises(RCPError) as caught:
        preflight.check(
            spec=spec,
            task=_task(task_payload),
            execution_worktree=tmp_path / "missing-worktree",
        )

    assert caught.value.code == "run_required_input_missing"
    assert caught.value.context["kind"] == "dataset"
    assert caught.value.context["logical_id"] == "validation-split"


def test_local_preflight_rejects_all_required_input_constraint_mismatches(
    tmp_path: Path,
    run_spec_payload,
    task_payload,
) -> None:
    required_digest = "sha256:" + "5" * 64
    declared_digest = "sha256:" + "6" * 64
    spec = _spec(
        run_spec_payload,
        inputs=(
            {
                "kind": "dataset",
                "logical_id": "validation-split",
                "version": "2026-07-31",
                "digest": declared_digest,
                "uri": "file:///datasets/stale",
                "waiver_allowed": True,
            },
        ),
        argv=("./missing-executable",),
    )
    task = _task(
        task_payload,
        required_inputs=[
            {
                "kind": "dataset",
                "logical_id": "validation-split",
                "version": "2026-08-01",
                "digest": required_digest,
                "uri": "file:///datasets/frozen",
                "waiver_allowed": True,
            }
        ],
    )
    preflight = LocalRunPreflight(
        local_host="host-a",
        identities=_resolver(),
        minimum_free_bytes=0,
    )

    with pytest.raises(RCPError) as caught:
        preflight.check(
            spec=spec,
            task=task,
            execution_worktree=tmp_path / "missing-worktree",
        )

    assert caught.value.code == "run_required_input_mismatch"
    assert caught.value.context["mismatches"] == ["version", "digest", "uri"]
    assert caught.value.context["required"]["waiver_allowed"] is True
    assert caught.value.context["declared"]["waiver_allowed"] is True


def test_local_preflight_matches_required_environment_config_and_inputs(
    tmp_path: Path,
    run_spec_payload,
    task_payload,
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    executable = worktree / "run.sh"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    config_uri = "file:///configs/candidate.yaml"
    dataset_uri = "file:///datasets/validation-split"
    spec = _spec(
        run_spec_payload,
        argv=("./run.sh",),
        config={
            "kind": "config",
            "logical_id": "candidate",
            "version": "7",
            "uri": config_uri,
        },
        inputs=(
            {
                "kind": "dataset",
                "logical_id": "validation-split",
                "version": "2026-08-01",
                "uri": dataset_uri,
            },
        ),
    )
    task = _task(
        task_payload,
        required_inputs=[
            {
                "kind": "environment",
                "logical_id": "trainer-cu128",
                "digest": "sha256:" + "3" * 64,
            },
            {
                "kind": "config",
                "logical_id": "candidate",
                "version": "7",
                "uri": config_uri,
            },
            {
                "kind": "dataset",
                "logical_id": "validation-split",
                "version": "2026-08-01",
                "uri": dataset_uri,
            },
        ],
    )
    resolver = StaticIdentityResolver(
        (
            IdentityObservation(
                kind="environment",
                logical_id="trainer-cu128",
                digest="sha256:" + "3" * 64,
            ),
            IdentityObservation(
                kind="config",
                logical_id="candidate",
                version="7",
                uri=config_uri,
            ),
            IdentityObservation(
                kind="dataset",
                logical_id="validation-split",
                version="2026-08-01",
                uri=dataset_uri,
            ),
        )
    )

    receipt = LocalRunPreflight(
        local_host="host-a",
        identities=resolver,
        minimum_free_bytes=0,
    ).check(spec=spec, task=task, execution_worktree=worktree)

    assert [item.kind for item in receipt.identities] == [
        "environment",
        "config",
        "dataset",
    ]


def test_local_preflight_rejects_symlink_escape_and_non_executable(
    tmp_path: Path,
    run_spec_payload,
    task_payload,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "linked").symlink_to(outside, target_is_directory=True)
    linked_spec = _spec(run_spec_payload, working_directory="linked", argv=("tool",))
    preflight = LocalRunPreflight(
        local_host="host-a",
        identities=_resolver(),
        minimum_free_bytes=0,
        path_environment=os.environ.get("PATH"),
    )

    with pytest.raises(RCPError) as linked:
        preflight.check(
            spec=linked_spec,
            task=_task(task_payload),
            execution_worktree=worktree,
        )
    assert linked.value.code == "run_working_directory_invalid"

    plain = worktree / "plain.sh"
    plain.write_text("#!/bin/sh\n", encoding="utf-8")
    plain_spec = _spec(run_spec_payload, argv=("./plain.sh",))
    with pytest.raises(RCPError) as non_executable:
        preflight.check(
            spec=plain_spec,
            task=_task(task_payload),
            execution_worktree=worktree,
        )
    assert non_executable.value.code == "run_executable_invalid"


def test_local_preflight_rejects_stale_gpu_inventory(
    tmp_path: Path,
    run_spec_payload,
    task_payload,
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    executable = worktree / "run.sh"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    spec = _spec(
        run_spec_payload,
        argv=("./run.sh",),
        resources={
            "gpu_count": 1,
            "preferred_hosts": [],
            "preferred_pools": [],
        },
    )
    preflight = LocalRunPreflight(
        local_host="host-a",
        identities=_resolver(),
        gpu_inventory=(
            GPUObservation("GPU-1", "H100", 80, True, NOW - timedelta(seconds=31)),
        ),
        minimum_free_bytes=0,
        clock=lambda: NOW,
    )

    with pytest.raises(RCPError) as caught:
        preflight.check(
            spec=spec,
            task=_task(task_payload),
            execution_worktree=worktree,
            assigned_gpu_uuids=("GPU-1",),
        )

    assert caught.value.code == "run_gpu_inventory_stale"
