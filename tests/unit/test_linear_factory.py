from __future__ import annotations

import inspect
from typing import cast

import pytest

from researchctl.domain.enums import ProjectState
from researchctl.domain.models import (
    LinearProjectionPolicy,
    ProjectRecord,
)
from researchctl.errors import RCPError
from researchctl.serialization import dump_yaml, load_model
from researchctl.services.actor import ActorRole
from researchctl.services.factory import (
    open_application,
    open_linear_worker_application,
)
from researchctl.services.linear_delivery import AcceptedMergeReader
from researchctl.services.linear_worker import LinearWorkerPort

WORKSPACE_ID = "11111111-1111-4111-8111-111111111111"
TEAM_ID = "22222222-2222-4222-8222-222222222222"
AUTHOR_ID = "33333333-3333-4333-8333-333333333333"


def _mark_managed(repository) -> None:
    project_path = repository / ".research" / "project.yaml"
    project = load_model(project_path, ProjectRecord)
    project_path.write_text(
        dump_yaml(project.model_copy(update={"state": ProjectState.MANAGED})),
        encoding="utf-8",
    )


def _write_linear_policy(repository) -> None:
    policy = LinearProjectionPolicy(
        workspace_id=WORKSPACE_ID,
        team_id=TEAM_ID,
        notification_author_ids=(AUTHOR_ID,),
    )
    (repository / ".research" / "policies" / "linear.yaml").write_text(
        dump_yaml(policy),
        encoding="utf-8",
    )


def test_trusted_linear_factory_has_no_actor_environment_or_policy_override() -> None:
    parameters = inspect.signature(open_linear_worker_application).parameters

    assert "environment" not in parameters
    assert "actor" not in parameters
    assert "role" not in parameters
    assert "linear_policy" not in parameters


def test_trusted_linear_factory_loads_policy_and_binds_one_shared_service(
    initialized_repository,
) -> None:
    _mark_managed(initialized_repository)
    _write_linear_policy(initialized_repository)

    with open_linear_worker_application(
        initialized_repository,
        accepted_merges=cast(AcceptedMergeReader, object()),
        remote=cast(LinearWorkerPort, object()),
        app_id="researchctl-linear-app",
        credential_identity="researchctl-app",
        local_host="host-a",
    ) as handle:
        assert handle.actor.role is ActorRole.TRUSTED_AUTOMATION
        assert handle.actor.actor_id == "researchctl-app"
        assert handle.service.runtime is handle.runtime
        assert handle.ingress._runtime is handle.runtime
        assert handle.ingress._notification_author_ids == frozenset({AUTHOR_ID})
        assert not hasattr(handle, "worker")
        idle = handle.service.linear_delivery_run_once(
            claim_id="factory-idle-claim",
            actor=handle.actor,
        )
        assert idle.state == "idle"


def test_trusted_linear_factory_fails_closed_without_canonical_policy(
    initialized_repository,
) -> None:
    _mark_managed(initialized_repository)

    with pytest.raises(RCPError) as missing:
        open_linear_worker_application(
            initialized_repository,
            accepted_merges=cast(AcceptedMergeReader, object()),
            remote=cast(LinearWorkerPort, object()),
            app_id="researchctl-linear-app",
            credential_identity="researchctl-app",
            local_host="host-a",
        )

    assert missing.value.code == "linear_projection_policy_missing"


def test_normal_factory_ignores_automation_shaped_environment(
    initialized_repository,
) -> None:
    _mark_managed(initialized_repository)
    environment = {
        "RESEARCHCTL_ROLE": "trusted_automation",
        "RESEARCHCTL_LINEAR_CREDENTIAL_IDENTITY": "researchctl-app",
    }

    with open_application(
        initialized_repository,
        local_host="host-a",
        environment=environment,
    ) as handle:
        assert handle.actor.role is ActorRole.MANAGER
