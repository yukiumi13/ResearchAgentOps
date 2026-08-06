from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

from pydantic import BaseModel, ValidationError

from researchctl.config import load_project_config
from researchctl.constants import PROJECT_CONFIG_NAME, PROJECT_DIR_NAME, PROTOCOL_VERSION
from researchctl.domain.models import (
    DocumentLayoutPolicy,
    ImpactDecision,
    ProjectPolicy,
    ProjectRecord,
    ReportProposal,
    ReportRecord,
    ResearchSubmission,
    ReviewDecision,
    RunResult,
    RunSpec,
    TaskRecord,
)
from researchctl.errors import RCPError
from researchctl.repository import (
    GitRepository,
    discover_repository,
    safe_repository_path,
    status_porcelain,
)
from researchctl.schema import generate_schema_files, schema_manifest_digest
from researchctl.serialization import SerializationError, load_model, load_yaml
from researchctl.services.project_documents import (
    lint_document_tree,
    require_adopted_document_policy,
)

CheckStatus = Literal["pass", "warn", "error"]


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    name: str
    status: CheckStatus
    message: str
    remediation: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "name": self.name,
            "status": self.status,
            "message": self.message,
            "remediation": self.remediation,
        }


@dataclass(frozen=True, slots=True)
class DoctorReport:
    repository: Path
    checks: tuple[DoctorCheck, ...]

    @property
    def healthy(self) -> bool:
        return not any(check.status == "error" for check in self.checks)

    def as_dict(self) -> dict[str, object]:
        return {
            "repository": str(self.repository),
            "healthy": self.healthy,
            "checks": [check.as_dict() for check in self.checks],
        }


def _check_file_bytes(
    root: Path,
    relative: str,
    expected: bytes,
) -> DoctorCheck:
    path = safe_repository_path(root, relative, managed_only=True)
    if not path.exists():
        return DoctorCheck(
            name=f"schema:{relative}",
            status="error",
            message="Generated schema file is missing.",
            remediation="Run researchctl init or review a protocol upgrade.",
        )
    if not path.is_file() or path.read_bytes() != expected:
        return DoctorCheck(
            name=f"schema:{relative}",
            status="error",
            message="Generated schema file does not match the pinned protocol.",
            remediation="Do not hand-edit generated schemas; review an explicit upgrade.",
        )
    return DoctorCheck(
        name=f"schema:{relative}",
        status="pass",
        message="Generated schema matches the pinned protocol.",
    )


_RECORD_REMEDIATION = "Repair or remove it through a manager-reviewed change."
_SUBMISSION_MARKDOWN = frozenset({"report-preview.md", "review.md"})


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _unexpected_record(relative: str, *, message: str) -> DoctorCheck:
    return DoctorCheck(
        name=f"record:{relative}",
        status="error",
        message=message,
        remediation=_RECORD_REMEDIATION,
    )


def _managed_directory(
    root: Path,
    relative: str,
    checks: list[DoctorCheck],
) -> Path | None:
    directory = safe_repository_path(root, relative, managed_only=True)
    if not directory.exists() or not directory.is_dir():
        checks.append(
            DoctorCheck(
                name=f"records:{relative}",
                status="error",
                message="Managed record directory is missing or is not a directory.",
                remediation="Restore the managed directory through an explicit change.",
            )
        )
        return None
    return directory


def _safe_children(root: Path, directory: Path) -> tuple[Path, ...]:
    children: list[Path] = []
    for discovered in sorted(directory.iterdir()):
        relative = _relative(root, discovered)
        children.append(safe_repository_path(root, relative, managed_only=True))
    return tuple(children)


def _is_root_gitkeep(root: Path, path: Path, checks: list[DoctorCheck]) -> bool:
    if path.name != ".gitkeep":
        return False
    if not path.is_file():
        checks.append(
            _unexpected_record(
                _relative(root, path),
                message="Managed .gitkeep path is not a regular file.",
            )
        )
    return True


def _record_check(
    root: Path,
    relative: str,
    model_type: type[BaseModel],
    checks: list[DoctorCheck],
    *,
    canonical_relative: Callable[[BaseModel], str] | None = None,
    content_valid: Callable[[BaseModel], bool] | None = None,
    invalid_content_message: str | None = None,
) -> BaseModel | None:
    path = safe_repository_path(root, relative, managed_only=True)
    if not path.exists():
        checks.append(
            _unexpected_record(
                relative,
                message=f"Required managed {model_type.__name__} record is missing.",
            )
        )
        return None
    if not path.is_file():
        checks.append(
            _unexpected_record(
                relative,
                message="Managed record path is not a regular file.",
            )
        )
        return None

    try:
        record = load_model(path, model_type)
    except (OSError, UnicodeError, ValidationError, SerializationError):
        checks.append(
            _unexpected_record(
                relative,
                message=f"Managed record is not a valid {model_type.__name__}.",
            )
        )
        return None

    if canonical_relative is not None and canonical_relative(record) != relative:
        checks.append(
            _unexpected_record(
                relative,
                message=(
                    f"Managed {model_type.__name__} identity does not match its "
                    "canonical path."
                ),
            )
        )
        return None
    if content_valid is not None and not content_valid(record):
        checks.append(
            _unexpected_record(
                relative,
                message=invalid_content_message or "Managed record linkage is invalid.",
            )
        )
        return None

    checks.append(
        DoctorCheck(
            name=f"record:{relative}",
            status="pass",
            message=f"Managed {model_type.__name__} record is valid.",
        )
    )
    return record


def _task_record_checks(root: Path, checks: list[DoctorCheck]) -> None:
    relative_directory = f"{PROJECT_DIR_NAME}/tasks"
    base = _managed_directory(root, relative_directory, checks)
    if base is None:
        return
    for path in _safe_children(root, base):
        if _is_root_gitkeep(root, path, checks):
            continue
        relative = _relative(root, path)
        if not path.is_file() or path.suffix != ".yaml":
            checks.append(
                _unexpected_record(
                    relative,
                    message="Managed Task store contains a non-canonical entry.",
                )
            )
            continue
        _record_check(
            root,
            relative,
            TaskRecord,
            checks,
            canonical_relative=lambda record: (
                f"{relative_directory}/{record.task_id}.yaml"
            ),
        )


def _run_record_checks(root: Path, checks: list[DoctorCheck]) -> None:
    relative_directory = f"{PROJECT_DIR_NAME}/runs"
    base = _managed_directory(root, relative_directory, checks)
    if base is None:
        return
    for run_directory in _safe_children(root, base):
        if _is_root_gitkeep(root, run_directory, checks):
            continue
        run_relative = _relative(root, run_directory)
        if not run_directory.is_dir():
            checks.append(
                _unexpected_record(
                    run_relative,
                    message="Managed Run store contains a non-canonical entry.",
                )
            )
            continue
        allowed = {"result.yaml", "spec.yaml"}
        for path in _safe_children(root, run_directory):
            if path.name not in allowed:
                checks.append(
                    _unexpected_record(
                        _relative(root, path),
                        message="Managed Run directory contains a non-canonical entry.",
                    )
                )

        spec_relative = f"{run_relative}/spec.yaml"
        spec = _record_check(
            root,
            spec_relative,
            RunSpec,
            checks,
            canonical_relative=lambda record: (
                f"{relative_directory}/{record.run_id}/spec.yaml"
            ),
        )
        result_path = safe_repository_path(
            root,
            f"{run_relative}/result.yaml",
            managed_only=True,
        )
        if not result_path.exists():
            continue
        result_relative = _relative(root, result_path)
        _record_check(
            root,
            result_relative,
            RunResult,
            checks,
            canonical_relative=lambda record: (
                f"{relative_directory}/{record.run_id}/result.yaml"
            ),
            content_valid=lambda record: (
                not isinstance(spec, RunSpec)
                or record.run_spec_digest == spec.spec_digest
            ),
            invalid_content_message=(
                "Managed RunResult does not bind the colocated RunSpec."
            ),
        )


def _submission_evidence_checks(
    root: Path,
    evidence_directory: Path,
    checks: list[DoctorCheck],
) -> None:
    evidence_relative = _relative(root, evidence_directory)
    if not evidence_directory.exists() or not evidence_directory.is_dir():
        checks.append(
            _unexpected_record(
                evidence_relative,
                message="Required submission evidence directory is missing.",
            )
        )
        return
    run_directories = _safe_children(root, evidence_directory)
    if not run_directories:
        checks.append(
            _unexpected_record(
                evidence_relative,
                message="Submission evidence directory contains no Run evidence.",
            )
        )
        return
    for run_directory in run_directories:
        run_relative = _relative(root, run_directory)
        if not run_directory.is_dir():
            checks.append(
                _unexpected_record(
                    run_relative,
                    message="Submission evidence contains a non-canonical entry.",
                )
            )
            continue
        allowed = {"result.yaml", "spec.yaml"}
        for path in _safe_children(root, run_directory):
            if path.name not in allowed:
                checks.append(
                    _unexpected_record(
                        _relative(root, path),
                        message="Run evidence directory contains a non-canonical entry.",
                    )
                )
        spec_relative = f"{run_relative}/spec.yaml"
        result_relative = f"{run_relative}/result.yaml"
        spec = _record_check(
            root,
            spec_relative,
            RunSpec,
            checks,
            canonical_relative=lambda record: (
                f"{evidence_relative}/{record.run_id}/spec.yaml"
            ),
        )
        _record_check(
            root,
            result_relative,
            RunResult,
            checks,
            canonical_relative=lambda record: (
                f"{evidence_relative}/{record.run_id}/result.yaml"
            ),
            content_valid=lambda record: (
                not isinstance(spec, RunSpec)
                or record.run_spec_digest == spec.spec_digest
            ),
            invalid_content_message=(
                "Evidence RunResult does not bind the colocated RunSpec."
            ),
        )


def _submission_record_checks(root: Path, checks: list[DoctorCheck]) -> None:
    relative_directory = f"{PROJECT_DIR_NAME}/submissions"
    base = _managed_directory(root, relative_directory, checks)
    if base is None:
        return
    for submission_directory in _safe_children(root, base):
        if _is_root_gitkeep(root, submission_directory, checks):
            continue
        submission_relative = _relative(root, submission_directory)
        if not submission_directory.is_dir():
            checks.append(
                _unexpected_record(
                    submission_relative,
                    message="Managed Submission store contains a non-canonical entry.",
                )
            )
            continue

        allowed = {
            "evidence",
            "proposed-report.yaml",
            "report-preview.md",
            "review.md",
            "submission.yaml",
        }
        for path in _safe_children(root, submission_directory):
            relative = _relative(root, path)
            if path.name not in allowed:
                checks.append(
                    _unexpected_record(
                        relative,
                        message="Submission directory contains a non-canonical entry.",
                    )
                )
            elif path.name in _SUBMISSION_MARKDOWN:
                if path.is_file():
                    checks.append(
                        DoctorCheck(
                            name=f"record:{relative}",
                            status="pass",
                            message="Generated submission Markdown is in a canonical path.",
                        )
                    )
                else:
                    checks.append(
                        _unexpected_record(
                            relative,
                            message="Generated submission Markdown is not a regular file.",
                        )
                    )

        _record_check(
            root,
            f"{submission_relative}/submission.yaml",
            ResearchSubmission,
            checks,
            canonical_relative=lambda record: (
                f"{relative_directory}/{record.submission_id}/submission.yaml"
            ),
        )
        _record_check(
            root,
            f"{submission_relative}/proposed-report.yaml",
            ReportProposal,
            checks,
            canonical_relative=lambda record: (
                f"{relative_directory}/{record.submission_id}/proposed-report.yaml"
            ),
        )
        evidence = safe_repository_path(
            root,
            f"{submission_relative}/evidence",
            managed_only=True,
        )
        _submission_evidence_checks(root, evidence, checks)


def _decision_record_checks(root: Path, checks: list[DoctorCheck]) -> None:
    relative_directory = f"{PROJECT_DIR_NAME}/decisions"
    base = _managed_directory(root, relative_directory, checks)
    if base is None:
        return
    for path in _safe_children(root, base):
        if _is_root_gitkeep(root, path, checks):
            continue
        relative = _relative(root, path)
        if not path.is_file() or path.suffix != ".yaml":
            checks.append(
                _unexpected_record(
                    relative,
                    message="Managed Decision store contains a non-canonical entry.",
                )
            )
            continue
        try:
            decision_payload = load_yaml(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, SerializationError):
            decision_payload = {}
        decision_model = (
            ImpactDecision
            if "impact_id" in decision_payload
            else ReviewDecision
        )
        _record_check(
            root,
            relative,
            decision_model,
            checks,
            canonical_relative=lambda record: (
                f"{relative_directory}/{record.decision_id}.yaml"
            ),
        )


def _report_record_checks(root: Path, checks: list[DoctorCheck]) -> None:
    relative_directory = f"{PROJECT_DIR_NAME}/reports"
    base = _managed_directory(root, relative_directory, checks)
    if base is None:
        return
    for report_directory in _safe_children(root, base):
        if _is_root_gitkeep(root, report_directory, checks):
            continue
        report_relative = _relative(root, report_directory)
        if not report_directory.is_dir():
            checks.append(
                _unexpected_record(
                    report_relative,
                    message="Managed Report store contains a non-canonical entry.",
                )
            )
            continue
        revisions = _safe_children(root, report_directory)
        if not revisions:
            checks.append(
                _unexpected_record(
                    report_relative,
                    message="Managed Report directory contains no immutable revision.",
                )
            )
            continue
        for path in revisions:
            relative = _relative(root, path)
            if path.is_file() and path.suffix == ".md" and path.stem.isdecimal():
                checks.append(
                    DoctorCheck(
                        name=f"record:{relative}",
                        status="pass",
                        message="Generated accepted Report Markdown is in a canonical path.",
                    )
                )
                continue
            if not path.is_file() or path.suffix != ".yaml":
                checks.append(
                    _unexpected_record(
                        relative,
                        message="Managed Report directory contains a non-canonical entry.",
                    )
                )
                continue
            _record_check(
                root,
                relative,
                ReportRecord,
                checks,
                canonical_relative=lambda record: (
                    f"{relative_directory}/{record.report_id}/{record.revision}.yaml"
                ),
            )


def _managed_record_checks(root: Path) -> tuple[DoctorCheck, ...]:
    checks: list[DoctorCheck] = []
    _task_record_checks(root, checks)
    _run_record_checks(root, checks)
    _submission_record_checks(root, checks)
    _decision_record_checks(root, checks)
    _report_record_checks(root, checks)
    return tuple(checks)


def _standalone_document_report(
    root: Path,
    repository: GitRepository,
) -> DoctorReport:
    checks = [
        DoctorCheck(
            name="mode:standalone-documents",
            status="pass",
            message=(
                "Standalone document mode is valid without researchctl init; managed "
                "Project, Session, record, and generated-schema checks do not apply."
            ),
        )
    ]
    policy_path = safe_repository_path(root, ".researchctl-docs.yaml")
    try:
        if policy_path.is_symlink() or not policy_path.is_file():
            raise ValueError("standalone policy is not a regular non-symlink file")
        policy = load_model(policy_path, DocumentLayoutPolicy)
        require_adopted_document_policy(policy)
    except (
        OSError,
        UnicodeError,
        ValueError,
        ValidationError,
        SerializationError,
        RCPError,
    ) as exc:
        checks.append(
            DoctorCheck(
                name="document-policy",
                status="error",
                message=f"Standalone document policy is invalid: {exc}",
                remediation="Run researchctl doc policy-lint .researchctl-docs.yaml.",
            )
        )
    else:
        checks.append(
            DoctorCheck(
                name="document-policy",
                status="pass",
                message="Standalone document policy is structurally valid and adopted.",
            )
        )
        tree = lint_document_tree(root, policy)
        checks.append(
            DoctorCheck(
                name="document-tree",
                status="pass" if tree.passed else "error",
                message=(
                    f"Document tree passed across {tree.checked_files} checked files."
                    if tree.passed
                    else (
                        "Document tree has "
                        f"{sum(item.kind == 'invalid' for item in tree.findings)} "
                        "invalid finding(s)."
                    )
                ),
                remediation=(
                    None
                    if tree.passed
                    else "Run researchctl doc tree --project . --json for exact findings."
                ),
            )
        )
    dirty = status_porcelain(repository)
    checks.append(
        DoctorCheck(
            name="git-worktree",
            status="warn" if dirty else "pass",
            message=(
                f"Git worktree has {len(dirty)} changed or untracked path(s)."
                if dirty
                else "Git worktree is clean."
            ),
            remediation="Review changes before proposing the document change." if dirty else None,
        )
    )
    return DoctorReport(repository=root, checks=tuple(checks))


def doctor(path: Path) -> DoctorReport:
    repository = discover_repository(path)
    root = repository.root
    standalone_policy = root / ".researchctl-docs.yaml"
    managed_config = root / PROJECT_CONFIG_NAME
    managed_root = root / PROJECT_DIR_NAME
    if (
        (standalone_policy.exists() or standalone_policy.is_symlink())
        and not managed_config.exists()
        and not managed_config.is_symlink()
        and not managed_root.exists()
        and not managed_root.is_symlink()
    ):
        return _standalone_document_report(root, repository)
    checks: list[DoctorCheck] = []
    schema_files = generate_schema_files()
    expected_manifest_digest = schema_manifest_digest(schema_files)

    config_path = safe_repository_path(root, PROJECT_CONFIG_NAME)
    config = None
    if not config_path.exists():
        checks.append(
            DoctorCheck(
                name="project-config",
                status="error",
                message=f"{PROJECT_CONFIG_NAME} is missing.",
                remediation="Run researchctl init.",
            )
        )
    else:
        try:
            config = load_project_config(config_path)
            if config.protocol_version != PROTOCOL_VERSION:
                checks.append(
                    DoctorCheck(
                        name="protocol-version",
                        status="error",
                        message=(
                            f"Project protocol {config.protocol_version} does not match "
                            f"CLI protocol {PROTOCOL_VERSION}."
                        ),
                        remediation="Use a compatible CLI or review an explicit migration.",
                    )
                )
            else:
                checks.append(
                    DoctorCheck(
                        name="protocol-version",
                        status="pass",
                        message=f"Protocol {PROTOCOL_VERSION} is supported.",
                    )
                )
            if config.schema_manifest_digest == expected_manifest_digest:
                checks.append(
                    DoctorCheck(
                        name="schema-lock",
                        status="pass",
                        message="Pinned schema manifest matches this CLI.",
                    )
                )
            else:
                checks.append(
                    DoctorCheck(
                        name="schema-lock",
                        status="error",
                        message="Pinned schema manifest does not match this CLI.",
                        remediation=(
                            "Use the pinned environment or review an explicit upgrade."
                        ),
                    )
                )
        except (OSError, ValueError, ValidationError) as exc:
            checks.append(
                DoctorCheck(
                    name="project-config",
                    status="error",
                    message=f"Project config is invalid: {exc}",
                    remediation="Repair the config through a manager-reviewed change.",
                )
            )

    if config is not None:
        project_path = safe_repository_path(root, config.project_file, managed_only=True)
        if not project_path.exists():
            checks.append(
                DoctorCheck(
                    name="project-record",
                    status="error",
                    message=f"{config.project_file} is missing.",
                    remediation="Restore it from Git or rerun initialization before management.",
                )
            )
        else:
            try:
                project = load_model(project_path, ProjectRecord)
                if project.project_id != config.project_id:
                    checks.append(
                        DoctorCheck(
                            name="project-record",
                            status="error",
                            message="Project IDs differ between config and project record.",
                        )
                    )
                else:
                    checks.append(
                        DoctorCheck(
                            name="project-record",
                            status="pass",
                            message=f"Project {project.key} is structurally valid.",
                        )
                    )
                if project.state.value == "bootstrapping":
                    checks.append(
                        DoctorCheck(
                            name="project-state",
                            status="warn",
                            message="Project is still bootstrapping.",
                            remediation=(
                                "Review bootstrap inventory and prepare manager acceptance."
                            ),
                        )
                    )
            except (OSError, ValueError, ValidationError, SerializationError) as exc:
                checks.append(
                    DoctorCheck(
                        name="project-record",
                        status="error",
                        message=f"Project record is invalid: {exc}",
                    )
                )

    policy_relative = f"{PROJECT_DIR_NAME}/policies/default.yaml"
    policy_path = safe_repository_path(root, policy_relative, managed_only=True)
    if not policy_path.is_file():
        checks.append(
            DoctorCheck(
                name="project-policy",
                status="error",
                message="Default project policy is missing or is not a regular file.",
                remediation="Restore it through a manager-reviewed change.",
            )
        )
    else:
        try:
            load_model(policy_path, ProjectPolicy)
        except (OSError, UnicodeError, ValidationError, SerializationError):
            checks.append(
                DoctorCheck(
                    name="project-policy",
                    status="error",
                    message="Default project policy is invalid.",
                    remediation="Repair it through a manager-reviewed change.",
                )
            )
        else:
            checks.append(
                DoctorCheck(
                    name="project-policy",
                    status="pass",
                    message="Default project policy is structurally valid.",
                )
            )

    checks.extend(_managed_record_checks(root))

    for schema_name, content in schema_files.items():
        relative = f"{PROJECT_DIR_NAME}/schemas/{schema_name}"
        checks.append(_check_file_bytes(root, relative, content))

    lock_candidates = (
        "uv.lock",
        "poetry.lock",
        "Pipfile.lock",
        "requirements.lock",
        "conda-lock.yml",
    )
    has_lock = any(
        safe_repository_path(root, candidate).is_file()
        for candidate in lock_candidates
    )
    if has_lock:
        checks.append(
            DoctorCheck(
                name="environment-lock",
                status="pass",
                message="A recognized dependency lock file is present.",
            )
        )
    else:
        checks.append(
            DoctorCheck(
                name="environment-lock",
                status="warn",
                message="No recognized dependency lock file was found.",
                remediation="Record an environment or image digest before accepting runs.",
            )
        )

    dirty = status_porcelain(repository)
    checks.append(
        DoctorCheck(
            name="git-worktree",
            status="warn" if dirty else "pass",
            message=(
                f"Git worktree has {len(dirty)} changed or untracked path(s)."
                if dirty
                else "Git worktree is clean."
            ),
            remediation="Review changes before creating an immutable run." if dirty else None,
        )
    )
    return DoctorReport(repository=root, checks=tuple(checks))
