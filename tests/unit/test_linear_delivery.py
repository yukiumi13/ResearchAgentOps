from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime

import pytest

from researchctl.domain.enums import ClaimScope, CodeDisposition, ReviewDisposition
from researchctl.domain.models import (
    CIValidationAttestation,
    CIValidationCheck,
    GeneratedOutputDigest,
    LinearProjectionDisabled,
    LinearProjectionPolicy,
    ReportProposal,
    ResearchSubmission,
    RunResult,
    RunSpec,
    TaskRecord,
)
from researchctl.errors import RCPError
from researchctl.runtime import RuntimeStore
from researchctl.serialization import canonical_digest, dump_yaml
from researchctl.services.actor import (
    ActorContext,
    ActorRole,
    CredentialKind,
)
from researchctl.services.ci_dispatch import CIPRDispatchAttestation
from researchctl.services.ci_validation import CI_CHECK_IDENTITY, CI_WORKFLOW_ID
from researchctl.services.github_post_merge import (
    GITHUB_WORKFLOW_EVENT,
    GITHUB_WORKFLOW_PATH,
    AuthenticatedGitHubPostMergeBridge,
    AuthenticatedGitHubPostMergeObservation,
    github_artifact_name,
)
from researchctl.services.linear_delivery import (
    AcceptedMergeSnapshot,
    LinearAcceptedResultDeliveryService,
    LinearCommentObservation,
    LinearDeliveryUnavailable,
    LinearTarget,
    LinearTargetObservation,
    parse_linear_delivery_marker,
    strip_linear_transport_envelope,
)
from researchctl.services.linear_preview import build_linear_preview
from researchctl.services.post_merge import (
    TrustedPostMergeService,
    post_merge_request_from_artifact,
    write_post_merge_artifact,
)
from researchctl.services.review_acceptance import ReviewAcceptanceBuilder
from researchctl.services.submissions import SubmissionEvidence

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
WORKSPACE_ID = "11111111-1111-4111-8111-111111111111"
TEAM_ID = "22222222-2222-4222-8222-222222222222"
LINEAR_PROJECT_ID = "33333333-3333-4333-8333-333333333333"
ISSUE_ID = "44444444-4444-4444-8444-444444444444"
COMMENT_ID = "55555555-5555-4555-8555-555555555555"
THREAD_ID = "66666666-6666-4666-8666-666666666666"
APP_ID = "researchctl-linear-app"
SUBJECT_HEAD = "d" * 40
MERGE_COMMIT = "e" * 40


def _id(kind: str, fill: str) -> str:
    return f"{kind}_20260803T120000Z_{fill * 24}"


@dataclass(frozen=True, slots=True)
class DeliveryCase:
    project_id: str
    snapshot: AcceptedMergeSnapshot
    ci: CIValidationAttestation
    renderer_payload: bytes


class FakeAcceptedMergeReader:
    def __init__(self, snapshot: AcceptedMergeSnapshot | None) -> None:
        self.snapshot = snapshot
        self.calls: list[tuple[str, str, str]] = []

    def read_accepted_merge(
        self,
        *,
        project_id: str,
        merge_commit: str,
        ci: CIValidationAttestation,
    ) -> AcceptedMergeSnapshot | None:
        self.calls.append((project_id, merge_commit, ci.attestation_id))
        return self.snapshot


class SimulatedWorkerCrash(RuntimeError):
    pass


class FakeLinearPort:
    def __init__(self, target: LinearTarget) -> None:
        self.target_observation: LinearTargetObservation | None = _observation(target)
        self.comments: list[LinearCommentObservation] = []
        self.calls: list[str] = []
        self.created_bodies: list[bytes] = []
        self.unavailable_at: str | None = None
        self.crash_after_create = False

    def preflight_target(
        self,
        target: LinearTarget,
    ) -> LinearTargetObservation | None:
        self.calls.append("preflight")
        if self.unavailable_at == "preflight":
            raise LinearDeliveryUnavailable("workspace lookup unavailable")
        return self.target_observation

    def observe_comment(
        self,
        *,
        issue_id: str,
        marker: str,
        expected_author_app_id: str,
        thread_id: str | None = None,
    ) -> LinearCommentObservation | None:
        self.calls.append("observe")
        if self.unavailable_at == "observe":
            raise LinearDeliveryUnavailable("comment lookup unavailable")
        encoded = marker.encode("ascii")
        return next(
            (
                comment
                for comment in self.comments
                if comment.issue_id == issue_id
                and comment.author_app_id == expected_author_app_id
                and encoded in comment.body
            ),
            None,
        )

    def create_comment(
        self,
        *,
        issue_id: str,
        body: bytes,
        thread_id: str | None = None,
    ) -> LinearCommentObservation:
        self.calls.append("create")
        if self.unavailable_at == "create":
            raise LinearDeliveryUnavailable("comment create unavailable")
        comment = LinearCommentObservation(
            comment_id=COMMENT_ID,
            issue_id=issue_id,
            thread_id=thread_id or THREAD_ID,
            author_app_id=APP_ID,
            body=body,
        )
        self.created_bodies.append(body)
        self.comments.append(comment)
        if self.crash_after_create:
            raise SimulatedWorkerCrash("worker stopped before recording receipt")
        return comment


def _observation(
    target: LinearTarget,
    **updates: object,
) -> LinearTargetObservation:
    values: dict[str, object] = {
        "workspace_id": target.workspace_id,
        "team_id": target.team_id,
        "team_workspace_id": target.workspace_id,
        "project_id": target.project_id,
        "project_workspace_id": (
            target.workspace_id if target.project_id is not None else None
        ),
        "project_team_ids": (
            (target.team_id,) if target.project_id is not None else ()
        ),
        "issue_id": target.issue_id,
        "issue_workspace_id": target.workspace_id,
        "issue_team_id": target.team_id,
        "issue_project_ids": (
            (target.project_id,) if target.project_id is not None else ()
        ),
    }
    values.update(updates)
    return LinearTargetObservation(**values)  # type: ignore[arg-type]


def _trusted_actor() -> ActorContext:
    return ActorContext(
        actor_id="researchctl-app-post-merge",
        role=ActorRole.TRUSTED_AUTOMATION,
        credential_kind=CredentialKind.AUTOMATION_CREDENTIAL,
    )


@pytest.fixture
def delivery_case(
    task_payload,
    run_spec_payload,
    run_result_payload,
    submission_payload,
) -> DeliveryCase:
    project_id = _id("project", "9")
    task = TaskRecord.model_validate(
        task_payload(state="ready", linear_issue_id=ISSUE_ID)
    )
    spec = RunSpec.model_validate(
        run_spec_payload(
            inputs=[
                {
                    "kind": "dataset",
                    "logical_id": "validation-split",
                    "version": "2026-08-01",
                    "waiver_allowed": False,
                }
            ]
        )
    )
    result = RunResult.model_validate(
        run_result_payload(run_spec_digest=spec.spec_digest)
    )
    submission = ResearchSubmission.model_validate(
        submission_payload(
            state="open",
            run_result_ids=[result.result_id],
            limitations=["One seed was evaluated."],
        )
    )
    proposal = ReportProposal(
        submission_id=submission.submission_id,
        report_id=_id("report", "8"),
        expected_report_revision=0,
        title="Stopping policy result",
        evidence_tree=spec.source_tree,
    )
    accepted = ReviewAcceptanceBuilder().build(
        task=task,
        submission=submission,
        proposal=proposal,
        evidence=(SubmissionEvidence(spec, result),),
        current_report=None,
        decision_id=_id("decision", "7"),
        reviewer_actor="uid-1000",
        decided_at=NOW,
        disposition=ReviewDisposition.ACCEPTED,
        conditions=(),
        claim_scope=ClaimScope.SNAPSHOT,
        code_disposition=CodeDisposition.RETAIN_ISOLATED,
        accepted_base_tree="a" * 40,
    )
    policy = LinearProjectionPolicy(
        workspace_id=WORKSPACE_ID,
        team_id=TEAM_ID,
        project_id=LINEAR_PROJECT_ID,
    )
    preview = build_linear_preview(
        policy=policy,
        task=task,
        submission=accepted.submission,
        decision=accepted.decision,
        report=accepted.report,
    )
    assert preview.body is not None
    outputs = (
        GeneratedOutputDigest(
            path="generated/report-preview.md",
            digest="sha256:" + "1" * 64,
            size_bytes=42,
        ),
    )
    ci = CIValidationAttestation(
        attestation_id=_id("attestation", "6"),
        project_id=project_id,
        task_id=task.task_id,
        submission_id=accepted.submission.submission_id,
        repository="example/research",
        pull_request_number=17,
        subject_head=SUBJECT_HEAD,
        subject_tree="b" * 40,
        base_commit="c" * 40,
        validator_version="0.1.0",
        schema_manifest_digest="sha256:" + "2" * 64,
        workflow_id="research-validate-pr",
        check_identity="researchctl/exact-head",
        checks=(CIValidationCheck(name="accepted_records", status="passed"),),
        generated_outputs=outputs,
        submission_digest=canonical_digest(accepted.submission),
        report_proposal_digest=canonical_digest(proposal),
        decision_digest=canonical_digest(accepted.decision),
        report_id=accepted.report.report_id,
        report_revision=accepted.report.revision,
        report_digest=canonical_digest(accepted.report),
        report_preview_digest="sha256:" + "3" * 64,
        projection=preview.projection,
        generated_at=NOW,
        artifact_digest=canonical_digest(
            {
                "generated_outputs": [
                    item.model_dump(mode="json") for item in outputs
                ]
            }
        ),
        overall_result="passed",
    )
    snapshot = AcceptedMergeSnapshot(
        project_id=project_id,
        merge_commit=MERGE_COMMIT,
        subject_head=SUBJECT_HEAD,
        default_branch="main",
        protected_ref="refs/heads/main",
        accepted_on_protected_default_branch=True,
        attested_subject_incorporated=True,
        task=task,
        submission=accepted.submission,
        decision=accepted.decision,
        report=accepted.report,
        policy=policy,
    )
    return DeliveryCase(
        project_id=project_id,
        snapshot=snapshot,
        ci=ci,
        renderer_payload=preview.body,
    )


def _enqueue(delivery_case: DeliveryCase):
    source = FakeAcceptedMergeReader(delivery_case.snapshot)
    service = LinearAcceptedResultDeliveryService(source)
    event = service.enqueue(
        actor=_trusted_actor(),
        project_id=delivery_case.project_id,
        merge_commit=MERGE_COMMIT,
        ci=delivery_case.ci,
    )
    assert event is not None
    return service, source, event


def _dispatch_artifact(delivery_case: DeliveryCase) -> bytes:
    ci = delivery_case.ci
    artifact = CIPRDispatchAttestation(
        attestation_id=ci.attestation_id,
        repository=ci.repository,
        pull_request_number=ci.pull_request_number,
        subject_head=ci.subject_head,
        subject_tree=ci.subject_tree,
        base_commit=ci.base_commit,
        head_ref=f"research/submission/{ci.submission_id}",
        base_ref="main",
        pr_type="submission",
        applicability="validated",
        checks=(CIValidationCheck(name="pr_type_dispatch", status="passed"),),
        submission_attestation=ci,
        generated_at=ci.generated_at,
        overall_result="passed",
    )
    return dump_yaml(artifact).encode("utf-8")


def test_post_merge_shadow_is_credential_free_and_writes_no_live_outbox(
    tmp_path,
    delivery_case: DeliveryCase,
) -> None:
    content = _dispatch_artifact(delivery_case)
    request = post_merge_request_from_artifact(
        dispatch_artifact=content,
        merge_commit=MERGE_COMMIT,
    )
    with RuntimeStore(tmp_path / "runtime.sqlite3") as runtime:
        service = TrustedPostMergeService(
            runtime=runtime,
            accepted=LinearAcceptedResultDeliveryService(
                FakeAcceptedMergeReader(delivery_case.snapshot)
            ),
        )

        result = service.process(
            request=request,
            dispatch_artifact=content,
            actor=_trusted_actor(),
        )

        assert result.state == "shadow_validated"
        assert result.event is not None
        assert result.event.ci_attestation_id == delivery_case.ci.attestation_id
        assert result.event.accepted_merge_commit == MERGE_COMMIT
        assert runtime.list_linear_projection_outbox(delivery_case.project_id) == ()
        assert result.as_dict()["remote_mutation_performed"] is False

    output = tmp_path / "post-merge-shadow.json"
    first = write_post_merge_artifact(result, output)
    replay = write_post_merge_artifact(result, output)
    observed = json.loads(output.read_text(encoding="utf-8"))
    assert first.created is True
    assert replay.created is False
    assert observed["event"]["renderer_payload"] == (
        delivery_case.renderer_payload.decode("utf-8")
    )
    assert observed["outbox_state"] is None


def test_post_merge_enqueue_requires_authenticated_github_bridge_and_is_idempotent(
    tmp_path,
    delivery_case: DeliveryCase,
) -> None:
    content = _dispatch_artifact(delivery_case)
    with pytest.raises(ValueError, match="live enqueue requires"):
        post_merge_request_from_artifact(
            dispatch_artifact=content,
            merge_commit=MERGE_COMMIT,
            mode="enqueue",
        )
    with pytest.raises(ValueError, match="shadow-only"):
        post_merge_request_from_artifact(
            dispatch_artifact=content,
            merge_commit=MERGE_COMMIT,
            mode="enqueue",
            provenance="github_authenticated",
            workflow_run_id="17",
            check_run_id="23",
            artifact_id="29",
        )

    observation = AuthenticatedGitHubPostMergeObservation(
        repository=delivery_case.ci.repository,
        pull_request_number=delivery_case.ci.pull_request_number,
        merged=True,
        base_ref="main",
        base_sha=delivery_case.ci.base_commit,
        subject_head=delivery_case.ci.subject_head,
        merge_commit=MERGE_COMMIT,
        workflow_id=CI_WORKFLOW_ID,
        workflow_path=GITHUB_WORKFLOW_PATH,
        workflow_event=GITHUB_WORKFLOW_EVENT,
        workflow_run_id="17",
        workflow_status="completed",
        workflow_conclusion="success",
        check_identity=CI_CHECK_IDENTITY,
        check_run_id="23",
        check_status="completed",
        check_conclusion="success",
        artifact_id="29",
        artifact_name=github_artifact_name(
            delivery_case.ci.pull_request_number,
            delivery_case.ci.subject_head,
        ),
        artifact_expired=False,
        artifact_bytes=content,
        artifact_digest="sha256:" + hashlib.sha256(content).hexdigest(),
    )

    class GitHubObservation:
        def observe(self, *, repository: str, pull_request_number: int):
            assert repository == observation.repository
            assert pull_request_number == observation.pull_request_number
            return observation

    with RuntimeStore(tmp_path / "runtime.sqlite3") as runtime:
        service = TrustedPostMergeService(
            runtime=runtime,
            accepted=LinearAcceptedResultDeliveryService(
                FakeAcceptedMergeReader(delivery_case.snapshot)
            ),
        )

        class PostMergeApplication:
            def post_merge_process(self, *, request, dispatch_artifact, actor):
                return service.process(
                    request=request,
                    dispatch_artifact=dispatch_artifact,
                    actor=actor,
                )

        bridge = AuthenticatedGitHubPostMergeBridge(
            github=GitHubObservation(),
            application=PostMergeApplication(),
            actor=_trusted_actor(),
        )

        queued = bridge.enqueue(
            repository=observation.repository,
            pull_request_number=observation.pull_request_number,
        )
        replay = bridge.enqueue(
            repository=observation.repository,
            pull_request_number=observation.pull_request_number,
        )

        assert queued.state == "queued"
        assert replay.state == "already_queued"
        assert queued.event == replay.event
        assert queued.event is not None
        assert queued.event.workflow_run_id == "17"
        assert queued.event.check_run_id == "23"
        assert queued.event.artifact_id == "29"
        rows = runtime.list_linear_projection_outbox(delivery_case.project_id)
        assert len(rows) == 1
        assert rows[0].state == "pending"


def test_post_merge_artifact_digest_mismatch_has_zero_side_effects(
    tmp_path,
    delivery_case: DeliveryCase,
) -> None:
    content = _dispatch_artifact(delivery_case)
    request = post_merge_request_from_artifact(
        dispatch_artifact=content,
        merge_commit=MERGE_COMMIT,
    ).model_copy(update={"artifact_digest": "sha256:" + "0" * 64})
    source = FakeAcceptedMergeReader(delivery_case.snapshot)
    with RuntimeStore(tmp_path / "runtime.sqlite3") as runtime:
        service = TrustedPostMergeService(
            runtime=runtime,
            accepted=LinearAcceptedResultDeliveryService(source),
        )

        with pytest.raises(RCPError) as caught:
            service.process(
                request=request,
                dispatch_artifact=content,
                actor=_trusted_actor(),
            )

        assert caught.value.code == "post_merge_artifact_digest_mismatch"
        assert source.calls == []
        assert runtime.list_linear_projection_outbox(delivery_case.project_id) == ()


def _deliver(service, event, remote):
    return service.deliver(
        actor=_trusted_actor(),
        event=event,
        remote=remote,
        expected_author_app_id=APP_ID,
    )


def test_only_trusted_post_merge_context_can_enqueue(
    delivery_case: DeliveryCase,
) -> None:
    source = FakeAcceptedMergeReader(delivery_case.snapshot)
    service = LinearAcceptedResultDeliveryService(source)
    agent = ActorContext(
        actor_id="session-agent",
        role=ActorRole.AGENT,
        credential_kind=CredentialKind.SESSION_CAPABILITY,
        bound_session_id=delivery_case.snapshot.submission.session_id,
    )

    with pytest.raises(RCPError) as caught:
        service.enqueue(
            actor=agent,
            project_id=delivery_case.project_id,
            merge_commit=MERGE_COMMIT,
            ci=delivery_case.ci,
        )

    assert caught.value.code == "authorization_denied"
    assert source.calls == []

    event = service.enqueue(
        actor=_trusted_actor(),
        project_id=delivery_case.project_id,
        merge_commit=MERGE_COMMIT,
        ci=delivery_case.ci,
    )
    assert event is not None
    remote = FakeLinearPort(event.target)
    with pytest.raises(RCPError) as delivery_error:
        service.deliver(
            actor=agent,
            event=event,
            remote=remote,
            expected_author_app_id=APP_ID,
        )
    assert delivery_error.value.code == "authorization_denied"
    assert remote.calls == []


@pytest.mark.parametrize(
    "snapshot_update",
    [
        {"accepted_on_protected_default_branch": False},
        {"attested_subject_incorporated": False},
        {"protected_ref": "refs/heads/research/proposal"},
    ],
)
def test_candidate_or_unprotected_commit_cannot_enqueue(
    delivery_case: DeliveryCase,
    snapshot_update: dict[str, object],
) -> None:
    source = FakeAcceptedMergeReader(
        replace(delivery_case.snapshot, **snapshot_update)
    )
    service = LinearAcceptedResultDeliveryService(source)

    with pytest.raises(RCPError) as caught:
        service.enqueue(
            actor=_trusted_actor(),
            project_id=delivery_case.project_id,
            merge_commit=MERGE_COMMIT,
            ci=delivery_case.ci,
        )

    assert caught.value.code == "linear_delivery_not_accepted_merge"


def test_enqueue_uses_stable_identity_manager_target_and_exact_ci_payload(
    delivery_case: DeliveryCase,
) -> None:
    service, _, first = _enqueue(delivery_case)
    second = service.enqueue(
        actor=_trusted_actor(),
        project_id=delivery_case.project_id,
        merge_commit=MERGE_COMMIT,
        ci=delivery_case.ci,
    )

    assert second == first
    assert first.target == LinearTarget(
        workspace_id=WORKSPACE_ID,
        team_id=TEAM_ID,
        project_id=LINEAR_PROJECT_ID,
        issue_id=ISSUE_ID,
    )
    assert first.renderer_payload == delivery_case.renderer_payload
    assert strip_linear_transport_envelope(
        first.transport_body,
        first.marker,
    ) == delivery_case.renderer_payload
    assert first.transport_body == (
        delivery_case.renderer_payload + first.marker.encode("ascii") + b"\n"
    )
    assert first.payload_digest == (
        "sha256:" + hashlib.sha256(delivery_case.renderer_payload).hexdigest()
    )
    assert first.marker.encode("ascii") not in first.renderer_payload
    assert MERGE_COMMIT.encode("ascii") not in first.renderer_payload
    assert MERGE_COMMIT not in first.marker
    assert first.session_id == delivery_case.snapshot.submission.session_id
    assert parse_linear_delivery_marker(first.marker) == {
        "event_id": first.event_id,
        "payload_digest": first.payload_digest,
        "agent_id": first.agent_id,
        "session_id": first.session_id,
        "task_id": first.task_id,
        "report_id": first.report_id,
    }


def test_policy_without_project_skips_project_remote_validation(
    delivery_case: DeliveryCase,
) -> None:
    policy = delivery_case.snapshot.policy
    assert policy is not None
    policy = policy.model_copy(update={"project_id": None})
    snapshot = replace(delivery_case.snapshot, policy=policy)
    preview = build_linear_preview(
        policy=policy,
        task=snapshot.task,
        submission=snapshot.submission,
        decision=snapshot.decision,
        report=snapshot.report,
    )
    ci = delivery_case.ci.model_copy(update={"projection": preview.projection})
    service = LinearAcceptedResultDeliveryService(FakeAcceptedMergeReader(snapshot))
    event = service.enqueue(
        actor=_trusted_actor(),
        project_id=delivery_case.project_id,
        merge_commit=MERGE_COMMIT,
        ci=ci,
    )
    assert event is not None
    assert event.target.project_id is None
    remote = FakeLinearPort(event.target)
    remote.target_observation = _observation(
        event.target,
        project_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        project_workspace_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        project_team_ids=("cccccccc-cccc-4ccc-8ccc-cccccccccccc",),
        issue_project_ids=("dddddddd-dddd-4ddd-8ddd-dddddddddddd",),
        project_archived=True,
    )

    outcome = _deliver(service, event, remote)

    assert outcome.state == "delivered"


def test_matching_disabled_manager_policy_and_ci_produce_no_event(
    delivery_case: DeliveryCase,
) -> None:
    source = FakeAcceptedMergeReader(
        replace(delivery_case.snapshot, policy=None)
    )
    service = LinearAcceptedResultDeliveryService(source)
    ci = delivery_case.ci.model_copy(
        update={
            "projection": LinearProjectionDisabled(
                reason="integration_not_configured"
            )
        }
    )

    event = service.enqueue(
        actor=_trusted_actor(),
        project_id=delivery_case.project_id,
        merge_commit=MERGE_COMMIT,
        ci=ci,
    )

    assert event is None


@pytest.mark.parametrize(
    ("mismatch", "error_code"),
    [
        ("target", "linear_ci_target_mismatch"),
        ("digest", "linear_ci_payload_digest_mismatch"),
    ],
)
def test_ci_preview_mismatch_dead_letters_before_remote_access(
    delivery_case: DeliveryCase,
    mismatch: str,
    error_code: str,
) -> None:
    projection = delivery_case.ci.projection
    assert projection.state == "configured"
    if mismatch == "target":
        projection = projection.model_copy(
            update={"issue_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"}
        )
    else:
        projection = projection.model_copy(
            update={"payload_digest": "sha256:" + "f" * 64}
        )
    ci = delivery_case.ci.model_copy(update={"projection": projection})
    source = FakeAcceptedMergeReader(delivery_case.snapshot)
    service = LinearAcceptedResultDeliveryService(source)
    event = service.enqueue(
        actor=_trusted_actor(),
        project_id=delivery_case.project_id,
        merge_commit=MERGE_COMMIT,
        ci=ci,
    )
    assert event is not None
    remote = FakeLinearPort(event.target)

    outcome = _deliver(service, event, remote)

    assert outcome.state == "dead_letter"
    assert outcome.error_code == error_code
    assert remote.calls == []
    assert remote.created_bodies == []


def test_delivery_observes_marker_before_create_and_replay_is_exactly_once(
    delivery_case: DeliveryCase,
) -> None:
    service, _, event = _enqueue(delivery_case)
    remote = FakeLinearPort(event.target)

    first = _deliver(service, event, remote)
    second = _deliver(service, event, remote)

    assert first.state == "delivered"
    assert second.state == "delivered"
    assert first.receipt == second.receipt
    assert first.receipt is not None
    assert first.receipt.payload_digest == event.payload_digest
    assert first.receipt.marker == event.marker
    assert first.receipt.source_marker.task_id == event.task_id
    assert first.receipt.source_marker.session_id == event.session_id
    assert first.receipt.source_marker.report_id == event.report_id
    assert first.receipt.comment_id == COMMENT_ID
    assert len(remote.created_bodies) == 1
    assert len(remote.comments) == 1
    assert remote.calls == [
        "preflight",
        "observe",
        "create",
        "preflight",
        "observe",
    ]


def test_api_success_then_worker_crash_recovers_receipt_by_marker(
    delivery_case: DeliveryCase,
) -> None:
    service, _, event = _enqueue(delivery_case)
    remote = FakeLinearPort(event.target)
    remote.crash_after_create = True

    with pytest.raises(SimulatedWorkerCrash):
        _deliver(service, event, remote)
    assert len(remote.comments) == 1
    assert len(remote.created_bodies) == 1

    remote.crash_after_create = False
    recovered = _deliver(service, event, remote)
    replay = _deliver(service, event, remote)

    assert recovered.state == "delivered"
    assert recovered.receipt == replay.receipt
    assert recovered.receipt is not None
    assert recovered.receipt.payload_digest == event.payload_digest
    assert recovered.receipt.marker == event.marker
    assert len(remote.comments) == 1
    assert len(remote.created_bodies) == 1


@pytest.mark.parametrize("stage", ["preflight", "observe", "create"])
def test_linear_outage_is_retryable(
    delivery_case: DeliveryCase,
    stage: str,
) -> None:
    service, _, event = _enqueue(delivery_case)
    remote = FakeLinearPort(event.target)
    remote.unavailable_at = stage

    outcome = _deliver(service, event, remote)

    assert outcome.state == "retryable"
    assert outcome.error_code == "linear_delivery_unavailable"
    assert outcome.retry_stage == stage
    assert remote.created_bodies == []
    assert remote.comments == []


def test_local_payload_digest_mismatch_dead_letters_without_remote_access(
    delivery_case: DeliveryCase,
) -> None:
    service, _, event = _enqueue(delivery_case)
    corrupt = replace(event, renderer_payload=event.renderer_payload + b"corrupt")
    remote = FakeLinearPort(event.target)

    outcome = _deliver(service, corrupt, remote)

    assert outcome.state == "dead_letter"
    assert outcome.error_code == "linear_payload_digest_mismatch"
    assert remote.calls == []
    assert remote.created_bodies == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("accepted_merge_commit", "not-a-commit"),
        ("ci_subject_head", "not-a-commit"),
        ("report_digest", "sha256:not-a-digest"),
    ],
)
def test_local_lineage_tamper_dead_letters_without_remote_access(
    delivery_case: DeliveryCase,
    field: str,
    value: str,
) -> None:
    service, _, event = _enqueue(delivery_case)
    corrupt = replace(event, **{field: value})
    remote = FakeLinearPort(event.target)

    outcome = _deliver(service, corrupt, remote)

    assert outcome.state == "dead_letter"
    assert outcome.error_code == "linear_event_lineage_invalid"
    assert remote.calls == []


def test_remote_target_mismatch_dead_letters_before_any_mutation(
    delivery_case: DeliveryCase,
) -> None:
    service, _, event = _enqueue(delivery_case)
    remote = FakeLinearPort(event.target)
    remote.target_observation = _observation(
        event.target,
        issue_team_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    )

    outcome = _deliver(service, event, remote)

    assert outcome.state == "dead_letter"
    assert outcome.error_code == "linear_target_mismatch"
    assert remote.calls == ["preflight"]
    assert remote.created_bodies == []
    assert remote.comments == []


@pytest.mark.parametrize(
    "archived_field",
    [
        "workspace_archived",
        "team_archived",
        "project_archived",
        "issue_archived",
    ],
)
def test_archived_remote_resource_dead_letters_before_mutation(
    delivery_case: DeliveryCase,
    archived_field: str,
) -> None:
    service, _, event = _enqueue(delivery_case)
    remote = FakeLinearPort(event.target)
    remote.target_observation = _observation(
        event.target,
        **{archived_field: True},
    )

    outcome = _deliver(service, event, remote)

    assert outcome.state == "dead_letter"
    assert outcome.error_code == "linear_target_archived"
    assert remote.calls == ["preflight"]
    assert remote.created_bodies == []


def test_existing_marker_with_wrong_payload_dead_letters_without_duplicate(
    delivery_case: DeliveryCase,
) -> None:
    service, _, event = _enqueue(delivery_case)
    remote = FakeLinearPort(event.target)
    remote.comments.append(
        LinearCommentObservation(
            comment_id=COMMENT_ID,
            issue_id=event.target.issue_id,
            thread_id=THREAD_ID,
            author_app_id=APP_ID,
            body=b"forged payload\n" + event.marker.encode("ascii") + b"\n",
        )
    )

    outcome = _deliver(service, event, remote)

    assert outcome.state == "dead_letter"
    assert outcome.error_code == "linear_comment_payload_mismatch"
    assert remote.calls == ["preflight", "observe"]
    assert remote.created_bodies == []
    assert len(remote.comments) == 1
