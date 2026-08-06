from __future__ import annotations

from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Annotated

from pydantic import (
    AfterValidator,
    BeforeValidator,
    PlainSerializer,
    StringConstraints,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


def _validate_datetime_input(value: object) -> object:
    if not isinstance(value, (str, datetime)):
        raise ValueError("timestamp must be an RFC 3339 string or datetime")
    return value


def _validate_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(UTC)


def _serialize_utc(value: datetime) -> str:
    value = value.astimezone(UTC)
    timespec = "microseconds" if value.microsecond else "seconds"
    return value.isoformat(timespec=timespec).replace("+00:00", "Z")


def _validate_repo_path(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("repository path must be a string")
    if value == ".":
        return value
    if not value or "\x00" in value or "\\" in value:
        raise ValueError("repository path is empty or contains forbidden characters")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("repository path must be relative and cannot traverse parents")
    normalized = path.as_posix()
    if normalized == ".":
        return normalized
    if not normalized:
        raise ValueError("repository path must identify a file or directory")
    return normalized


def _validate_git_branch(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Git branch must be a string")
    branch = value.strip()
    forbidden = frozenset(" ~^:?*[\\")
    if (
        not branch
        or len(branch.encode("utf-8")) > 255
        or branch in {"@", "."}
        or branch.startswith(("/", "."))
        or branch.endswith(("/", "."))
        or "//" in branch
        or ".." in branch
        or "@{" in branch
        or any(ord(character) < 32 or ord(character) == 127 for character in branch)
        or any(character in forbidden for character in branch)
        or any(
            component.startswith(".") or component.endswith(".lock")
            for component in branch.split("/")
        )
    ):
        raise ValueError("Git branch is not a canonical branch name")
    return branch


def _record_id_pattern(kind: str) -> str:
    return rf"^{kind}_\d{{8}}T\d{{6}}Z_[0-9a-f]{{24}}$"


UtcDateTime = Annotated[
    datetime,
    BeforeValidator(_validate_datetime_input),
    AfterValidator(_validate_utc),
    PlainSerializer(_serialize_utc, return_type=str),
]
RepositoryPath = Annotated[str, BeforeValidator(_validate_repo_path)]
GitBranchName = Annotated[str, BeforeValidator(_validate_git_branch)]
NonEmptyStr = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=4096),
]
ShortText = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=512),
]
RecordId = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^[a-z][a-z0-9_]*_\d{8}T\d{6}Z_[0-9a-f]{24}$"),
]
ProjectId = Annotated[
    str,
    StringConstraints(strict=True, pattern=_record_id_pattern("project")),
]
TaskId = Annotated[
    str,
    StringConstraints(strict=True, pattern=_record_id_pattern("task")),
]
SessionId = Annotated[
    str,
    StringConstraints(strict=True, pattern=_record_id_pattern("session")),
]
DocumentId = Annotated[
    str,
    StringConstraints(strict=True, pattern=_record_id_pattern("document")),
]
OperationId = Annotated[
    str,
    StringConstraints(strict=True, pattern=_record_id_pattern("operation")),
]
BootstrapId = Annotated[
    str,
    StringConstraints(strict=True, pattern=_record_id_pattern("bootstrap")),
]
AttestationId = Annotated[
    str,
    StringConstraints(strict=True, pattern=_record_id_pattern("attestation")),
]
RunId = Annotated[
    str,
    StringConstraints(strict=True, pattern=_record_id_pattern("run")),
]
RunAttemptId = Annotated[
    str,
    StringConstraints(strict=True, pattern=_record_id_pattern("attempt")),
]
RunResultId = Annotated[
    str,
    StringConstraints(strict=True, pattern=_record_id_pattern("result")),
]
PlanId = Annotated[
    str,
    StringConstraints(strict=True, pattern=_record_id_pattern("plan")),
]
PlanReviewId = Annotated[
    str,
    StringConstraints(strict=True, pattern=_record_id_pattern("plan_review")),
]
SubmissionId = Annotated[
    str,
    StringConstraints(strict=True, pattern=_record_id_pattern("submission")),
]
DecisionId = Annotated[
    str,
    StringConstraints(strict=True, pattern=_record_id_pattern("decision")),
]
ReportId = Annotated[
    str,
    StringConstraints(strict=True, pattern=_record_id_pattern("report")),
]
ImpactId = Annotated[
    str,
    StringConstraints(strict=True, pattern=_record_id_pattern("impact")),
]
DependencyReceiptId = Annotated[
    str,
    StringConstraints(strict=True, pattern=_record_id_pattern("receipt")),
]
StatusUpdateId = Annotated[
    str,
    StringConstraints(strict=True, pattern=_record_id_pattern("update")),
]
NotificationId = Annotated[
    str,
    StringConstraints(strict=True, pattern=_record_id_pattern("notification")),
]
NotificationReplyId = Annotated[
    str,
    StringConstraints(strict=True, pattern=_record_id_pattern("reply")),
]
HumanKey = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^[A-Za-z][A-Za-z0-9._-]{0,63}$"),
]
GitHubRepository = Annotated[
    str,
    StringConstraints(
        strict=True,
        pattern=(
            r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,98}[A-Za-z0-9])?/"
            r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,98}[A-Za-z0-9_.-])?$"
        ),
        max_length=201,
    ),
]
GitHubAccountLogin = Annotated[
    str,
    StringConstraints(
        strict=True,
        pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,98}[A-Za-z0-9])?(?:\[bot\])?$",
        max_length=105,
    ),
]
GitHubTeamSlug = Annotated[
    str,
    StringConstraints(
        strict=True,
        pattern=r"^[a-z0-9](?:[a-z0-9-]{0,98}[a-z0-9])?$",
        max_length=100,
    ),
]
DocumentSlug = Annotated[
    str,
    StringConstraints(
        strict=True,
        pattern=r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$",
        max_length=80,
    ),
]
DocumentLabel = Annotated[
    str,
    StringConstraints(
        strict=True,
        pattern=(
            r"^[a-z][a-z0-9-]*(?:/[a-z][a-z0-9-]*)*"
            r":[a-z][a-z0-9-]*$"
        ),
        max_length=128,
    ),
]
Sha256Digest = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^sha256:[0-9a-f]{64}$"),
]
GitObjectId = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"),
]
LinearUuid = Annotated[
    str,
    StringConstraints(
        strict=True,
        pattern=(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
            r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
        ),
    ),
]
