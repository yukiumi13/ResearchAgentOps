from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from researchctl.constants import PROJECT_POLICY_PATH
from researchctl.domain.enums import ProjectState
from researchctl.domain.models import PlanReviewPolicy, ProjectPolicy, ProjectRecord
from researchctl.errors import RCPError
from researchctl.serialization import dump_yaml, load_model
from researchctl.services.actor import ActorContext, ActorRole, CredentialKind
from researchctl.services.control_plan_review_policy import (
    ControlPlanReviewPolicyRepository,
)
from researchctl.services.factory import open_application
from researchctl.services.requests import PlanReviewConfigureRequest

OPERATION_ID = "operation_20260804T130000Z_" + "a" * 24
OTHER_OPERATION_ID = "operation_20260804T130001Z_" + "b" * 24


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


def _review_policy(*, model: str = "gpt-test-reviewer") -> PlanReviewPolicy:
    return PlanReviewPolicy(
        provider="codex",
        model=model,
        policy_version="plan-review-v1",
        timeout_seconds=60,
    )


def _control(
    repository: Path,
    *,
    operation_id: str,
    expected_head: str,
) -> ControlPlanReviewPolicyRepository:
    worktrees = repository / ".git" / "researchctl" / "worktrees"
    worktrees.mkdir(parents=True, exist_ok=True)
    return ControlPlanReviewPolicyRepository(
        repository_root=repository,
        worktrees_directory=worktrees,
        default_branch="main",
        operation_id=operation_id,
        expected_default_head=expected_head,
    )


def test_plan_review_policy_changes_only_project_policy_and_retry_is_stable(
    initialized_repository: Path,
) -> None:
    base = _commit_managed(initialized_repository)
    accepted = load_model(
        initialized_repository / PROJECT_POLICY_PATH,
        ProjectPolicy,
    )
    before_status = _git(initialized_repository, "status", "--porcelain=v1")
    requested = _review_policy()

    first = _control(
        initialized_repository,
        operation_id=OPERATION_ID,
        expected_head=base,
    ).configure(requested)
    repeated = _control(
        initialized_repository,
        operation_id=OPERATION_ID,
        expected_head=base,
    ).configure(requested)

    expected = accepted.model_copy(update={"plan_review": requested})
    assert first.project_policy == expected
    assert first.proposal.effect_applied is True
    assert repeated.proposal.changed is False
    assert repeated.proposal.commit == first.proposal.commit
    assert _git(initialized_repository, "rev-parse", "main").strip() == base
    assert _git(initialized_repository, "status", "--porcelain=v1") == before_status
    assert _git(
        initialized_repository,
        "show",
        "-s",
        "--format=%B",
        first.proposal.commit,
    ).rstrip("\n") == f"researchctl: plan.configure-review {OPERATION_ID}"
    assert _git(
        initialized_repository,
        "diff-tree",
        "--no-commit-id",
        "--name-only",
        "-r",
        first.proposal.commit,
    ).splitlines() == [PROJECT_POLICY_PATH]
    assert _git(
        initialized_repository,
        "show",
        f"{first.proposal.commit}:{PROJECT_POLICY_PATH}",
    ) == dump_yaml(expected)

    with pytest.raises(RCPError) as mismatch:
        _control(
            initialized_repository,
            operation_id=OPERATION_ID,
            expected_head=base,
        ).configure(_review_policy(model="different-reviewer"))
    assert mismatch.value.code == "control_plan_review_retry_mismatch"


def test_application_journals_manager_plan_review_policy_and_denies_agent(
    initialized_repository: Path,
) -> None:
    base = _commit_managed(initialized_repository)
    request = PlanReviewConfigureRequest(
        operation_id=OPERATION_ID,
        idempotency_key="plan-review-configure",
        expected_default_head=base,
        review_policy=_review_policy(),
    )
    options = {
        "local_host": "host-a",
        "environment": {},
        "plan_review_operation_id": request.operation_id,
        "plan_review_expected_default_head": base,
    }

    with open_application(initialized_repository, **options) as handle:
        first = handle.service.plan_review_configure(request, handle.actor)
    with open_application(initialized_repository, **options) as handle:
        repeated = handle.service.plan_review_configure(request, handle.actor)

    assert repeated == first
    assert first.terminal_result == "proposal_prepared"
    assert first.data["path"] == PROJECT_POLICY_PATH

    denied_request = request.model_copy(
        update={
            "operation_id": OTHER_OPERATION_ID,
            "idempotency_key": "plan-review-agent-denied",
        }
    )
    agent = ActorContext(
        actor_id="agent-session-test",
        role=ActorRole.AGENT,
        credential_kind=CredentialKind.SESSION_CAPABILITY,
        bound_session_id="session_20260804T130000Z_" + "c" * 24,
    )
    with open_application(
        initialized_repository,
        local_host="host-a",
        environment={},
        plan_review_operation_id=OTHER_OPERATION_ID,
        plan_review_expected_default_head=base,
    ) as handle, pytest.raises(RCPError) as denied:
        handle.service.plan_review_configure(denied_request, agent)
    assert denied.value.code == "authorization_denied"
