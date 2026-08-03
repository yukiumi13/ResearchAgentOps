from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from researchctl.adapters.git_ci import GitCIObjectReader
from researchctl.domain.enums import ProjectState
from researchctl.domain.models import (
    CIValidationAttestation,
    LinearProjectionPolicy,
    ProjectRecord,
    ReportRecord,
    ResearchSubmission,
    ReviewDecision,
    TaskRecord,
)
from researchctl.errors import RCPError
from researchctl.serialization import (
    SerializationError,
    canonical_digest,
    dump_yaml,
    load_yaml,
)
from researchctl.services.linear_delivery import AcceptedMergeSnapshot


_ModelT = TypeVar("_ModelT", bound=BaseModel)


class GitAcceptedMergeReader:
    """Recover one accepted result from immutable protected-branch Git data."""

    def __init__(
        self,
        *,
        repository_root: Path,
        expected_project_id: str,
        expected_default_branch: str,
        git: GitCIObjectReader | None = None,
    ) -> None:
        root = Path(os.path.abspath(os.fspath(repository_root)))
        if root.is_symlink() or not root.is_dir():
            raise RCPError(
                code="linear_accepted_repository_invalid",
                message="Accepted-merge verification requires a non-symlink repository.",
            )
        if not expected_project_id or not expected_default_branch:
            raise ValueError("expected Project and default branch must be non-empty")
        self.repository_root = root
        self.expected_project_id = expected_project_id
        self.expected_default_branch = expected_default_branch
        self.git = git or GitCIObjectReader()

    def read_accepted_merge(
        self,
        *,
        project_id: str,
        merge_commit: str,
        ci: CIValidationAttestation,
    ) -> AcceptedMergeSnapshot | None:
        if project_id != self.expected_project_id or ci.project_id != project_id:
            return None

        merge = self.git.read_commit(self.repository_root, merge_commit)
        subject = self.git.read_commit(self.repository_root, ci.subject_head)
        if subject.tree != ci.subject_tree:
            return None

        project = self._record(
            merge_commit,
            ".research/project.yaml",
            ProjectRecord,
            required=False,
        )
        if not isinstance(project, ProjectRecord):
            return None
        if (
            project.project_id != project_id
            or project.state is not ProjectState.MANAGED
            or project.repository.default_branch != self.expected_default_branch
        ):
            return None

        protected_ref = f"refs/heads/{project.repository.default_branch}"
        protected_head = self.git.resolve_protected_branch(
            self.repository_root,
            protected_ref,
        )
        accepted_on_protected = (
            protected_head is not None
            and self.git.is_ancestor(
                self.repository_root,
                ancestor=merge_commit,
                descendant=protected_head,
            )
        )
        incorporated = self._subject_newly_incorporated(
            merge_commit=merge_commit,
            merge_parents=merge.parents,
            subject_head=ci.subject_head,
            base_commit=ci.base_commit,
        )

        task = self._record(
            merge_commit,
            f".research/tasks/{ci.task_id}.yaml",
            TaskRecord,
            required=False,
        )
        submission = self._record(
            merge_commit,
            (
                f".research/submissions/{ci.submission_id}/"
                "submission.yaml"
            ),
            ResearchSubmission,
            required=False,
        )
        if ci.report_id is None or ci.report_revision is None:
            return None
        report = self._record(
            merge_commit,
            (
                f".research/reports/{ci.report_id}/"
                f"{ci.report_revision}.yaml"
            ),
            ReportRecord,
            required=False,
        )
        if not isinstance(task, TaskRecord):
            return None
        if not isinstance(submission, ResearchSubmission):
            return None
        if not isinstance(report, ReportRecord):
            return None
        decision = self._decision(merge_commit, ci)
        if decision is None:
            return None
        if not self._generated_outputs_match(merge_commit, ci):
            return None

        policy = self._record(
            merge_commit,
            ".research/policies/linear.yaml",
            LinearProjectionPolicy,
            required=False,
        )
        if policy is not None and not isinstance(policy, LinearProjectionPolicy):
            return None
        return AcceptedMergeSnapshot(
            project_id=project.project_id,
            merge_commit=merge_commit,
            subject_head=ci.subject_head,
            default_branch=project.repository.default_branch,
            protected_ref=protected_ref,
            accepted_on_protected_default_branch=accepted_on_protected,
            attested_subject_incorporated=incorporated,
            task=task,
            submission=submission,
            decision=decision,
            report=report,
            policy=policy,
        )

    def _subject_newly_incorporated(
        self,
        *,
        merge_commit: str,
        merge_parents: tuple[str, ...],
        subject_head: str,
        base_commit: str,
    ) -> bool:
        if not self.git.is_ancestor(
            self.repository_root,
            ancestor=base_commit,
            descendant=subject_head,
        ):
            return False
        if merge_commit == subject_head:
            return True
        if len(merge_parents) != 2:
            return False
        if not self.git.is_ancestor(
            self.repository_root,
            ancestor=subject_head,
            descendant=merge_commit,
        ):
            return False
        return not self.git.is_ancestor(
            self.repository_root,
            ancestor=subject_head,
            descendant=merge_parents[0],
        )

    def _decision(
        self,
        commit: str,
        ci: CIValidationAttestation,
    ) -> ReviewDecision | None:
        if ci.decision_digest is None or ci.report_id is None:
            return None
        entries = self.git.list_entries(
            self.repository_root,
            commit=commit,
            path=".research/decisions",
        )
        matches: list[ReviewDecision] = []
        for entry in entries:
            if not entry.path.endswith(".yaml"):
                continue
            decision = self._record(
                commit,
                entry.path,
                ReviewDecision,
                required=True,
            )
            assert isinstance(decision, ReviewDecision)
            if (
                entry.path
                != f".research/decisions/{decision.decision_id}.yaml"
            ):
                self._invalid_record(entry.path, "Decision path is not canonical.")
            if (
                decision.submission_id == ci.submission_id
                and decision.report_id == ci.report_id
                and canonical_digest(decision) == ci.decision_digest
            ):
                matches.append(decision)
        if not matches:
            return None
        if len(matches) != 1:
            raise RCPError(
                code="linear_accepted_decision_ambiguous",
                message="Accepted merge contains ambiguous matching Decisions.",
            )
        return matches[0]

    def _generated_outputs_match(
        self,
        commit: str,
        ci: CIValidationAttestation,
    ) -> bool:
        for expected in ci.generated_outputs:
            content = self.git.read_blob_at(
                self.repository_root,
                commit=commit,
                path=expected.path,
                required=False,
            )
            if content is None or len(content) != expected.size_bytes:
                return False
            digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
            if digest != expected.digest:
                return False
        return True

    def _record(
        self,
        commit: str,
        path: str,
        model_type: type[_ModelT],
        *,
        required: bool,
    ) -> _ModelT | None:
        content = self.git.read_blob_at(
            self.repository_root,
            commit=commit,
            path=path,
            required=required,
        )
        if content is None:
            return None
        try:
            model = model_type.model_validate(load_yaml(content.decode("utf-8")))
        except (
            UnicodeError,
            SerializationError,
            TypeError,
            ValueError,
            ValidationError,
        ) as error:
            raise RCPError(
                code="linear_accepted_record_invalid",
                message="Accepted merge contains an invalid canonical record.",
                context={"path": path},
            ) from error
        if dump_yaml(model).encode("utf-8") != content:
            self._invalid_record(path, "Accepted record is not canonical YAML.")
        return model

    @staticmethod
    def _invalid_record(path: str, message: str) -> None:
        raise RCPError(
            code="linear_accepted_record_invalid",
            message=message,
            context={"path": path},
        )
