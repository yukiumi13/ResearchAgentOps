from __future__ import annotations

import os
import socket
from pathlib import Path
from typing import Annotated

import typer

from researchctl.adapters.reconcile import LocalReconcileObserver
from researchctl.output import dump_envelope, envelope
from researchctl.runtime import RuntimeStore
from researchctl.services.project_runtime import ProjectRuntimeService
from researchctl.services.reconcile import (
    LocalReconcileService,
    ReconcileClassification,
    ReconcilePlan,
    RunReconcileState,
    RuntimeObservationState,
)


def _warnings(plan: ReconcilePlan) -> list[str]:
    warnings = [failure.message for failure in plan.observation_failures]
    if plan.runtime_observation is not RuntimeObservationState.AVAILABLE:
        warnings.append(
            "Runtime-only capabilities, journals, status delivery, and inbox actions "
            "cannot be reconstructed from Git alone."
        )
    return warnings


def _render_human(plan: ReconcilePlan) -> None:
    counts = {
        classification: sum(
            item.classification is classification for item in plan.items
        )
        for classification in ReconcileClassification
    }
    typer.echo(
        f"Reconcile: {plan.outcome.value}; {len(plan.items)} session(s) "
        f"(clean={counts[ReconcileClassification.CLEAN]}, "
        f"recoverable={counts[ReconcileClassification.RECOVERABLE]}, "
        f"uncertain={counts[ReconcileClassification.UNCERTAIN]}, "
        f"lost={counts[ReconcileClassification.LOST]})"
    )
    for item in plan.items:
        actions = ", ".join(item.proposed_actions) or "none"
        typer.echo(f"[{item.classification.value}] {item.session_id} -> {actions}")
    run_counts = {
        state: sum(item.state is state for item in plan.run_items)
        for state in RunReconcileState
    }
    typer.echo(
        f"Runs: {len(plan.run_items)} "
        f"(frozen={run_counts[RunReconcileState.FROZEN]}, "
        f"collected={run_counts[RunReconcileState.COLLECTED]}, "
        f"collect_candidate={run_counts[RunReconcileState.COLLECT_CANDIDATE]}, "
        f"execution_uncertain={run_counts[RunReconcileState.EXECUTION_UNCERTAIN]}, "
        f"inconsistent={run_counts[RunReconcileState.INCONSISTENT]})"
    )
    for item in plan.run_items:
        actions = ", ".join(item.proposed_actions) or "none"
        typer.echo(
            f"[{item.classification.value}/{item.state.value}] "
            f"{item.run_id or item.observation_key} -> {actions}"
        )
    if plan.runtime_observation is not RuntimeObservationState.AVAILABLE:
        typer.echo(
            f"Runtime DB: {plan.runtime_observation.value}; Git cannot reconstruct "
            "capabilities, operation journal, undelivered status/outbox, or inbox "
            "ack/snooze/resolve."
        )
    for failure in plan.observation_failures:
        typer.echo(f"Observation incomplete: {failure.component} ({failure.code})")
    typer.echo(f"Plan: {plan.plan_digest}")


def reconcile_command(
    path: Annotated[
        Path,
        typer.Argument(help="Managed Git repository to inspect."),
    ] = Path("."),
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the complete stable reconcile plan."),
    ] = False,
) -> None:
    """Compare local Session and Run evidence and print a read-only recovery plan."""
    command = "reconcile"
    runtime: RuntimeStore | None = None
    try:
        locator = ProjectRuntimeService()
        project = locator.discover(path)

        # RuntimeStore creates a database when absent, so absence must be decided first.
        if os.path.lexists(project.runtime.database_path):
            runtime = RuntimeStore(project.runtime.database_path)

        observer = LocalReconcileObserver(
            project.repository_root,
            run_marker_directory=(
                project.runtime.worktrees_directory / ".researchctl-run-markers"
            ),
        )
        plan = LocalReconcileService(
            project_id=project.project_id,
            local_host=socket.gethostname().split(".", maxsplit=1)[0],
            worktrees_directory=project.runtime.worktrees_directory,
            observer=observer,
            runtime=runtime,
        ).plan()
    except Exception as exc:
        from researchctl.cli import _abort, _known_error

        error = _known_error(exc)
        if error is None:
            raise
        _abort(error, command=command, json_output=json_output)
    finally:
        if runtime is not None:
            runtime.close()

    if json_output:
        typer.echo(
            dump_envelope(
                envelope(
                    command=command,
                    success=True,
                    data=plan.as_dict(),
                    warnings=_warnings(plan),
                )
            )
        )
        return
    _render_human(plan)
