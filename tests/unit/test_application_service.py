from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from researchctl.domain.enums import (
    ClaimScope,
    CodeDisposition,
    ImpactDisposition,
    ReviewDisposition,
    RunAttemptState,
    SessionState,
    TaskState,
)
from researchctl.domain.models import (
    AgentPolicy,
    ExecutionDomainPolicy,
    ProjectPolicy,
    ReportProposal,
    ResearchSubmission,
    RunAttemptEvent,
    RunSpec,
    StatusUpdate,
    TaskRecord,
)
from researchctl.errors import RCPError
from researchctl.runtime import RuntimeSession, RuntimeStore, hash_session_token
from researchctl.serialization import canonical_digest
from researchctl.services.actor import ActorContext, ActorRole, CredentialKind
from researchctl.services.application import ApplicationService
from researchctl.services.requests import (
    BootstrapAcceptRequest,
    BootstrapProposalRequest,
    InboxAckRequest,
    InboxListRequest,
    InboxResolveRequest,
    ImpactBatchCreateRequest,
    ImpactCreateRequest,
    ImpactDecisionCreateRequest,
    ReviewAcceptRequest,
    RunCollectRequest,
    RunRetryRequest,
    RunStartRequest,
    StatusPublishRequest,
    SubmissionCreateRequest,
    TaskCreateRequest,
    TaskUpdateRequest,
)
from researchctl.services.task_records import TaskRecordRepository


NOW = datetime(2026, 8, 2, 12, 34, 56, tzinfo=UTC)
PROJECT_ID = "project_20260802T123456Z_" + "9" * 24
BOOTSTRAP_ID = "bootstrap_20260802T123456Z_" + "8" * 24


def _id(kind: str, fill: str) -> str:
    return f"{kind}_20260802T123456Z_{fill * 24}"


def _manager() -> ActorContext:
    return ActorContext(
        actor_id="uid-1000",
        role=ActorRole.MANAGER,
        credential_kind=CredentialKind.LOCAL_OS,
    )


def _agent(session_id: str) -> ActorContext:
    return ActorContext(
        actor_id=f"agent-{session_id}",
        role=ActorRole.AGENT,
        credential_kind=CredentialKind.SESSION_CAPABILITY,
        bound_session_id=session_id,
    )


def _automation() -> ActorContext:
    return ActorContext(
        actor_id="researchctl-impact",
        role=ActorRole.TRUSTED_AUTOMATION,
        credential_kind=CredentialKind.AUTOMATION_CREDENTIAL,
    )


def _service(tmp_path: Path) -> tuple[ApplicationService, RuntimeStore]:
    (tmp_path / ".research" / "tasks").mkdir(parents=True)
    tasks = TaskRecordRepository(tmp_path)
    runtime = RuntimeStore(tmp_path / "runtime.sqlite3")
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
    service = ApplicationService(
        project_id=PROJECT_ID,
        policy=policy,
        tasks=tasks,
        runtime=runtime,
        clock=lambda: NOW,
    )
    return service, runtime


class _BootstrapReceipt:
    def __init__(self, proposal_commit: str) -> None:
        self.proposal_commit = proposal_commit

    def as_dict(self) -> dict[str, object]:
        return {
            "proposal_commit": self.proposal_commit,
            "commit": "b" * 40,
            "manifest_digest": "sha256:" + "c" * 64,
            "accepted": False,
            "requires_merge": True,
        }


class _BootstrapAcceptance:
    def __init__(self, proposal_commit: str) -> None:
        self.receipt = _BootstrapReceipt(proposal_commit)
        self.calls = 0

    def prepare(self) -> _BootstrapReceipt:
        self.calls += 1
        return self.receipt


class _BootstrapProposalReceipt:
    def __init__(self, request: BootstrapProposalRequest) -> None:
        self.request = request

    def as_dict(self) -> dict[str, object]:
        return {
            "operation_id": self.request.operation_id,
            "bootstrap_id": self.request.bootstrap_id,
            "base_commit": self.request.expected_default_head,
            "commit": "d" * 40,
            "manifest_digest": "sha256:" + "e" * 64,
            "proposal_only": True,
            "accepted": False,
        }


class _BootstrapProposal:
    def __init__(self, request: BootstrapProposalRequest) -> None:
        self.receipt = _BootstrapProposalReceipt(request)
        self.calls = 0

    def prepare(self) -> _BootstrapProposalReceipt:
        self.calls += 1
        return self.receipt


def test_bootstrap_proposal_allows_agent_but_remains_unaccepted(
    tmp_path: Path,
) -> None:
    service, runtime = _service(tmp_path)
    request = BootstrapProposalRequest(
        operation_id=_id("operation", "e"),
        idempotency_key="prepare-bootstrap-proposal",
        bootstrap_id=BOOTSTRAP_ID,
        expected_default_head="a" * 40,
    )
    proposal = _BootstrapProposal(request)
    service.bootstrap_proposal = proposal
    actor = _agent(_id("session", "e"))

    first = service.bootstrap_propose(request, actor)
    replayed = service.bootstrap_propose(request, actor)

    assert replayed == first
    assert first.terminal_result == "proposal_prepared"
    assert first.data["project_state"] == "bootstrapping"
    assert first.data["proposal"]["proposal_only"] is True
    assert first.data["proposal"]["accepted"] is False
    assert proposal.calls == 1
    operation = runtime.get_operation(request.operation_id)
    assert operation is not None
    assert operation.terminal_result == "proposal_prepared"


class _RunExecution:
    def __init__(self, terminal_result: str = "collected") -> None:
        self.terminal_result = terminal_result

    def as_dict(self) -> dict[str, object]:
        return {
            "run_id": _id("run", "c"),
            "attempt_id": _id("attempt", "b"),
            "collected": self.terminal_result == "collected",
        }


class _RunCoordinator:
    def __init__(self, *, error: RCPError | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self.error = error

    def execute(self, **values):
        self.calls.append(values)
        if self.error is not None:
            raise self.error
        values["event_callback"](
            RunAttemptEvent(
                operation_id=values["operation_id"],
                sequence=0,
                state=RunAttemptState.SUCCEEDED,
                observed_at=NOW,
                idempotency_key=f"test-run:{values['attempt_id']}:0",
                host="host-a",
            )
        )
        return _RunExecution()

    def collect(self, **values):
        self.calls.append(values)
        if self.error is not None:
            raise self.error
        return _RunExecution()


class _SubmissionWorkflowResult:
    def __init__(self, terminal_result: str, submission_id: str, commit: str) -> None:
        self.terminal_result = terminal_result
        self.submission_id = submission_id
        self.commit = commit

    def as_dict(self) -> dict[str, object]:
        return {
            "terminal_result": self.terminal_result,
            "bundle": {"submission_id": self.submission_id},
            "proposal": {"commit": self.commit},
            "delivery": {
                "branch": {
                    "branch": f"research/submission/{self.submission_id}",
                    "commit": self.commit,
                    "pushed": True,
                },
                "pull_request": {
                    "repository": "owner/project",
                    "number": 17,
                    "url": "https://github.example.invalid/owner/project/pull/17",
                    "state": "open",
                    "head_commit": self.commit,
                    "created": True,
                },
            },
        }


class _SubmissionWorkflow:
    def __init__(self) -> None:
        self.proposal_calls: list[tuple[SubmissionCreateRequest, TaskRecord]] = []
        self.acceptance_calls: list[dict[str, object]] = []

    def propose(
        self,
        request: SubmissionCreateRequest,
        task: TaskRecord,
        *,
        event_callback=None,
    ) -> _SubmissionWorkflowResult:
        self.proposal_calls.append((request, task))
        if event_callback is not None:
            event_callback(
                "submission_proposal_prepared",
                {
                    "submission_id": request.submission.submission_id,
                    "proposal_commit": "6" * 40,
                },
            )
            event_callback(
                "submission_branch_pushed",
                {
                    "submission_id": request.submission.submission_id,
                    "proposal_commit": "6" * 40,
                },
            )
            event_callback(
                "submission_pr_created",
                {
                    "submission_id": request.submission.submission_id,
                    "pull_request_number": 17,
                },
            )
        return _SubmissionWorkflowResult(
            "proposal_open",
            request.submission.submission_id,
            "6" * 40,
        )

    def prepare_acceptance(
        self,
        request: ReviewAcceptRequest,
        task: TaskRecord,
        *,
        reviewer_actor: str,
        decided_at: datetime,
    ) -> _SubmissionWorkflowResult:
        self.acceptance_calls.append(
            {
                "request": request,
                "task": task,
                "reviewer_actor": reviewer_actor,
                "decided_at": decided_at,
            }
        )
        return _SubmissionWorkflowResult(
            "acceptance_prepared",
            request.submission_id,
            "7" * 40,
        )


class _ImpactWorkflowResult:
    terminal_result = "proposal_open"

    def as_dict(self) -> dict[str, object]:
        return {
            "terminal_result": self.terminal_result,
            "bundle": {"impact_id": _id("impact", "8")},
            "proposal": {"commit": "8" * 40},
        }


class _ImpactWorkflow:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def propose(self, request, *, generated_at, event_callback=None):
        self.calls.append(
            {"request": request, "generated_at": generated_at}
        )
        if event_callback is not None:
            event_callback(
                "impact_proposal_prepared",
                {"impact_id": request.impact_id, "proposal_commit": "8" * 40},
            )
            event_callback(
                "impact_branch_pushed",
                {"impact_id": request.impact_id, "proposal_commit": "8" * 40},
            )
            event_callback(
                "impact_pr_created",
                {"impact_id": request.impact_id, "pull_request_number": 18},
            )
        return _ImpactWorkflowResult()

    def propose_batch(self, request, *, event_callback=None):
        self.calls.append({"request": request})
        if event_callback is not None:
            event_callback(
                "impact_proposal_prepared",
                {"impact_id": request.impact_id, "proposal_commit": "8" * 40},
            )
            event_callback(
                "impact_branch_pushed",
                {"impact_id": request.impact_id, "proposal_commit": "8" * 40},
            )
            event_callback(
                "impact_pr_created",
                {"impact_id": request.impact_id, "pull_request_number": 18},
            )
        return _ImpactWorkflowResult()


class _ImpactDecisionWorkflow:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def propose(
        self,
        request,
        *,
        reviewer_actor,
        decided_at,
        event_callback=None,
    ):
        self.calls.append(
            {
                "request": request,
                "reviewer_actor": reviewer_actor,
                "decided_at": decided_at,
            }
        )
        if event_callback is not None:
            event_callback(
                "impact_decision_prepared",
                {"decision_id": request.decision_id, "proposal_commit": "9" * 40},
            )
            event_callback(
                "impact_decision_branch_pushed",
                {"decision_id": request.decision_id, "proposal_commit": "9" * 40},
            )
            event_callback(
                "impact_decision_pr_created",
                {"decision_id": request.decision_id, "pull_request_number": 19},
            )
        return _ImpactWorkflowResult()

def _run_spec(run_spec_payload, task: TaskRecord, session_id: str) -> RunSpec:
    return RunSpec.model_validate(
        run_spec_payload(
            task_id=task.task_id,
            session_id=session_id,
            requested_host="host-a",
        )
    )


def test_run_start_and_retry_share_journal_scope_and_terminal_replay(
    tmp_path: Path,
    task_payload,
    run_spec_payload,
) -> None:
    service, runtime = _service(tmp_path)
    task = TaskRecord.model_validate(task_payload(state="ready"))
    service.tasks.create(task)
    session_id = _session(runtime, task)
    spec = _run_spec(run_spec_payload, task, session_id)
    coordinator = _RunCoordinator()
    service.runs = coordinator
    request = RunStartRequest(
        operation_id=spec.operation_id,
        idempotency_key="run-start",
        spec=spec,
        attempt_id=_id("attempt", "b"),
    )

    first = service.run_start(request, _agent(session_id))
    replayed = service.run_start(request, _agent(session_id))

    assert replayed == first
    assert first.terminal_result == "collected"
    assert len(coordinator.calls) == 1
    operation = runtime.get_operation(request.operation_id)
    assert operation is not None
    assert operation.terminal_result == "collected"
    assert [event.kind for event in operation.events] == [
        "operation_started",
        "actor_authorized",
        "run_request_validated",
        "run_attempt.succeeded",
        "operation_finished",
    ]

    retry = RunRetryRequest(
        operation_id=_id("operation", "e"),
        idempotency_key="run-retry",
        spec=spec,
        attempt_id=_id("attempt", "d"),
        retry_of=request.attempt_id,
    )
    retried = service.run_retry(retry, _agent(session_id))
    assert retried.terminal_result == "collected"
    assert len(coordinator.calls) == 2
    assert coordinator.calls[-1]["retry_of"] == request.attempt_id


def test_run_collect_uses_new_journal_and_replays_without_recollection(
    tmp_path: Path,
    task_payload,
    run_spec_payload,
) -> None:
    service, runtime = _service(tmp_path)
    task = TaskRecord.model_validate(task_payload(state="ready"))
    service.tasks.create(task)
    session_id = _session(runtime, task)
    spec = _run_spec(run_spec_payload, task, session_id)
    coordinator = _RunCoordinator()
    service.runs = coordinator
    request = RunCollectRequest(
        operation_id=_id("operation", "7"),
        idempotency_key="collect-failed-run",
        spec=spec,
        attempt_id=_id("attempt", "b"),
    )

    first = service.run_collect(request, _agent(session_id))
    replayed = service.run_collect(request, _agent(session_id))

    assert replayed == first
    assert first.terminal_result == "collected"
    assert len(coordinator.calls) == 1
    assert coordinator.calls[0]["operation_id"] == request.operation_id
    operation = runtime.get_operation(request.operation_id)
    assert operation is not None
    assert operation.terminal_result == "collected"
    assert [event.kind for event in operation.events] == [
        "operation_started",
        "actor_authorized",
        "run_collection_validated",
        "operation_finished",
    ]


def test_run_scope_denial_is_terminal_but_uncertain_execution_stays_running(
    tmp_path: Path,
    task_payload,
    run_spec_payload,
) -> None:
    service, runtime = _service(tmp_path)
    task = TaskRecord.model_validate(task_payload(state="ready"))
    service.tasks.create(task)
    session_id = _session(runtime, task)
    spec = _run_spec(run_spec_payload, task, session_id)
    request = RunStartRequest(
        operation_id=spec.operation_id,
        idempotency_key="uncertain-run-start",
        spec=spec,
        attempt_id=_id("attempt", "b"),
    )

    with pytest.raises(RCPError) as denied:
        service.run_start(request, _agent(_id("session", "f")))
    assert denied.value.code == "session_scope_denied"
    denied_operation = runtime.get_operation(request.operation_id)
    assert denied_operation is not None
    assert denied_operation.terminal_result == "denied"

    uncertain_spec = spec.model_copy(
        update={"operation_id": _id("operation", "f")}
    )
    uncertain_payload = uncertain_spec.model_dump(
        mode="json",
        exclude={"spec_digest"},
        exclude_none=True,
    )
    uncertain_payload["spec_digest"] = canonical_digest(uncertain_payload)
    uncertain_spec = RunSpec.model_validate(uncertain_payload)
    uncertain_request = RunStartRequest(
        operation_id=uncertain_spec.operation_id,
        idempotency_key="uncertain-run-execution",
        spec=uncertain_spec,
        attempt_id=_id("attempt", "f"),
    )
    service.runs = _RunCoordinator(
        error=RCPError(
            code="run_execution_uncertain",
            message="Process ownership is ambiguous.",
        )
    )
    with pytest.raises(RCPError) as uncertain:
        service.run_start(uncertain_request, _agent(session_id))
    assert uncertain.value.code == "run_execution_uncertain"
    uncertain_operation = runtime.get_operation(uncertain_request.operation_id)
    assert uncertain_operation is not None
    assert uncertain_operation.state == "running"
    assert uncertain_operation.terminal_result is None


def test_bootstrap_accept_is_manager_only_replayable_and_never_reports_managed(
    tmp_path: Path,
) -> None:
    service, runtime = _service(tmp_path)
    proposal_commit = "a" * 40
    bootstrap = _BootstrapAcceptance(proposal_commit)
    service.bootstrap_acceptance = bootstrap
    request = BootstrapAcceptRequest(
        operation_id=_id("operation", "0"),
        idempotency_key="accept-initial-bootstrap",
        bootstrap_id=BOOTSTRAP_ID,
        proposal_commit=proposal_commit,
    )

    first = service.bootstrap_accept(request, _manager())
    replayed = service.bootstrap_accept(request, _manager())

    assert first == replayed
    assert first.terminal_result == "proposal_prepared"
    assert first.data["project_state"] == "bootstrapping"
    assert first.data["proposal"]["accepted"] is False
    assert first.data["proposal"]["requires_merge"] is True
    assert bootstrap.calls == 1
    operation = runtime.get_operation(request.operation_id)
    assert operation is not None
    assert any(
        event.kind == "bootstrap_acceptance_prepared" for event in operation.events
    )

    denied = request.model_copy(
        update={
            "operation_id": _id("operation", "f"),
            "idempotency_key": "agent-bootstrap-denied",
        }
    )
    with pytest.raises(RCPError) as caught:
        service.bootstrap_accept(denied, _agent(_id("session", "f")))
    assert caught.value.code == "authorization_denied"
    denied_operation = runtime.get_operation(denied.operation_id)
    assert denied_operation is not None
    assert denied_operation.terminal_result == "denied"


def _session(runtime: RuntimeStore, task: TaskRecord, fill: str = "e") -> str:
    session_id = _id("session", fill)
    runtime.save_session(
        RuntimeSession(
            session_id=session_id,
            project_id=PROJECT_ID,
            task_id=task.task_id,
            state=SessionState.ACTIVE,
            created_at=NOW,
            updated_at=NOW,
            host="host-a",
            branch=f"research/task/{task.key}/{session_id}",
            worktree_path=f"/worktrees/{session_id}",
            actor_token_digest=hash_session_token(f"token-{fill}"),
        )
    )
    return session_id


def _status(
    task: TaskRecord,
    session_id: str,
    *,
    fill: str = "1",
    observed_at: datetime = NOW,
    summary: str = "Evaluating the candidate configuration.",
) -> StatusUpdate:
    return StatusUpdate(
        update_id=_id("update", fill),
        task_id=task.task_id,
        session_id=session_id,
        status="running",
        summary=summary,
        observed_at=observed_at,
    )


def _submission_create_request(
    task: TaskRecord,
    session_id: str,
    submission_payload,
) -> SubmissionCreateRequest:
    submission = ResearchSubmission.model_validate(
        submission_payload(
            task_id=task.task_id,
            session_id=session_id,
            state="open",
        )
    )
    proposal = ReportProposal(
        submission_id=submission.submission_id,
        report_id=_id("report", "9"),
        expected_report_revision=0,
        title="Stopping policy result",
        evidence_tree="a" * 40,
    )
    return SubmissionCreateRequest(
        operation_id=_id("operation", "d"),
        idempotency_key="submit-stopping-policy",
        base_commit="b" * 40,
        submission=submission,
        report_proposal=proposal,
        run_ids=(_id("run", "c"),),
    )


def test_submission_create_is_session_scoped_journaled_and_replayable(
    tmp_path: Path,
    task_payload,
    submission_payload,
) -> None:
    service, runtime = _service(tmp_path)
    task = TaskRecord.model_validate(task_payload(state="ready"))
    service.tasks.create(task)
    session_id = _session(runtime, task)
    workflow = _SubmissionWorkflow()
    service.submission_workflow = workflow
    request = _submission_create_request(task, session_id, submission_payload)

    first = service.submission_create(request, _agent(session_id))
    replayed = service.submission_create(request, _agent(session_id))

    assert replayed == first
    assert first.terminal_result == "proposal_open"
    assert first.data["submission"]["proposal"]["commit"] == "6" * 40
    assert first.data["submission"]["delivery"]["pull_request"]["number"] == 17
    assert workflow.proposal_calls == [(request, task)]
    operation = runtime.get_operation(request.operation_id)
    assert operation is not None
    assert operation.terminal_result == "proposal_open"
    assert [event.kind for event in operation.events] == [
        "operation_started",
        "actor_authorized",
        "submission_proposal_prepared",
        "submission_branch_pushed",
        "submission_pr_created",
        "operation_finished",
    ]

    denied_request = request.model_copy(
        update={
            "operation_id": _id("operation", "e"),
            "idempotency_key": "cross-session-submission",
        }
    )
    wrong_agent = _agent(_id("session", "f"))
    for _ in range(2):
        with pytest.raises(RCPError) as denied:
            service.submission_create(denied_request, wrong_agent)
        assert denied.value.code == "session_scope_denied"

    assert workflow.proposal_calls == [(request, task)]
    denied_operation = runtime.get_operation(denied_request.operation_id)
    assert denied_operation is not None
    assert denied_operation.terminal_result == "denied"
    assert [event.kind for event in denied_operation.events].count(
        "authorization_denied"
    ) == 1


def test_impact_is_manager_owned_journaled_and_denies_agent_before_workflow(
    tmp_path: Path,
) -> None:
    service, runtime = _service(tmp_path)
    workflow = _ImpactWorkflow()
    service.impact_workflow = workflow
    request = ImpactCreateRequest(
        operation_id=_id("operation", "8"),
        idempotency_key="impact-evaluator-fix",
        impact_id=_id("impact", "8"),
        report_id=_id("report", "9"),
        expected_report_revision=1,
        target_commit="8" * 40,
    )

    first = service.impact_create(request, _manager())
    replayed = service.impact_create(request, _manager())

    assert replayed == first
    assert first.terminal_result == "proposal_open"
    assert len(workflow.calls) == 1
    operation = runtime.get_operation(request.operation_id)
    assert operation is not None
    assert [event.kind for event in operation.events] == [
        "operation_started",
        "actor_authorized",
        "impact_proposal_prepared",
        "impact_branch_pushed",
        "impact_pr_created",
        "operation_finished",
    ]

    denied = request.model_copy(
        update={
            "operation_id": _id("operation", "5"),
            "idempotency_key": "agent-batch-impact-denied",
        }
    )
    with pytest.raises(RCPError) as raised:
        service.impact_batch_create(denied, _agent(_id("session", "5")))

    assert raised.value.code == "authorization_denied"
    assert len(workflow.calls) == 1
    assert workflow.calls[0]["generated_at"] == NOW
    operation = runtime.get_operation(request.operation_id)
    assert operation is not None
    assert [event.kind for event in operation.events] == [
        "operation_started",
        "actor_authorized",
        "impact_proposal_prepared",
        "impact_branch_pushed",
        "impact_pr_created",
        "operation_finished",
    ]

    denied = request.model_copy(
        update={
            "operation_id": _id("operation", "7"),
            "idempotency_key": "agent-impact-denied",
        }
    )
    with pytest.raises(RCPError) as raised:
        service.impact_create(denied, _agent(_id("session", "7")))

    assert raised.value.code == "authorization_denied"
    assert len(workflow.calls) == 1
    denied_operation = runtime.get_operation(denied.operation_id)
    assert denied_operation is not None
    assert denied_operation.terminal_result == "denied"


def test_impact_batch_is_trusted_automation_owned_and_replayable(
    tmp_path: Path,
) -> None:
    service, runtime = _service(tmp_path)
    workflow = _ImpactWorkflow()
    service.impact_workflow = workflow
    request = ImpactBatchCreateRequest(
        operation_id=_id("operation", "6"),
        idempotency_key="main-push-impact",
        impact_id=_id("impact", "6"),
        before_commit="6" * 40,
        target_commit="7" * 40,
        generated_at=NOW,
    )

    first = service.impact_batch_create(request, _automation())
    replayed = service.impact_batch_create(request, _automation())

    assert replayed == first
    assert first.terminal_result == "proposal_open"
    assert len(workflow.calls) == 1


def test_impact_decision_is_manager_only_journaled_and_never_runs_work(
    tmp_path: Path,
) -> None:
    service, runtime = _service(tmp_path)
    workflow = _ImpactDecisionWorkflow()
    service.impact_decision_workflow = workflow
    request = ImpactDecisionCreateRequest(
        operation_id=_id("operation", "3"),
        idempotency_key="keep-impact-stale",
        decision_id=_id("decision", "3"),
        impact_id=_id("impact", "3"),
        report_id=_id("report", "3"),
        expected_report_revision=2,
        expected_impact_digest="sha256:" + "3" * 64,
        target_commit="3" * 40,
        disposition=ImpactDisposition.KEEP_STALE,
        reason="Awaiting a reviewed rerun plan.",
    )

    first = service.impact_decide(request, _manager())
    replayed = service.impact_decide(request, _manager())

    assert replayed == first
    assert first.terminal_result == "proposal_open"
    assert len(workflow.calls) == 1
    assert workflow.calls[0]["reviewer_actor"] == "uid-1000"
    operation = runtime.get_operation(request.operation_id)
    assert operation is not None
    assert [event.kind for event in operation.events] == [
        "operation_started",
        "actor_authorized",
        "impact_decision_prepared",
        "impact_decision_branch_pushed",
        "impact_decision_pr_created",
        "operation_finished",
    ]

    denied = request.model_copy(
        update={
            "operation_id": _id("operation", "2"),
            "idempotency_key": "agent-impact-decision-denied",
        }
    )
    with pytest.raises(RCPError) as raised:
        service.impact_decide(denied, _agent(_id("session", "2")))

    assert raised.value.code == "authorization_denied"
    assert len(workflow.calls) == 1


def test_delivery_workflow_event_namespace_fails_closed(
    tmp_path: Path,
) -> None:
    service, runtime = _service(tmp_path)

    class _InvalidImpactWorkflow(_ImpactWorkflow):
        def propose_batch(self, request, *, event_callback=None):
            assert event_callback is not None
            event_callback("caller_selected_event", {"impact_id": request.impact_id})
            raise AssertionError("unsupported event must stop the workflow")

    service.impact_workflow = _InvalidImpactWorkflow()
    request = ImpactBatchCreateRequest(
        operation_id=_id("operation", "4"),
        idempotency_key="invalid-impact-event",
        impact_id=_id("impact", "4"),
        before_commit="4" * 40,
        target_commit="5" * 40,
        generated_at=NOW,
    )

    with pytest.raises(RCPError) as raised:
        service.impact_batch_create(request, _automation())

    assert raised.value.code == "workflow_event_invalid"
    operation = runtime.get_operation(request.operation_id)
    assert operation is not None
    assert operation.terminal_result == "failed"
    assert [event.kind for event in operation.events] == [
        "operation_started",
        "actor_authorized",
        "operation_failed",
        "operation_finished",
    ]


def test_submission_delivery_uncertainty_keeps_operation_running_and_recovers(
    tmp_path: Path,
    task_payload,
    submission_payload,
) -> None:
    service, runtime = _service(tmp_path)
    task = TaskRecord.model_validate(task_payload(state="ready"))
    service.tasks.create(task)
    session_id = _session(runtime, task)
    request = _submission_create_request(task, session_id, submission_payload)

    class _RecoveringWorkflow(_SubmissionWorkflow):
        def propose(self, request, task, *, event_callback=None):
            self.proposal_calls.append((request, task))
            assert event_callback is not None
            event_callback(
                "submission_proposal_prepared",
                {
                    "submission_id": request.submission.submission_id,
                    "proposal_commit": "6" * 40,
                },
            )
            event_callback(
                "submission_branch_pushed",
                {
                    "submission_id": request.submission.submission_id,
                    "proposal_commit": "6" * 40,
                },
            )
            if len(self.proposal_calls) == 1:
                raise RCPError(
                    code="submission_delivery_uncertain",
                    message="PR create outcome is uncertain.",
                    context={"stage": "pull_request_create"},
                )
            event_callback(
                "submission_pr_observed",
                {
                    "submission_id": request.submission.submission_id,
                    "pull_request_number": 17,
                },
            )
            return _SubmissionWorkflowResult(
                "proposal_open",
                request.submission.submission_id,
                "6" * 40,
            )

    workflow = _RecoveringWorkflow()
    service.submission_workflow = workflow

    with pytest.raises(RCPError) as uncertain:
        service.submission_create(request, _agent(session_id))

    assert uncertain.value.code == "submission_delivery_uncertain"
    running = runtime.get_operation(request.operation_id)
    assert running is not None
    assert running.state == "running"
    assert [event.kind for event in running.events] == [
        "operation_started",
        "actor_authorized",
        "submission_proposal_prepared",
        "submission_branch_pushed",
    ]

    recovered = service.submission_create(request, _agent(session_id))

    assert recovered.terminal_result == "proposal_open"
    assert len(workflow.proposal_calls) == 2
    terminal = runtime.get_operation(request.operation_id)
    assert terminal is not None
    assert [event.kind for event in terminal.events] == [
        "operation_started",
        "actor_authorized",
        "submission_proposal_prepared",
        "submission_branch_pushed",
        "submission_pr_observed",
        "operation_finished",
    ]


def test_review_accept_is_manager_only_journaled_and_replayable(
    tmp_path: Path,
    task_payload,
) -> None:
    service, runtime = _service(tmp_path)
    task = TaskRecord.model_validate(task_payload(state="needs_review"))
    service.tasks.create(task)
    workflow = _SubmissionWorkflow()
    service.submission_workflow = workflow
    request = ReviewAcceptRequest(
        operation_id=_id("operation", "6"),
        idempotency_key="accept-stopping-policy",
        submission_id=_id("submission", "f"),
        task_id=task.task_id,
        expected_head="6" * 40,
        decision_id=_id("decision", "7"),
        expected_report_revision=0,
        disposition=ReviewDisposition.ACCEPTED,
        claim_scope=ClaimScope.SNAPSHOT,
        code_disposition=CodeDisposition.RETAIN_ISOLATED,
    )

    first = service.review_accept(request, _manager())
    replayed = service.review_accept(request, _manager())

    assert replayed == first
    assert first.terminal_result == "acceptance_prepared"
    assert first.data["review"]["proposal"]["commit"] == "7" * 40
    assert workflow.acceptance_calls == [
        {
            "request": request,
            "task": task,
            "reviewer_actor": "uid-1000",
            "decided_at": NOW,
        }
    ]
    operation = runtime.get_operation(request.operation_id)
    assert operation is not None
    assert operation.terminal_result == "acceptance_prepared"
    assert [event.kind for event in operation.events] == [
        "operation_started",
        "actor_authorized",
        "review_acceptance_prepared",
        "operation_finished",
    ]

    denied_request = request.model_copy(
        update={
            "operation_id": _id("operation", "8"),
            "idempotency_key": "agent-cannot-accept",
        }
    )
    agent = _agent(_id("session", "e"))
    for _ in range(2):
        with pytest.raises(RCPError) as denied:
            service.review_accept(denied_request, agent)
        assert denied.value.code == "authorization_denied"

    assert len(workflow.acceptance_calls) == 1
    denied_operation = runtime.get_operation(denied_request.operation_id)
    assert denied_operation is not None
    assert denied_operation.terminal_result == "denied"
    assert [event.kind for event in denied_operation.events].count(
        "authorization_denied"
    ) == 1


def test_manager_task_create_is_idempotent_and_agent_denial_is_audited(
    tmp_path: Path,
    task_payload,
) -> None:
    service, runtime = _service(tmp_path)
    task = TaskRecord.model_validate(task_payload(state="planned"))
    request = TaskCreateRequest(
        operation_id=_id("operation", "1"),
        idempotency_key="create-mar-17",
        task=task,
    )

    first = service.task_create(request, _manager())
    repeated = service.task_create(request, _manager())

    assert first == repeated
    assert first.terminal_result == "created"
    assert first.data["changed"] is True
    assert service.tasks.list() == (task,)
    operation = runtime.get_operation(request.operation_id)
    assert operation is not None and operation.state == "terminal"
    assert [event.kind for event in operation.events].count("operation_started") == 1

    denied_task = TaskRecord.model_validate(
        task_payload(
            task_id=_id("task", "2"),
            key="DENIED-2",
            state="planned",
        )
    )
    denied_request = TaskCreateRequest(
        operation_id=_id("operation", "2"),
        idempotency_key="denied-create",
        task=denied_task,
    )
    with pytest.raises(RCPError) as denied:
        service.task_create(denied_request, _agent(_id("session", "2")))

    assert denied.value.code == "authorization_denied"
    assert not service.tasks.path_for(denied_task.task_id).exists()
    denied_operation = runtime.get_operation(denied_request.operation_id)
    assert denied_operation is not None
    assert denied_operation.terminal_result == "denied"
    assert any(event.kind == "authorization_denied" for event in denied_operation.events)


def test_task_update_recovers_after_record_write_without_duplicate_effect(
    tmp_path: Path,
    task_payload,
) -> None:
    service, runtime = _service(tmp_path)
    current = TaskRecord.model_validate(task_payload(state="planned"))
    service.tasks.create(current)
    replacement = current.model_copy(
        update={
            "state": TaskState.READY,
            "updated_at": current.updated_at + timedelta(seconds=1),
        }
    )
    request = TaskUpdateRequest(
        operation_id=_id("operation", "3"),
        idempotency_key="ready-mar-17",
        task_id=current.task_id,
        expected_digest=canonical_digest(current),
        replacement=replacement,
    )
    actor = _manager()
    request_digest = canonical_digest(
        {
            "request": request.model_dump(mode="json", exclude_none=True),
            "actor": actor.model_dump(mode="json", exclude_none=True),
        }
    )
    runtime.begin_operation(
        PROJECT_ID,
        "task.update",
        request.idempotency_key,
        request_digest,
        request.operation_id,
        NOW,
    )
    service.tasks.replace(current.task_id, canonical_digest(current), replacement)

    result = service.task_update(request, actor)

    assert result.terminal_result == "no_change"
    assert service.tasks.load(current.task_id) == replacement
    assert len(service.tasks.list()) == 1


def test_agent_status_is_session_scoped_and_published_atomically(
    tmp_path: Path,
    task_payload,
) -> None:
    service, runtime = _service(tmp_path)
    task = TaskRecord.model_validate(task_payload())
    service.tasks.create(task)
    session_id = _session(runtime, task)
    update = _status(task, session_id)
    request = StatusPublishRequest(
        operation_id=_id("operation", "4"),
        idempotency_key="status-running-1",
        update=update,
    )

    result = service.status_publish(request, _agent(session_id))

    assert result.terminal_result == "persisted"
    assert runtime.get_status_update(update.update_id) == update
    assert len(runtime.list_outbox(PROJECT_ID)) == 1
    assert len(runtime.list_inbox(PROJECT_ID)) == 1

    other_session = _id("session", "f")
    denied_request = StatusPublishRequest(
        operation_id=_id("operation", "5"),
        idempotency_key="cross-session-status",
        update=_status(task, session_id, fill="2"),
    )
    with pytest.raises(RCPError) as denied:
        service.status_publish(denied_request, _agent(other_session))

    assert denied.value.code == "session_scope_denied"
    assert runtime.get_status_update(denied_request.update.update_id) is None


def test_inbox_actions_use_visible_generation_and_never_mutate_task(
    tmp_path: Path,
    task_payload,
) -> None:
    service, runtime = _service(tmp_path)
    task = TaskRecord.model_validate(task_payload())
    service.tasks.create(task)
    task_bytes = service.tasks.path_for(task.task_id).read_bytes()
    session_id = _session(runtime, task)
    agent = _agent(session_id)
    first = _status(task, session_id)
    service.status_publish(
        StatusPublishRequest(
            operation_id=_id("operation", "6"),
            idempotency_key="first-running",
            update=first,
        ),
        agent,
    )
    visible = service.inbox_list(InboxListRequest(), _manager())
    assert len(visible) == 1 and visible[0].generation == 1

    service.inbox_ack(
        InboxAckRequest(
            operation_id=_id("operation", "7"),
            idempotency_key="ack-first",
            update_id=first.update_id,
            expected_generation=1,
        ),
        _manager(),
    )
    second = _status(
        task,
        session_id,
        fill="2",
        observed_at=NOW + timedelta(seconds=1),
        summary="Evaluation changed after a new input was resolved.",
    )
    service.status_publish(
        StatusPublishRequest(
            operation_id=_id("operation", "8"),
            idempotency_key="second-running",
            update=second,
        ),
        agent,
    )
    reopened = service.inbox_list(InboxListRequest(), _manager())
    assert len(reopened) == 1
    assert reopened[0].state == "open"
    assert reopened[0].generation == 2

    with pytest.raises(RCPError) as stale:
        service.inbox_ack(
            InboxAckRequest(
                operation_id=_id("operation", "9"),
                idempotency_key="stale-ack",
                update_id=first.update_id,
                expected_generation=1,
            ),
            _manager(),
        )
    assert stale.value.code == "stale_attention"

    service.inbox_resolve(
        InboxResolveRequest(
            operation_id=_id("operation", "a"),
            idempotency_key="resolve-second",
            update_id=second.update_id,
            expected_generation=2,
            reason="Manager recorded the observation in the active review.",
        ),
        _manager(),
    )

    assert service.inbox_list(InboxListRequest(), _manager()) == ()
    all_items = service.inbox_list(
        InboxListRequest(include_resolved=True),
        _manager(),
    )
    assert len(all_items) == 1 and all_items[0].state == "resolved"
    assert len(runtime.list_status_updates(PROJECT_ID)) == 2
    assert service.tasks.path_for(task.task_id).read_bytes() == task_bytes


def test_deterministic_domain_failure_is_terminal_and_replayed(
    tmp_path: Path,
    task_payload,
) -> None:
    service, runtime = _service(tmp_path)
    task = TaskRecord.model_validate(task_payload(state="ready"))
    request = TaskCreateRequest(
        operation_id=_id("operation", "b"),
        idempotency_key="invalid-initial-state",
        task=task,
    )

    for _ in range(2):
        with pytest.raises(RCPError) as caught:
            service.task_create(request, _manager())
        assert caught.value.code == "invalid_task_initial_state"

    operation = runtime.get_operation(request.operation_id)
    assert operation is not None
    assert operation.state == "terminal"
    assert operation.terminal_result == "failed"
    assert [event.kind for event in operation.events].count("operation_failed") == 1


def test_conflicting_retry_cannot_fail_the_original_running_operation(
    tmp_path: Path,
    task_payload,
) -> None:
    service, runtime = _service(tmp_path)
    actor = _manager()
    operation_id = _id("operation", "c")
    original = TaskCreateRequest(
        operation_id=operation_id,
        idempotency_key="claimed-create",
        task=TaskRecord.model_validate(task_payload(state="planned")),
    )
    runtime.begin_operation(
        PROJECT_ID,
        "task.create",
        original.idempotency_key,
        canonical_digest(
            {
                "request": original.model_dump(mode="json", exclude_none=True),
                "actor": actor.model_dump(mode="json", exclude_none=True),
            }
        ),
        operation_id,
        NOW,
    )
    conflict = original.model_copy(
        update={
            "task": TaskRecord.model_validate(
                task_payload(
                    task_id=_id("task", "c"),
                    key="OTHER-1",
                    state="planned",
                )
            )
        }
    )

    with pytest.raises(RCPError) as caught:
        service.task_create(conflict, actor)

    assert caught.value.code == "idempotency_conflict"
    operation = runtime.get_operation(operation_id)
    assert operation is not None and operation.state == "running"
    assert not any(event.kind == "operation_failed" for event in operation.events)
