from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from researchctl.constants import LINEAR_PROJECTION_POLICY_PATH
from researchctl.domain.enums import ProjectState
from researchctl.domain.models import LinearProjectionPolicy, ProjectRecord
from researchctl.errors import RCPError
from researchctl.serialization import dump_yaml, load_model
from researchctl.services.actor import ActorContext, ActorRole, CredentialKind
from researchctl.services.control_linear_policy import ControlLinearPolicyRepository
from researchctl.services.factory import open_application
from researchctl.services.requests import LinearConfigureRequest

OPERATION_ID = "operation_20260803T130000Z_" + "a" * 24
OTHER_OPERATION_ID = "operation_20260803T130001Z_" + "b" * 24


def _git(repository: Path, *arguments: str) -> str:
    environment = os.environ.copy()
    for key in tuple(environment):
        if key.startswith("GIT_CONFIG_") or key in {
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            "GIT_COMMON_DIR",
            "GIT_DIR",
            "GIT_INDEX_FILE",
            "GIT_OBJECT_DIRECTORY",
            "GIT_WORK_TREE",
        }:
            environment.pop(key, None)
    environment.pop("GIT_CONFIG_PARAMETERS", None)
    return subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        env=environment,
        shell=False,
        text=True,
    ).stdout


def _commit_managed(repository: Path) -> str:
    project_path = repository / ".research" / "project.yaml"
    project = load_model(project_path, ProjectRecord)
    project_path.write_text(
        dump_yaml(project.model_copy(update={"state": ProjectState.MANAGED})),
        encoding="utf-8",
    )
    _git(repository, "add", ".researchctl.toml", ".research")
    _git(
        repository,
        "-c",
        "user.name=Tests",
        "-c",
        "user.email=tests@example.invalid",
        "commit",
        "--no-gpg-sign",
        "-m",
        "accept managed fixture",
    )
    return _git(repository, "rev-parse", "HEAD").strip()


def _policy(*, project_id: str | None = None) -> LinearProjectionPolicy:
    return LinearProjectionPolicy(
        workspace_id="11111111-1111-4111-8111-111111111111",
        team_id="22222222-2222-4222-8222-222222222222",
        project_id=project_id,
        notification_author_ids=(
            "33333333-3333-4333-8333-333333333333",
        ),
    )


def _control(
    repository: Path,
    *,
    operation_id: str,
    expected_head: str,
) -> ControlLinearPolicyRepository:
    worktrees = repository / ".git" / "researchctl" / "worktrees"
    worktrees.mkdir(parents=True, exist_ok=True)
    return ControlLinearPolicyRepository(
        repository_root=repository,
        worktrees_directory=worktrees,
        default_branch="main",
        operation_id=operation_id,
        expected_default_head=expected_head,
    )


def test_linear_policy_create_is_one_local_commit_and_retry_is_stable(
    initialized_repository: Path,
) -> None:
    base = _commit_managed(initialized_repository)
    before_status = _git(initialized_repository, "status", "--porcelain=v1")
    policy = _policy()

    first = _control(
        initialized_repository,
        operation_id=OPERATION_ID,
        expected_head=base,
    ).configure(policy)
    repeated = _control(
        initialized_repository,
        operation_id=OPERATION_ID,
        expected_head=base,
    ).configure(policy)

    assert first.previous_digest is None
    assert first.changed is True
    assert first.proposal.changed is True
    assert first.proposal.effect_applied is True
    assert repeated.changed is False
    assert repeated.proposal.changed is False
    assert repeated.proposal.effect_applied is True
    assert repeated.proposal.commit == first.proposal.commit
    assert _git(initialized_repository, "rev-parse", "main").strip() == base
    assert _git(initialized_repository, "status", "--porcelain=v1") == before_status
    assert _git(
        initialized_repository,
        "rev-list",
        "--parents",
        "-n",
        "1",
        first.proposal.commit,
    ).split() == [first.proposal.commit, base]
    assert _git(
        initialized_repository,
        "show",
        "-s",
        "--format=%B",
        first.proposal.commit,
    ).rstrip("\n") == f"researchctl: linear.configure {OPERATION_ID}"
    assert _git(
        initialized_repository,
        "diff-tree",
        "--no-commit-id",
        "--name-only",
        "-r",
        first.proposal.commit,
    ).splitlines() == [LINEAR_PROJECTION_POLICY_PATH]
    assert _git(
        initialized_repository,
        "show",
        f"{first.proposal.commit}:{LINEAR_PROJECTION_POLICY_PATH}",
    ) == dump_yaml(policy)


def test_linear_policy_same_bytes_is_no_effect_and_update_is_canonical(
    initialized_repository: Path,
) -> None:
    original = _policy()
    path = initialized_repository / LINEAR_PROJECTION_POLICY_PATH
    path.write_text(dump_yaml(original), encoding="utf-8")
    base = _commit_managed(initialized_repository)

    unchanged = _control(
        initialized_repository,
        operation_id=OPERATION_ID,
        expected_head=base,
    ).configure(original)
    replacement = _policy(project_id="44444444-4444-4444-8444-444444444444")
    updated = _control(
        initialized_repository,
        operation_id=OTHER_OPERATION_ID,
        expected_head=base,
    ).configure(replacement)

    assert unchanged.changed is False
    assert unchanged.proposal.effect_applied is False
    assert unchanged.proposal.commit == base
    assert updated.previous_digest == unchanged.digest
    assert updated.changed is True
    assert updated.proposal.effect_applied is True
    assert _git(
        initialized_repository,
        "show",
        f"{updated.proposal.commit}:{LINEAR_PROJECTION_POLICY_PATH}",
    ) == dump_yaml(replacement)


def test_linear_policy_rejects_stale_default_and_extra_dirty_path(
    initialized_repository: Path,
) -> None:
    base = _commit_managed(initialized_repository)
    source = initialized_repository / "source.txt"
    source.write_text("new default\n", encoding="utf-8")
    _git(initialized_repository, "add", "source.txt")
    _git(
        initialized_repository,
        "-c",
        "user.name=Tests",
        "-c",
        "user.email=tests@example.invalid",
        "commit",
        "--no-gpg-sign",
        "-m",
        "advance default",
    )
    stale = _control(
        initialized_repository,
        operation_id=OPERATION_ID,
        expected_head=base,
    )
    with pytest.raises(RCPError) as raised:
        stale.configure(_policy())
    assert raised.value.code == "control_linear_default_head_changed"
    assert not stale.worktree.exists()

    current = _git(initialized_repository, "rev-parse", "main").strip()
    control = _control(
        initialized_repository,
        operation_id=OTHER_OPERATION_ID,
        expected_head=current,
    )
    control.configure(_policy())
    extra = control.worktree / "unexpected.txt"
    extra.write_text("dirty\n", encoding="utf-8")
    with pytest.raises(RCPError) as dirty:
        control.configure(_policy())
    assert dirty.value.code == "control_worktree_dirty"


def test_application_factory_journals_manager_linear_proposal_and_denies_agent(
    initialized_repository: Path,
) -> None:
    base = _commit_managed(initialized_repository)
    request = LinearConfigureRequest(
        operation_id=OPERATION_ID,
        idempotency_key="linear-factory-configure",
        expected_default_head=base,
        policy=_policy(),
    )
    options = {
        "local_host": "host-a",
        "environment": {},
        "linear_operation_id": request.operation_id,
        "linear_expected_default_head": base,
    }

    with open_application(initialized_repository, **options) as handle:
        first = handle.service.linear_configure(request, handle.actor)
    with open_application(initialized_repository, **options) as handle:
        replayed = handle.service.linear_configure(request, handle.actor)

    assert replayed == first
    assert first.terminal_result == "proposal_prepared"
    assert first.data["proposal"]["effect_applied"] is True
    assert first.data["path"] == LINEAR_PROJECTION_POLICY_PATH

    denied_operation = OTHER_OPERATION_ID
    denied_request = request.model_copy(
        update={
            "operation_id": denied_operation,
            "idempotency_key": "linear-agent-denied",
        }
    )
    agent = ActorContext(
        actor_id="agent-session-test",
        role=ActorRole.AGENT,
        credential_kind=CredentialKind.SESSION_CAPABILITY,
        bound_session_id="session_20260803T130000Z_" + "c" * 24,
    )
    with open_application(
        initialized_repository,
        local_host="host-a",
        environment={},
        linear_operation_id=denied_operation,
        linear_expected_default_head=base,
    ) as handle, pytest.raises(RCPError) as denied:
        handle.service.linear_configure(denied_request, agent)
    assert denied.value.code == "authorization_denied"
    assert f"research/control/{denied_operation}" not in _git(
        initialized_repository,
        "for-each-ref",
        "--format=%(refname:short)",
        "refs/heads/research/control",
    ).splitlines()


def test_factory_requires_complete_exclusive_linear_mutation_context(
    initialized_repository: Path,
) -> None:
    _commit_managed(initialized_repository)
    with pytest.raises(RCPError) as incomplete:
        open_application(
            initialized_repository,
            linear_operation_id=OPERATION_ID,
        )
    assert incomplete.value.code == "linear_mutation_context_incomplete"

    with pytest.raises(RCPError) as conflict:
        open_application(
            initialized_repository,
            task_operation_id=OTHER_OPERATION_ID,
            task_command="task.create",
            linear_operation_id=OPERATION_ID,
            linear_expected_default_head="a" * 40,
        )
    assert conflict.value.code == "mutation_context_conflict"
