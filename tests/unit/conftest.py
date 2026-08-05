from __future__ import annotations

from collections.abc import Callable, Sequence
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from researchctl.serialization import canonical_digest

OBSERVED_AT = datetime(2026, 8, 2, 12, 34, 56, tzinfo=UTC)


def record_id(kind: str, fill: str) -> str:
    return f"{kind}_20260802T123456Z_{fill * 24}"


@pytest.fixture
def task_payload() -> Callable[..., dict[str, Any]]:
    base: dict[str, Any] = {
        "schema_version": "0.1",
        "task_id": record_id("task", "a"),
        "key": "MAR-17",
        "title": "Evaluate a stopping policy",
        "state": "ready",
        "priority": "high",
        "goal": "Determine whether the candidate policy improves validation loss.",
        "done_when": ["The comparison and its uncertainty are recorded."],
        "execution_domain": "on-prem",
        "allowed_write_paths": ["src/training", "results/MAR-17"],
        "deliverables": ["A comparison report with uncertainty."],
        "constraints": ["Use the frozen validation split."],
        "required_inputs": [
            {
                "kind": "dataset",
                "logical_id": "validation-split",
                "version": "2026-08-01",
                "waiver_allowed": False,
            }
        ],
        "execution": {
            "preferred_hosts": ["host-a"],
            "preferred_pools": ["interactive"],
            "gpu_count": 1,
            "gpu_type": "H100",
            "min_gpu_memory_gb": 70,
        },
        "created_at": OBSERVED_AT,
        "updated_at": OBSERVED_AT,
    }

    def factory(**overrides: Any) -> dict[str, Any]:
        payload = deepcopy(base)
        payload.update(overrides)
        return payload

    return factory


@pytest.fixture
def run_attempt_payload() -> Callable[..., dict[str, Any]]:
    states = ("preparing", "snapshotted", "preflighted", "allocated", "launching")

    def factory(
        sequences: Sequence[int] = (0,),
        **overrides: Any,
    ) -> dict[str, Any]:
        events = [
            {
                "operation_id": record_id("operation", "d"),
                "sequence": sequence,
                "state": states[index % len(states)],
                "observed_at": OBSERVED_AT + timedelta(seconds=index),
                "idempotency_key": f"launch-step-{index}",
                "host": "host-a",
                "external_ids": {"tmux_session": f"research-{index}"},
            }
            for index, sequence in enumerate(sequences)
        ]
        payload: dict[str, Any] = {
            "schema_version": "0.1",
            "attempt_id": record_id("attempt", "b"),
            "run_id": record_id("run", "c"),
            "operation_id": record_id("operation", "d"),
            "events": events,
        }
        payload.update(overrides)
        return payload

    return factory


@pytest.fixture
def run_spec_payload() -> Callable[..., dict[str, Any]]:
    base: dict[str, Any] = {
        "schema_version": "0.1",
        "run_id": record_id("run", "c"),
        "task_id": record_id("task", "a"),
        "session_id": record_id("session", "e"),
        "operation_id": record_id("operation", "d"),
        "source_commit": "1" * 40,
        "source_tree": "2" * 40,
        "argv": ["python", "train.py", "--config", "candidate"],
        "working_directory": ".",
        "environment": {
            "kind": "environment",
            "logical_id": "trainer-cu128",
            "digest": "sha256:" + "3" * 64,
            "waiver_allowed": False,
        },
        "inputs": [],
        "resources": {
            "gpu_count": 0,
            "preferred_hosts": [],
            "preferred_pools": [],
        },
        "artifact_declarations": [],
        "created_at": "2026-08-02T12:34:56Z",
    }

    def factory(**overrides: Any) -> dict[str, Any]:
        payload = deepcopy(base)
        payload.update(overrides)
        if "spec_digest" not in overrides:
            payload["spec_digest"] = canonical_digest(payload)
        return payload

    return factory


@pytest.fixture
def run_result_payload() -> Callable[..., dict[str, Any]]:
    base: dict[str, Any] = {
        "schema_version": "0.1",
        "result_id": record_id("result", "1"),
        "run_id": record_id("run", "c"),
        "run_spec_digest": "sha256:" + "4" * 64,
        "attempt_ids": [record_id("attempt", "b")],
        "outcome": "complete",
        "started_at": OBSERVED_AT,
        "finished_at": OBSERVED_AT + timedelta(seconds=5),
        "exit_code": 0,
    }

    def factory(**overrides: Any) -> dict[str, Any]:
        payload = deepcopy(base)
        payload.update(overrides)
        return payload

    return factory


@pytest.fixture
def submission_payload() -> Callable[..., dict[str, Any]]:
    base: dict[str, Any] = {
        "schema_version": "0.1",
        "submission_id": record_id("submission", "f"),
        "task_id": record_id("task", "a"),
        "session_id": record_id("session", "e"),
        "category": "candidate_result",
        "claim": "The candidate improves validation loss.",
        "run_result_ids": [record_id("result", "1")],
        "created_at": OBSERVED_AT,
    }

    def factory(**overrides: Any) -> dict[str, Any]:
        payload = deepcopy(base)
        payload.update(overrides)
        return payload

    return factory


@pytest.fixture
def review_decision_payload() -> Callable[..., dict[str, Any]]:
    base: dict[str, Any] = {
        "schema_version": "0.1",
        "decision_id": record_id("decision", "2"),
        "submission_id": record_id("submission", "f"),
        "disposition": "accepted",
        "reviewer_actor": "manager",
        "decided_at": OBSERVED_AT,
        "conditions": [],
        "claim_scope": "snapshot",
        "code_disposition": "retain_isolated",
        "report_id": record_id("report", "e"),
        "expected_report_revision": 1,
        "accepted_submission_digest": "sha256:" + "5" * 64,
    }

    def factory(**overrides: Any) -> dict[str, Any]:
        payload = deepcopy(base)
        payload.update(overrides)
        return payload

    return factory


@pytest.fixture
def report_payload() -> Callable[..., dict[str, Any]]:
    base: dict[str, Any] = {
        "schema_version": "0.1",
        "report_id": record_id("report", "e"),
        "revision": 1,
        "title": "Stopping policy comparison",
        "claim": "The candidate policy improves validation loss on the declared baseline.",
        "claim_scope": "baseline",
        "evidence_status": "verified",
        "applicability": "current",
        "submission_id": record_id("submission", "f"),
        "run_result_ids": [record_id("result", "1")],
        "evidence_tree": "a" * 40,
        "accepted_at_main_tree": "b" * 40,
        "validation_basis": {
            "main_tree": "c" * 40,
            "assessed_at": OBSERVED_AT,
        },
        "dependencies": {
            "paths": ["src/training/stop.py"],
            "resources": ["validation-split"],
            "environments": ["trainer-cu128"],
        },
    }

    def factory(**overrides: Any) -> dict[str, Any]:
        payload = deepcopy(base)
        payload.update(overrides)
        return payload

    return factory


@pytest.fixture
def initialized_repository(tmp_path):
    import subprocess

    from researchctl.services.init_project import initialize_project

    repository = tmp_path / "managed-project"
    repository.mkdir()
    subprocess.run(
        ["git", "-C", str(repository), "init", "--initial-branch=main"],
        check=True,
        capture_output=True,
        text=True,
    )
    for key, value in (
        ("user.name", "Tests"),
        ("user.email", "tests@example.invalid"),
    ):
        subprocess.run(
            ["git", "-C", str(repository), "config", key, value],
            check=True,
            capture_output=True,
            text=True,
        )
    (repository / "README.md").write_text("# Project\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repository), "add", "README.md"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "-c",
            "user.name=Tests",
            "-c",
            "user.email=tests@example.invalid",
            "commit",
            "-m",
            "initial",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    initialize_project(repository)
    return repository
