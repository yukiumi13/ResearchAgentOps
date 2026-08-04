from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from pydantic import BaseModel, ValidationError

from researchctl.adapters._subprocess import (
    CommandResult,
    CommandRunner,
    SubprocessCommandRunner,
)
from researchctl.adapters.git_evidence import GitRunEvidenceReader
from researchctl.adapters.git_scope import GitWriteScopeValidator
from researchctl.adapters.git_submission import SubmissionCommitReceipt
from researchctl.adapters.git_worktree import GitWorktreeAdapter
from researchctl.domain.models import (
    ReportProposal,
    ReportRecord,
    ResearchSubmission,
    RunResult,
    RunSpec,
    TaskRecord,
)
from researchctl.errors import RCPError
from researchctl.serialization import dump_yaml, load_yaml
from researchctl.services.requests import (
    ReviewAcceptRequest,
    SubmissionCreateRequest,
)
from researchctl.services.review_acceptance import (
    AcceptanceBundle,
    ReviewAcceptanceBuilder,
)
from researchctl.services.submission_records import SubmissionRecordRepository
from researchctl.services.submission_delivery import (
    SubmissionBranchDelivery,
    SubmissionDeliveryPort,
    SubmissionPullRequestReceipt,
    render_submission_pull_request,
)
from researchctl.services.submissions import (
    SubmissionBundle,
    SubmissionBundleBuilder,
    SubmissionEvidence,
)


_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_PROPOSAL_MESSAGE = re.compile(
    r"^researchctl: submission\.create "
    r"(submission_\d{8}T\d{6}Z_[0-9a-f]{24}) "
    r"(operation_\d{8}T\d{6}Z_[0-9a-f]{24})$"
)
_MAX_RECORD_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class PreparedSubmissionProposal:
    bundle: SubmissionBundle
    commit: SubmissionCommitReceipt
    evidence_commits: tuple[dict[str, str], ...]
    source_scopes: tuple[dict[str, object], ...]


@dataclass(frozen=True, slots=True)
class SubmissionProposalReceipt:
    prepared: PreparedSubmissionProposal
    branch_delivery: SubmissionBranchDelivery
    pull_request: SubmissionPullRequestReceipt

    @property
    def bundle(self) -> SubmissionBundle:
        return self.prepared.bundle

    @property
    def commit(self) -> SubmissionCommitReceipt:
        return self.prepared.commit

    @property
    def evidence_commits(self) -> tuple[dict[str, str], ...]:
        return self.prepared.evidence_commits

    @property
    def source_scopes(self) -> tuple[dict[str, object], ...]:
        return self.prepared.source_scopes

    @property
    def terminal_result(self) -> str:
        return "proposal_open"

    def as_dict(self) -> dict[str, object]:
        return {
            "terminal_result": self.terminal_result,
            "bundle": self.bundle.as_dict(),
            "proposal": self.commit.as_dict(),
            "delivery": {
                "branch": self.branch_delivery.as_dict(),
                "pull_request": self.pull_request.as_dict(),
            },
            "evidence_commits": list(self.evidence_commits),
            "source_scopes": list(self.source_scopes),
            "accepted": False,
            "requires_review": True,
        }


@dataclass(frozen=True, slots=True)
class ReviewAcceptanceReceipt:
    bundle: AcceptanceBundle
    commit: SubmissionCommitReceipt

    @property
    def terminal_result(self) -> str:
        return "acceptance_prepared"

    def as_dict(self) -> dict[str, object]:
        return {
            "terminal_result": self.terminal_result,
            "bundle": self.bundle.as_dict(),
            "proposal": self.commit.as_dict(),
            "accepted": False,
            "requires_exact_head_ci": True,
            "requires_codeowner_approval": True,
            "requires_merge": True,
        }


class SubmissionWorkflowService:
    def __init__(
        self,
        *,
        repository_root: Path,
        worktrees_directory: Path,
        default_branch: str,
        evidence: GitRunEvidenceReader | None = None,
        records: SubmissionRecordRepository | None = None,
        submissions: SubmissionBundleBuilder | None = None,
        acceptance: ReviewAcceptanceBuilder | None = None,
        git: GitWorktreeAdapter | None = None,
        write_scope: GitWriteScopeValidator | None = None,
        delivery: SubmissionDeliveryPort | None = None,
        runner: CommandRunner | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.repository_root = repository_root.resolve()
        self.worktrees_directory = worktrees_directory.resolve()
        self.default_branch = default_branch
        self.evidence = evidence or GitRunEvidenceReader()
        self.records = records or SubmissionRecordRepository(
            repository_root=self.repository_root,
            worktrees_directory=self.worktrees_directory,
        )
        self.submissions = submissions or SubmissionBundleBuilder()
        self.acceptance = acceptance or ReviewAcceptanceBuilder(self.submissions)
        self.git = git or GitWorktreeAdapter()
        self.delivery = delivery
        self._runner = runner or SubprocessCommandRunner()
        self._timeout_seconds = timeout_seconds
        self.write_scope = write_scope or GitWriteScopeValidator(
            runner=self._runner,
            timeout_seconds=timeout_seconds,
        )

    def propose(
        self,
        request: SubmissionCreateRequest,
        task: TaskRecord,
        *,
        event_callback: Callable[[str, dict[str, object]], None] | None = None,
    ) -> SubmissionProposalReceipt:
        if self.delivery is None:
            raise RCPError(
                code="submission_delivery_not_configured",
                message="Submission GitHub delivery is not configured.",
            )
        prepared = self.prepare_proposal(request, task)
        self._event(
            event_callback,
            "submission_proposal_prepared",
            {
                "submission_id": request.submission.submission_id,
                "base_commit": request.base_commit,
                "proposal_commit": prepared.commit.commit,
            },
        )
        branch = self.delivery.push_exact(
            repository_root=self.repository_root,
            branch=prepared.commit.branch,
            commit=prepared.commit.commit,
        )
        self._event(
            event_callback,
            "submission_branch_pushed",
            {
                "submission_id": request.submission.submission_id,
                "branch": branch.branch,
                "proposal_commit": branch.commit,
                "effect_applied": branch.pushed,
            },
        )
        title, body = render_submission_pull_request(
            task=task,
            submission=request.submission,
            proposal=request.report_proposal,
            bundle=prepared.bundle,
            proposal_commit=prepared.commit.commit,
        )
        pull_request = self.delivery.open_or_observe(
            submission_id=request.submission.submission_id,
            branch=branch,
            base_branch=self.default_branch,
            title=title,
            body=body,
        )
        self._event(
            event_callback,
            (
                "submission_pr_created"
                if pull_request.created
                else "submission_pr_observed"
            ),
            {
                "submission_id": request.submission.submission_id,
                "repository": pull_request.repository,
                "pull_request_number": pull_request.number,
                "base_branch": pull_request.base_branch,
                "head_branch": pull_request.head_branch,
                "proposal_commit": pull_request.head_commit,
            },
        )
        return SubmissionProposalReceipt(
            prepared=prepared,
            branch_delivery=branch,
            pull_request=pull_request,
        )

    def prepare_proposal(
        self,
        request: SubmissionCreateRequest,
        task: TaskRecord,
    ) -> PreparedSubmissionProposal:
        default_head = self.git.resolve_commit(
            self.repository_root,
            f"refs/heads/{self.default_branch}",
        )
        if default_head != request.base_commit:
            raise RCPError(
                code="stale_submission_base",
                message="Submission base is not the current protected default head.",
                context={
                    "expected_base": request.base_commit,
                    "observed_default_head": default_head,
                },
            )
        observed = tuple(
            self.evidence.read(self.repository_root, run_id)
            for run_id in request.run_ids
        )
        bundle = self.submissions.build(
            task=task,
            submission=request.submission,
            proposal=request.report_proposal,
            evidence=tuple(
                SubmissionEvidence(item.spec, item.result) for item in observed
            ),
        )
        source_scopes = tuple(
            {
                "run_id": item.spec.run_id,
                **self.write_scope.validate_source(
                    task=task,
                    repository_root=self.repository_root,
                    trusted_base_commit=request.base_commit,
                    baseline_commit=item.spec.baseline_commit,
                    source_commit=item.spec.source_commit,
                ).as_dict(),
            }
            for item in observed
        )
        committed = self.records.write_proposal(
            operation_id=request.operation_id,
            base_commit=request.base_commit,
            bundle=bundle,
        )
        evidence_commits = tuple(
            {
                "run_id": item.spec.run_id,
                "spec_commit": item.spec_commit,
                "result_commit": item.result_commit,
            }
            for item in observed
        )
        return PreparedSubmissionProposal(
            bundle=bundle,
            commit=committed,
            evidence_commits=evidence_commits,
            source_scopes=source_scopes,
        )

    @staticmethod
    def _event(
        callback: Callable[[str, dict[str, object]], None] | None,
        kind: str,
        payload: dict[str, object],
    ) -> None:
        if callback is not None:
            callback(kind, payload)

    def prepare_acceptance(
        self,
        request: ReviewAcceptRequest,
        task: TaskRecord,
        *,
        reviewer_actor: str,
        decided_at: datetime,
    ) -> ReviewAcceptanceReceipt:
        branch = f"refs/heads/research/submission/{request.submission_id}"
        observed_head = self.git.resolve_commit(self.repository_root, branch)
        if observed_head != request.expected_head:
            raise RCPError(
                code="stale_submission_head",
                message="Submission branch changed after manager review.",
                context={
                    "expected_head": request.expected_head,
                    "observed_head": observed_head,
                },
            )
        submission, proposal, evidence, open_bundle, base_commit = self._load_open_bundle(
            task=task,
            submission_id=request.submission_id,
            commit=request.expected_head,
        )
        if request.expected_report_revision != proposal.expected_report_revision:
            raise RCPError(
                code="stale_report_revision",
                message="Review request does not match the proposed Report revision.",
            )
        current_report = self._current_report(
            commit=base_commit,
            proposal=proposal,
        )
        base_tree = self._resolve(f"{base_commit}^{{tree}}")
        accepted = self.acceptance.build(
            task=task,
            submission=submission,
            proposal=proposal,
            evidence=evidence,
            current_report=current_report,
            decision_id=request.decision_id,
            reviewer_actor=reviewer_actor,
            decided_at=decided_at,
            disposition=request.disposition,
            conditions=request.conditions,
            claim_scope=request.claim_scope,
            code_disposition=request.code_disposition,
            accepted_base_tree=base_tree,
        )
        committed = self.records.write_acceptance(
            operation_id=request.operation_id,
            expected_head=request.expected_head,
            bundle=accepted,
            expected_open_submission=dump_yaml(submission).encode("utf-8"),
        )
        return ReviewAcceptanceReceipt(bundle=accepted, commit=committed)

    def _load_open_bundle(
        self,
        *,
        task: TaskRecord,
        submission_id: str,
        commit: str,
    ) -> tuple[
        ResearchSubmission,
        ReportProposal,
        tuple[SubmissionEvidence, ...],
        SubmissionBundle,
        str,
    ]:
        root = f".research/submissions/{submission_id}"
        entries = self._tree_entries(commit, root)
        submission = self._record(
            commit,
            f"{root}/submission.yaml",
            ResearchSubmission,
        )
        proposal = self._record(
            commit,
            f"{root}/proposed-report.yaml",
            ReportProposal,
        )
        evidence_paths = [
            path
            for path in entries
            if path.startswith(f"{root}/evidence/") and path.endswith("/spec.yaml")
        ]
        evidence: list[SubmissionEvidence] = []
        for spec_path in sorted(evidence_paths):
            result_path = spec_path.removesuffix("spec.yaml") + "result.yaml"
            spec = self._record(commit, spec_path, RunSpec)
            result = self._record(commit, result_path, RunResult)
            evidence.append(SubmissionEvidence(spec, result))
        bundle = self.submissions.build(
            task=task,
            submission=submission,
            proposal=proposal,
            evidence=tuple(evidence),
        )
        expected = {item.path: item.content for item in bundle.files}
        if set(entries) != set(expected):
            raise RCPError(
                code="submission_file_set_invalid",
                message="Submission proposal contains an unexpected generated file set.",
                context={
                    "missing": sorted(set(expected) - set(entries)),
                    "unexpected": sorted(set(entries) - set(expected)),
                },
            )
        for path, content in expected.items():
            if self._show(commit, path).encode("utf-8") != content:
                raise RCPError(
                    code="submission_generated_output_mismatch",
                    message="Submission generated output is not reproducible.",
                    context={"path": path},
                )
        base_commit = self._verify_proposal_commit(
            commit=commit,
            submission_id=submission_id,
            expected_paths=tuple(expected),
        )
        return submission, proposal, tuple(evidence), bundle, base_commit

    def _verify_proposal_commit(
        self,
        *,
        commit: str,
        submission_id: str,
        expected_paths: tuple[str, ...],
    ) -> str:
        parents = self._git(
            "show",
            "-s",
            "--format=%P",
            commit,
        ).stdout.strip().split()
        if len(parents) != 1 or not _OBJECT_ID.fullmatch(parents[0]):
            raise RCPError(
                code="submission_proposal_commit_invalid",
                message="Submission proposal must have exactly one protected-base parent.",
            )
        message = self._git("show", "-s", "--format=%B", commit).stdout.rstrip("\n")
        matched = _PROPOSAL_MESSAGE.fullmatch(message)
        if matched is None or matched.group(1) != submission_id:
            raise RCPError(
                code="submission_proposal_commit_invalid",
                message="Submission proposal commit marker is invalid.",
            )
        changed = self._git(
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "-r",
            "-z",
            commit,
        ).stdout
        observed = tuple(sorted(item for item in changed.split("\x00") if item))
        if observed != tuple(sorted(expected_paths)):
            raise RCPError(
                code="submission_proposal_commit_invalid",
                message="Submission proposal commit changed an unexpected path set.",
            )
        return parents[0]

    def _current_report(
        self,
        *,
        commit: str,
        proposal: ReportProposal,
    ) -> ReportRecord | None:
        root = f".research/reports/{proposal.report_id}"
        entries = self._tree_entries(commit, root, missing_ok=True)
        yaml_paths = sorted(path for path in entries if path.endswith(".yaml"))
        if not yaml_paths:
            return None
        revisions: list[tuple[int, str]] = []
        for path in yaml_paths:
            name = Path(path).name.removesuffix(".yaml")
            if not name.isdigit() or int(name) < 1:
                raise RCPError(
                    code="report_store_invalid",
                    message="Report revision path is not canonical.",
                )
            revisions.append((int(name), path))
        revision, path = max(revisions)
        report = self._record(commit, path, ReportRecord)
        if report.report_id != proposal.report_id or report.revision != revision:
            raise RCPError(
                code="report_store_invalid",
                message="Report revision path does not match its record.",
            )
        return report

    def _tree_entries(
        self,
        commit: str,
        root: str,
        *,
        missing_ok: bool = False,
    ) -> tuple[str, ...]:
        result = self._git("ls-tree", "-r", "-z", commit, "--", root)
        if not result.stdout:
            if missing_ok:
                return ()
            raise RCPError(
                code="submission_not_found",
                message="Submission proposal files were not found at the reviewed head.",
            )
        paths: list[str] = []
        for entry in (item for item in result.stdout.split("\x00") if item):
            metadata, separator, path = entry.partition("\t")
            parts = metadata.split()
            if not separator or len(parts) != 3 or parts[0] != "100644" or parts[1] != "blob":
                raise RCPError(
                    code="submission_tree_entry_invalid",
                    message="Submission generated files must be regular Git blobs.",
                )
            paths.append(path)
        return tuple(sorted(paths))

    def _record(
        self,
        commit: str,
        path: str,
        model: type[BaseModel],
    ):
        text = self._show(commit, path)
        try:
            value = model.model_validate(load_yaml(text))
        except (ValidationError, ValueError) as error:
            raise RCPError(
                code="submission_record_invalid",
                message="Submission proposal contains a malformed protocol record.",
                context={"path": path},
            ) from error
        if dump_yaml(value) != text:
            raise RCPError(
                code="submission_record_not_canonical",
                message="Submission protocol record is not canonical YAML.",
                context={"path": path},
            )
        return value

    def _show(self, commit: str, path: str) -> str:
        result = self._git("show", f"{commit}:{path}", check=False)
        if result.returncode != 0:
            raise RCPError(
                code="submission_record_missing",
                message="Submission proposal is missing a required generated record.",
                context={"path": path},
            )
        if len(result.stdout.encode("utf-8")) > _MAX_RECORD_BYTES:
            raise RCPError(
                code="submission_record_too_large",
                message="Submission generated record exceeds the protocol limit.",
                context={"path": path},
            )
        return result.stdout

    def _resolve(self, revision: str) -> str:
        value = self._git("rev-parse", "--verify", revision).stdout.strip()
        if not _OBJECT_ID.fullmatch(value):
            raise RCPError(
                code="git_output_invalid",
                message="Git returned an invalid object ID.",
            )
        return value

    def _git(self, *arguments: str, check: bool = True) -> CommandResult:
        result = self._runner.run(
            (
                "git",
                "-c",
                "core.fsmonitor=false",
                "-C",
                str(self.repository_root),
                *arguments,
            ),
            cwd=None,
            env={"GIT_OPTIONAL_LOCKS": "0", "PATH": os.defpath},
            timeout_seconds=self._timeout_seconds,
        )
        if check and result.returncode != 0:
            raise RCPError(
                code="git_command_failed",
                message=result.stderr.strip() or "Git command failed.",
                context={"args": list(arguments), "returncode": result.returncode},
            )
        return result
