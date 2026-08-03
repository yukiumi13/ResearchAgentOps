from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from researchctl.domain.enums import (
    ClaimScope,
    CodeDisposition,
    ReviewDisposition,
)
from researchctl.domain.models import (
    ReportProposal,
    ResearchSubmission,
    RunResult,
    RunSpec,
    TaskRecord,
)
from researchctl.errors import RCPError
from researchctl.services.requests import (
    ReviewAcceptRequest,
    SubmissionCreateRequest,
)
from researchctl.services.run_records import GitRunRecordRepository
from researchctl.services.submission_workflow import SubmissionWorkflowService
from researchctl.services.task_records import TaskRecordRepository


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def test_real_submission_and_acceptance_are_isolated_atomic_and_reproducible(
    initialized_repository: Path,
    task_payload,
    run_spec_payload,
    run_result_payload,
    submission_payload,
) -> None:
    repository = initialized_repository
    task = TaskRecord.model_validate(task_payload(state="ready"))
    TaskRecordRepository(repository).create(task)
    (repository / "experiment.py").write_text("print('evidence')\n", encoding="utf-8")
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
        "managed source and task",
    )
    base_commit = _git(repository, "rev-parse", "HEAD").strip()
    worktrees = repository / ".git" / "researchctl" / "worktrees"
    worktrees.mkdir(parents=True, exist_ok=True)
    session_id = run_spec_payload()["session_id"]
    session_worktree = worktrees / session_id
    _git(
        repository,
        "worktree",
        "add",
        "-b",
        f"research/task/{task.key}/{session_id}",
        str(session_worktree),
        base_commit,
    )
    source_path = session_worktree / "src" / "training" / "candidate.py"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("candidate = True\n", encoding="utf-8")
    source_commit = _commit(session_worktree, "allowed Session source")
    source_tree = _git(session_worktree, "rev-parse", "HEAD^{tree}").strip()
    spec = RunSpec.model_validate(
        run_spec_payload(
            task_id=task.task_id,
            session_id=session_id,
            source_commit=source_commit,
            source_tree=source_tree,
            baseline_commit=base_commit,
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
        run_result_payload(run_spec_digest=spec.spec_digest)
    )
    run_records = GitRunRecordRepository(
        repository_root=repository,
        worktrees_directory=worktrees,
        spec=spec,
    )
    collected = run_records.collect(result)
    default_status = _git(repository, "status", "--porcelain=v1", "-z")
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
        report_id="report_20260802T123456Z_" + "9" * 24,
        expected_report_revision=0,
        title="Stopping policy result",
        evidence_tree=source_tree,
    )
    workflow = SubmissionWorkflowService(
        repository_root=repository,
        worktrees_directory=worktrees,
        default_branch="main",
    )
    create = SubmissionCreateRequest(
        operation_id="operation_20260803T120000Z_" + "1" * 24,
        idempotency_key="submit-result",
        base_commit=base_commit,
        submission=submission,
        report_proposal=proposal,
        run_ids=(spec.run_id,),
    )

    first = workflow.propose(create, task)
    repeated = workflow.propose(create, task)

    assert first.commit.commit == repeated.commit.commit
    assert first.commit.changed is True
    assert repeated.commit.changed is False
    assert first.evidence_commits == (
        {
            "run_id": spec.run_id,
            "spec_commit": collected.spec_commit,
            "result_commit": collected.result_commit,
        },
    )
    assert first.source_scopes == (
        {
            "run_id": spec.run_id,
            "trusted_base_commit": base_commit,
            "baseline_commit": base_commit,
            "source_commit": source_commit,
            "paths": ["src/training/candidate.py"],
        },
    )
    assert _git(repository, "rev-parse", "HEAD").strip() == base_commit
    assert _git(repository, "status", "--porcelain=v1", "-z") == default_status

    accept = ReviewAcceptRequest(
        operation_id="operation_20260803T120000Z_" + "2" * 24,
        idempotency_key="accept-result",
        submission_id=submission.submission_id,
        task_id=task.task_id,
        expected_head=first.commit.commit,
        decision_id="decision_20260803T120000Z_" + "3" * 24,
        expected_report_revision=0,
        disposition=ReviewDisposition.ACCEPTED,
        claim_scope=ClaimScope.SNAPSHOT,
        code_disposition=CodeDisposition.RETAIN_ISOLATED,
    )
    accepted = workflow.prepare_acceptance(
        accept,
        task,
        reviewer_actor="uid-1000",
        decided_at="2026-08-03T12:00:00Z",
    )

    assert accepted.terminal_result == "acceptance_prepared"
    assert accepted.bundle.submission.state.value == "accepted"
    assert accepted.bundle.report.revision == 1
    assert accepted.commit.parent_commit == first.commit.commit
    assert _git(
        repository,
        "rev-list",
        "--count",
        f"{base_commit}..{accepted.commit.branch}",
    ).strip() == "2"
    changed = _git(
        repository,
        "diff-tree",
        "--no-commit-id",
        "--name-only",
        "-r",
        accepted.commit.commit,
    ).splitlines()
    assert sorted(changed) == sorted(accepted.commit.paths)
    assert _git(repository, "rev-parse", "HEAD").strip() == base_commit
    assert _git(repository, "status", "--porcelain=v1", "-z") == default_status


def _commit(repository: Path, message: str) -> str:
    _git(repository, "add", "-A")
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
    return _git(repository, "rev-parse", "HEAD").strip()


def test_submission_rejects_out_of_scope_source_before_proposal_side_effects(
    initialized_repository: Path,
    task_payload,
    run_spec_payload,
    run_result_payload,
    submission_payload,
) -> None:
    repository = initialized_repository
    task = TaskRecord.model_validate(task_payload(state="ready"))
    TaskRecordRepository(repository).create(task)
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
        "managed task baseline",
    )
    base_commit = _git(repository, "rev-parse", "HEAD").strip()
    worktrees = repository / ".git" / "researchctl" / "worktrees"
    worktrees.mkdir(parents=True, exist_ok=True)
    session_id = run_spec_payload()["session_id"]
    session_worktree = worktrees / session_id
    _git(
        repository,
        "worktree",
        "add",
        "-b",
        f"research/task/{task.key}/{session_id}",
        str(session_worktree),
        base_commit,
    )
    (session_worktree / "outside-task.txt").write_text(
        "not task-owned\n",
        encoding="utf-8",
    )
    source_commit = _commit(session_worktree, "out-of-scope Session source")
    source_tree = _git(session_worktree, "rev-parse", "HEAD^{tree}").strip()
    spec = RunSpec.model_validate(
        run_spec_payload(
            task_id=task.task_id,
            session_id=session_id,
            source_commit=source_commit,
            source_tree=source_tree,
            baseline_commit=base_commit,
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
        run_result_payload(run_spec_digest=spec.spec_digest)
    )
    GitRunRecordRepository(
        repository_root=repository,
        worktrees_directory=worktrees,
        spec=spec,
    ).collect(result)
    submission = ResearchSubmission.model_validate(
        submission_payload(
            task_id=task.task_id,
            session_id=session_id,
            state="open",
            run_result_ids=[result.result_id],
        )
    )
    proposal = ReportProposal(
        submission_id=submission.submission_id,
        report_id="report_20260802T123456Z_" + "9" * 24,
        expected_report_revision=0,
        title="Out-of-scope result",
        evidence_tree=source_tree,
    )
    request = SubmissionCreateRequest(
        operation_id="operation_20260803T120000Z_" + "1" * 24,
        idempotency_key="reject-out-of-scope",
        base_commit=base_commit,
        submission=submission,
        report_proposal=proposal,
        run_ids=(spec.run_id,),
    )
    workflow = SubmissionWorkflowService(
        repository_root=repository,
        worktrees_directory=worktrees,
        default_branch="main",
    )
    refs_before = _git(
        repository,
        "for-each-ref",
        "--format=%(refname)%00%(objectname)",
    )

    with pytest.raises(RCPError) as raised:
        workflow.propose(request, task)

    assert raised.value.code == "write_scope_violation"
    assert raised.value.context["violations"] == [
        {
            "path": "outside-task.txt",
            "reason": "outside_allowed_write_paths",
            "status": "A",
        }
    ]
    assert _git(
        repository,
        "for-each-ref",
        "--format=%(refname)%00%(objectname)",
    ) == refs_before
    assert not (
        worktrees / f"submission-{submission.submission_id}"
    ).exists()
