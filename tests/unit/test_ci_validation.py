from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest
from typer.testing import CliRunner

from researchctl.adapters._subprocess import (
    CommandResult,
    SubprocessCommandRunner,
)
from researchctl.adapters.git_ci import GitCIObjectReader
from researchctl.cli import app
from researchctl.domain.enums import (
    ClaimScope,
    CodeDisposition,
    ReviewDisposition,
)
from researchctl.domain.models import (
    LinearProjectionPolicy,
    ReportProposal,
    ReportRecord,
    ResearchSubmission,
    RunResult,
    RunSpec,
    TaskRecord,
)
from researchctl.errors import RCPError
from researchctl.serialization import canonical_digest, dump_yaml, load_yaml
from researchctl.services.ci_validation import (
    CIValidationRequest,
    ExactHeadCIValidator,
    LINEAR_PROJECTION_POLICY_PATH,
    write_ci_validation_artifact,
)
from researchctl.services.requests import (
    ReviewAcceptRequest,
    SubmissionCreateRequest,
)
from researchctl.services.run_records import GitRunRecordRepository
from researchctl.services.submission_workflow import SubmissionWorkflowService
from researchctl.services.submissions import (
    SubmissionBundleBuilder,
    SubmissionEvidence,
)
from researchctl.services.task_records import TaskRecordRepository


WORKSPACE_ID = "11111111-1111-4111-8111-111111111111"
TEAM_ID = "22222222-2222-4222-8222-222222222222"
PROJECT_ID = "33333333-3333-4333-8333-333333333333"
ISSUE_ID = "44444444-4444-4444-8444-444444444444"


def _git(
    repository: Path,
    *arguments: str,
    input_text: str | None = None,
    environment: Mapping[str, str] | None = None,
) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
        input=input_text,
        env=dict(environment) if environment is not None else None,
    ).stdout


@dataclass(frozen=True, slots=True)
class _PreparedSubmission:
    repository: Path
    task: TaskRecord
    spec: RunSpec
    result: RunResult
    submission: ResearchSubmission
    proposal: ReportProposal
    base_commit: str
    proposal_head: str
    proposal_message: str
    acceptance_head: str
    acceptance_message: str
    report_path: str
    default_status: str


def _prepare_submission(
    repository: Path,
    *,
    task_payload,
    run_spec_payload,
    run_result_payload,
    submission_payload,
    linear: bool,
) -> _PreparedSubmission:
    task = TaskRecord.model_validate(
        task_payload(state="ready", linear_issue_id=ISSUE_ID if linear else None)
    )
    TaskRecordRepository(repository).create(task)
    if linear:
        policy = LinearProjectionPolicy(
            workspace_id=WORKSPACE_ID,
            team_id=TEAM_ID,
            project_id=PROJECT_ID,
        )
        policy_path = repository / LINEAR_PROJECTION_POLICY_PATH
        policy_path.write_text(dump_yaml(policy), encoding="utf-8")
    (repository / "experiment.py").write_text(
        "print('trusted source')\n",
        encoding="utf-8",
    )
    _git(repository, "add", ".researchctl.toml", ".research", "experiment.py")
    _git(
        repository,
        "-c",
        "user.name=Tests",
        "-c",
        "user.email=tests@example.invalid",
        "commit",
        "--no-gpg-sign",
        "-m",
        "managed task and source",
    )
    base_commit = _git(repository, "rev-parse", "HEAD").strip()
    source_tree = _git(repository, "rev-parse", "HEAD^{tree}").strip()
    spec = RunSpec.model_validate(
        run_spec_payload(
            task_id=task.task_id,
            source_commit=base_commit,
            source_tree=source_tree,
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
    worktrees = repository / ".git" / "researchctl" / "worktrees"
    worktrees.mkdir(parents=True, exist_ok=True)
    GitRunRecordRepository(
        repository_root=repository,
        worktrees_directory=worktrees,
        spec=spec,
    ).collect(result)
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
        report_id="report_20260803T120000Z_" + "9" * 24,
        expected_report_revision=0,
        title="Stopping policy result",
        evidence_tree=source_tree,
    )
    workflow = SubmissionWorkflowService(
        repository_root=repository,
        worktrees_directory=worktrees,
        default_branch="main",
    )
    proposed = workflow.prepare_proposal(
        SubmissionCreateRequest(
            operation_id="operation_20260803T120000Z_" + "1" * 24,
            idempotency_key="ci-proposal",
            base_commit=base_commit,
            submission=submission,
            report_proposal=proposal,
            run_ids=(spec.run_id,),
        ),
        task,
    )
    accepted = workflow.prepare_acceptance(
        ReviewAcceptRequest(
            operation_id="operation_20260803T120000Z_" + "2" * 24,
            idempotency_key="ci-acceptance",
            submission_id=submission.submission_id,
            task_id=task.task_id,
            expected_head=proposed.commit.commit,
            decision_id="decision_20260803T120000Z_" + "3" * 24,
            expected_report_revision=0,
            disposition=ReviewDisposition.ACCEPTED,
            claim_scope=ClaimScope.SNAPSHOT,
            code_disposition=CodeDisposition.RETAIN_ISOLATED,
        ),
        task,
        reviewer_actor="uid-1000",
        decided_at="2026-08-03T12:00:00Z",
    )
    return _PreparedSubmission(
        repository=repository,
        task=task,
        spec=spec,
        result=result,
        submission=submission,
        proposal=proposal,
        base_commit=base_commit,
        proposal_head=proposed.commit.commit,
        proposal_message=_git(
            repository,
            "show",
            "-s",
            "--format=%B",
            proposed.commit.commit,
        ).rstrip("\n"),
        acceptance_head=accepted.commit.commit,
        acceptance_message=_git(
            repository,
            "show",
            "-s",
            "--format=%B",
            accepted.commit.commit,
        ).rstrip("\n"),
        report_path=(
            f".research/reports/{accepted.bundle.report.report_id}/"
            f"{accepted.bundle.report.revision}.yaml"
        ),
        default_status=_git(repository, "status", "--porcelain=v1", "-z"),
    )


def _request(prepared: _PreparedSubmission, head: str) -> CIValidationRequest:
    return CIValidationRequest(
        attestation_id="attestation_20260803T120000Z_" + "4" * 24,
        repository="example/research",
        pull_request_number=17,
        subject_head=head,
        base_commit=prepared.base_commit,
        submission_id=prepared.submission.submission_id,
        generated_at="2026-08-03T12:05:00Z",
    )


class _RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.environments: list[Mapping[str, str]] = []
        self.delegate = SubprocessCommandRunner()

    def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path | None,
        env: Mapping[str, str] | None,
        timeout_seconds: float,
    ) -> CommandResult:
        self.calls.append(argv)
        self.environments.append(dict(env or {}))
        return self.delegate.run(
            argv,
            cwd=cwd,
            env=env,
            timeout_seconds=timeout_seconds,
        )


def test_proposal_ci_reads_only_git_data_and_emits_exact_head_attestation(
    initialized_repository: Path,
    task_payload,
    run_spec_payload,
    run_result_payload,
    submission_payload,
) -> None:
    prepared = _prepare_submission(
        initialized_repository,
        task_payload=task_payload,
        run_spec_payload=run_spec_payload,
        run_result_payload=run_result_payload,
        submission_payload=submission_payload,
        linear=False,
    )
    refs_before = _git(
        prepared.repository,
        "for-each-ref",
        "--format=%(refname)%00%(objectname)",
    )
    runner = _RecordingRunner()
    validator = ExactHeadCIValidator(git=GitCIObjectReader(runner=runner))
    result = validator.validate(
        prepared.repository,
        _request(prepared, prepared.proposal_head),
    )
    accepted_result = validator.validate(
        prepared.repository,
        _request(prepared, prepared.acceptance_head),
    )

    assert result.head_kind == "submission_proposal"
    assert result.attestation.subject_head == prepared.proposal_head
    assert result.attestation.subject_tree == _git(
        prepared.repository,
        "rev-parse",
        f"{prepared.proposal_head}^{{tree}}",
    ).strip()
    assert result.attestation.base_commit == prepared.base_commit
    assert result.attestation.report_id is None
    assert result.attestation.projection.state == "disabled"
    assert result.attestation.projection.reason == "integration_not_configured"
    assert result.linear_body is None
    assert accepted_result.attestation.projection.state == "disabled"
    assert accepted_result.attestation.projection.reason == "integration_not_configured"
    assert accepted_result.linear_body is None
    assert [item.name for item in result.attestation.checks] == sorted(
        item.name for item in result.attestation.checks
    )
    assert [item.path for item in result.attestation.generated_outputs] == sorted(
        item.path for item in result.attestation.generated_outputs
    )
    assert result.attestation_bytes == dump_yaml(result.attestation).encode("utf-8")
    assert {call[5] for call in runner.calls} <= {
        "cat-file",
        "diff-tree",
        "ls-tree",
        "merge-base",
    }
    assert all(env.get("GIT_OPTIONAL_LOCKS") == "0" for env in runner.environments)
    assert all(env.get("GIT_NO_REPLACE_OBJECTS") == "1" for env in runner.environments)
    assert _git(prepared.repository, "status", "--porcelain=v1", "-z") == (
        prepared.default_status
    )
    assert _git(
        prepared.repository,
        "for-each-ref",
        "--format=%(refname)%00%(objectname)",
    ) == refs_before


def test_acceptance_ci_rebuilds_report_and_secretless_linear_preview(
    initialized_repository: Path,
    tmp_path: Path,
    task_payload,
    run_spec_payload,
    run_result_payload,
    submission_payload,
) -> None:
    prepared = _prepare_submission(
        initialized_repository,
        task_payload=task_payload,
        run_spec_payload=run_spec_payload,
        run_result_payload=run_result_payload,
        submission_payload=submission_payload,
        linear=True,
    )
    result = ExactHeadCIValidator().validate(
        prepared.repository,
        _request(prepared, prepared.acceptance_head),
    )

    assert result.head_kind == "acceptance_prepared"
    assert result.attestation.report_id == prepared.proposal.report_id
    assert result.attestation.report_revision == 1
    assert result.attestation.decision_digest is not None
    assert result.attestation.report_digest is not None
    assert result.attestation.projection.state == "configured"
    assert result.attestation.projection.workspace_id == WORKSPACE_ID
    assert result.attestation.projection.team_id == TEAM_ID
    assert result.attestation.projection.project_id == PROJECT_ID
    assert result.attestation.projection.issue_id == ISSUE_ID
    assert result.linear_body is not None
    assert result.attestation.projection.payload_digest == (
        "sha256:" + hashlib.sha256(result.linear_body).hexdigest()
    )
    assert prepared.report_path in {
        item.path for item in result.attestation.generated_outputs
    }
    report_markdown_path = prepared.report_path.removesuffix(".yaml") + ".md"
    output_digests = {
        item.path: item.digest for item in result.attestation.generated_outputs
    }
    assert report_markdown_path in output_digests
    assert result.attestation.report_preview_digest == output_digests[report_markdown_path]
    artifact = tmp_path / "ci-validation-attestation.yaml"
    first = write_ci_validation_artifact(result, artifact)
    repeated = write_ci_validation_artifact(result, artifact)
    assert first.created is True
    assert repeated.created is False
    assert first.content_digest == repeated.content_digest
    assert artifact.read_bytes() == result.attestation_bytes

    conflict = tmp_path / "conflict.yaml"
    conflict.write_text("different\n", encoding="utf-8")
    with pytest.raises(RCPError) as existing:
        write_ci_validation_artifact(result, conflict)
    assert existing.value.code == "ci_artifact_conflict"

    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(RCPError) as unsafe:
        write_ci_validation_artifact(result, linked_parent / "attestation.yaml")
    assert unsafe.value.code == "ci_artifact_path_invalid"
    assert _git(prepared.repository, "status", "--porcelain=v1", "-z") == (
        prepared.default_status
    )


def _tree_with_updates(
    prepared: _PreparedSubmission,
    tmp_path: Path,
    *,
    start_tree: str,
    files: Mapping[str, tuple[str, bytes]],
) -> str:
    index = tmp_path / f"ci-forge-{len(tuple(tmp_path.iterdir()))}.index"
    environment = os.environ.copy()
    environment["GIT_INDEX_FILE"] = str(index)
    _git(prepared.repository, "read-tree", start_tree, environment=environment)
    try:
        for path, (mode, content) in files.items():
            blob = _git(
                prepared.repository,
                "hash-object",
                "-w",
                "--stdin",
                input_text=content.decode("utf-8"),
                environment=environment,
            ).strip()
            _git(
                prepared.repository,
                "update-index",
                "--add",
                "--cacheinfo",
                mode,
                blob,
                path,
                environment=environment,
            )
        return _git(
            prepared.repository,
            "write-tree",
            environment=environment,
        ).strip()
    finally:
        index.unlink(missing_ok=True)


def _commit_tree(
    prepared: _PreparedSubmission,
    *,
    tree: str,
    parent: str,
    message: str,
) -> str:
    return _git(
        prepared.repository,
        "-c",
        "user.name=Tests",
        "-c",
        "user.email=tests@example.invalid",
        "commit-tree",
        tree,
        "-p",
        parent,
        "-m",
        message,
    ).strip()


def test_ci_rejects_extra_path_symlink_mode_and_semantic_report_tampering(
    initialized_repository: Path,
    tmp_path: Path,
    task_payload,
    run_spec_payload,
    run_result_payload,
    submission_payload,
) -> None:
    prepared = _prepare_submission(
        initialized_repository,
        task_payload=task_payload,
        run_spec_payload=run_spec_payload,
        run_result_payload=run_result_payload,
        submission_payload=submission_payload,
        linear=False,
    )
    validator = ExactHeadCIValidator()
    proposal_tree = _git(
        prepared.repository,
        "rev-parse",
        f"{prepared.proposal_head}^{{tree}}",
    ).strip()
    bundle_root = f".research/submissions/{prepared.submission.submission_id}"

    extra_tree = _tree_with_updates(
        prepared,
        tmp_path,
        start_tree=proposal_tree,
        files={f"{bundle_root}/rogue.yaml": ("100644", b"rogue: true\n")},
    )
    extra_head = _commit_tree(
        prepared,
        tree=extra_tree,
        parent=prepared.base_commit,
        message=prepared.proposal_message,
    )
    with pytest.raises(RCPError) as extra:
        validator.validate(prepared.repository, _request(prepared, extra_head))
    assert extra.value.code == "ci_changed_path_scope_invalid"

    symlink_tree = _tree_with_updates(
        prepared,
        tmp_path,
        start_tree=proposal_tree,
        files={f"{bundle_root}/report-preview.md": ("120000", b"../../outside\n")},
    )
    symlink_head = _commit_tree(
        prepared,
        tree=symlink_tree,
        parent=prepared.base_commit,
        message=prepared.proposal_message,
    )
    with pytest.raises(RCPError) as symlink:
        validator.validate(prepared.repository, _request(prepared, symlink_head))
    assert symlink.value.code == "ci_tree_entry_invalid"

    acceptance_tree = _git(
        prepared.repository,
        "rev-parse",
        f"{prepared.acceptance_head}^{{tree}}",
    ).strip()
    executable_tree = _tree_with_updates(
        prepared,
        tmp_path,
        start_tree=acceptance_tree,
        files={
            prepared.report_path: (
                "100755",
                _git(
                    prepared.repository,
                    "show",
                    f"{prepared.acceptance_head}:{prepared.report_path}",
                ).encode("utf-8"),
            )
        },
    )
    executable_head = _commit_tree(
        prepared,
        tree=executable_tree,
        parent=prepared.proposal_head,
        message=prepared.acceptance_message,
    )
    with pytest.raises(RCPError) as executable:
        validator.validate(prepared.repository, _request(prepared, executable_head))
    assert executable.value.code == "ci_tree_entry_invalid"

    report_markdown_path = prepared.report_path.removesuffix(".yaml") + ".md"
    markdown_tree = _tree_with_updates(
        prepared,
        tmp_path,
        start_tree=acceptance_tree,
        files={
            report_markdown_path: (
                "100644",
                b"# Hand-edited accepted claim\n",
            )
        },
    )
    markdown_head = _commit_tree(
        prepared,
        tree=markdown_tree,
        parent=prepared.proposal_head,
        message=prepared.acceptance_message,
    )
    with pytest.raises(RCPError) as markdown:
        validator.validate(prepared.repository, _request(prepared, markdown_head))
    assert markdown.value.code == "ci_generated_output_mismatch"
    assert markdown.value.context == {"path": report_markdown_path}

    report = ReportRecord.model_validate(
        load_yaml(
            _git(
                prepared.repository,
                "show",
                f"{prepared.acceptance_head}:{prepared.report_path}",
            )
        )
    )
    forged_report = ReportRecord.model_validate(
        {
            **report.model_dump(mode="json", exclude_none=True),
            "accepted_at_main_tree": "f" * 40,
        }
    )
    semantic_tree = _tree_with_updates(
        prepared,
        tmp_path,
        start_tree=acceptance_tree,
        files={
            prepared.report_path: (
                "100644",
                dump_yaml(forged_report).encode("utf-8"),
            )
        },
    )
    semantic_head = _commit_tree(
        prepared,
        tree=semantic_tree,
        parent=prepared.proposal_head,
        message=prepared.acceptance_message,
    )
    with pytest.raises(RCPError) as semantic:
        validator.validate(prepared.repository, _request(prepared, semantic_head))
    assert semantic.value.code == "ci_acceptance_linkage_invalid"


def test_ci_rejects_self_consistent_bundle_with_false_run_source_tree(
    initialized_repository: Path,
    tmp_path: Path,
    task_payload,
    run_spec_payload,
    run_result_payload,
    submission_payload,
) -> None:
    prepared = _prepare_submission(
        initialized_repository,
        task_payload=task_payload,
        run_spec_payload=run_spec_payload,
        run_result_payload=run_result_payload,
        submission_payload=submission_payload,
        linear=False,
    )
    spec_payload = prepared.spec.model_dump(mode="json", exclude_none=True)
    spec_payload["source_tree"] = "f" * 40
    spec_payload.pop("spec_digest")
    spec_payload["spec_digest"] = canonical_digest(spec_payload)
    forged_spec = RunSpec.model_validate(spec_payload)
    forged_result = RunResult.model_validate(
        {
            **prepared.result.model_dump(mode="json", exclude_none=True),
            "run_spec_digest": forged_spec.spec_digest,
        }
    )
    forged_proposal = ReportProposal.model_validate(
        {
            **prepared.proposal.model_dump(mode="json", exclude_none=True),
            "evidence_tree": forged_spec.source_tree,
        }
    )
    forged_bundle = SubmissionBundleBuilder().build(
        task=prepared.task,
        submission=prepared.submission,
        proposal=forged_proposal,
        evidence=(SubmissionEvidence(forged_spec, forged_result),),
    )
    base_tree = _git(
        prepared.repository,
        "rev-parse",
        f"{prepared.base_commit}^{{tree}}",
    ).strip()
    forged_tree = _tree_with_updates(
        prepared,
        tmp_path,
        start_tree=base_tree,
        files={item.path: ("100644", item.content) for item in forged_bundle.files},
    )
    forged_head = _commit_tree(
        prepared,
        tree=forged_tree,
        parent=prepared.base_commit,
        message=prepared.proposal_message,
    )

    with pytest.raises(RCPError) as raised:
        ExactHeadCIValidator().validate(
            prepared.repository,
            _request(prepared, forged_head),
        )
    assert raised.value.code == "ci_run_source_mismatch"


def test_ci_rejects_self_consistent_bundle_with_out_of_scope_run_source(
    initialized_repository: Path,
    tmp_path: Path,
    task_payload,
    run_spec_payload,
    run_result_payload,
    submission_payload,
) -> None:
    prepared = _prepare_submission(
        initialized_repository,
        task_payload=task_payload,
        run_spec_payload=run_spec_payload,
        run_result_payload=run_result_payload,
        submission_payload=submission_payload,
        linear=False,
    )
    base_tree = _git(
        prepared.repository,
        "rev-parse",
        f"{prepared.base_commit}^{{tree}}",
    ).strip()
    source_tree = _tree_with_updates(
        prepared,
        tmp_path,
        start_tree=base_tree,
        files={"outside-task.txt": ("100644", b"not task-owned\n")},
    )
    source_commit = _commit_tree(
        prepared,
        tree=source_tree,
        parent=prepared.base_commit,
        message="agent source outside Task scope",
    )
    spec_payload = prepared.spec.model_dump(mode="json", exclude_none=True)
    spec_payload.update(
        {
            "baseline_commit": prepared.base_commit,
            "source_commit": source_commit,
            "source_tree": source_tree,
        }
    )
    spec_payload.pop("spec_digest")
    spec_payload["spec_digest"] = canonical_digest(spec_payload)
    forged_spec = RunSpec.model_validate(spec_payload)
    forged_result = RunResult.model_validate(
        {
            **prepared.result.model_dump(mode="json", exclude_none=True),
            "run_spec_digest": forged_spec.spec_digest,
        }
    )
    forged_proposal = ReportProposal.model_validate(
        {
            **prepared.proposal.model_dump(mode="json", exclude_none=True),
            "evidence_tree": source_tree,
        }
    )
    forged_bundle = SubmissionBundleBuilder().build(
        task=prepared.task,
        submission=prepared.submission,
        proposal=forged_proposal,
        evidence=(SubmissionEvidence(forged_spec, forged_result),),
    )
    forged_tree = _tree_with_updates(
        prepared,
        tmp_path,
        start_tree=base_tree,
        files={item.path: ("100644", item.content) for item in forged_bundle.files},
    )
    forged_head = _commit_tree(
        prepared,
        tree=forged_tree,
        parent=prepared.base_commit,
        message=prepared.proposal_message,
    )

    with pytest.raises(RCPError) as raised:
        ExactHeadCIValidator().validate(
            prepared.repository,
            _request(prepared, forged_head),
        )

    assert raised.value.code == "write_scope_violation"
    assert raised.value.context["violations"] == [
        {
            "path": "outside-task.txt",
            "reason": "outside_allowed_write_paths",
            "status": "A",
        }
    ]


def test_ci_cli_human_and_strict_json_emit_the_same_attestation(
    initialized_repository: Path,
    tmp_path: Path,
    task_payload,
    run_spec_payload,
    run_result_payload,
    submission_payload,
) -> None:
    prepared = _prepare_submission(
        initialized_repository,
        task_payload=task_payload,
        run_spec_payload=run_spec_payload,
        run_result_payload=run_result_payload,
        submission_payload=submission_payload,
        linear=False,
    )
    request = _request(prepared, prepared.acceptance_head)
    human_artifact = tmp_path / "human-attestation.yaml"
    machine_artifact = tmp_path / "machine-attestation.yaml"
    runner = CliRunner()
    human = runner.invoke(
        app,
        [
            "ci",
            "validate",
            "--project",
            str(prepared.repository),
            "--artifact",
            str(human_artifact),
            "--repository",
            request.repository,
            "--pull-request-number",
            str(request.pull_request_number),
            "--subject-head",
            request.subject_head,
            "--base-commit",
            request.base_commit,
            "--submission-id",
            request.submission_id,
            "--attestation-id",
            request.attestation_id,
            "--generated-at",
            "2026-08-03T12:05:00Z",
        ],
    )
    assert human.exit_code == 0, human.output
    assert f"Attestation: {request.attestation_id}" in human.output
    assert "Result: passed" in human.output

    machine = runner.invoke(
        app,
        [
            "ci",
            "validate",
            "--json",
            "--project",
            str(prepared.repository),
            "--artifact",
            str(machine_artifact),
        ],
        input=request.model_dump_json(),
    )
    assert machine.exit_code == 0, machine.output
    payload = json.loads(machine.output)
    assert payload["command"] == "ci.validate"
    assert payload["success"] is True
    assert payload["data"]["attestation"]["subject_head"] == request.subject_head
    assert payload["data"]["artifact"]["path"] == str(machine_artifact)
    assert human_artifact.read_bytes() == machine_artifact.read_bytes()

    forged_payload = request.model_dump(mode="json")
    forged_payload["validator_version"] = "agent-selected"
    rejected_artifact = tmp_path / "rejected-attestation.yaml"
    rejected = runner.invoke(
        app,
        [
            "ci",
            "validate",
            "--json",
            "--project",
            str(prepared.repository),
            "--artifact",
            str(rejected_artifact),
        ],
        input=json.dumps(forged_payload),
    )
    assert rejected.exit_code == 2
    error = json.loads(rejected.output)
    assert error["errors"][0]["code"] == "validation_error"
    assert not rejected_artifact.exists()
