from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from researchctl.cli import app
from researchctl.domain.enums import ProjectState, SessionState
from researchctl.domain.models import (
    AgentPolicy,
    ExecutionDomainPolicy,
    ProjectPolicy,
    ProjectRecord,
    RunSpec,
    TaskRecord,
)
from researchctl.errors import RCPError
from researchctl.runtime import RuntimeSession, RuntimeStore, hash_session_token
from researchctl.serialization import dump_yaml, load_model
from researchctl.services.actor import ActorRole
from researchctl.services.factory import (
    SESSION_ID_ENV,
    SESSION_TOKEN_ENV,
    actor_from_environment,
    open_application,
)
from researchctl.services.project_runtime import discover_managed_project
from researchctl.services.requests import (
    BootstrapAcceptRequest,
    BootstrapProposalRequest,
    RunStartRequest,
    TaskCreateRequest,
)


NOW = datetime(2026, 8, 2, 12, 34, 56, tzinfo=UTC)
BOOTSTRAP_ID = "bootstrap_20260802T123456Z_" + "8" * 24


def _id(kind: str, fill: str) -> str:
    return f"{kind}_20260802T123456Z_{fill * 24}"


def _mark_project_managed(repository) -> None:
    path = repository / ".research" / "project.yaml"
    project = load_model(path, ProjectRecord)
    path.write_text(
        dump_yaml(project.model_copy(update={"state": ProjectState.MANAGED})),
        encoding="utf-8",
    )


def test_open_application_builds_one_shared_manager_service(
    initialized_repository,
) -> None:
    _mark_project_managed(initialized_repository)
    with open_application(initialized_repository, local_host="host-a", environment={}) as handle:
        assert handle.actor.role is ActorRole.MANAGER
        assert handle.service.project_id == handle.project.project_id
        assert handle.service.runtime is handle.runtime
        assert handle.service.tasks.root == initialized_repository.resolve()
        assert handle.runtime.database_path == handle.project.runtime.database_path


def test_session_capability_actor_is_injected_and_bound_outside_request_json(
    initialized_repository,
) -> None:
    _mark_project_managed(initialized_repository)
    project = discover_managed_project(initialized_repository)
    project.runtime.state_directory.mkdir(mode=0o700)
    project.runtime.worktrees_directory.mkdir(mode=0o700)
    session_id = _id("session", "a")
    token = "factory-session-capability"
    with RuntimeStore(project.runtime.database_path) as runtime:
        runtime.save_session(
            RuntimeSession(
                session_id=session_id,
                project_id=project.project_id,
                task_id=_id("task", "b"),
                state=SessionState.ACTIVE,
                created_at=NOW,
                updated_at=NOW,
                host="host-a",
                actor_token_digest=hash_session_token(token),
            )
        )

    environment = {SESSION_ID_ENV: session_id, SESSION_TOKEN_ENV: token}
    with open_application(
        initialized_repository,
        local_host="host-a",
        environment=environment,
    ) as handle:
        assert handle.actor.role is ActorRole.AGENT
        assert handle.actor.bound_session_id == session_id
        assert token not in repr(handle.actor)


def test_incomplete_or_wrong_session_credential_fails_closed(
    initialized_repository,
) -> None:
    project = discover_managed_project(initialized_repository)
    project.runtime.state_directory.mkdir(mode=0o700)
    project.runtime.worktrees_directory.mkdir(mode=0o700)
    with RuntimeStore(project.runtime.database_path) as runtime:
        with pytest.raises(RCPError) as incomplete:
            actor_from_environment(
                runtime,
                project.project_id,
                environment={SESSION_ID_ENV: _id("session", "a")},
            )
        assert incomplete.value.code == "incomplete_actor_credential"

        with pytest.raises(RCPError) as wrong:
            actor_from_environment(
                runtime,
                project.project_id,
                environment={
                    SESSION_ID_ENV: _id("session", "a"),
                    SESSION_TOKEN_ENV: "wrong-token",
                },
            )
        assert wrong.value.code == "unauthorized_actor"


def test_bootstrapping_project_is_gated_before_runtime_creation(
    initialized_repository,
) -> None:
    project = discover_managed_project(initialized_repository)
    assert project.project.state is ProjectState.BOOTSTRAPPING
    assert not project.runtime.state_directory.exists()

    with pytest.raises(RCPError) as caught:
        open_application(initialized_repository, local_host="host-a", environment={})

    assert caught.value.code == "project_not_managed"
    assert not project.runtime.state_directory.exists()


def test_bootstrap_factory_composes_uncommitted_init_proposal_and_acceptance(
    initialized_repository,
) -> None:
    default_head = subprocess.run(
        ["git", "-C", str(initialized_repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    before_status = subprocess.run(
        [
            "git",
            "-C",
            str(initialized_repository),
            "status",
            "--porcelain=v1",
            "-z",
        ],
        check=True,
        capture_output=True,
    ).stdout

    proposal_operation_id = _id("operation", "d")
    proposal_request = BootstrapProposalRequest(
        operation_id=proposal_operation_id,
        idempotency_key="factory-bootstrap-propose",
        bootstrap_id=BOOTSTRAP_ID,
        expected_default_head=default_head,
    )
    proposal_options = {
        "local_host": "host-a",
        "environment": {},
        "bootstrap_proposal_operation_id": proposal_operation_id,
        "bootstrap_id": BOOTSTRAP_ID,
        "bootstrap_expected_default_head": default_head,
    }
    with open_application(initialized_repository, **proposal_options) as handle:
        proposed = handle.service.bootstrap_propose(proposal_request, handle.actor)
    with open_application(initialized_repository, **proposal_options) as handle:
        proposed_replay = handle.service.bootstrap_propose(
            proposal_request,
            handle.actor,
        )

    assert proposed_replay == proposed
    assert proposed.terminal_result == "proposal_prepared"
    assert proposed.data["proposal"]["proposal_only"] is True
    assert proposed.data["proposal"]["accepted"] is False
    proposal_commit = proposed.data["proposal"]["commit"]

    acceptance_operation_id = _id("operation", "e")
    acceptance_request = BootstrapAcceptRequest(
        operation_id=acceptance_operation_id,
        idempotency_key="factory-bootstrap-accept",
        bootstrap_id=BOOTSTRAP_ID,
        proposal_commit=proposal_commit,
    )
    acceptance_options = {
        "local_host": "host-a",
        "environment": {},
        "bootstrap_operation_id": acceptance_operation_id,
        "bootstrap_proposal_commit": proposal_commit,
    }
    with open_application(initialized_repository, **acceptance_options) as handle:
        accepted = handle.service.bootstrap_accept(acceptance_request, handle.actor)
    with open_application(initialized_repository, **acceptance_options) as handle:
        accepted_replay = handle.service.bootstrap_accept(
            acceptance_request,
            handle.actor,
        )

    assert accepted_replay == accepted
    assert accepted.terminal_result == "proposal_prepared"
    assert accepted.data["project_state"] == "bootstrapping"
    assert accepted.data["proposal"]["accepted"] is False
    assert accepted.data["proposal"]["requires_merge"] is True
    assert accepted.data["proposal"]["proposal_commit"] == proposal_commit
    assert subprocess.run(
        ["git", "-C", str(initialized_repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip() == default_head
    assert subprocess.run(
        [
            "git",
            "-C",
            str(initialized_repository),
            "status",
            "--porcelain=v1",
            "-z",
        ],
        check=True,
        capture_output=True,
    ).stdout == before_status

    worktrees = discover_managed_project(
        initialized_repository
    ).runtime.worktrees_directory
    proposed_path = (
        worktrees
        / f"bootstrap-{BOOTSTRAP_ID}"
        / ".research"
        / "project.yaml"
    )
    accepted_path = (
        worktrees
        / f"control-{acceptance_operation_id}"
        / ".research"
        / "project.yaml"
    )
    proposed_project = load_model(proposed_path, ProjectRecord)
    prepared = load_model(accepted_path, ProjectRecord)
    source = load_model(initialized_repository / ".research" / "project.yaml", ProjectRecord)
    assert proposed_project.state is ProjectState.BOOTSTRAPPING
    assert prepared.state is ProjectState.MANAGED
    assert source.state is ProjectState.BOOTSTRAPPING


def test_task_mutation_factory_journals_an_isolated_replayable_proposal(
    initialized_repository,
    task_payload,
) -> None:
    _mark_project_managed(initialized_repository)
    policy = ProjectPolicy(
        agent=AgentPolicy(
            accepted_paths_denied=(
                ".research/decisions/**",
                ".research/policies/**",
                ".research/project.yaml",
                ".research/impacts/**",
                ".research/reports/**",
                ".research/tasks/**",
            )
        ),
        execution_domains=(
            ExecutionDomainPolicy(
                execution_domain="on-prem",
                host_pools=("interactive",),
            ),
        ),
    )
    policy_path = initialized_repository / ".research" / "policies" / "default.yaml"
    policy_path.write_text(dump_yaml(policy), encoding="utf-8")
    subprocess.run(
        [
            "git",
            "-C",
            str(initialized_repository),
            "add",
            ".researchctl.toml",
            ".research",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(initialized_repository),
            "-c",
            "user.name=Tests",
            "-c",
            "user.email=tests@example.invalid",
            "commit",
            "--no-gpg-sign",
            "-m",
            "accept managed protocol fixture",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    operation_id = _id("operation", "c")
    task = TaskRecord.model_validate(task_payload(state="planned"))
    request = TaskCreateRequest(
        operation_id=operation_id,
        idempotency_key="factory-control-create",
        task=task,
    )
    before_head = subprocess.run(
        ["git", "-C", str(initialized_repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    before_status = subprocess.run(
        [
            "git",
            "-C",
            str(initialized_repository),
            "status",
            "--porcelain=v1",
            "-z",
        ],
        check=True,
        capture_output=True,
    ).stdout

    with open_application(
        initialized_repository,
        local_host="host-a",
        environment={},
        task_operation_id=operation_id,
        task_command="task.create",
    ) as handle:
        first = handle.service.task_create(request, handle.actor)
        operation = handle.runtime.get_operation(operation_id)
        assert operation is not None
        assert operation.events[0].kind == "operation_started"

    assert first.terminal_result == "proposal_prepared"
    assert first.data["proposal"]["effect_applied"] is True
    assert first.data["proposal"]["delivery"] == "local_control_change"
    task_path = initialized_repository / ".research" / "tasks" / f"{task.task_id}.yaml"
    assert not task_path.exists()
    assert subprocess.run(
        ["git", "-C", str(initialized_repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout == before_head
    assert subprocess.run(
        [
            "git",
            "-C",
            str(initialized_repository),
            "status",
            "--porcelain=v1",
            "-z",
        ],
        check=True,
        capture_output=True,
    ).stdout == before_status

    with open_application(
        initialized_repository,
        local_host="host-a",
        environment={},
        task_operation_id=operation_id,
        task_command="task.create",
    ) as handle:
        replayed = handle.service.task_create(request, handle.actor)

    assert replayed == first


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _prepare_factory_run(
    repository: Path,
    task_payload,
    run_spec_payload,
) -> tuple[RunStartRequest, str, Path]:
    _mark_project_managed(repository)
    policy = ProjectPolicy(
        agent=AgentPolicy(
            accepted_paths_denied=(
                ".research/decisions/**",
                ".research/policies/**",
                ".research/project.yaml",
                ".research/impacts/**",
                ".research/reports/**",
                ".research/tasks/**",
            )
        ),
        execution_domains=(
            ExecutionDomainPolicy(
                execution_domain="on-prem",
                host_pools=("interactive",),
            ),
        ),
    )
    (repository / ".research" / "policies" / "default.yaml").write_text(
        dump_yaml(policy),
        encoding="utf-8",
    )
    task = TaskRecord.model_validate(
        task_payload(
            state="ready",
            execution={
                "preferred_hosts": ["host-a"],
                "preferred_pools": ["interactive"],
                "gpu_count": 0,
            },
        )
    )
    task_path = repository / ".research" / "tasks" / f"{task.task_id}.yaml"
    task_path.write_text(dump_yaml(task), encoding="utf-8")
    program = """
import json
import sqlite3
import sys
from pathlib import Path

database_path, operation_id = sys.argv[1:3]
with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True) as database:
    state_row = database.execute(
        "SELECT state FROM operations WHERE operation_id = ?", (operation_id,)
    ).fetchone()
    event_kinds = [
        row[0]
        for row in database.execute(
            "SELECT kind FROM operation_events WHERE operation_id = ? ORDER BY sequence",
            (operation_id,),
        ).fetchall()
    ]
assert state_row == ("running",), state_row
assert event_kinds[:3] == [
    "operation_started",
    "actor_authorized",
    "run_request_validated",
], event_kinds
output = Path("results/MAR-17/factory-run.json")
output.parent.mkdir(parents=True, exist_ok=True)
launch_count = 1
if output.exists():
    launch_count += json.loads(output.read_text(encoding="utf-8"))["launch_count"]
output.write_text(
    json.dumps(
        {
            "event_kinds_seen": event_kinds,
            "launch_count": launch_count,
            "operation_state_seen": state_row[0],
            "source_variant": "frozen",
        },
        sort_keys=True,
    ),
    encoding="utf-8",
)
""".lstrip()
    (repository / "factory_experiment.py").write_text(program, encoding="utf-8")
    _git(repository, "add", ".researchctl.toml", ".research", "factory_experiment.py")
    _git(
        repository,
        "-c",
        "user.name=Tests",
        "-c",
        "user.email=tests@example.invalid",
        "commit",
        "--no-gpg-sign",
        "-m",
        "accepted managed factory run fixture",
    )
    source_commit = _git(repository, "rev-parse", "HEAD").strip()
    source_tree = _git(repository, "rev-parse", "HEAD^{tree}").strip()

    project = discover_managed_project(repository)
    project.runtime.state_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    project.runtime.worktrees_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    controlled_path = str(Path(sys.executable).resolve().parent)
    profile_path = project.runtime.state_directory / "host-profile-v1.yaml"
    profile_path.write_text(
        dump_yaml(
            {
                "version": 1,
                "host": "host-a",
                "identity_observations": [
                    {
                        "kind": "environment",
                        "logical_id": "trainer-cu128",
                        "digest": "sha256:" + "3" * 64,
                    },
                    {
                        "kind": "dataset",
                        "logical_id": "validation-split",
                        "version": "2026-08-01",
                    },
                ],
                "gpu_observations": [],
                "minimum_free_bytes": 0,
                "controlled_path": controlled_path,
            }
        ),
        encoding="utf-8",
    )
    session_id = _id("session", "e")
    token = "factory-run-session-capability"
    with RuntimeStore(project.runtime.database_path) as runtime:
        runtime.save_session(
            RuntimeSession(
                session_id=session_id,
                project_id=project.project_id,
                task_id=task.task_id,
                state=SessionState.ACTIVE,
                created_at=NOW,
                updated_at=NOW,
                host="host-a",
                actor_token_digest=hash_session_token(token),
            )
        )

    operation_id = _id("operation", "d")
    spec = RunSpec.model_validate(
        run_spec_payload(
            task_id=task.task_id,
            session_id=session_id,
            operation_id=operation_id,
            source_commit=source_commit,
            source_tree=source_tree,
            argv=[
                "python",
                "factory_experiment.py",
                str(project.runtime.database_path),
                operation_id,
            ],
            requested_host="host-a",
            environment={
                "kind": "environment",
                "logical_id": "trainer-cu128",
                "digest": "sha256:" + "3" * 64,
                "waiver_allowed": False,
            },
            inputs=[
                {
                    "kind": "dataset",
                    "logical_id": "validation-split",
                    "version": "2026-08-01",
                    "waiver_allowed": False,
                }
            ],
            resources={
                "gpu_count": 0,
                "preferred_hosts": ["host-a"],
                "preferred_pools": ["interactive"],
            },
            artifact_declarations=[
                {
                    "name": "factory-run",
                    "path": "results/MAR-17/factory-run.json",
                    "media_type": "application/json",
                    "required": True,
                }
            ],
        )
    )
    request = RunStartRequest(
        operation_id=operation_id,
        idempotency_key="factory-run-start",
        spec=spec,
        attempt_id=_id("attempt", "b"),
    )
    spec_path = repository.parent / "factory-run-spec.yaml"
    spec_path.write_text(dump_yaml(spec), encoding="utf-8")
    return request, token, spec_path


def test_factory_run_is_frozen_journaled_replayable_and_shared_by_callers(
    initialized_repository: Path,
    task_payload,
    run_spec_payload,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, token, spec_path = _prepare_factory_run(
        initialized_repository,
        task_payload,
        run_spec_payload,
    )
    environment = {
        SESSION_ID_ENV: request.spec.session_id,
        SESSION_TOKEN_ENV: token,
    }
    default_head = _git(initialized_repository, "rev-parse", "HEAD")
    (initialized_repository / "factory_experiment.py").write_text(
        "raise RuntimeError('dirty default source must not execute')\n",
        encoding="utf-8",
    )
    default_status = _git(
        initialized_repository,
        "status",
        "--porcelain=v1",
        "-z",
    )

    with open_application(
        initialized_repository,
        local_host="host-a",
        environment=environment,
        run_spec=request.spec,
    ) as handle:
        first = handle.service.run_start(request, handle.actor)
        after_first = handle.runtime.get_operation(request.operation_id)
        assert after_first is not None
        event_kinds = [event.kind for event in after_first.events]
        artifact_path = Path(
            first.data["run"]["frozen"]["execution_worktree"]
        ) / "results/MAR-17/factory-run.json"
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        repeated = handle.service.run_start(request, handle.actor)
        after_replay = handle.runtime.get_operation(request.operation_id)

    assert repeated == first
    assert first.terminal_result == "collected"
    assert first.data["run"]["execution"]["process_launched"] is True
    assert first.data["run"]["preflight"]["executable"] == str(
        Path(sys.executable).resolve()
    )
    assert artifact["source_variant"] == "frozen"
    assert artifact["launch_count"] == 1
    assert artifact["operation_state_seen"] == "running"
    assert artifact["event_kinds_seen"][:3] == [
        "operation_started",
        "actor_authorized",
        "run_request_validated",
    ]
    assert event_kinds[:3] == artifact["event_kinds_seen"][:3]
    assert event_kinds[-1] == "operation_finished"
    assert after_replay is not None
    assert after_replay.events == after_first.events
    assert json.loads(artifact_path.read_text(encoding="utf-8"))["launch_count"] == 1
    assert first.data["run"]["collection"]["changed"] is True
    assert _git(
        initialized_repository,
        "rev-list",
        "--count",
        f"{request.spec.source_commit}..research/run/{request.spec.run_id}",
    ).strip() == "2"
    result_files = list(
        Path(first.data["run"]["frozen"]["metadata_worktree"]).glob(
            ".research/runs/*/result.yaml"
        )
    )
    assert len(result_files) == 1
    assert _git(initialized_repository, "rev-parse", "HEAD") == default_head
    assert _git(
        initialized_repository,
        "status",
        "--porcelain=v1",
        "-z",
    ) == default_status

    monkeypatch.setattr(
        "researchctl.services.factory.socket.gethostname",
        lambda: "host-a.example.invalid",
    )
    monkeypatch.setenv(SESSION_ID_ENV, request.spec.session_id)
    monkeypatch.setenv(SESSION_TOKEN_ENV, token)
    runner = CliRunner()
    machine = runner.invoke(
        app,
        ["run", "start", "--json", "--project", str(initialized_repository)],
        input=json.dumps(request.model_dump(mode="json", exclude_none=True)),
    )
    assert machine.exit_code == 0, machine.output
    assert json.loads(machine.output)["data"] == first.as_dict()
    human = runner.invoke(
        app,
        [
            "run",
            "start",
            "--project",
            str(initialized_repository),
            "--spec-file",
            str(spec_path),
            "--attempt-id",
            request.attempt_id,
            "--operation-id",
            request.operation_id,
            "--idempotency-key",
            request.idempotency_key,
        ],
    )
    assert human.exit_code == 0, human.output
    assert f"collected: {request.operation_id}" in human.output
    assert f"Run: {request.spec.run_id}" in human.output
    assert f"Attempt: {request.attempt_id}" in human.output
    assert "Outcome: complete" in human.output
    assert json.loads(artifact_path.read_text(encoding="utf-8"))["launch_count"] == 1
    assert _git(initialized_repository, "rev-parse", "HEAD") == default_head
    assert _git(
        initialized_repository,
        "status",
        "--porcelain=v1",
        "-z",
    ) == default_status


def test_factory_run_requires_an_explicit_existing_host_profile(
    initialized_repository: Path,
    run_spec_payload,
) -> None:
    _mark_project_managed(initialized_repository)
    source_commit = _git(initialized_repository, "rev-parse", "HEAD").strip()
    source_tree = _git(initialized_repository, "rev-parse", "HEAD^{tree}").strip()
    spec = RunSpec.model_validate(
        run_spec_payload(
            source_commit=source_commit,
            source_tree=source_tree,
            requested_host="host-a",
        )
    )
    profile_path = (
        discover_managed_project(initialized_repository).runtime.state_directory
        / "host-profile-v1.yaml"
    )
    assert not profile_path.exists()

    with pytest.raises(RCPError) as caught:
        open_application(
            initialized_repository,
            local_host="host-a",
            environment={},
            run_spec=spec,
        )

    assert caught.value.code == "run_host_profile_missing"
    assert caught.value.context == {"path": str(profile_path)}
    assert not profile_path.exists()
