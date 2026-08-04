from enum import StrEnum


class ProjectState(StrEnum):
    BOOTSTRAPPING = "bootstrapping"
    MANAGED = "managed"
    SUSPENDED = "suspended"
    ARCHIVED = "archived"


class TaskState(StrEnum):
    PLANNED = "planned"
    READY = "ready"
    ACTIVE = "active"
    BLOCKED = "blocked"
    NEEDS_REVIEW = "needs_review"
    DONE = "done"
    CANCELED = "canceled"


class Priority(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


class SessionState(StrEnum):
    PREPARING = "preparing"
    ACTIVE = "active"
    IDLE = "idle"
    STOPPING = "stopping"
    STOPPED = "stopped"
    LOST = "lost"


class NotificationRoute(StrEnum):
    SESSION = "session"
    MANAGER_EXCEPTION = "manager_exception"


class NotificationState(StrEnum):
    PENDING = "pending"
    ACKNOWLEDGED = "acknowledged"
    REPLIED = "replied"


class RunAttemptState(StrEnum):
    PREPARING = "preparing"
    SNAPSHOTTED = "snapshotted"
    PREFLIGHTED = "preflighted"
    ALLOCATED = "allocated"
    LAUNCHING = "launching"
    RUNNING = "running"
    COLLECTING = "collecting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"
    LOST = "lost"


class RunOutcome(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELED = "canceled"
    LOST = "lost"


class SubmissionState(StrEnum):
    DRAFT = "draft"
    OPEN = "open"
    CHANGES_REQUESTED = "changes_requested"
    ACCEPTANCE_PREPARED = "acceptance_prepared"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


class SubmissionCategory(StrEnum):
    CANDIDATE_RESULT = "candidate_result"
    FAILURE_RECORD = "failure_record"
    NEGATIVE_RESULT = "negative_result"


class ReviewDisposition(StrEnum):
    ACCEPTED = "accepted"
    ACCEPTED_WITH_CONDITIONS = "accepted_with_conditions"
    REJECTED = "rejected"
    CHANGES_REQUESTED = "changes_requested"


class CodeDisposition(StrEnum):
    MERGE = "merge"
    ABANDON = "abandon"
    RETAIN_ISOLATED = "retain_isolated"


class ClaimScope(StrEnum):
    SNAPSHOT = "snapshot"
    BASELINE = "baseline"


class EvidenceStatus(StrEnum):
    VERIFIED = "verified"
    INVALID = "invalid"


class ReportApplicability(StrEnum):
    SNAPSHOT_ONLY = "snapshot_only"
    CURRENT = "current"
    IMPACT_PENDING = "impact_pending"
    STALE = "stale"
    SUPERSEDED = "superseded"


class ImpactDisposition(StrEnum):
    RERUN = "rerun"
    WAIVE = "waive"
    KEEP_STALE = "keep_stale"
    INVALIDATE = "invalidate"
    DEPENDENCY_FIX = "dependency_fix"


class InputKind(StrEnum):
    CONFIG = "config"
    DATASET = "dataset"
    CHECKPOINT = "checkpoint"
    ENVIRONMENT = "environment"
    OTHER = "other"


class PlanMetricDirection(StrEnum):
    MINIMIZE = "minimize"
    MAXIMIZE = "maximize"
    TARGET = "target"


class PlanReviewOutcome(StrEnum):
    PASSED = "passed"
    NEEDS_INPUT = "needs_input"
    INVALID = "invalid"


class PlanFindingKind(StrEnum):
    WARNING = "warning"
    NEEDS_INPUT = "needs_input"
    INVALID = "invalid"


class PlanValueSourceKind(StrEnum):
    ACCEPTED_TASK = "accepted_task"
    PROJECT_POLICY = "project_policy"


class ArtifactVerification(StrEnum):
    DECLARED = "declared"
    PRODUCER_VERIFIED = "producer_verified"
    REVIEWER_VERIFIED = "reviewer_verified"
    DURABLE = "durable"


class RetentionClass(StrEnum):
    EPHEMERAL = "ephemeral"
    TASK = "task"
    PROJECT = "project"
    PERMANENT = "permanent"


class StatusKind(StrEnum):
    RUNNING = "running"
    BLOCKED = "blocked"
    NEEDS_INPUT = "needs_input"
    READY_FOR_REVIEW = "ready_for_review"


class FailureClass(StrEnum):
    COMMAND = "command"
    INPUT = "input"
    ENVIRONMENT = "environment"
    RESOURCE = "resource"
    INFRASTRUCTURE = "infrastructure"
    CANCELED = "canceled"
    UNKNOWN = "unknown"
