from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from researchctl.adapters._subprocess import CommandResult
from researchctl.adapters.reconcile import LocalReconcileObserver
from researchctl.domain.enums import SessionState
from researchctl.runtime.models import RuntimeSession
from researchctl.services.reconcile import (
    LocalReconcileService,
    ReconcileClassification,
    ReconcileOutcome,
    RuntimeObservationState,
)

NOW = datetime(2026, 8, 3, 12, 0, 0, tzinfo=UTC)
PROJECT_ID = "project_20260803T120000Z_" + "a" * 24
TASK_ID = "task_20260803T120000Z_" + "b" * 24
SESSION_ID = "session_20260803T120000Z_" + "c" * 24
BRANCH = f"research/task/MAR-17/{SESSION_ID}"
BRANCH_REF = f"refs/heads/{BRANCH}"
TMUX_NAME = f"research-{SESSION_ID}"
COMMIT = "d" * 40
NATIVE_ID = "0199a213-81c0-7800-8aa1-bbab2a035a53"
CAPABILITY_DIGEST = "sha256:" + "e" * 64


@dataclass(frozen=True, slots=True)
class RunnerCall:
    argv: tuple[str, ...]
    cwd: Path | None
    env: dict[str, str] | None
    timeout_seconds: float


class InventoryRunner:
    def __init__(
        self,
        *,
        branches: tuple[tuple[str, str], ...] = ((BRANCH, COMMIT),),
        worktrees: tuple[tuple[Path, str | None, str | None, bool], ...] = (),
        tmux_sessions: tuple[str, ...] = (),
        failure: str | None = None,
    ) -> None:
        self.branches = branches
        self.worktrees = worktrees
        self.tmux_sessions = tmux_sessions
        self.failure = failure
        self.calls: list[RunnerCall] = []

    def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path | None,
        env: dict[str, str] | None,
        timeout_seconds: float,
    ) -> CommandResult:
        self.calls.append(
            RunnerCall(
                argv=argv,
                cwd=cwd,
                env=dict(env) if env is not None else None,
                timeout_seconds=timeout_seconds,
            )
        )
        if (
            argv[0] == "git"
            and argv[5] == "for-each-ref"
            and argv[-1] == "refs/heads/research/task/"
        ):
            if self.failure == "git_branches":
                return CommandResult(2, stderr="sensitive stderr is never reported")
            return CommandResult(
                0,
                stdout="".join(
                    f"refs/heads/{branch}\x00{commit}\n"
                    for branch, commit in self.branches
                ),
            )
        if (
            argv[0] == "git"
            and argv[5] == "for-each-ref"
            and argv[-2:] == (
                "refs/heads/research/run/",
                "refs/tags/research-run/",
            )
        ):
            if self.failure == "run_refs":
                return CommandResult(2, stderr="sensitive stderr is never reported")
            return CommandResult(0)
        if argv[0] == "git" and argv[5:7] == ("worktree", "list"):
            if self.failure == "git_worktrees":
                return CommandResult(2, stderr="sensitive stderr is never reported")
            fields: list[str] = []
            for path, head, branch_ref, prunable in self.worktrees:
                fields.append(f"worktree {path}")
                if head is not None:
                    fields.append(f"HEAD {head}")
                if branch_ref is not None:
                    fields.append(f"branch {branch_ref}")
                if prunable:
                    fields.append("prunable missing")
                fields.append("")
            return CommandResult(0, stdout="\x00".join(fields))
        if argv == ("tmux", "list-sessions", "-F", "#{session_name}"):
            if self.failure == "tmux":
                return CommandResult(2, stderr="sensitive stderr is never reported")
            if self.failure == "tmux_unknown_rc1":
                return CommandResult(1, stderr="permission denied")
            if not self.tmux_sessions:
                return CommandResult(1, stderr="no server running on test socket")
            return CommandResult(
                0,
                stdout="".join(f"{name}\n" for name in self.tmux_sessions),
            )
        raise AssertionError(f"unexpected observation command: {argv!r}")


class FakeRuntime:
    def __init__(
        self,
        sessions: tuple[RuntimeSession, ...],
        *,
        fail: bool = False,
    ) -> None:
        self.sessions = sessions
        self.fail = fail
        self.calls: list[str | None] = []

    def list_sessions(self, project_id: str | None = None) -> tuple[RuntimeSession, ...]:
        self.calls.append(project_id)
        if self.fail:
            raise RuntimeError("database is unreadable")
        return self.sessions


def _runtime_session(
    worktree: Path,
    *,
    state: SessionState = SessionState.ACTIVE,
    native_session_id: str | None = NATIVE_ID,
    capability_digest: str | None = CAPABILITY_DIGEST,
    branch: str = BRANCH,
    host: str = "host-a",
) -> RuntimeSession:
    metadata = {"agent": "codex", "tmux_session": TMUX_NAME}
    if native_session_id is not None:
        metadata["native_session_id"] = native_session_id
    return RuntimeSession(
        session_id=SESSION_ID,
        project_id=PROJECT_ID,
        task_id=TASK_ID,
        state=state,
        created_at=NOW,
        updated_at=NOW,
        host=host,
        branch=branch,
        worktree_path=str(worktree),
        continued_from=None,
        actor_token_digest=capability_digest,
        metadata=metadata,
    )


def _service(
    tmp_path: Path,
    *,
    runner: InventoryRunner,
    runtime: FakeRuntime | None,
) -> LocalReconcileService:
    for path, _, _, prunable in runner.worktrees:
        if not prunable:
            path.mkdir(parents=True, exist_ok=True)
    repository = tmp_path / "repository"
    repository.mkdir(exist_ok=True)
    worktrees = tmp_path / "worktrees"
    worktrees.mkdir(exist_ok=True)
    observer = LocalReconcileObserver(
        repository,
        runner=runner,  # type: ignore[arg-type]
        timeout_seconds=1.25,
        max_records=50,
    )
    return LocalReconcileService(
        project_id=PROJECT_ID,
        local_host="host-a",
        worktrees_directory=worktrees,
        observer=observer,
        runtime=runtime,
        clock=lambda: NOW,
        max_runtime_sessions=50,
    )


def _exact_worktree(tmp_path: Path) -> tuple[Path, str, str, bool]:
    return tmp_path / "worktrees" / SESSION_ID, COMMIT, BRANCH_REF, False


def test_clean_plan_requires_git_worktree_tmux_runtime_native_and_capability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_DIR", "/must/not/leak")
    worktree = tmp_path / "worktrees" / SESSION_ID
    runtime = FakeRuntime((_runtime_session(worktree),))
    runner = InventoryRunner(
        worktrees=(_exact_worktree(tmp_path),),
        tmux_sessions=(TMUX_NAME,),
    )
    service = _service(tmp_path, runner=runner, runtime=runtime)

    first = service.plan()
    second = service.plan()

    assert first == second
    assert first.outcome is ReconcileOutcome.CLEAN
    assert first.runtime_observation is RuntimeObservationState.AVAILABLE
    assert first.takeover_token_created is False
    assert first.runtime_recovery_limits == ()
    assert first.plan_digest.startswith("sha256:") and len(first.plan_digest) == 71
    assert len(first.items) == 1
    item = first.items[0]
    assert item.classification is ReconcileClassification.CLEAN
    assert item.native_session_id == NATIVE_ID
    assert item.capability_digest_present is True
    assert item.proposed_actions == ()
    assert runtime.calls == [PROJECT_ID, PROJECT_ID]

    assert len(runner.calls) == 8
    for call in runner.calls:
        assert call.cwd is None
        assert call.timeout_seconds == 1.25
        command_text = " ".join(call.argv)
        assert all(
            mutation not in command_text
            for mutation in (" add ", " remove ", " kill-", " new-", " start-")
        )
        if call.argv[0] == "git":
            assert call.env is not None and "GIT_DIR" not in call.env


@pytest.mark.parametrize(
    ("state", "include_worktree", "expected"),
    [
        (SessionState.ACTIVE, True, ReconcileClassification.LOST),
        (SessionState.LOST, True, ReconcileClassification.LOST),
        (SessionState.PREPARING, False, ReconcileClassification.RECOVERABLE),
        (SessionState.IDLE, True, ReconcileClassification.CLEAN),
        (SessionState.STOPPED, False, ReconcileClassification.RECOVERABLE),
    ],
)
def test_stopped_tmux_classification_is_deterministic(
    tmp_path: Path,
    state: SessionState,
    include_worktree: bool,
    expected: ReconcileClassification,
) -> None:
    worktree = tmp_path / "worktrees" / SESSION_ID
    runtime = FakeRuntime((_runtime_session(worktree, state=state),))
    runner = InventoryRunner(
        worktrees=(_exact_worktree(tmp_path),) if include_worktree else (),
    )

    plan = _service(tmp_path, runner=runner, runtime=runtime).plan()

    assert plan.items[0].classification is expected
    if expected is ReconcileClassification.LOST:
        assert plan.items[0].proposed_actions == ("continue_with_new_session_id",)
    assert plan.outcome is (
        ReconcileOutcome.CLEAN
        if expected is ReconcileClassification.CLEAN
        else ReconcileOutcome.PLAN_READY
    )


@pytest.mark.parametrize(
    ("native_id", "capability_digest", "reason"),
    [
        (None, CAPABILITY_DIGEST, "live_tmux_without_native_session_id"),
        (NATIVE_ID, None, "live_tmux_without_capability_digest"),
    ],
)
def test_live_tmux_without_complete_runtime_identity_is_uncertain(
    tmp_path: Path,
    native_id: str | None,
    capability_digest: str | None,
    reason: str,
) -> None:
    worktree = tmp_path / "worktrees" / SESSION_ID
    runtime = FakeRuntime(
        (
            _runtime_session(
                worktree,
                native_session_id=native_id,
                capability_digest=capability_digest,
            ),
        )
    )
    runner = InventoryRunner(
        worktrees=(_exact_worktree(tmp_path),),
        tmux_sessions=(TMUX_NAME,),
    )

    plan = _service(tmp_path, runner=runner, runtime=runtime).plan()

    assert plan.outcome is ReconcileOutcome.PLAN_READY
    assert plan.items[0].classification is ReconcileClassification.UNCERTAIN
    assert reason in plan.items[0].reasons
    assert plan.items[0].proposed_actions == ("manual_observation_required",)
    assert plan.takeover_token_created is False


def test_missing_runtime_db_reports_rpo_limits_and_never_invents_live_identity(
    tmp_path: Path,
) -> None:
    stopped_runner = InventoryRunner(worktrees=(_exact_worktree(tmp_path),))
    stopped = _service(tmp_path, runner=stopped_runner, runtime=None).plan()

    assert stopped.outcome is ReconcileOutcome.PLAN_READY
    assert stopped.runtime_observation is RuntimeObservationState.MISSING
    assert stopped.items[0].classification is ReconcileClassification.RECOVERABLE
    assert stopped.items[0].native_session_id is None
    assert stopped.items[0].capability_digest_present is False
    assert stopped.items[0].proposed_actions == (
        "restore_runtime_backup_if_available",
        "continue_with_new_session_id",
    )
    assert stopped.takeover_token_created is False
    assert {limit.code for limit in stopped.runtime_recovery_limits} == {
        "session_capabilities_not_reconstructible",
        "operation_journal_not_reconstructible",
        "undelivered_status_not_reconstructible",
        "attention_actions_not_reconstructible",
    }
    descriptions = " ".join(limit.description for limit in stopped.runtime_recovery_limits)
    for phrase in (
        "capabilities",
        "operation journal",
        "Status updates",
        "acknowledgements, snoozes, and resolutions",
        "never creates a capability or takeover token",
    ):
        assert phrase in descriptions

    live_runner = InventoryRunner(
        worktrees=(_exact_worktree(tmp_path),),
        tmux_sessions=(TMUX_NAME,),
    )
    live = _service(tmp_path, runner=live_runner, runtime=None).plan()

    assert live.items[0].classification is ReconcileClassification.UNCERTAIN
    assert "live_tmux_without_runtime_identity" in live.items[0].reasons
    assert live.items[0].proposed_actions == ("manual_observation_required",)


def test_branch_checked_out_at_wrong_worktree_is_uncertain_not_recoverable(
    tmp_path: Path,
) -> None:
    expected = tmp_path / "worktrees" / SESSION_ID
    wrong = tmp_path / "other" / SESSION_ID
    runtime = FakeRuntime((_runtime_session(expected, state=SessionState.IDLE),))
    runner = InventoryRunner(worktrees=((wrong, COMMIT, BRANCH_REF, False),))

    plan = _service(tmp_path, runner=runner, runtime=runtime).plan()

    assert plan.items[0].classification is ReconcileClassification.UNCERTAIN
    assert "worktree_missing_or_mismatched" in plan.items[0].reasons


def test_unregistered_canonical_worktree_path_is_uncertain(
    tmp_path: Path,
) -> None:
    expected = tmp_path / "worktrees" / SESSION_ID
    expected.mkdir(parents=True)
    runtime = FakeRuntime((_runtime_session(expected, state=SessionState.IDLE),))
    runner = InventoryRunner(worktrees=())

    plan = _service(tmp_path, runner=runner, runtime=runtime).plan()

    assert plan.items[0].classification is ReconcileClassification.UNCERTAIN
    assert "unregistered_worktree_path_exists" in plan.items[0].reasons


def test_stopping_session_with_missing_branch_is_uncertain(
    tmp_path: Path,
) -> None:
    expected = tmp_path / "worktrees" / SESSION_ID
    runtime = FakeRuntime((_runtime_session(expected, state=SessionState.STOPPING),))
    runner = InventoryRunner(branches=(), worktrees=())

    plan = _service(tmp_path, runner=runner, runtime=runtime).plan()

    assert plan.items[0].classification is ReconcileClassification.UNCERTAIN
    assert "branch_missing" in plan.items[0].reasons


@pytest.mark.parametrize("failure", ["git_branches", "git_worktrees", "tmux"])
def test_observation_error_returns_partial_and_does_not_guess_lost(
    tmp_path: Path,
    failure: str,
) -> None:
    worktree = tmp_path / "worktrees" / SESSION_ID
    runtime = FakeRuntime((_runtime_session(worktree),))
    runner = InventoryRunner(
        worktrees=(_exact_worktree(tmp_path),),
        tmux_sessions=(TMUX_NAME,),
        failure=failure,
    )

    plan = _service(tmp_path, runner=runner, runtime=runtime).plan()

    assert plan.outcome is ReconcileOutcome.PARTIAL_OBSERVATION
    assert len(plan.observation_failures) == 1
    assert plan.observation_failures[0].component == failure
    assert "sensitive stderr" not in repr(plan)
    assert plan.items[0].classification is ReconcileClassification.UNCERTAIN
    assert plan.items[0].classification is not ReconcileClassification.LOST
    assert f"observation_incomplete:{failure}" in plan.items[0].reasons



def test_unknown_tmux_rc_one_remains_partial_observation(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "worktrees" / SESSION_ID
    runtime = FakeRuntime((_runtime_session(worktree),))
    runner = InventoryRunner(
        worktrees=(_exact_worktree(tmp_path),),
        failure="tmux_unknown_rc1",
    )

    plan = _service(tmp_path, runner=runner, runtime=runtime).plan()

    assert plan.outcome is ReconcileOutcome.PARTIAL_OBSERVATION
    assert plan.observation_failures[0].code == "tmux_command_failed"
    assert plan.items[0].classification is ReconcileClassification.UNCERTAIN
    assert plan.items[0].classification is not ReconcileClassification.LOST


def test_runtime_read_error_is_partial_and_exposes_recovery_limits(
    tmp_path: Path,
) -> None:
    runtime = FakeRuntime((), fail=True)
    runner = InventoryRunner(
        worktrees=(_exact_worktree(tmp_path),),
        tmux_sessions=(TMUX_NAME,),
    )

    plan = _service(tmp_path, runner=runner, runtime=runtime).plan()

    assert plan.outcome is ReconcileOutcome.PARTIAL_OBSERVATION
    assert plan.runtime_observation is RuntimeObservationState.ERROR
    assert plan.observation_failures[0].code == "runtime_observation_failed"
    assert plan.items[0].classification is ReconcileClassification.UNCERTAIN
    assert "observation_incomplete:runtime" in plan.items[0].reasons
    assert plan.runtime_recovery_limits
    assert plan.takeover_token_created is False


def test_noncanonical_research_tmux_inventory_is_partial_observation(
    tmp_path: Path,
) -> None:
    runner = InventoryRunner(tmux_sessions=("research-not-a-session-id",))

    plan = _service(tmp_path, runner=runner, runtime=FakeRuntime(())).plan()

    assert plan.outcome is ReconcileOutcome.PARTIAL_OBSERVATION
    assert plan.observation_failures[0].code == "tmux_output_invalid"
    assert plan.items[0].classification is ReconcileClassification.UNCERTAIN
    assert "observation_incomplete:tmux" in plan.items[0].reasons
