from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import pytest

from researchctl.adapters.git_accepted_merge import GitAcceptedMergeReader
from researchctl.domain.enums import ClaimScope, CodeDisposition, ProjectState
from researchctl.domain.models import (
    CIValidationAttestation,
    CIValidationCheck,
    GeneratedOutputDigest,
    LinearProjectionPolicy,
    ProjectRecord,
    ReportProposal,
    ResearchSubmission,
    RunResult,
    RunSpec,
    TaskRecord,
)
from researchctl.errors import RCPError
from researchctl.runtime import RuntimeStore
from researchctl.serialization import canonical_digest, dump_yaml, load_model
from researchctl.services.actor import ActorContext, ActorRole, CredentialKind
from researchctl.services.ci_dispatch import CIPRDispatchAttestation
from researchctl.services.linear_delivery import LinearAcceptedResultDeliveryService
from researchctl.services.linear_preview import build_linear_preview
from researchctl.services.post_merge import (
    TrustedPostMergeService,
    post_merge_request_from_artifact,
)
from researchctl.services.review_acceptance import ReviewAcceptanceBuilder
from researchctl.services.submissions import SubmissionEvidence
from researchctl.services.task_records import TaskRecordRepository

WORKSPACE_ID = "11111111-1111-4111-8111-111111111111"
TEAM_ID = "22222222-2222-4222-8222-222222222222"
LINEAR_PROJECT_ID = "33333333-3333-4333-8333-333333333333"
ISSUE_ID = "44444444-4444-4444-8444-444444444444"
ATTESTATION_ID = "attestation_20260803T130000Z_" + "a" * 24
DECISION_ID = "decision_20260803T130000Z_" + "b" * 24
REPORT_ID = "report_20260803T130000Z_" + "c" * 24

RecordKind = Literal["project", "task", "policy", "submission", "decision", "report"]


@dataclass(frozen=True, slots=True)
class AcceptedGraph:
    repository: Path
    project_id: str
    base_commit: str
    subject_head: str
    merge_commit: str
    ci: CIValidationAttestation
    dispatch_artifact: bytes
    generated_paths: tuple[str, ...]


def _git(
    repository: Path,
    *arguments: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=check,
        capture_output=True,
        text=True,
    )


def _commit(repository: Path, message: str) -> str:
    _git(repository, "add", "--all")
    _git(
        repository,
        "-c",
        "user.name=Tests",
        "-c",
        "user.email=tests@example.invalid",
        "commit",
        "--no-gpg-sign",
        "-m",
        message,
    )
    return _git(repository, "rev-parse", "HEAD").stdout.strip()


def _append_noncanonical_newline(path: Path) -> None:
    path.write_bytes(path.read_bytes() + b"\n")


def _output_digest(path: str, content: bytes) -> GeneratedOutputDigest:
    return GeneratedOutputDigest(
        path=path,
        digest=f"sha256:{hashlib.sha256(content).hexdigest()}",
        size_bytes=len(content),
    )


def _accepted_graph(
    repository: Path,
    *,
    task_payload: Callable[..., dict[str, Any]],
    run_spec_payload: Callable[..., dict[str, Any]],
    run_result_payload: Callable[..., dict[str, Any]],
    submission_payload: Callable[..., dict[str, Any]],
    noncanonical_record: RecordKind | None = None,
    corrupt_merged_output: bool = False,
) -> AcceptedGraph:
    project_path = repository / ".research" / "project.yaml"
    project = load_model(project_path, ProjectRecord)
    project = ProjectRecord.model_validate(
        {**project.model_dump(mode="python"), "state": ProjectState.MANAGED}
    )
    project_path.write_text(dump_yaml(project), encoding="utf-8")

    task = TaskRecord.model_validate(
        task_payload(state="ready", linear_issue_id=ISSUE_ID)
    )
    TaskRecordRepository(repository).create(task)
    task_path = repository / ".research" / "tasks" / f"{task.task_id}.yaml"
    policy = LinearProjectionPolicy(
        workspace_id=WORKSPACE_ID,
        team_id=TEAM_ID,
        project_id=LINEAR_PROJECT_ID,
    )
    policy_path = repository / ".research" / "policies" / "linear.yaml"
    policy_path.write_text(dump_yaml(policy), encoding="utf-8")

    base_record_paths = {
        "project": project_path,
        "task": task_path,
        "policy": policy_path,
    }
    if noncanonical_record in base_record_paths:
        _append_noncanonical_newline(base_record_paths[noncanonical_record])

    base_commit = _commit(repository, "accepted graph base")
    base_tree = _git(repository, "rev-parse", "HEAD^{tree}").stdout.strip()
    spec = RunSpec.model_validate(
        run_spec_payload(
            task_id=task.task_id,
            source_commit=base_commit,
            source_tree=base_tree,
            inputs=[
                {
                    "kind": "dataset",
                    "logical_id": "validation-split",
                    "version": "2026-08-01",
                    "waiver_allowed": False,
                }
            ],
        )
    )
    result = RunResult.model_validate(
        run_result_payload(
            run_id=spec.run_id,
            run_spec_digest=spec.spec_digest,
        )
    )
    submission = ResearchSubmission.model_validate(
        submission_payload(
            task_id=task.task_id,
            session_id=spec.session_id,
            state="open",
            run_result_ids=[result.result_id],
        )
    )
    proposal = ReportProposal(
        submission_id=submission.submission_id,
        report_id=REPORT_ID,
        expected_report_revision=0,
        title="Stopping policy result",
        evidence_tree=base_tree,
    )
    accepted = ReviewAcceptanceBuilder().build(
        task=task,
        submission=submission,
        proposal=proposal,
        evidence=(SubmissionEvidence(spec, result),),
        current_report=None,
        decision_id=DECISION_ID,
        reviewer_actor="uid-1000",
        decided_at="2026-08-03T13:00:00Z",
        disposition="accepted",
        conditions=(),
        claim_scope=ClaimScope.SNAPSHOT,
        code_disposition=CodeDisposition.RETAIN_ISOLATED,
        accepted_base_tree=base_tree,
    )

    _git(repository, "switch", "-c", "accepted-subject")
    accepted_record_paths: dict[RecordKind, Path] = {}
    for rendered in accepted.files:
        destination = repository / rendered.path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(rendered.content)
        if rendered.path.endswith("/submission.yaml"):
            accepted_record_paths["submission"] = destination
        elif rendered.path.startswith(".research/decisions/"):
            accepted_record_paths["decision"] = destination
        elif rendered.path.endswith(".yaml"):
            accepted_record_paths["report"] = destination
    if noncanonical_record in accepted_record_paths:
        _append_noncanonical_newline(accepted_record_paths[noncanonical_record])
    subject_head = _commit(repository, "accepted subject")
    subject_tree = _git(repository, "rev-parse", "HEAD^{tree}").stdout.strip()

    generated_outputs = tuple(
        _output_digest(rendered.path, (repository / rendered.path).read_bytes())
        for rendered in accepted.files
    )
    preview = build_linear_preview(
        policy=policy,
        task=task,
        submission=accepted.submission,
        decision=accepted.decision,
        report=accepted.report,
    )
    report_markdown = next(
        rendered
        for rendered in accepted.files
        if rendered.path.endswith(".md")
    )
    ci = CIValidationAttestation(
        attestation_id=ATTESTATION_ID,
        project_id=project.project_id,
        task_id=task.task_id,
        submission_id=accepted.submission.submission_id,
        repository="example/research",
        pull_request_number=17,
        subject_head=subject_head,
        subject_tree=subject_tree,
        base_commit=base_commit,
        validator_version="0.1.0",
        schema_manifest_digest="sha256:" + "1" * 64,
        workflow_id="research-validate-pr",
        check_identity="researchctl/exact-head",
        checks=(CIValidationCheck(name="accepted_records", status="passed"),),
        generated_outputs=generated_outputs,
        submission_digest=canonical_digest(accepted.submission),
        report_proposal_digest=canonical_digest(proposal),
        decision_digest=canonical_digest(accepted.decision),
        report_id=accepted.report.report_id,
        report_revision=accepted.report.revision,
        report_digest=canonical_digest(accepted.report),
        report_preview_digest=_output_digest(
            report_markdown.path,
            report_markdown.content,
        ).digest,
        projection=preview.projection,
        generated_at="2026-08-03T13:01:00Z",
        artifact_digest=canonical_digest(
            {
                "generated_outputs": [
                    item.model_dump(mode="json") for item in generated_outputs
                ]
            }
        ),
        overall_result="passed",
    )

    _git(repository, "switch", "main")
    if corrupt_merged_output:
        conflict_path = repository / report_markdown.path
        conflict_path.parent.mkdir(parents=True, exist_ok=True)
        conflict_path.write_bytes(report_markdown.content + b"merge-side corruption\n")
        _commit(repository, "conflicting protected output")
        merged = _git(
            repository,
            "merge",
            "--no-ff",
            "--no-commit",
            "accepted-subject",
            check=False,
        )
        assert merged.returncode != 0
        conflict_path.write_bytes(report_markdown.content + b"merge-side corruption\n")
        merge_commit = _commit(repository, "merge accepted subject with corrupt output")
    else:
        _git(
            repository,
            "merge",
            "--no-ff",
            "--no-gpg-sign",
            "-m",
            "merge accepted subject",
            "accepted-subject",
        )
        merge_commit = _git(repository, "rev-parse", "HEAD").stdout.strip()

    dispatch = CIPRDispatchAttestation(
        attestation_id=ci.attestation_id,
        repository=ci.repository,
        pull_request_number=ci.pull_request_number,
        subject_head=ci.subject_head,
        subject_tree=ci.subject_tree,
        base_commit=ci.base_commit,
        head_ref="accepted-subject",
        base_ref="main",
        pr_type="submission",
        applicability="validated",
        checks=(CIValidationCheck(name="pr_type_dispatch", status="passed"),),
        submission_attestation=ci,
        generated_at=ci.generated_at,
        overall_result="passed",
    )
    return AcceptedGraph(
        repository=repository,
        project_id=project.project_id,
        base_commit=base_commit,
        subject_head=subject_head,
        merge_commit=merge_commit,
        ci=ci,
        dispatch_artifact=dump_yaml(dispatch).encode("utf-8"),
        generated_paths=tuple(item.path for item in generated_outputs),
    )


def _reader(graph: AcceptedGraph) -> GitAcceptedMergeReader:
    return GitAcceptedMergeReader(
        repository_root=graph.repository,
        expected_project_id=graph.project_id,
        expected_default_branch="main",
    )


def _trusted_actor() -> ActorContext:
    return ActorContext(
        actor_id="github-post-merge",
        role=ActorRole.TRUSTED_AUTOMATION,
        credential_kind=CredentialKind.AUTOMATION_CREDENTIAL,
    )


def _graph_from_fixtures(
    initialized_repository: Path,
    task_payload,
    run_spec_payload,
    run_result_payload,
    submission_payload,
    **options,
) -> AcceptedGraph:
    return _accepted_graph(
        initialized_repository,
        task_payload=task_payload,
        run_spec_payload=run_spec_payload,
        run_result_payload=run_result_payload,
        submission_payload=submission_payload,
        **options,
    )


def test_real_git_merge_reaches_shadow_post_merge_without_outbox_write(
    initialized_repository,
    task_payload,
    run_spec_payload,
    run_result_payload,
    submission_payload,
    tmp_path,
) -> None:
    graph = _graph_from_fixtures(
        initialized_repository,
        task_payload,
        run_spec_payload,
        run_result_payload,
        submission_payload,
    )
    reader = _reader(graph)
    snapshot = reader.read_accepted_merge(
        project_id=graph.project_id,
        merge_commit=graph.merge_commit,
        ci=graph.ci,
    )

    assert snapshot is not None
    assert snapshot.accepted_on_protected_default_branch is True
    assert snapshot.attested_subject_incorporated is True
    assert snapshot.subject_head == graph.subject_head

    request = post_merge_request_from_artifact(
        dispatch_artifact=graph.dispatch_artifact,
        merge_commit=graph.merge_commit,
    )
    with RuntimeStore(tmp_path / "runtime.sqlite3") as runtime:
        post_merge = TrustedPostMergeService(
            runtime=runtime,
            accepted=LinearAcceptedResultDeliveryService(reader),
        )
        result = post_merge.process(
            request=request,
            dispatch_artifact=graph.dispatch_artifact,
            actor=_trusted_actor(),
        )

        assert result.state == "shadow_validated"
        assert result.event is not None
        assert result.event.accepted_merge_commit == graph.merge_commit
        assert result.event.ci_subject_head == graph.subject_head
        assert runtime.list_linear_projection_outbox(graph.project_id) == ()


def test_commit_outside_protected_ref_is_observed_but_rejected(
    initialized_repository,
    task_payload,
    run_spec_payload,
    run_result_payload,
    submission_payload,
) -> None:
    graph = _graph_from_fixtures(
        initialized_repository,
        task_payload,
        run_spec_payload,
        run_result_payload,
        submission_payload,
    )
    _git(graph.repository, "switch", "accepted-subject")
    (graph.repository / "outside.txt").write_text("outside protected ref\n", encoding="utf-8")
    outside = _commit(graph.repository, "unmerged accepted descendant")
    outside_tree = _git(graph.repository, "rev-parse", "HEAD^{tree}").stdout.strip()
    ci = graph.ci.model_copy(
        update={"subject_head": outside, "subject_tree": outside_tree}
    )
    reader = _reader(graph)

    snapshot = reader.read_accepted_merge(
        project_id=graph.project_id,
        merge_commit=outside,
        ci=ci,
    )

    assert snapshot is not None
    assert snapshot.accepted_on_protected_default_branch is False
    assert snapshot.attested_subject_incorporated is True
    with pytest.raises(RCPError) as caught:
        LinearAcceptedResultDeliveryService(reader).enqueue(
            actor=_trusted_actor(),
            project_id=graph.project_id,
            merge_commit=outside,
            ci=ci,
        )
    assert caught.value.code == "linear_delivery_not_accepted_merge"


def test_content_equivalent_subject_not_in_merge_is_rejected(
    initialized_repository,
    task_payload,
    run_spec_payload,
    run_result_payload,
    submission_payload,
) -> None:
    graph = _graph_from_fixtures(
        initialized_repository,
        task_payload,
        run_spec_payload,
        run_result_payload,
        submission_payload,
    )
    _git(graph.repository, "switch", "-c", "lookalike-subject", graph.base_commit)
    _git(
        graph.repository,
        "checkout",
        graph.subject_head,
        "--",
        *graph.generated_paths,
    )
    lookalike = _commit(graph.repository, "content-equivalent unmerged subject")
    lookalike_tree = _git(graph.repository, "rev-parse", "HEAD^{tree}").stdout.strip()
    ci = graph.ci.model_copy(
        update={"subject_head": lookalike, "subject_tree": lookalike_tree}
    )

    snapshot = _reader(graph).read_accepted_merge(
        project_id=graph.project_id,
        merge_commit=graph.merge_commit,
        ci=ci,
    )

    assert snapshot is not None
    assert snapshot.accepted_on_protected_default_branch is True
    assert snapshot.attested_subject_incorporated is False


def test_subject_tree_mismatch_fails_closed(
    initialized_repository,
    task_payload,
    run_spec_payload,
    run_result_payload,
    submission_payload,
) -> None:
    graph = _graph_from_fixtures(
        initialized_repository,
        task_payload,
        run_spec_payload,
        run_result_payload,
        submission_payload,
    )
    base_tree = _git(
        graph.repository,
        "rev-parse",
        f"{graph.base_commit}^{{tree}}",
    ).stdout.strip()
    ci = graph.ci.model_copy(update={"subject_tree": base_tree})

    assert (
        _reader(graph).read_accepted_merge(
            project_id=graph.project_id,
            merge_commit=graph.merge_commit,
            ci=ci,
        )
        is None
    )


def test_merge_side_generated_output_bytes_are_not_accepted(
    initialized_repository,
    task_payload,
    run_spec_payload,
    run_result_payload,
    submission_payload,
) -> None:
    graph = _graph_from_fixtures(
        initialized_repository,
        task_payload,
        run_spec_payload,
        run_result_payload,
        submission_payload,
        corrupt_merged_output=True,
    )

    assert (
        _reader(graph).read_accepted_merge(
            project_id=graph.project_id,
            merge_commit=graph.merge_commit,
            ci=graph.ci,
        )
        is None
    )


def test_generated_output_digest_mismatch_fails_closed(
    initialized_repository,
    task_payload,
    run_spec_payload,
    run_result_payload,
    submission_payload,
) -> None:
    graph = _graph_from_fixtures(
        initialized_repository,
        task_payload,
        run_spec_payload,
        run_result_payload,
        submission_payload,
    )
    outputs = list(graph.ci.generated_outputs)
    outputs[0] = outputs[0].model_copy(update={"digest": "sha256:" + "0" * 64})
    payload = graph.ci.model_dump(mode="python")
    payload["generated_outputs"] = outputs
    payload["artifact_digest"] = canonical_digest(
        {"generated_outputs": [item.model_dump(mode="json") for item in outputs]}
    )
    ci = CIValidationAttestation.model_validate(payload)

    assert (
        _reader(graph).read_accepted_merge(
            project_id=graph.project_id,
            merge_commit=graph.merge_commit,
            ci=ci,
        )
        is None
    )


@pytest.mark.parametrize(
    "record_kind",
    ["project", "task", "policy", "submission", "decision", "report"],
)
def test_noncanonical_accepted_records_fail_closed(
    initialized_repository,
    task_payload,
    run_spec_payload,
    run_result_payload,
    submission_payload,
    record_kind: RecordKind,
) -> None:
    graph = _graph_from_fixtures(
        initialized_repository,
        task_payload,
        run_spec_payload,
        run_result_payload,
        submission_payload,
        noncanonical_record=record_kind,
    )

    with pytest.raises(RCPError) as caught:
        _reader(graph).read_accepted_merge(
            project_id=graph.project_id,
            merge_commit=graph.merge_commit,
            ci=graph.ci,
        )

    assert caught.value.code == "linear_accepted_record_invalid"
