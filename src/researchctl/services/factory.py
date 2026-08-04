from __future__ import annotations

import os
import socket
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from researchctl.adapters.git_commit import GitSessionCommitVerifier
from researchctl.adapters.git_accepted_merge import GitAcceptedMergeReader
from researchctl.adapters.github_impact import GitHubImpactDelivery
from researchctl.adapters.github_impact_decision import (
    GitHubImpactDecisionDelivery,
)
from researchctl.adapters.github_submission import GitHubSubmissionDelivery
from researchctl.constants import LINEAR_PROJECTION_POLICY_PATH
from researchctl.domain.enums import ProjectState
from researchctl.domain.models import LinearProjectionPolicy, RunSpec
from researchctl.domain.types import utc_now
from researchctl.errors import RCPError
from researchctl.repository import safe_repository_path
from researchctl.runtime import RuntimeStore
from researchctl.serialization import load_model
from researchctl.services.actor import ActorContext, ActorRole, CredentialKind
from researchctl.services.application import ApplicationService
from researchctl.services.bootstrap_proposal import BootstrapProposalService
from researchctl.services.control_bootstrap import ControlBootstrapAcceptance
from researchctl.services.control_linear_policy import ControlLinearPolicyRepository
from researchctl.services.control_tasks import ControlTaskRecordRepository
from researchctl.services.impact_workflow import ImpactWorkflowService
from researchctl.services.impact_decision_workflow import (
    ImpactDecisionWorkflowService,
)
from researchctl.services.report_status import ReportStatusService
from researchctl.services.local_run import LocalRunExecutor
from researchctl.services.linear_delivery import (
    AcceptedMergeReader,
    LinearAcceptedResultDeliveryService,
)
from researchctl.services.linear_notification_ingress import (
    LinearNotificationIngressFacade,
)
from researchctl.services.linear_worker import LinearTransportWorker, LinearWorkerPort
from researchctl.services.project_runtime import (
    ManagedProject,
    ProjectRuntimeService,
)
from researchctl.services.post_merge import TrustedPostMergeService
from researchctl.services.run_execution import LocalRunCoordinator
from researchctl.services.run_profiles import LocalRunProfile
from researchctl.services.session_harness import LocalSessionHarness
from researchctl.services.submission_workflow import SubmissionWorkflowService
from researchctl.services.task_records import TaskRecordRepository


SESSION_ID_ENV = "RESEARCHCTL_SESSION_ID"
SESSION_TOKEN_ENV = "RESEARCHCTL_SESSION_TOKEN"


@dataclass(slots=True)
class ApplicationHandle:
    project: ManagedProject
    runtime: RuntimeStore
    service: ApplicationService
    actor: ActorContext

    def close(self) -> None:
        self.runtime.close()

    def __enter__(self) -> ApplicationHandle:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


@dataclass(slots=True)
class TrustedLinearApplicationHandle(ApplicationHandle):
    ingress: LinearNotificationIngressFacade


@dataclass(slots=True)
class TrustedPostMergeApplicationHandle(ApplicationHandle):
    pass


@dataclass(slots=True)
class TrustedImpactApplicationHandle(ApplicationHandle):
    pass


def actor_from_environment(
    runtime: RuntimeStore,
    project_id: str,
    *,
    environment: dict[str, str] | None = None,
) -> ActorContext:
    source = os.environ if environment is None else environment
    session_id = source.get(SESSION_ID_ENV)
    token = source.get(SESSION_TOKEN_ENV)
    if (session_id is None) != (token is None):
        raise RCPError(
            code="incomplete_actor_credential",
            message="Session ID and capability token must be supplied together.",
        )
    if session_id is not None and token is not None:
        session = runtime.authenticate_session(session_id, token)
        if session.project_id != project_id:
            raise RCPError(
                code="unauthorized_actor",
                message="Session capability belongs to a different Project.",
            )
        return ActorContext(
            actor_id=f"agent-{session.session_id}",
            role=ActorRole.AGENT,
            credential_kind=CredentialKind.SESSION_CAPABILITY,
            bound_session_id=session.session_id,
        )
    return ActorContext(
        actor_id=f"uid-{os.getuid()}",
        role=ActorRole.MANAGER,
        credential_kind=CredentialKind.LOCAL_OS,
    )


def open_application(
    path: Path = Path("."),
    *,
    local_host: str | None = None,
    environment: dict[str, str] | None = None,
    task_operation_id: str | None = None,
    task_command: str | None = None,
    bootstrap_operation_id: str | None = None,
    bootstrap_proposal_commit: str | None = None,
    bootstrap_proposal_operation_id: str | None = None,
    bootstrap_id: str | None = None,
    bootstrap_expected_default_head: str | None = None,
    linear_operation_id: str | None = None,
    linear_expected_default_head: str | None = None,
    run_spec: RunSpec | None = None,
) -> ApplicationHandle:
    if (task_operation_id is None) != (task_command is None):
        raise RCPError(
            code="task_mutation_context_incomplete",
            message="Task operation ID and command must be supplied together.",
        )
    if task_command is not None and task_command not in {
        "task.create",
        "task.update",
        "task.cancel",
    }:
        raise RCPError(
            code="task_mutation_command_invalid",
            message="Control worktrees are reserved for canonical Task mutations.",
            context={"command": task_command},
        )
    if (bootstrap_operation_id is None) != (bootstrap_proposal_commit is None):
        raise RCPError(
            code="bootstrap_mutation_context_incomplete",
            message="Bootstrap operation ID and proposal commit must be supplied together.",
        )
    proposal_context = (
        bootstrap_proposal_operation_id,
        bootstrap_id,
        bootstrap_expected_default_head,
    )
    if any(value is not None for value in proposal_context) and not all(
        value is not None for value in proposal_context
    ):
        raise RCPError(
            code="bootstrap_proposal_context_incomplete",
            message=(
                "Bootstrap proposal operation, Bootstrap ID, and default head "
                "must be supplied together."
            ),
        )
    if bootstrap_operation_id is not None and bootstrap_proposal_operation_id is not None:
        raise RCPError(
            code="mutation_context_conflict",
            message="Bootstrap propose and accept contexts are mutually exclusive.",
        )
    if (linear_operation_id is None) != (linear_expected_default_head is None):
        raise RCPError(
            code="linear_mutation_context_incomplete",
            message=(
                "Linear operation ID and expected default head must be supplied "
                "together."
            ),
        )
    if task_operation_id is not None and (
        bootstrap_operation_id is not None
        or bootstrap_proposal_operation_id is not None
    ):
        raise RCPError(
            code="mutation_context_conflict",
            message="One ApplicationService handle cannot prepare two control mutations.",
        )
    if run_spec is not None and (
        task_operation_id is not None
        or bootstrap_operation_id is not None
        or bootstrap_proposal_operation_id is not None
    ):
        raise RCPError(
            code="mutation_context_conflict",
            message="Run execution cannot share a control-mutation factory context.",
        )
    if linear_operation_id is not None and (
        task_operation_id is not None
        or bootstrap_operation_id is not None
        or bootstrap_proposal_operation_id is not None
        or run_spec is not None
    ):
        raise RCPError(
            code="mutation_context_conflict",
            message=(
                "Linear policy configuration cannot share another mutation "
                "factory context."
            ),
        )
    locator = ProjectRuntimeService()
    project = locator.discover(path)
    preparing_bootstrap = (
        bootstrap_operation_id is not None
        or bootstrap_proposal_operation_id is not None
    )
    if preparing_bootstrap:
        if project.project.state is not ProjectState.BOOTSTRAPPING:
            raise RCPError(
                code="project_state_invalid",
                message="Bootstrap acceptance requires a bootstrapping Project.",
                context={"project_state": project.project.state.value},
            )
    elif project.project.state is not ProjectState.MANAGED:
        raise RCPError(
            code="project_not_managed",
            message="This operation requires an accepted managed Project.",
            remediation="Prepare bootstrap acceptance and merge it into the default branch.",
            context={"project_state": project.project.state.value},
        )
    locator.ensure_runtime_directories(project)
    runtime = RuntimeStore(project.runtime.database_path)
    try:
        actor = actor_from_environment(
            runtime,
            project.project_id,
            environment=environment,
        )
        selected_host = local_host or socket.gethostname().split(".", maxsplit=1)[0]
        sessions = LocalSessionHarness(
            project_id=project.project_id,
            repository_root=project.repository_root,
            worktrees_directory=project.runtime.worktrees_directory,
            local_host=selected_host,
            runtime=runtime,
        )
        tasks = (
            TaskRecordRepository(project.repository_root)
            if task_operation_id is None or task_command is None
            else ControlTaskRecordRepository(
                repository_root=project.repository_root,
                worktrees_directory=project.runtime.worktrees_directory,
                default_branch=project.project.repository.default_branch,
                operation_id=task_operation_id,
                command=task_command,
            )
        )
        bootstrap_acceptance = (
            None
            if bootstrap_operation_id is None or bootstrap_proposal_commit is None
            else ControlBootstrapAcceptance(
                repository_root=project.repository_root,
                worktrees_directory=project.runtime.worktrees_directory,
                default_branch=project.project.repository.default_branch,
                operation_id=bootstrap_operation_id,
                proposal_commit=bootstrap_proposal_commit,
            )
        )
        bootstrap_proposal = (
            None
            if (
                bootstrap_proposal_operation_id is None
                or bootstrap_id is None
                or bootstrap_expected_default_head is None
            )
            else BootstrapProposalService(
                repository_root=project.repository_root,
                worktrees_directory=project.runtime.worktrees_directory,
                default_branch=project.project.repository.default_branch,
                expected_default_head=bootstrap_expected_default_head,
                operation_id=bootstrap_proposal_operation_id,
                bootstrap_id=bootstrap_id,
            )
        )
        runs = None
        if run_spec is not None:
            profile = LocalRunProfile.load(
                project.runtime.state_directory / "host-profile-v1.yaml",
                expected_host=selected_host,
            )
            runs = LocalRunCoordinator(
                repository_root=project.repository_root,
                worktrees_directory=project.runtime.worktrees_directory,
                default_branch=project.project.repository.default_branch,
                preflight=profile.build_preflight(),
                executor=LocalRunExecutor(local_host=selected_host),
            )
        linear_policy_control = (
            None
            if linear_operation_id is None or linear_expected_default_head is None
            else ControlLinearPolicyRepository(
                repository_root=project.repository_root,
                worktrees_directory=project.runtime.worktrees_directory,
                default_branch=project.project.repository.default_branch,
                operation_id=linear_operation_id,
                expected_default_head=linear_expected_default_head,
            )
        )
        service = ApplicationService(
            project_id=project.project_id,
            policy=project.policy,
            tasks=tasks,
            runtime=runtime,
            sessions=sessions,
            bootstrap_acceptance=bootstrap_acceptance,
            bootstrap_proposal=bootstrap_proposal,
            runs=runs,
            linear_policy_control=linear_policy_control,
            notification_commits=GitSessionCommitVerifier(
                project.repository_root,
            ),
            submission_workflow=SubmissionWorkflowService(
                repository_root=project.repository_root,
                worktrees_directory=project.runtime.worktrees_directory,
                default_branch=project.project.repository.default_branch,
                delivery=GitHubSubmissionDelivery(
                    accepted_remote_url=project.project.repository.remote_url,
                    environment=environment,
                ),
            ),
            impact_workflow=ImpactWorkflowService(
                repository_root=project.repository_root,
                worktrees_directory=project.runtime.worktrees_directory,
                default_branch=project.project.repository.default_branch,
                delivery=GitHubImpactDelivery(
                    accepted_remote_url=project.project.repository.remote_url,
                    environment=environment,
                ),
            ),
            report_status_reader=ReportStatusService(
                repository_root=project.repository_root,
                default_branch=project.project.repository.default_branch,
            ),
            impact_decision_workflow=ImpactDecisionWorkflowService(
                repository_root=project.repository_root,
                worktrees_directory=project.runtime.worktrees_directory,
                default_branch=project.project.repository.default_branch,
                delivery=GitHubImpactDecisionDelivery(
                    accepted_remote_url=project.project.repository.remote_url,
                    environment=environment,
                ),
            ),
        )
        return ApplicationHandle(
            project=project,
            runtime=runtime,
            service=service,
            actor=actor,
        )
    except Exception:
        runtime.close()
        raise


def open_linear_worker_application(
    path: Path = Path("."),
    *,
    accepted_merges: AcceptedMergeReader,
    remote: LinearWorkerPort,
    app_id: str,
    credential_identity: str,
    local_host: str | None = None,
    clock: Callable[[], datetime] = utc_now,
    lease_seconds: int = 300,
) -> TrustedLinearApplicationHandle:
    """Compose the Linear ingress/worker boundary for a trusted launcher only.

    This entry point intentionally has no environment or actor override. Normal
    CLI/API callers continue through ``open_application`` and cannot promote a
    manager or Session capability into trusted automation.
    """

    base = open_application(
        path,
        local_host=local_host,
        environment={},
    )
    try:
        policy_path = safe_repository_path(
            base.project.repository_root,
            LINEAR_PROJECTION_POLICY_PATH,
            managed_only=True,
        )
        if not policy_path.is_file():
            raise RCPError(
                code="linear_projection_policy_missing",
                message="Canonical Linear projection policy is missing.",
                remediation=(
                    "Add .research/policies/linear.yaml through a manager-reviewed "
                    "control change."
                ),
            )
        try:
            linear_policy = load_model(policy_path, LinearProjectionPolicy)
        except Exception as error:
            raise RCPError(
                code="linear_projection_policy_invalid",
                message="Canonical Linear projection policy is invalid.",
            ) from error
        actor = ActorContext(
            actor_id=credential_identity,
            role=ActorRole.TRUSTED_AUTOMATION,
            credential_kind=CredentialKind.AUTOMATION_CREDENTIAL,
        )
        accepted = LinearAcceptedResultDeliveryService(accepted_merges)
        worker = LinearTransportWorker(
            runtime=base.runtime,
            accepted=accepted,
            remote=remote,
            app_id=app_id,
            credential_identity=credential_identity,
            clock=clock,
            lease_seconds=lease_seconds,
        )
        base.service._bind_linear_worker(worker)
        ingress = LinearNotificationIngressFacade(
            application=base.service,
            runtime=base.runtime,
            workspace_id=linear_policy.workspace_id,
            app_id=app_id,
            notification_author_ids=linear_policy.notification_author_ids,
            credential_identity=credential_identity,
            actor=actor,
        )
        return TrustedLinearApplicationHandle(
            project=base.project,
            runtime=base.runtime,
            service=base.service,
            actor=actor,
            ingress=ingress,
        )
    except Exception:
        base.close()
        raise


def open_post_merge_application(
    path: Path = Path("."),
    *,
    automation_identity: str = "researchctl-post-merge",
    local_host: str | None = None,
) -> TrustedPostMergeApplicationHandle:
    """Compose credential-free accepted-merge validation for a trusted launcher."""

    if not automation_identity.strip():
        raise ValueError("automation_identity must be non-empty")
    base = open_application(path, local_host=local_host, environment={})
    try:
        actor = ActorContext(
            actor_id=automation_identity,
            role=ActorRole.TRUSTED_AUTOMATION,
            credential_kind=CredentialKind.AUTOMATION_CREDENTIAL,
        )
        reader = GitAcceptedMergeReader(
            repository_root=base.project.repository_root,
            expected_project_id=base.project.project_id,
            expected_default_branch=base.project.project.repository.default_branch,
        )
        post_merge = TrustedPostMergeService(
            runtime=base.runtime,
            accepted=LinearAcceptedResultDeliveryService(reader),
        )
        base.service._bind_post_merge(post_merge)
        return TrustedPostMergeApplicationHandle(
            project=base.project,
            runtime=base.runtime,
            service=base.service,
            actor=actor,
        )
    except Exception:
        base.close()
        raise


def open_impact_automation_application(
    path: Path = Path("."),
    *,
    automation_identity: str = "researchctl-impact",
    local_host: str | None = None,
) -> TrustedImpactApplicationHandle:
    """Compose the main-push Impact scanner under one trusted identity."""

    if not automation_identity.strip():
        raise ValueError("automation_identity must be non-empty")
    base = open_application(path, local_host=local_host, environment={})
    actor = ActorContext(
        actor_id=automation_identity,
        role=ActorRole.TRUSTED_AUTOMATION,
        credential_kind=CredentialKind.AUTOMATION_CREDENTIAL,
    )
    return TrustedImpactApplicationHandle(
        project=base.project,
        runtime=base.runtime,
        service=base.service,
        actor=actor,
    )
