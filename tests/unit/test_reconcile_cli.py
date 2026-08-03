from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from researchctl.cli import app
from researchctl.services.reconcile import (
    RUNTIME_RECOVERY_LIMITS,
    ReconcileClassification,
    ReconcileOutcome,
    ReconcilePlan,
    ReconcilePlanItem,
    RunMarkerPlanItem,
    RunReconcilePlanItem,
    RunReconcileState,
    RuntimeObservationState,
)


NOW = datetime(2026, 8, 3, 12, 0, 0, tzinfo=UTC)
PROJECT_ID = "project_20260803T120000Z_" + "a" * 24
SESSION_ID = "session_20260803T120000Z_" + "b" * 24
COMMIT = "c" * 40
PLAN_DIGEST = "sha256:" + "d" * 64
RUN_ID = "run_20260803T120000Z_" + "e" * 24
ATTEMPT_ID = "attempt_20260803T120000Z_" + "f" * 24


def _missing_runtime_plan(worktrees: Path) -> ReconcilePlan:
    item = ReconcilePlanItem(
        session_id=SESSION_ID,
        classification=ReconcileClassification.RECOVERABLE,
        task_id=None,
        task_key="MAR-17",
        runtime_state=None,
        branch=f"research/task/MAR-17/{SESSION_ID}",
        branch_commit=COMMIT,
        worktree_path=str(worktrees / SESSION_ID),
        worktree_head=COMMIT,
        tmux_session=f"research-{SESSION_ID}",
        tmux_present=False,
        native_session_id=None,
        capability_digest_present=False,
        continued_from=None,
        reasons=("runtime_database_missing",),
        proposed_actions=(
            "restore_runtime_backup_if_available",
            "continue_with_new_session_id",
        ),
    )
    run_item = RunReconcilePlanItem(
        observation_key=RUN_ID,
        run_id=RUN_ID,
        classification=ReconcileClassification.RECOVERABLE,
        state=RunReconcileState.COLLECT_CANDIDATE,
        branch=f"research/run/{RUN_ID}",
        branch_commit=COMMIT,
        tag=f"research-run/{RUN_ID}",
        tag_commit=COMMIT,
        spec_digest="sha256:" + "a" * 64,
        result_id=None,
        markers=(
            RunMarkerPlanItem(
                observation_key="marker:sha256:" + "b" * 64,
                path=str(
                    worktrees
                    / ".researchctl-run-markers"
                    / f"{ATTEMPT_ID}.json"
                ),
                attempt_id=ATTEMPT_ID,
                phase="terminal",
                pid=None,
                terminal=True,
                valid=True,
            ),
        ),
        reasons=("terminal_marker_without_run_result",),
        proposed_actions=("collect_terminal_attempt_explicitly",),
    )
    return ReconcilePlan(
        outcome=ReconcileOutcome.PLAN_READY,
        observed_at=NOW,
        runtime_observation=RuntimeObservationState.MISSING,
        items=(item,),
        run_items=(run_item,),
        observation_failures=(),
        runtime_recovery_limits=RUNTIME_RECOVERY_LIMITS,
        takeover_token_created=False,
        plan_digest=PLAN_DIGEST,
    )


def test_reconcile_cli_missing_db_is_read_only_and_human_json_are_equivalent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import researchctl.reconcile_cli as cli_module

    repository = tmp_path / "repository"
    repository.mkdir()
    state_directory = tmp_path / "git-common" / "researchctl"
    database_path = state_directory / "runtime-v1.sqlite3"
    worktrees_directory = state_directory / "worktrees"
    project = SimpleNamespace(
        project_id=PROJECT_ID,
        repository_root=repository,
        runtime=SimpleNamespace(
            database_path=database_path,
            worktrees_directory=worktrees_directory,
        ),
    )
    observed: dict[str, list[Any]] = {
        "discover": [],
        "observer": [],
        "service": [],
    }

    class FakeLocator:
        def discover(self, path: Path) -> Any:
            observed["discover"].append(path)
            return project

    class FakeObserver:
        def __init__(self, root: Path, **kwargs: Any) -> None:
            observed["observer"].append(root)
            assert kwargs["run_marker_directory"] == (
                worktrees_directory / ".researchctl-run-markers"
            )

    class FakeService:
        def __init__(self, **kwargs: Any) -> None:
            observed["service"].append(kwargs)

        def plan(self) -> ReconcilePlan:
            return _missing_runtime_plan(worktrees_directory)

    def forbidden_runtime_store(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("RuntimeStore must not be constructed for a missing DB")

    monkeypatch.setattr(cli_module, "ProjectRuntimeService", FakeLocator)
    monkeypatch.setattr(cli_module, "LocalReconcileObserver", FakeObserver)
    monkeypatch.setattr(cli_module, "LocalReconcileService", FakeService)
    monkeypatch.setattr(cli_module, "RuntimeStore", forbidden_runtime_store)

    runner = CliRunner()
    human = runner.invoke(app, ["reconcile", str(repository)])
    machine = runner.invoke(app, ["reconcile", str(repository), "--json"])

    assert human.exit_code == 0, human.output
    assert machine.exit_code == 0, machine.output
    assert not database_path.exists()
    assert not state_directory.exists()
    assert observed["discover"] == [repository, repository]
    assert observed["observer"] == [repository, repository]
    assert len(observed["service"]) == 2
    for arguments in observed["service"]:
        assert arguments["project_id"] == PROJECT_ID
        assert arguments["worktrees_directory"] == worktrees_directory
        assert arguments["runtime"] is None

    payload = json.loads(machine.stdout)
    assert payload["command"] == "reconcile"
    assert payload["success"] is True
    assert payload["errors"] == []
    assert payload["data"] == _missing_runtime_plan(worktrees_directory).as_dict()
    assert set(payload["data"]) == {
        "version",
        "outcome",
        "observed_at",
        "runtime_observation",
        "takeover_token_created",
        "items",
        "runs",
        "observation_failures",
        "runtime_recovery_limits",
        "plan_digest",
    }

    item = payload["data"]["items"][0]
    human_item = (
        f"[{item['classification']}] {item['session_id']} -> "
        f"{', '.join(item['proposed_actions'])}"
    )
    assert human_item in human.stdout
    run = payload["data"]["runs"][0]
    human_run = (
        f"[{run['classification']}/{run['state']}] {run['run_id']} -> "
        f"{', '.join(run['proposed_actions'])}"
    )
    assert human_run in human.stdout
    assert "Reconcile: plan_ready; 1 session(s)" in human.stdout
    assert "Runs: 1" in human.stdout
    assert "Runtime DB: missing" in human.stdout
    for phrase in (
        "capabilities",
        "operation journal",
        "undelivered status/outbox",
        "ack/snooze/resolve",
    ):
        assert phrase in human.stdout
    assert f"Plan: {PLAN_DIGEST}" in human.stdout
    assert len(human.stdout.splitlines()) == 6
