from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
import stat
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from researchctl.domain.enums import (
    NotificationRoute,
    NotificationState,
    SessionState,
    StatusKind,
)
from researchctl.domain.models import (
    SessionNotificationOrigin,
    SessionNotificationSourceMarker,
    StatusUpdate,
)
from researchctl.errors import RCPError
from researchctl.runtime.models import (
    AttentionItem,
    LinearDeliveryClaim,
    LinearDeliveryReceiptRecord,
    LinearDeliveryRecord,
    OperationEvent,
    OperationRecord,
    OutboxRecord,
    PublishedNotificationReply,
    PublishedStatus,
    RuntimeSession,
    SessionNotification,
    SessionNotificationReply,
    VerifiedLinearIngressReceipt,
)
from researchctl.serialization import canonical_json_bytes

_SCHEMA_VERSION = 3
_DEFAULT_BUSY_TIMEOUT_MS = 5_000
_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_DUMMY_TOKEN_DIGEST = "sha256:" + "0" * 64
_OBJECT_ID_PATTERN = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_LINEAR_ACCEPTED_TOPIC = "linear.accepted-result.v1"
_LINEAR_REPLY_TOPIC = "linear.session-reply.v1"
_LINEAR_DELIVERY_TOPICS = {_LINEAR_ACCEPTED_TOPIC, _LINEAR_REPLY_TOPIC}

_MIGRATION_1 = (
    """
    CREATE TABLE operations (
        operation_id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        command TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        request_digest TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('running', 'terminal')),
        started_at TEXT NOT NULL,
        finished_at TEXT,
        terminal_result TEXT,
        result_json TEXT,
        UNIQUE (project_id, command, idempotency_key)
    )
    """,
    """
    CREATE TABLE operation_events (
        operation_id TEXT NOT NULL REFERENCES operations(operation_id) ON DELETE CASCADE,
        sequence INTEGER NOT NULL CHECK (sequence >= 0),
        kind TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        PRIMARY KEY (operation_id, sequence)
    )
    """,
    """
    CREATE TABLE runtime_sessions (
        session_id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        task_id TEXT NOT NULL,
        state TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        host TEXT,
        branch TEXT,
        worktree_path TEXT,
        continued_from TEXT REFERENCES runtime_sessions(session_id),
        actor_token_digest TEXT,
        metadata_json TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX runtime_sessions_project_order
    ON runtime_sessions(project_id, created_at, session_id)
    """,
    """
    CREATE TABLE status_updates (
        update_id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        task_id TEXT NOT NULL,
        session_id TEXT NOT NULL REFERENCES runtime_sessions(session_id),
        status TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        payload_digest TEXT NOT NULL,
        payload_json TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX status_updates_project_order
    ON status_updates(project_id, observed_at, update_id)
    """,
    """
    CREATE TABLE outbox_events (
        outbox_id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        topic TEXT NOT NULL,
        aggregate_id TEXT NOT NULL REFERENCES status_updates(update_id),
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('pending', 'delivered', 'dead_letter')),
        UNIQUE (topic, aggregate_id)
    )
    """,
    """
    CREATE INDEX outbox_pending_order
    ON outbox_events(state, created_at, outbox_id)
    """,
    """
    CREATE TABLE attention_items (
        dedupe_key TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        task_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        identity_text TEXT NOT NULL,
        priority INTEGER NOT NULL,
        state TEXT NOT NULL CHECK (
            state IN ('open', 'acknowledged', 'snoozed', 'resolved')
        ),
        generation INTEGER NOT NULL CHECK (generation >= 1),
        evidence_digest TEXT NOT NULL,
        current_update_id TEXT NOT NULL REFERENCES status_updates(update_id),
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        acknowledged_by TEXT,
        acknowledged_at TEXT,
        acknowledged_update_id TEXT,
        snoozed_by TEXT,
        snoozed_until TEXT,
        snoozed_update_id TEXT,
        resolved_by TEXT,
        resolved_at TEXT,
        resolved_update_id TEXT,
        resolution_reason TEXT
    )
    """,
    """
    CREATE INDEX attention_inbox_order
    ON attention_items(project_id, state, priority, last_seen_at, dedupe_key)
    """,
    """
    CREATE TABLE attention_actions (
        action_key TEXT PRIMARY KEY,
        dedupe_key TEXT NOT NULL REFERENCES attention_items(dedupe_key),
        action TEXT NOT NULL CHECK (action IN ('ack', 'snooze', 'resolve')),
        actor TEXT NOT NULL,
        source_update_id TEXT NOT NULL REFERENCES status_updates(update_id),
        operation_id TEXT REFERENCES operations(operation_id),
        generation INTEGER NOT NULL,
        observed_at TEXT NOT NULL,
        detail_json TEXT NOT NULL
    )
    """,
)

_MIGRATION_2 = (
    """
    CREATE TABLE session_notifications (
        notification_id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        task_id TEXT NOT NULL,
        session_id TEXT NOT NULL REFERENCES runtime_sessions(session_id),
        linear_issue_id TEXT NOT NULL,
        source_thread_id TEXT NOT NULL,
        source_comment_id TEXT NOT NULL,
        commit_sha TEXT NOT NULL,
        message_text TEXT NOT NULL,
        origin_json TEXT NOT NULL,
        route TEXT NOT NULL CHECK (route IN ('session', 'manager_exception')),
        state TEXT NOT NULL CHECK (state IN ('pending', 'acknowledged', 'replied')),
        revision INTEGER NOT NULL CHECK (revision >= 1),
        created_at TEXT NOT NULL,
        routed_at TEXT NOT NULL,
        fallback_reason TEXT,
        acknowledged_by TEXT,
        acknowledged_at TEXT,
        acknowledged_operation_id TEXT REFERENCES operations(operation_id),
        reply_id TEXT,
        replied_by TEXT,
        replied_at TEXT,
        UNIQUE (project_id, source_comment_id, session_id)
    )
    """,
    """
    CREATE INDEX session_notifications_inbox_order
    ON session_notifications(
        project_id, route, state, created_at, notification_id
    )
    """,
    """
    CREATE TABLE notification_replies (
        reply_id TEXT PRIMARY KEY,
        notification_id TEXT NOT NULL UNIQUE
            REFERENCES session_notifications(notification_id),
        project_id TEXT NOT NULL,
        task_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        actor_id TEXT NOT NULL,
        body_text TEXT NOT NULL,
        payload_digest TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        operation_id TEXT NOT NULL REFERENCES operations(operation_id)
    )
    """,
    """
    CREATE TABLE notification_reply_outbox (
        outbox_id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        topic TEXT NOT NULL CHECK (topic = 'linear.session-reply.v1'),
        aggregate_id TEXT NOT NULL UNIQUE
            REFERENCES notification_replies(reply_id),
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('pending', 'delivered', 'dead_letter'))
    )
    """,
    """
    CREATE INDEX notification_reply_outbox_pending_order
    ON notification_reply_outbox(state, created_at, outbox_id)
    """,
)

_MIGRATION_3 = (
    """
    CREATE TABLE linear_projection_outbox (
        outbox_id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        topic TEXT NOT NULL CHECK (topic = 'linear.accepted-result.v1'),
        aggregate_id TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('pending', 'delivered', 'dead_letter')),
        UNIQUE (topic, aggregate_id)
    )
    """,
    """
    CREATE INDEX linear_projection_outbox_pending_order
    ON linear_projection_outbox(state, created_at, outbox_id)
    """,
    """
    CREATE TABLE linear_delivery_claims (
        topic TEXT NOT NULL,
        outbox_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        claim_id TEXT NOT NULL UNIQUE,
        claimed_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        PRIMARY KEY (topic, outbox_id)
    )
    """,
    """
    CREATE INDEX linear_delivery_claim_expiry
    ON linear_delivery_claims(expires_at, topic, outbox_id)
    """,
    """
    CREATE TABLE linear_delivery_status (
        topic TEXT NOT NULL,
        outbox_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        attempt_count INTEGER NOT NULL CHECK (attempt_count >= 0),
        last_error_code TEXT,
        last_claim_id TEXT,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (topic, outbox_id)
    )
    """,
    """
    CREATE TABLE linear_delivery_receipts (
        receipt_id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        credential_identity TEXT NOT NULL,
        direction TEXT NOT NULL CHECK (direction = 'outbound'),
        topic TEXT NOT NULL,
        outbox_id TEXT NOT NULL,
        workspace_id TEXT NOT NULL,
        issue_id TEXT NOT NULL,
        thread_id TEXT NOT NULL,
        comment_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        task_id TEXT NOT NULL,
        source_marker_json TEXT,
        payload_digest TEXT NOT NULL,
        transport_digest TEXT NOT NULL,
        marker_text TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (topic, outbox_id),
        UNIQUE (workspace_id, comment_id)
    )
    """,
    """
    CREATE INDEX linear_delivery_receipt_thread
    ON linear_delivery_receipts(workspace_id, issue_id, thread_id, created_at)
    """,
    """
    CREATE TABLE verified_linear_ingress_receipts (
        receipt_id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        provider TEXT NOT NULL CHECK (provider = 'linear'),
        authenticated_app_id TEXT NOT NULL,
        mentioned_app_id TEXT NOT NULL,
        author_id TEXT NOT NULL,
        credential_identity TEXT NOT NULL,
        workspace_id TEXT NOT NULL,
        issue_id TEXT NOT NULL,
        thread_id TEXT NOT NULL,
        comment_id TEXT NOT NULL,
        webhook_event_id TEXT,
        task_id TEXT NOT NULL,
        source_marker_json TEXT,
        command_digest TEXT NOT NULL,
        observed_payload_digest TEXT NOT NULL,
        binding_digest TEXT NOT NULL,
        verified_at TEXT NOT NULL,
        UNIQUE (provider, workspace_id, comment_id),
        UNIQUE (provider, workspace_id, webhook_event_id)
    )
    """,
    """
    CREATE INDEX verified_linear_ingress_origin
    ON verified_linear_ingress_receipts(
        workspace_id, issue_id, thread_id, comment_id
    )
    """,
)


def _error(
    code: str,
    message: str,
    *,
    context: dict[str, Any] | None = None,
    remediation: str | None = None,
) -> RCPError:
    return RCPError(
        code=code,
        message=message,
        remediation=remediation,
        context=context or {},
    )


def _require_text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _error(
            "invalid_runtime_record",
            f"{field} must be a non-empty string.",
            context={"field": field},
        )
    return value


def _require_digest(value: str, field: str) -> str:
    if not isinstance(value, str) or _DIGEST_PATTERN.fullmatch(value) is None:
        raise _error(
            "invalid_runtime_record",
            f"{field} must be a canonical SHA-256 digest.",
            context={"field": field},
        )
    return value


def _encode_time(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise _error(
            "invalid_runtime_timestamp",
            "Runtime timestamps must be timezone-aware datetime values.",
        )
    normalized = value.astimezone(UTC)
    return normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _decode_time(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _json_object(value: Mapping[str, Any] | None) -> tuple[dict[str, Any], str]:
    copied = dict(value or {})
    try:
        encoded = json.dumps(
            copied,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise _error(
            "invalid_runtime_payload",
            "Runtime payload must contain only finite JSON values.",
        ) from exc
    return json.loads(encoded), encoded


def _decode_object(value: str | None) -> dict[str, Any] | None:
    if value is None:
        return None
    decoded = json.loads(value)
    if not isinstance(decoded, dict):
        raise _error("runtime_store_corrupt", "Stored runtime payload is not an object.")
    return decoded


def _normalize_identity(value: str | None) -> str:
    return " ".join((value or "").casefold().split())


def hash_session_token(plaintext_token: str) -> str:
    if not isinstance(plaintext_token, str) or not plaintext_token:
        raise ValueError("session token must be a non-empty string")
    return "sha256:" + hashlib.sha256(plaintext_token.encode("utf-8")).hexdigest()


def _attention_descriptor(update: StatusUpdate) -> tuple[str, str, int]:
    if update.decision_needed is not None:
        return "needs_decision", _normalize_identity(update.decision_needed.question), 10
    if update.status == StatusKind.BLOCKED:
        category = _normalize_identity(update.blocker_category)
        if category in {"failed", "lost", "failed_or_lost"}:
            return "failed_or_lost", category, 50
        if category in {"stale", "needs_rerun", "stale_or_needs_rerun"}:
            return "stale_or_needs_rerun", category, 40
        if category == "waiting":
            return "waiting", category, 70
        return "blocked", category or "blocked", 20
    if update.status == StatusKind.READY_FOR_REVIEW:
        return "needs_review", "review", 30
    if update.status == StatusKind.NEEDS_INPUT:
        return "needs_input", "input", 10
    return "running", "running", 60


def attention_dedupe_key(project_id: str, update: StatusUpdate) -> str:
    kind, identity, _ = _attention_descriptor(update)
    payload = {
        "version": 1,
        "project_id": project_id,
        "task_id": update.task_id,
        "session_id": update.session_id,
        "kind": kind,
        "identity": identity,
    }
    return "sha256:" + hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _attention_evidence_digest(update: StatusUpdate) -> str:
    payload = {
        "status": update.status.value,
        "summary": update.summary,
        "evidence": [item.model_dump(mode="json") for item in update.evidence],
        "blocker_category": update.blocker_category,
        "blocker_detail": update.blocker_detail,
        "decision_needed": (
            update.decision_needed.model_dump(mode="json")
            if update.decision_needed is not None
            else None
        ),
        "suggested_next_action": update.suggested_next_action,
    }
    return "sha256:" + hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


class RuntimeStore:
    """Repo-local, durable runtime state backed by single-writer SQLite."""

    def __init__(
        self,
        database_path: Path,
        *,
        busy_timeout_ms: int = _DEFAULT_BUSY_TIMEOUT_MS,
    ) -> None:
        if not isinstance(busy_timeout_ms, int) or not 1 <= busy_timeout_ms <= 60_000:
            raise ValueError("busy_timeout_ms must be between 1 and 60000")
        self._path = Path(database_path).expanduser().absolute()
        self._busy_timeout_ms = busy_timeout_ms
        self._lock = threading.RLock()
        self._closed = True
        self._prepare_database_file()
        try:
            self._connection = sqlite3.connect(
                str(self._path),
                timeout=busy_timeout_ms / 1000,
                isolation_level=None,
                check_same_thread=False,
            )
            self._connection.row_factory = sqlite3.Row
            self._closed = False
            self._configure_connection()
            self._migrate()
            self._harden_permissions()
        except Exception:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
            self._closed = True
            raise

    @property
    def database_path(self) -> Path:
        return self._path

    @property
    def schema_version(self) -> int:
        with self._read_lock():
            row = self._connection.execute(
                "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
            ).fetchone()
        return int(row["version"])

    def settings(self) -> dict[str, int | str]:
        with self._read_lock():
            journal_mode = self._connection.execute("PRAGMA journal_mode").fetchone()[0]
            foreign_keys = self._connection.execute("PRAGMA foreign_keys").fetchone()[0]
            busy_timeout = self._connection.execute("PRAGMA busy_timeout").fetchone()[0]
            synchronous = self._connection.execute("PRAGMA synchronous").fetchone()[0]
        return {
            "journal_mode": str(journal_mode).lower(),
            "foreign_keys": int(foreign_keys),
            "busy_timeout": int(busy_timeout),
            "synchronous": int(synchronous),
        }

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._harden_permissions()
            self._connection.close()
            self._closed = True

    def __enter__(self) -> RuntimeStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _prepare_database_file(self) -> None:
        self._reject_symlink_components(self._path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._reject_symlink_components(self._path)
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self._path, flags, 0o600)
        except FileExistsError:
            observed = self._path.lstat()
            if stat.S_ISLNK(observed.st_mode) or not stat.S_ISREG(observed.st_mode):
                raise _error(
                    "unsafe_runtime_database_path",
                    "Runtime database path must be a regular file, not a link.",
                    context={"path": str(self._path)},
                ) from None
        except OSError as exc:
            raise _error(
                "runtime_database_open_failed",
                "Runtime database file could not be created safely.",
                context={"path": str(self._path)},
            ) from exc
        else:
            os.close(descriptor)

    @staticmethod
    def _reject_symlink_components(path: Path) -> None:
        candidates = (path, *path.parents)
        for candidate in reversed(candidates):
            try:
                observed = candidate.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(observed.st_mode):
                raise _error(
                    "unsafe_runtime_database_path",
                    "Runtime database path must not traverse a symbolic link.",
                    context={"path": str(path)},
                )

    def _configure_connection(self) -> None:
        journal_mode = self._connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if str(journal_mode).lower() != "wal":
            raise _error(
                "runtime_wal_unavailable",
                "Runtime database could not enable WAL mode.",
            )
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute("PRAGMA trusted_schema=OFF")

    def _migrate(self) -> None:
        with self._write_transaction() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL
                )
                """
            )
            rows = connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
            versions = [int(row["version"]) for row in rows]
            if versions and versions[-1] > _SCHEMA_VERSION:
                raise _error(
                    "runtime_schema_too_new",
                    "Runtime database schema is newer than this researchctl build.",
                    context={"found": versions[-1], "supported": _SCHEMA_VERSION},
                )
            expected_history = list(range(1, (versions[-1] if versions else 0) + 1))
            if versions != expected_history:
                raise _error(
                    "runtime_schema_invalid",
                    "Runtime database migration history is not contiguous.",
                    context={"versions": versions},
                )
            if 1 not in versions:
                for statement in _MIGRATION_1:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations(version, name) VALUES (1, 'initial')"
                )
            if 2 not in versions:
                for statement in _MIGRATION_2:
                    connection.execute(statement)
                connection.execute(
                    """
                    INSERT INTO schema_migrations(version, name)
                    VALUES (2, 'session_notifications')
                    """
                )
            if 3 not in versions:
                for statement in _MIGRATION_3:
                    connection.execute(statement)
                connection.execute(
                    """
                    INSERT INTO schema_migrations(version, name)
                    VALUES (3, 'linear_delivery')
                    """
                )
            connection.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")

    @contextmanager
    def _read_lock(self) -> Iterator[None]:
        with self._lock:
            self._ensure_open()
            yield

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._ensure_open()
            try:
                self._connection.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                raise _error(
                    "runtime_store_busy",
                    "Runtime store writer is busy.",
                    remediation="Retry the operation with the same idempotency key.",
                ) from exc
            try:
                yield self._connection
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
            finally:
                self._harden_permissions()

    def _ensure_open(self) -> None:
        if self._closed:
            raise _error("runtime_store_closed", "Runtime store is closed.")

    def _harden_permissions(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(self._path) + suffix)
            try:
                os.chmod(candidate, 0o600, follow_symlinks=False)
            except (FileNotFoundError, NotImplementedError, PermissionError):
                continue

    def begin_operation(
        self,
        project_id: str,
        command: str,
        idempotency_key: str,
        request_digest: str,
        operation_id: str,
        observed_at: datetime,
    ) -> OperationRecord:
        _require_text(project_id, "project_id")
        _require_text(command, "command")
        _require_text(idempotency_key, "idempotency_key")
        _require_digest(request_digest, "request_digest")
        _require_text(operation_id, "operation_id")
        timestamp = _encode_time(observed_at)
        started_payload, started_json = _json_object(
            {
                "project_id": project_id,
                "command": command,
                "idempotency_key": idempotency_key,
                "request_digest": request_digest,
            }
        )
        del started_payload
        with self._write_transaction() as connection:
            existing = connection.execute(
                """
                SELECT * FROM operations
                WHERE project_id = ? AND command = ? AND idempotency_key = ?
                """,
                (project_id, command, idempotency_key),
            ).fetchone()
            if existing is not None:
                if existing["request_digest"] != request_digest:
                    raise _error(
                        "idempotency_conflict",
                        "Idempotency key is already bound to a different request.",
                        context={
                            "project_id": project_id,
                            "command": command,
                            "idempotency_key": idempotency_key,
                            "operation_id": existing["operation_id"],
                        },
                    )
                selected_operation_id = str(existing["operation_id"])
            else:
                collision = connection.execute(
                    "SELECT operation_id FROM operations WHERE operation_id = ?",
                    (operation_id,),
                ).fetchone()
                if collision is not None:
                    raise _error(
                        "operation_id_conflict",
                        "Operation ID is already bound to another operation.",
                        context={"operation_id": operation_id},
                    )
                connection.execute(
                    """
                    INSERT INTO operations(
                        operation_id, project_id, command, idempotency_key,
                        request_digest, state, started_at
                    ) VALUES (?, ?, ?, ?, ?, 'running', ?)
                    """,
                    (
                        operation_id,
                        project_id,
                        command,
                        idempotency_key,
                        request_digest,
                        timestamp,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO operation_events(
                        operation_id, sequence, kind, observed_at, payload_json
                    ) VALUES (?, 0, 'operation_started', ?, ?)
                    """,
                    (operation_id, timestamp, started_json),
                )
                selected_operation_id = operation_id
        operation = self.get_operation(selected_operation_id)
        assert operation is not None
        return operation

    def append_operation_event(
        self,
        operation_id: str,
        kind: str,
        observed_at: datetime,
        payload: Mapping[str, Any] | None = None,
    ) -> OperationEvent:
        _require_text(operation_id, "operation_id")
        _require_text(kind, "kind")
        if kind in {"operation_started", "operation_finished"}:
            raise _error(
                "reserved_operation_event",
                "Reserved operation event kinds are managed by RuntimeStore.",
                context={"kind": kind},
            )
        timestamp = _encode_time(observed_at)
        copied, payload_json = _json_object(payload)
        with self._write_transaction() as connection:
            operation = connection.execute(
                "SELECT state FROM operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if operation is None:
                raise _error(
                    "operation_not_found",
                    "Operation was not found.",
                    context={"operation_id": operation_id},
                )
            if operation["state"] == "terminal":
                raise _error(
                    "operation_terminal",
                    "Terminal operation journals cannot be appended.",
                    context={"operation_id": operation_id},
                )
            row = connection.execute(
                """
                SELECT COALESCE(MAX(sequence), -1) + 1 AS next_sequence
                FROM operation_events WHERE operation_id = ?
                """,
                (operation_id,),
            ).fetchone()
            sequence = int(row["next_sequence"])
            connection.execute(
                """
                INSERT INTO operation_events(
                    operation_id, sequence, kind, observed_at, payload_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (operation_id, sequence, kind, timestamp, payload_json),
            )
        return OperationEvent(
            operation_id=operation_id,
            sequence=sequence,
            kind=kind,
            observed_at=observed_at.astimezone(UTC),
            payload=copied,
        )

    def finish_operation(
        self,
        operation_id: str,
        terminal_result: str,
        observed_at: datetime,
        result: Mapping[str, Any] | None = None,
    ) -> OperationRecord:
        _require_text(operation_id, "operation_id")
        _require_text(terminal_result, "terminal_result")
        timestamp = _encode_time(observed_at)
        copied, result_json = _json_object(result)
        stored_result_json = result_json if result is not None else None
        finish_payload = {"terminal_result": terminal_result, "result": copied}
        _, finish_json = _json_object(finish_payload)
        with self._write_transaction() as connection:
            operation = connection.execute(
                "SELECT * FROM operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if operation is None:
                raise _error(
                    "operation_not_found",
                    "Operation was not found.",
                    context={"operation_id": operation_id},
                )
            if operation["state"] == "terminal":
                if (
                    operation["terminal_result"] != terminal_result
                    or operation["result_json"] != stored_result_json
                ):
                    raise _error(
                        "operation_terminal",
                        "Terminal operation result is absorbing.",
                        context={"operation_id": operation_id},
                    )
            else:
                row = connection.execute(
                    """
                    SELECT COALESCE(MAX(sequence), -1) + 1 AS next_sequence
                    FROM operation_events WHERE operation_id = ?
                    """,
                    (operation_id,),
                ).fetchone()
                sequence = int(row["next_sequence"])
                connection.execute(
                    """
                    INSERT INTO operation_events(
                        operation_id, sequence, kind, observed_at, payload_json
                    ) VALUES (?, ?, 'operation_finished', ?, ?)
                    """,
                    (operation_id, sequence, timestamp, finish_json),
                )
                connection.execute(
                    """
                    UPDATE operations
                    SET state = 'terminal', finished_at = ?, terminal_result = ?,
                        result_json = ?
                    WHERE operation_id = ?
                    """,
                    (timestamp, terminal_result, stored_result_json, operation_id),
                )
        finished = self.get_operation(operation_id)
        assert finished is not None
        return finished

    def get_operation(self, operation_id: str) -> OperationRecord | None:
        with self._read_lock():
            row = self._connection.execute(
                "SELECT * FROM operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            return self._operation_from_row(self._connection, row) if row else None

    def list_operations(self, project_id: str | None = None) -> tuple[OperationRecord, ...]:
        query = "SELECT * FROM operations"
        parameters: tuple[object, ...] = ()
        if project_id is not None:
            query += " WHERE project_id = ?"
            parameters = (project_id,)
        query += " ORDER BY started_at, operation_id"
        with self._read_lock():
            rows = self._connection.execute(query, parameters).fetchall()
            return tuple(self._operation_from_row(self._connection, row) for row in rows)

    @staticmethod
    def _operation_from_row(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> OperationRecord:
        event_rows = connection.execute(
            """
            SELECT * FROM operation_events
            WHERE operation_id = ? ORDER BY sequence
            """,
            (row["operation_id"],),
        ).fetchall()
        events = tuple(
            OperationEvent(
                operation_id=event["operation_id"],
                sequence=int(event["sequence"]),
                kind=event["kind"],
                observed_at=_decode_time(event["observed_at"]),  # type: ignore[arg-type]
                payload=_decode_object(event["payload_json"]) or {},
            )
            for event in event_rows
        )
        return OperationRecord(
            operation_id=row["operation_id"],
            project_id=row["project_id"],
            command=row["command"],
            idempotency_key=row["idempotency_key"],
            request_digest=row["request_digest"],
            state=row["state"],
            started_at=_decode_time(row["started_at"]),  # type: ignore[arg-type]
            finished_at=_decode_time(row["finished_at"]),
            terminal_result=row["terminal_result"],
            result=_decode_object(row["result_json"]),
            events=events,
        )

    def save_session(self, session: RuntimeSession) -> RuntimeSession:
        state = SessionState(session.state)
        created_at = _encode_time(session.created_at)
        updated_at = _encode_time(session.updated_at)
        if updated_at < created_at:
            raise _error(
                "invalid_runtime_session",
                "Session updated_at cannot precede created_at.",
                context={"session_id": session.session_id},
            )
        for field_name in ("session_id", "project_id", "task_id"):
            _require_text(getattr(session, field_name), field_name)
        if session.continued_from == session.session_id:
            raise _error(
                "invalid_runtime_session",
                "Session cannot continue from itself.",
                context={"session_id": session.session_id},
            )
        if session.actor_token_digest is not None:
            _require_digest(session.actor_token_digest, "actor_token_digest")
        _, metadata_json = _json_object(session.metadata)
        with self._write_transaction() as connection:
            existing_row = connection.execute(
                "SELECT * FROM runtime_sessions WHERE session_id = ?",
                (session.session_id,),
            ).fetchone()
            if existing_row is None:
                if session.continued_from is not None:
                    source = connection.execute(
                        "SELECT project_id, task_id FROM runtime_sessions WHERE session_id = ?",
                        (session.continued_from,),
                    ).fetchone()
                    if source is None:
                        raise _error(
                            "continued_session_not_found",
                            "Continued-from Session was not found.",
                            context={"continued_from": session.continued_from},
                        )
                    if (
                        source["project_id"] != session.project_id
                        or source["task_id"] != session.task_id
                    ):
                        raise _error(
                            "session_identity_conflict",
                            "Continued Session must keep its project and Task identity.",
                            context={"session_id": session.session_id},
                        )
                connection.execute(
                    """
                    INSERT INTO runtime_sessions(
                        session_id, project_id, task_id, state, created_at, updated_at,
                        host, branch, worktree_path, continued_from,
                        actor_token_digest, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session.session_id,
                        session.project_id,
                        session.task_id,
                        state.value,
                        created_at,
                        updated_at,
                        session.host,
                        session.branch,
                        session.worktree_path,
                        session.continued_from,
                        session.actor_token_digest,
                        metadata_json,
                    ),
                )
            else:
                existing = self._session_from_row(existing_row)
                immutable_existing = (
                    existing.project_id,
                    existing.task_id,
                    existing.created_at,
                    existing.host,
                    existing.branch,
                    existing.worktree_path,
                    existing.continued_from,
                    existing.actor_token_digest,
                )
                immutable_new = (
                    session.project_id,
                    session.task_id,
                    session.created_at.astimezone(UTC),
                    session.host,
                    session.branch,
                    session.worktree_path,
                    session.continued_from,
                    session.actor_token_digest,
                )
                if immutable_existing != immutable_new:
                    raise _error(
                        "session_identity_conflict",
                        "Runtime Session identity fields are immutable.",
                        context={"session_id": session.session_id},
                    )
                if existing.state == SessionState.LOST and session != existing:
                    raise _error(
                        "session_terminal",
                        "A lost Session is terminal and cannot be changed.",
                        context={"session_id": session.session_id},
                    )
                if session.updated_at.astimezone(UTC) < existing.updated_at:
                    raise _error(
                        "stale_session_observation",
                        "Session observation is older than the stored record.",
                        context={"session_id": session.session_id},
                    )
                if (
                    session.updated_at.astimezone(UTC) == existing.updated_at
                    and session != existing
                ):
                    raise _error(
                        "stale_session_observation",
                        "Session changes require a newer observation timestamp.",
                        context={"session_id": session.session_id},
                    )
                connection.execute(
                    """
                    UPDATE runtime_sessions
                    SET state = ?, updated_at = ?, metadata_json = ?
                    WHERE session_id = ?
                    """,
                    (state.value, updated_at, metadata_json, session.session_id),
                )
                if state in {SessionState.STOPPED, SessionState.LOST}:
                    connection.execute(
                        """
                        UPDATE session_notifications
                        SET route = 'manager_exception',
                            routed_at = ?,
                            fallback_reason = ?,
                            revision = revision + 1
                        WHERE session_id = ?
                          AND route = 'session'
                          AND state IN ('pending', 'acknowledged')
                        """,
                        (
                            updated_at,
                            f"session_{state.value}",
                            session.session_id,
                        ),
                    )
        saved = self.get_session(session.session_id)
        assert saved is not None
        return saved

    def update_session_state(
        self,
        session_id: str,
        state: SessionState,
        observed_at: datetime,
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> RuntimeSession:
        existing = self.get_session(session_id)
        if existing is None:
            raise _error(
                "session_not_found",
                "Runtime Session was not found.",
                context={"session_id": session_id},
            )
        replacement = replace(
            existing,
            state=SessionState(state),
            updated_at=observed_at,
            metadata=dict(metadata) if metadata is not None else existing.metadata,
        )
        return self.save_session(replacement)

    def authenticate_session(
        self,
        session_id: str,
        plaintext_token: str,
    ) -> RuntimeSession:
        try:
            candidate_digest = hash_session_token(plaintext_token)
            token_was_valid = True
        except ValueError:
            candidate_digest = _DUMMY_TOKEN_DIGEST
            token_was_valid = False
        with self._read_lock():
            row = self._connection.execute(
                "SELECT * FROM runtime_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            stored_digest = (
                row["actor_token_digest"]
                if row is not None and row["actor_token_digest"] is not None
                else _DUMMY_TOKEN_DIGEST
            )
            matched = hmac.compare_digest(stored_digest, candidate_digest)
            if (
                not token_was_valid
                or row is None
                or row["actor_token_digest"] is None
                or not matched
            ):
                raise _error(
                    "unauthorized_actor",
                    "Session authentication failed.",
                    context={"session_id": session_id},
                )
            return self._session_from_row(row)

    def rotate_session_token(
        self,
        session_id: str,
        new_actor_token_digest: str,
        observed_at: datetime,
    ) -> RuntimeSession:
        _require_digest(new_actor_token_digest, "actor_token_digest")
        timestamp = _encode_time(observed_at)
        permitted = {
            SessionState.PREPARING.value,
            SessionState.IDLE.value,
            SessionState.STOPPED.value,
        }
        with self._write_transaction() as connection:
            row = connection.execute(
                "SELECT * FROM runtime_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise _error(
                    "session_not_found",
                    "Runtime Session was not found.",
                    context={"session_id": session_id},
                )
            if row["state"] not in permitted:
                raise _error(
                    "session_token_rotation_forbidden",
                    "Session token can rotate only before launch or while resumable.",
                    context={"session_id": session_id, "state": row["state"]},
                )
            if row["actor_token_digest"] == new_actor_token_digest:
                pass
            elif timestamp <= row["updated_at"]:
                raise _error(
                    "stale_session_observation",
                    "Session token rotation requires a newer observation timestamp.",
                    context={"session_id": session_id},
                )
            else:
                connection.execute(
                    """
                    UPDATE runtime_sessions
                    SET actor_token_digest = ?, updated_at = ?
                    WHERE session_id = ?
                    """,
                    (new_actor_token_digest, timestamp, session_id),
                )
        rotated = self.get_session(session_id)
        assert rotated is not None
        return rotated

    def get_session(self, session_id: str) -> RuntimeSession | None:
        with self._read_lock():
            row = self._connection.execute(
                "SELECT * FROM runtime_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            return self._session_from_row(row) if row else None

    def list_sessions(
        self,
        project_id: str | None = None,
        *,
        task_id: str | None = None,
        state: SessionState | None = None,
    ) -> tuple[RuntimeSession, ...]:
        clauses: list[str] = []
        parameters: list[object] = []
        if project_id is not None:
            clauses.append("project_id = ?")
            parameters.append(project_id)
        if task_id is not None:
            clauses.append("task_id = ?")
            parameters.append(task_id)
        if state is not None:
            clauses.append("state = ?")
            parameters.append(SessionState(state).value)
        query = "SELECT * FROM runtime_sessions"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at, session_id"
        with self._read_lock():
            rows = self._connection.execute(query, parameters).fetchall()
            return tuple(self._session_from_row(row) for row in rows)

    @staticmethod
    def _session_from_row(row: sqlite3.Row) -> RuntimeSession:
        return RuntimeSession(
            session_id=row["session_id"],
            project_id=row["project_id"],
            task_id=row["task_id"],
            state=SessionState(row["state"]),
            created_at=_decode_time(row["created_at"]),  # type: ignore[arg-type]
            updated_at=_decode_time(row["updated_at"]),  # type: ignore[arg-type]
            host=row["host"],
            branch=row["branch"],
            worktree_path=row["worktree_path"],
            continued_from=row["continued_from"],
            actor_token_digest=row["actor_token_digest"],
            metadata=_decode_object(row["metadata_json"]) or {},
        )

    def create_notification(
        self,
        *,
        notification_id: str,
        project_id: str,
        task_id: str,
        session_id: str,
        commit_sha: str,
        message: str,
        origin: SessionNotificationOrigin,
        created_at: datetime,
    ) -> SessionNotification:
        for field, value in (
            ("notification_id", notification_id),
            ("project_id", project_id),
            ("task_id", task_id),
            ("session_id", session_id),
            ("message", message),
        ):
            _require_text(value, field)
        if _OBJECT_ID_PATTERN.fullmatch(commit_sha) is None:
            raise _error(
                "invalid_notification_commit",
                "Notification commit must be a full Git object ID.",
                context={"commit_sha": commit_sha},
            )
        timestamp = _encode_time(created_at)
        origin_data, origin_json = _json_object(
            origin.model_dump(mode="json", exclude_none=True)
        )
        del origin_data
        with self._write_transaction() as connection:
            session = connection.execute(
                "SELECT project_id, task_id, state FROM runtime_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if session is None:
                raise _error(
                    "notification_session_not_found",
                    "Notification Session was not found in the runtime store.",
                    context={"session_id": session_id},
                )
            if session["project_id"] != project_id or session["task_id"] != task_id:
                raise _error(
                    "notification_session_mismatch",
                    "Notification does not match its runtime Session identity.",
                    context={"notification_id": notification_id},
                )

            source_owner = connection.execute(
                """
                SELECT notification_id FROM session_notifications
                WHERE project_id = ? AND source_comment_id = ? AND session_id = ?
                """,
                (project_id, origin.comment_id, session_id),
            ).fetchone()
            if (
                source_owner is not None
                and source_owner["notification_id"] != notification_id
            ):
                raise _error(
                    "notification_source_conflict",
                    "Linear source comment is already bound to another notification.",
                    context={
                        "notification_id": notification_id,
                        "existing_notification_id": source_owner["notification_id"],
                    },
                )

            existing = connection.execute(
                "SELECT * FROM session_notifications WHERE notification_id = ?",
                (notification_id,),
            ).fetchone()
            if existing is not None:
                observed_identity = (
                    existing["project_id"],
                    existing["task_id"],
                    existing["session_id"],
                    existing["commit_sha"],
                    existing["message_text"],
                    existing["linear_issue_id"],
                    existing["source_thread_id"],
                    existing["source_comment_id"],
                    existing["origin_json"],
                )
                requested_identity = (
                    project_id,
                    task_id,
                    session_id,
                    commit_sha,
                    message,
                    origin.issue_id,
                    origin.thread_id,
                    origin.comment_id,
                    origin_json,
                )
                if observed_identity != requested_identity:
                    raise _error(
                        "notification_id_conflict",
                        "Notification ID is already bound to different content.",
                        context={"notification_id": notification_id},
                    )
            else:
                terminal = session["state"] in {
                    SessionState.STOPPED.value,
                    SessionState.LOST.value,
                }
                route = (
                    NotificationRoute.MANAGER_EXCEPTION
                    if terminal
                    else NotificationRoute.SESSION
                )
                fallback_reason = (
                    f"session_{session['state']}" if terminal else None
                )
                connection.execute(
                    """
                    INSERT INTO session_notifications(
                        notification_id, project_id, task_id, session_id,
                        linear_issue_id, source_thread_id, source_comment_id,
                        commit_sha, message_text, origin_json, route, state,
                        revision, created_at, routed_at, fallback_reason
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 1, ?, ?, ?)
                    """,
                    (
                        notification_id,
                        project_id,
                        task_id,
                        session_id,
                        origin.issue_id,
                        origin.thread_id,
                        origin.comment_id,
                        commit_sha,
                        message,
                        origin_json,
                        route.value,
                        timestamp,
                        timestamp,
                        fallback_reason,
                    ),
                )
        notification = self.get_notification(notification_id)
        assert notification is not None
        return notification

    def get_notification(self, notification_id: str) -> SessionNotification | None:
        with self._read_lock():
            row = self._connection.execute(
                "SELECT * FROM session_notifications WHERE notification_id = ?",
                (notification_id,),
            ).fetchone()
            return self._notification_from_row(row) if row else None

    def list_notifications(
        self,
        project_id: str,
        *,
        session_id: str | None = None,
        route: NotificationRoute | None = None,
        include_closed: bool = False,
        limit: int = 100,
    ) -> tuple[SessionNotification, ...]:
        if not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("notification limit must be between 1 and 1000")
        clauses = ["project_id = ?"]
        parameters: list[object] = [project_id]
        if session_id is not None:
            clauses.append("session_id = ?")
            parameters.append(session_id)
        if route is not None:
            clauses.append("route = ?")
            parameters.append(NotificationRoute(route).value)
        if not include_closed:
            clauses.append("state != 'replied'")
        query = (
            "SELECT * FROM session_notifications WHERE "
            + " AND ".join(clauses)
            + " ORDER BY created_at, notification_id LIMIT ?"
        )
        parameters.append(limit)
        with self._read_lock():
            rows = self._connection.execute(query, parameters).fetchall()
            return tuple(self._notification_from_row(row) for row in rows)

    @staticmethod
    def _notification_from_row(row: sqlite3.Row) -> SessionNotification:
        return SessionNotification(
            notification_id=row["notification_id"],
            project_id=row["project_id"],
            task_id=row["task_id"],
            session_id=row["session_id"],
            commit_sha=row["commit_sha"],
            message=row["message_text"],
            origin=SessionNotificationOrigin.model_validate_json(row["origin_json"]),
            route=NotificationRoute(row["route"]),
            state=NotificationState(row["state"]),
            revision=int(row["revision"]),
            created_at=_decode_time(row["created_at"]),  # type: ignore[arg-type]
            routed_at=_decode_time(row["routed_at"]),  # type: ignore[arg-type]
            fallback_reason=row["fallback_reason"],
            acknowledged_by=row["acknowledged_by"],
            acknowledged_at=_decode_time(row["acknowledged_at"]),
            reply_id=row["reply_id"],
            replied_by=row["replied_by"],
            replied_at=_decode_time(row["replied_at"]),
        )

    def ack_notification(
        self,
        notification_id: str,
        actor_id: str,
        observed_at: datetime,
        *,
        expected_revision: int,
        operation_id: str,
    ) -> SessionNotification:
        _require_text(actor_id, "actor_id")
        timestamp = _encode_time(observed_at)
        with self._write_transaction() as connection:
            row = connection.execute(
                "SELECT * FROM session_notifications WHERE notification_id = ?",
                (notification_id,),
            ).fetchone()
            if row is None:
                raise _error(
                    "notification_not_found",
                    "Session notification was not found.",
                    context={"notification_id": notification_id},
                )
            same_operation_replay = (
                row["state"] == NotificationState.ACKNOWLEDGED.value
                and row["acknowledged_operation_id"] == operation_id
                and row["acknowledged_by"] == actor_id
            )
            if same_operation_replay:
                pass
            elif int(row["revision"]) != expected_revision:
                raise _error(
                    "stale_notification",
                    "Session notification changed before the requested action.",
                    context={
                        "notification_id": notification_id,
                        "expected_revision": expected_revision,
                        "observed_revision": int(row["revision"]),
                    },
                )
            elif row["state"] == NotificationState.REPLIED.value:
                raise _error(
                    "notification_closed",
                    "A replied Session notification is already closed.",
                    context={"notification_id": notification_id},
                )
            elif row["state"] == NotificationState.PENDING.value:
                self._require_operation(connection, operation_id)
                connection.execute(
                    """
                    UPDATE session_notifications
                    SET state = 'acknowledged', revision = revision + 1,
                        acknowledged_by = ?, acknowledged_at = ?,
                        acknowledged_operation_id = ?
                    WHERE notification_id = ?
                    """,
                    (actor_id, timestamp, operation_id, notification_id),
                )
        acknowledged = self.get_notification(notification_id)
        assert acknowledged is not None
        return acknowledged

    def reply_notification(
        self,
        *,
        notification_id: str,
        reply_id: str,
        actor_id: str,
        body: str,
        observed_at: datetime,
        expected_revision: int,
        operation_id: str,
    ) -> PublishedNotificationReply:
        _require_text(reply_id, "reply_id")
        _require_text(actor_id, "actor_id")
        _require_text(body, "body")
        timestamp = _encode_time(observed_at)
        with self._write_transaction() as connection:
            row = connection.execute(
                "SELECT * FROM session_notifications WHERE notification_id = ?",
                (notification_id,),
            ).fetchone()
            if row is None:
                raise _error(
                    "notification_not_found",
                    "Session notification was not found.",
                    context={"notification_id": notification_id},
                )
            known_reply = connection.execute(
                "SELECT * FROM notification_replies WHERE reply_id = ?",
                (reply_id,),
            ).fetchone()
            if known_reply is not None:
                if (
                    known_reply["notification_id"] != notification_id
                    or known_reply["actor_id"] != actor_id
                    or known_reply["body_text"] != body
                ):
                    raise _error(
                        "notification_reply_id_conflict",
                        "Reply ID is already bound to different content.",
                        context={"reply_id": reply_id},
                    )
            else:
                if int(row["revision"]) != expected_revision:
                    raise _error(
                        "stale_notification",
                        "Session notification changed before the requested action.",
                        context={
                            "notification_id": notification_id,
                            "expected_revision": expected_revision,
                            "observed_revision": int(row["revision"]),
                        },
                    )
                if row["state"] == NotificationState.REPLIED.value:
                    raise _error(
                        "notification_closed",
                        "A Session notification accepts only one reply.",
                        context={"notification_id": notification_id},
                    )
                self._require_operation(connection, operation_id)
                origin = _decode_object(row["origin_json"]) or {}
                source_marker = origin.get("source_marker")
                report_id = (
                    source_marker.get("report_id")
                    if isinstance(source_marker, dict)
                    else None
                )
                payload = {
                    "version": 1,
                    "transport": "linear",
                    "workspace_id": origin.get("workspace_id"),
                    "linear_issue_id": row["linear_issue_id"],
                    "source_thread_id": row["source_thread_id"],
                    "source_comment_id": row["source_comment_id"],
                    "notification_id": notification_id,
                    "reply_id": reply_id,
                    "task_id": row["task_id"],
                    "session_id": row["session_id"],
                    "commit_sha": row["commit_sha"],
                    "body": body,
                    "actor_id": actor_id,
                    "replied_at": timestamp,
                    "marker": {
                        "agent_id": actor_id,
                        "task_id": row["task_id"],
                        "session_id": row["session_id"],
                        "report_id": report_id,
                        "notification_id": notification_id,
                        "reply_id": reply_id,
                    },
                }
                payload_bytes = canonical_json_bytes(payload)
                payload_json = payload_bytes.decode("utf-8")
                payload_digest = (
                    "sha256:" + hashlib.sha256(payload_bytes).hexdigest()
                )
                connection.execute(
                    """
                    INSERT INTO notification_replies(
                        reply_id, notification_id, project_id, task_id,
                        session_id, actor_id, body_text, payload_digest,
                        payload_json, created_at, operation_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        reply_id,
                        notification_id,
                        row["project_id"],
                        row["task_id"],
                        row["session_id"],
                        actor_id,
                        body,
                        payload_digest,
                        payload_json,
                        timestamp,
                        operation_id,
                    ),
                )
                connection.execute(
                    """
                    UPDATE session_notifications
                    SET state = 'replied', revision = revision + 1,
                        reply_id = ?, replied_by = ?, replied_at = ?
                    WHERE notification_id = ?
                    """,
                    (reply_id, actor_id, timestamp, notification_id),
                )
                connection.execute(
                    """
                    INSERT INTO notification_reply_outbox(
                        outbox_id, project_id, topic, aggregate_id,
                        payload_json, created_at, state
                    ) VALUES (?, ?, 'linear.session-reply.v1', ?, ?, ?, 'pending')
                    """,
                    (
                        f"notification-reply:{reply_id}",
                        row["project_id"],
                        reply_id,
                        payload_json,
                        timestamp,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO linear_delivery_status(
                        topic, outbox_id, project_id, attempt_count,
                        updated_at
                    ) VALUES ('linear.session-reply.v1', ?, ?, 0, ?)
                    """,
                    (
                        f"notification-reply:{reply_id}",
                        row["project_id"],
                        timestamp,
                    ),
                )
        return self._published_notification_reply(reply_id)

    def enqueue_linear_projection(
        self,
        *,
        project_id: str,
        event_id: str,
        aggregate_id: str,
        payload: Mapping[str, Any],
        created_at: datetime,
    ) -> OutboxRecord:
        _require_text(project_id, "project_id")
        _require_text(event_id, "event_id")
        _require_text(aggregate_id, "aggregate_id")
        copied, payload_json = _json_object(payload)
        del copied
        timestamp = _encode_time(created_at)
        with self._write_transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM linear_projection_outbox WHERE outbox_id = ?",
                (event_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["project_id"] != project_id
                    or existing["aggregate_id"] != aggregate_id
                    or existing["payload_json"] != payload_json
                ):
                    raise _error(
                        "linear_projection_event_conflict",
                        "Linear event ID is already bound to different content.",
                        context={"event_id": event_id},
                    )
            else:
                connection.execute(
                    """
                    INSERT INTO linear_projection_outbox(
                        outbox_id, project_id, topic, aggregate_id,
                        payload_json, created_at, state
                    ) VALUES (?, ?, 'linear.accepted-result.v1', ?, ?, ?, 'pending')
                    """,
                    (event_id, project_id, aggregate_id, payload_json, timestamp),
                )
                connection.execute(
                    """
                    INSERT INTO linear_delivery_status(
                        topic, outbox_id, project_id, attempt_count, updated_at
                    ) VALUES ('linear.accepted-result.v1', ?, ?, 0, ?)
                    """,
                    (event_id, project_id, timestamp),
                )
        record = self.get_linear_projection_outbox(event_id)
        assert record is not None
        return record

    def get_linear_projection_outbox(self, event_id: str) -> OutboxRecord | None:
        with self._read_lock():
            row = self._connection.execute(
                "SELECT * FROM linear_projection_outbox WHERE outbox_id = ?",
                (event_id,),
            ).fetchone()
            return self._outbox_from_row(row) if row else None

    def list_linear_projection_outbox(
        self,
        project_id: str,
        *,
        state: str | None = None,
    ) -> tuple[OutboxRecord, ...]:
        _require_text(project_id, "project_id")
        query = "SELECT * FROM linear_projection_outbox WHERE project_id = ?"
        parameters: list[object] = [project_id]
        if state is not None:
            if state not in {"pending", "delivered", "dead_letter"}:
                raise ValueError("invalid Linear projection outbox state")
            query += " AND state = ?"
            parameters.append(state)
        query += " ORDER BY created_at, outbox_id"
        with self._read_lock():
            rows = self._connection.execute(query, parameters).fetchall()
            return tuple(self._outbox_from_row(row) for row in rows)

    def claim_linear_delivery(
        self,
        *,
        project_id: str,
        claim_id: str,
        claimed_at: datetime,
        lease_seconds: int = 300,
    ) -> LinearDeliveryClaim | None:
        _require_text(project_id, "project_id")
        _require_text(claim_id, "claim_id")
        if not isinstance(lease_seconds, int) or not 1 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be between 1 and 3600")
        timestamp = _encode_time(claimed_at)
        expires_at = _encode_time(claimed_at + timedelta(seconds=lease_seconds))
        with self._write_transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM linear_delivery_claims WHERE claim_id = ?",
                (claim_id,),
            ).fetchone()
            if existing is not None:
                if existing["project_id"] != project_id:
                    raise _error(
                        "linear_delivery_claim_conflict",
                        "Delivery claim belongs to another Project.",
                        context={"claim_id": claim_id},
                    )
                outbox = self._delivery_outbox(
                    connection,
                    existing["topic"],
                    existing["outbox_id"],
                )
                return LinearDeliveryClaim(
                    claim_id=claim_id,
                    outbox=outbox,
                    claimed_at=_decode_time(existing["claimed_at"]),  # type: ignore[arg-type]
                    expires_at=_decode_time(existing["expires_at"]),  # type: ignore[arg-type]
                )
            connection.execute(
                "DELETE FROM linear_delivery_claims WHERE expires_at <= ?",
                (timestamp,),
            )
            candidates = connection.execute(
                """
                SELECT outbox_id, project_id, topic, aggregate_id,
                       payload_json, created_at, state
                FROM linear_projection_outbox
                WHERE project_id = ? AND state = 'pending'
                UNION ALL
                SELECT outbox_id, project_id, topic, aggregate_id,
                       payload_json, created_at, state
                FROM notification_reply_outbox
                WHERE project_id = ? AND state = 'pending'
                ORDER BY created_at, outbox_id
                """,
                (project_id, project_id),
            ).fetchall()
            selected: sqlite3.Row | None = None
            for candidate in candidates:
                claimed = connection.execute(
                    """
                    SELECT 1 FROM linear_delivery_claims
                    WHERE topic = ? AND outbox_id = ?
                    """,
                    (candidate["topic"], candidate["outbox_id"]),
                ).fetchone()
                if claimed is None:
                    selected = candidate
                    break
            if selected is None:
                return None
            connection.execute(
                """
                INSERT INTO linear_delivery_claims(
                    topic, outbox_id, project_id, claim_id,
                    claimed_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    selected["topic"],
                    selected["outbox_id"],
                    project_id,
                    claim_id,
                    timestamp,
                    expires_at,
                ),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO linear_delivery_status(
                    topic, outbox_id, project_id, attempt_count, updated_at
                ) VALUES (?, ?, ?, 0, ?)
                """,
                (
                    selected["topic"],
                    selected["outbox_id"],
                    project_id,
                    timestamp,
                ),
            )
            return LinearDeliveryClaim(
                claim_id=claim_id,
                outbox=self._outbox_from_row(selected),
                claimed_at=claimed_at.astimezone(UTC),
                expires_at=(claimed_at + timedelta(seconds=lease_seconds)).astimezone(UTC),
            )

    def finish_linear_delivery(
        self,
        *,
        claim_id: str,
        state: str,
        error_code: str | None,
        receipt: Mapping[str, Any] | None,
        finished_at: datetime,
    ) -> OutboxRecord:
        if state not in {"retryable", "delivered", "dead_letter"}:
            raise ValueError("invalid Linear delivery outcome")
        if (state == "delivered") != (receipt is not None):
            raise ValueError("delivered outcome and receipt must be supplied together")
        timestamp = _encode_time(finished_at)
        with self._write_transaction() as connection:
            claim = connection.execute(
                "SELECT * FROM linear_delivery_claims WHERE claim_id = ?",
                (claim_id,),
            ).fetchone()
            if claim is None:
                raise _error(
                    "linear_delivery_claim_not_found",
                    "Linear delivery claim was not found or expired.",
                    context={"claim_id": claim_id},
                )
            topic = str(claim["topic"])
            outbox_id = str(claim["outbox_id"])
            table = self._delivery_table(topic)
            stored_state = "pending" if state == "retryable" else state
            connection.execute(
                f"UPDATE {table} SET state = ? WHERE outbox_id = ?",
                (stored_state, outbox_id),
            )
            connection.execute(
                """
                UPDATE linear_delivery_status
                SET attempt_count = attempt_count + 1,
                    last_error_code = ?, last_claim_id = ?, updated_at = ?
                WHERE topic = ? AND outbox_id = ?
                """,
                (error_code, claim_id, timestamp, topic, outbox_id),
            )
            if receipt is not None:
                self._insert_linear_delivery_receipt(
                    connection,
                    project_id=str(claim["project_id"]),
                    topic=topic,
                    outbox_id=outbox_id,
                    receipt=receipt,
                    created_at=timestamp,
                )
            connection.execute(
                "DELETE FROM linear_delivery_claims WHERE claim_id = ?",
                (claim_id,),
            )
            return self._delivery_outbox(connection, topic, outbox_id)

    def get_linear_delivery_status(
        self,
        *,
        topic: str,
        outbox_id: str,
    ) -> dict[str, Any] | None:
        self._delivery_table(topic)
        with self._read_lock():
            row = self._connection.execute(
                """
                SELECT * FROM linear_delivery_status
                WHERE topic = ? AND outbox_id = ?
                """,
                (topic, outbox_id),
            ).fetchone()
            if row is None:
                return None
            return {
                "topic": row["topic"],
                "outbox_id": row["outbox_id"],
                "project_id": row["project_id"],
                "attempt_count": int(row["attempt_count"]),
                "last_error_code": row["last_error_code"],
                "last_claim_id": row["last_claim_id"],
                "updated_at": _decode_time(row["updated_at"]),
            }

    def list_linear_deliveries(
        self,
        project_id: str,
        *,
        topic: str | None = None,
        state: str | None = None,
        limit: int = 100,
        observed_at: datetime | None = None,
    ) -> tuple[LinearDeliveryRecord, ...]:
        _require_text(project_id, "project_id")
        if topic is not None and topic not in _LINEAR_DELIVERY_TOPICS:
            raise ValueError("unknown Linear delivery topic")
        if state is not None and state not in {
            "pending",
            "delivered",
            "dead_letter",
        }:
            raise ValueError("invalid Linear delivery state")
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 1000
        ):
            raise ValueError("Linear delivery limit must be between 1 and 1000")
        return self._query_linear_deliveries(
            project_id=project_id,
            topic=topic,
            state=state,
            outbox_id=None,
            limit=limit,
            observed_at=observed_at,
        )

    def get_linear_delivery(
        self,
        project_id: str,
        *,
        topic: str,
        outbox_id: str,
        observed_at: datetime | None = None,
    ) -> LinearDeliveryRecord | None:
        _require_text(project_id, "project_id")
        _require_text(outbox_id, "outbox_id")
        if topic not in _LINEAR_DELIVERY_TOPICS:
            raise ValueError("unknown Linear delivery topic")
        records = self._query_linear_deliveries(
            project_id=project_id,
            topic=topic,
            state=None,
            outbox_id=outbox_id,
            limit=1,
            observed_at=observed_at,
        )
        return records[0] if records else None

    def _query_linear_deliveries(
        self,
        *,
        project_id: str,
        topic: str | None,
        state: str | None,
        outbox_id: str | None,
        limit: int,
        observed_at: datetime | None,
    ) -> tuple[LinearDeliveryRecord, ...]:
        observed = observed_at or datetime.now(UTC)
        observed_timestamp = _encode_time(observed)
        clauses = ["d.project_id = ?"]
        filters: list[object] = [project_id]
        if topic is not None:
            clauses.append("d.topic = ?")
            filters.append(topic)
        if state is not None:
            clauses.append("d.state = ?")
            filters.append(state)
        if outbox_id is not None:
            clauses.append("d.outbox_id = ?")
            filters.append(outbox_id)
        query = (
            """
            WITH deliveries AS (
                SELECT outbox_id, project_id, topic, aggregate_id,
                       payload_json, created_at, state
                FROM linear_projection_outbox
                UNION ALL
                SELECT outbox_id, project_id, topic, aggregate_id,
                       payload_json, created_at, state
                FROM notification_reply_outbox
            )
            SELECT
                d.outbox_id, d.project_id, d.topic, d.aggregate_id,
                d.payload_json, d.created_at, d.state,
                s.attempt_count, s.last_error_code, s.last_claim_id,
                s.updated_at AS status_updated_at,
                c.claim_id AS active_claim_id,
                c.claimed_at AS active_claimed_at,
                c.expires_at AS active_expires_at,
                r.receipt_id, r.credential_identity,
                r.workspace_id AS receipt_workspace_id,
                r.issue_id AS receipt_issue_id,
                r.thread_id AS receipt_thread_id,
                r.comment_id AS receipt_comment_id,
                r.event_id AS receipt_event_id,
                r.task_id AS receipt_task_id,
                r.source_marker_json AS receipt_source_marker_json,
                r.payload_digest AS receipt_payload_digest,
                r.transport_digest AS receipt_transport_digest,
                r.marker_text AS receipt_marker_text,
                r.payload_json AS receipt_payload_json,
                r.created_at AS receipt_created_at
            FROM deliveries AS d
            LEFT JOIN linear_delivery_status AS s
              ON s.topic = d.topic AND s.outbox_id = d.outbox_id
                 AND s.project_id = d.project_id
            LEFT JOIN linear_delivery_claims AS c
              ON c.topic = d.topic AND c.outbox_id = d.outbox_id
                 AND c.project_id = d.project_id AND c.expires_at > ?
            LEFT JOIN linear_delivery_receipts AS r
              ON r.topic = d.topic AND r.outbox_id = d.outbox_id
                 AND r.project_id = d.project_id
            WHERE
            """
            + " AND ".join(clauses)
            + " ORDER BY d.created_at DESC, d.outbox_id DESC LIMIT ?"
        )
        parameters = [observed_timestamp, *filters, limit]
        with self._read_lock():
            rows = self._connection.execute(query, parameters).fetchall()
            return tuple(self._linear_delivery_record_from_row(row) for row in rows)

    @classmethod
    def _linear_delivery_record_from_row(
        cls,
        row: sqlite3.Row,
    ) -> LinearDeliveryRecord:
        outbox = cls._outbox_from_row(row)
        active_claim = (
            None
            if row["active_claim_id"] is None
            else LinearDeliveryClaim(
                claim_id=row["active_claim_id"],
                outbox=outbox,
                claimed_at=_decode_time(row["active_claimed_at"]),  # type: ignore[arg-type]
                expires_at=_decode_time(row["active_expires_at"]),  # type: ignore[arg-type]
            )
        )
        receipt = cls._linear_delivery_receipt_from_query_row(row)
        return LinearDeliveryRecord(
            project_id=outbox.project_id,
            topic=outbox.topic,
            outbox_id=outbox.outbox_id,
            aggregate_id=outbox.aggregate_id,
            state=outbox.state,
            created_at=outbox.created_at,
            status_updated_at=_decode_time(row["status_updated_at"]),
            attempt_count=int(row["attempt_count"] or 0),
            last_error_code=row["last_error_code"],
            last_claim_id=row["last_claim_id"],
            active_claim=active_claim,
            receipt=receipt,
            lineage=cls._linear_delivery_lineage(outbox, receipt),
        )

    @staticmethod
    def _linear_delivery_receipt_from_query_row(
        row: sqlite3.Row,
    ) -> LinearDeliveryReceiptRecord | None:
        if row["receipt_id"] is None:
            return None
        marker = _decode_object(row["receipt_source_marker_json"])
        return LinearDeliveryReceiptRecord(
            receipt_id=row["receipt_id"],
            project_id=row["project_id"],
            credential_identity=row["credential_identity"],
            topic=row["topic"],
            outbox_id=row["outbox_id"],
            workspace_id=row["receipt_workspace_id"],
            issue_id=row["receipt_issue_id"],
            thread_id=row["receipt_thread_id"],
            comment_id=row["receipt_comment_id"],
            event_id=row["receipt_event_id"],
            task_id=row["receipt_task_id"],
            source_marker=(
                SessionNotificationSourceMarker.model_validate(marker)
                if marker is not None
                else None
            ),
            payload_digest=row["receipt_payload_digest"],
            transport_digest=row["receipt_transport_digest"],
            marker=row["receipt_marker_text"],
            payload=_decode_object(row["receipt_payload_json"]) or {},
            created_at=_decode_time(row["receipt_created_at"]),  # type: ignore[arg-type]
        )

    @staticmethod
    def _linear_delivery_lineage(
        outbox: OutboxRecord,
        receipt: LinearDeliveryReceiptRecord | None,
    ) -> dict[str, Any]:
        lineage: dict[str, Any] = {"aggregate_id": outbox.aggregate_id}
        for key in (
            "event_id",
            "agent_id",
            "task_id",
            "session_id",
            "submission_id",
            "decision_id",
            "report_id",
            "report_revision",
            "accepted_merge_commit",
            "ci_subject_head",
            "ci_attestation_id",
            "workflow_id",
            "check_identity",
            "notification_id",
            "reply_id",
            "commit_sha",
            "actor_id",
            "linear_issue_id",
            "source_thread_id",
            "source_comment_id",
        ):
            value = outbox.payload.get(key)
            if value is not None:
                lineage[key] = value
        target = outbox.payload.get("target")
        if isinstance(target, dict):
            lineage["target"] = {
                key: target[key]
                for key in ("workspace_id", "team_id", "project_id", "issue_id")
                if target.get(key) is not None
            }
        marker = outbox.payload.get("marker")
        if isinstance(marker, dict):
            lineage["marker"] = {
                key: marker[key]
                for key in (
                    "agent_id",
                    "task_id",
                    "session_id",
                    "report_id",
                    "notification_id",
                    "reply_id",
                )
                if marker.get(key) is not None
            }
        if receipt is not None:
            lineage.setdefault("event_id", receipt.event_id)
            lineage.setdefault("task_id", receipt.task_id)
            if receipt.source_marker is not None:
                lineage["source_marker"] = receipt.source_marker.model_dump(
                    mode="json",
                    exclude_none=True,
                )
        return lineage

    def get_linear_delivery_receipt(
        self,
        *,
        topic: str,
        outbox_id: str,
    ) -> LinearDeliveryReceiptRecord | None:
        with self._read_lock():
            row = self._connection.execute(
                """
                SELECT * FROM linear_delivery_receipts
                WHERE topic = ? AND outbox_id = ?
                """,
                (topic, outbox_id),
            ).fetchone()
            return self._linear_delivery_receipt_from_row(row) if row else None

    def record_verified_linear_ingress(
        self,
        *,
        project_id: str,
        authenticated_app_id: str,
        mentioned_app_id: str,
        author_id: str,
        credential_identity: str,
        workspace_id: str,
        issue_id: str,
        thread_id: str,
        comment_id: str,
        webhook_event_id: str | None,
        task_id: str,
        source_marker: SessionNotificationSourceMarker | None,
        command_digest: str,
        observed_payload_digest: str,
        verified_at: datetime,
    ) -> VerifiedLinearIngressReceipt:
        for value, field in (
            (project_id, "project_id"),
            (authenticated_app_id, "authenticated_app_id"),
            (mentioned_app_id, "mentioned_app_id"),
            (author_id, "author_id"),
            (credential_identity, "credential_identity"),
            (workspace_id, "workspace_id"),
            (issue_id, "issue_id"),
            (thread_id, "thread_id"),
            (comment_id, "comment_id"),
            (task_id, "task_id"),
        ):
            _require_text(value, field)
        _require_digest(command_digest, "command_digest")
        _require_digest(observed_payload_digest, "observed_payload_digest")
        if source_marker is not None and source_marker.task_id != task_id:
            raise _error(
                "linear_ingress_source_task_mismatch",
                "Linear ingress source marker belongs to another Task.",
            )
        if webhook_event_id is not None:
            _require_text(webhook_event_id, "webhook_event_id")
        with self._read_lock():
            existing_row = self._connection.execute(
                """
                SELECT * FROM verified_linear_ingress_receipts
                WHERE provider = 'linear' AND workspace_id = ? AND comment_id = ?
                """,
                (workspace_id, comment_id),
            ).fetchone()
        if existing_row is not None:
            existing = self._linear_ingress_receipt_from_row(existing_row)
            replay_fields = (
                (existing.project_id, project_id),
                (existing.authenticated_app_id, authenticated_app_id),
                (existing.mentioned_app_id, mentioned_app_id),
                (existing.author_id, author_id),
                (existing.credential_identity, credential_identity),
                (existing.workspace_id, workspace_id),
                (existing.issue_id, issue_id),
                (existing.thread_id, thread_id),
                (existing.comment_id, comment_id),
                (existing.webhook_event_id, webhook_event_id),
                (existing.task_id, task_id),
                (existing.source_marker, source_marker),
                (existing.command_digest, command_digest),
                (existing.observed_payload_digest, observed_payload_digest),
            )
            if all(stored == observed for stored, observed in replay_fields):
                return existing
            raise _error(
                "linear_ingress_receipt_conflict",
                "Authenticated Linear comment is bound to different content.",
                context={"comment_id": comment_id},
            )
        marker_data = (
            source_marker.model_dump(mode="json")
            if source_marker is not None
            else None
        )
        binding = {
            "provider": "linear",
            "authenticated_app_id": authenticated_app_id,
            "mentioned_app_id": mentioned_app_id,
            "author_id": author_id,
            "credential_identity": credential_identity,
            "workspace_id": workspace_id,
            "issue_id": issue_id,
            "thread_id": thread_id,
            "comment_id": comment_id,
            "webhook_event_id": webhook_event_id,
            "task_id": task_id,
            "source_marker": marker_data,
            "command_digest": command_digest,
            "observed_payload_digest": observed_payload_digest,
        }
        binding_digest = "sha256:" + hashlib.sha256(
            canonical_json_bytes(binding)
        ).hexdigest()
        receipt_id = f"linear-ingress-{binding_digest.removeprefix('sha256:')}"
        timestamp = _encode_time(verified_at)
        marker_json = _json_object(marker_data)[1] if marker_data is not None else None
        with self._write_transaction() as connection:
            existing = connection.execute(
                """
                SELECT * FROM verified_linear_ingress_receipts
                WHERE provider = 'linear' AND workspace_id = ? AND comment_id = ?
                """,
                (workspace_id, comment_id),
            ).fetchone()
            if existing is not None:
                if existing["binding_digest"] != binding_digest:
                    raise _error(
                        "linear_ingress_receipt_conflict",
                        "Authenticated Linear comment is bound to different content.",
                        context={"comment_id": comment_id},
                    )
                receipt_id = str(existing["receipt_id"])
            else:
                connection.execute(
                    """
                    INSERT INTO verified_linear_ingress_receipts(
                        receipt_id, project_id, provider, authenticated_app_id,
                        mentioned_app_id, author_id, credential_identity, workspace_id,
                        issue_id, thread_id,
                        comment_id, webhook_event_id, task_id, source_marker_json,
                        command_digest, observed_payload_digest, binding_digest,
                        verified_at
                    ) VALUES (
                        ?, ?, 'linear', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        receipt_id,
                        project_id,
                        authenticated_app_id,
                        mentioned_app_id,
                        author_id,
                        credential_identity,
                        workspace_id,
                        issue_id,
                        thread_id,
                        comment_id,
                        webhook_event_id,
                        task_id,
                        marker_json,
                        command_digest,
                        observed_payload_digest,
                        binding_digest,
                        timestamp,
                    ),
                )
        receipt = self.require_verified_notification_origin(
            project_id=project_id,
            workspace_id=workspace_id,
            issue_id=issue_id,
            thread_id=thread_id,
            comment_id=comment_id,
            task_id=task_id,
        )
        assert receipt.receipt_id == receipt_id
        return receipt

    def require_verified_notification_origin(
        self,
        *,
        project_id: str,
        workspace_id: str,
        issue_id: str,
        thread_id: str,
        comment_id: str,
        task_id: str,
    ) -> VerifiedLinearIngressReceipt:
        with self._read_lock():
            row = self._connection.execute(
                """
                SELECT * FROM verified_linear_ingress_receipts
                WHERE provider = 'linear' AND project_id = ?
                  AND workspace_id = ? AND issue_id = ? AND thread_id = ?
                  AND comment_id = ? AND task_id = ?
                """,
                (project_id, workspace_id, issue_id, thread_id, comment_id, task_id),
            ).fetchone()
        if row is None:
            raise _error(
                "linear_notification_origin_unverified",
                "Notification source is not a verified authenticated Linear comment.",
                context={
                    "workspace_id": workspace_id,
                    "issue_id": issue_id,
                    "thread_id": thread_id,
                    "comment_id": comment_id,
                },
            )
        return self._linear_ingress_receipt_from_row(row)

    def resolve_verified_source_marker(
        self,
        *,
        workspace_id: str,
        issue_id: str,
        thread_id: str,
    ) -> SessionNotificationSourceMarker | None:
        with self._read_lock():
            rows = self._connection.execute(
                """
                SELECT source_marker_json FROM linear_delivery_receipts
                WHERE topic IN (
                    'linear.accepted-result.v1',
                    'linear.session-reply.v1'
                )
                  AND workspace_id = ? AND issue_id = ? AND thread_id = ?
                  AND source_marker_json IS NOT NULL
                ORDER BY created_at DESC, receipt_id DESC
                """,
                (workspace_id, issue_id, thread_id),
            ).fetchall()
        markers = [
            SessionNotificationSourceMarker.model_validate(
                _decode_object(row["source_marker_json"])
            )
            for row in rows
            if row["source_marker_json"] is not None
        ]
        if not markers:
            return None
        routing_identities = {
            (
                marker.task_id,
                marker.session_id,
            )
            for marker in markers
        }
        if len(routing_identities) != 1:
            raise _error(
                "linear_source_marker_ambiguous",
                "Linear thread is bound to multiple Session routing identities.",
                context={"issue_id": issue_id, "thread_id": thread_id},
            )
        return markers[0]

    @staticmethod
    def _delivery_table(topic: str) -> str:
        if topic == _LINEAR_ACCEPTED_TOPIC:
            return "linear_projection_outbox"
        if topic == _LINEAR_REPLY_TOPIC:
            return "notification_reply_outbox"
        raise ValueError("unknown Linear delivery topic")

    def _delivery_outbox(
        self,
        connection: sqlite3.Connection,
        topic: str,
        outbox_id: str,
    ) -> OutboxRecord:
        table = self._delivery_table(topic)
        row = connection.execute(
            f"SELECT * FROM {table} WHERE outbox_id = ?",
            (outbox_id,),
        ).fetchone()
        if row is None:
            raise _error(
                "runtime_store_corrupt",
                "Linear delivery claim references a missing outbox event.",
                context={"topic": topic, "outbox_id": outbox_id},
            )
        return self._outbox_from_row(row)

    def _insert_linear_delivery_receipt(
        self,
        connection: sqlite3.Connection,
        *,
        project_id: str,
        topic: str,
        outbox_id: str,
        receipt: Mapping[str, Any],
        created_at: str,
    ) -> None:
        copied, payload_json = _json_object(receipt)
        required = {
            "receipt_id",
            "credential_identity",
            "workspace_id",
            "issue_id",
            "thread_id",
            "comment_id",
            "event_id",
            "task_id",
            "payload_digest",
            "transport_digest",
            "marker",
        }
        if not required.issubset(copied):
            raise _error(
                "linear_delivery_receipt_invalid",
                "Projection receipt is missing required identity fields.",
            )
        for field in required - {"payload_digest", "transport_digest"}:
            _require_text(copied[field], field)
        _require_digest(copied["payload_digest"], "payload_digest")
        _require_digest(copied["transport_digest"], "transport_digest")
        source_marker = copied.get("source_marker")
        marker_json: str | None = None
        if source_marker is not None:
            marker = SessionNotificationSourceMarker.model_validate(source_marker)
            marker_json = _json_object(marker.model_dump(mode="json"))[1]
        existing = connection.execute(
            """
            SELECT payload_json FROM linear_delivery_receipts
            WHERE topic = ? AND outbox_id = ?
            """,
            (topic, outbox_id),
        ).fetchone()
        if existing is not None:
            if existing["payload_json"] != payload_json:
                raise _error(
                    "linear_delivery_receipt_conflict",
                    "Delivery already has a different immutable receipt.",
                    context={"topic": topic, "outbox_id": outbox_id},
                )
            return
        connection.execute(
            """
            INSERT INTO linear_delivery_receipts(
                receipt_id, project_id, credential_identity, direction,
                topic, outbox_id,
                workspace_id, issue_id, thread_id, comment_id, event_id,
                task_id, source_marker_json, payload_digest, transport_digest,
                marker_text, payload_json, created_at
            ) VALUES (?, ?, ?, 'outbound', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                copied["receipt_id"],
                project_id,
                copied["credential_identity"],
                topic,
                outbox_id,
                copied["workspace_id"],
                copied["issue_id"],
                copied["thread_id"],
                copied["comment_id"],
                copied["event_id"],
                copied["task_id"],
                marker_json,
                copied["payload_digest"],
                copied["transport_digest"],
                copied["marker"],
                payload_json,
                created_at,
            ),
        )

    @staticmethod
    def _linear_delivery_receipt_from_row(
        row: sqlite3.Row,
    ) -> LinearDeliveryReceiptRecord:
        marker = _decode_object(row["source_marker_json"])
        return LinearDeliveryReceiptRecord(
            receipt_id=row["receipt_id"],
            project_id=row["project_id"],
            credential_identity=row["credential_identity"],
            topic=row["topic"],
            outbox_id=row["outbox_id"],
            workspace_id=row["workspace_id"],
            issue_id=row["issue_id"],
            thread_id=row["thread_id"],
            comment_id=row["comment_id"],
            event_id=row["event_id"],
            task_id=row["task_id"],
            source_marker=(
                SessionNotificationSourceMarker.model_validate(marker)
                if marker is not None
                else None
            ),
            payload_digest=row["payload_digest"],
            transport_digest=row["transport_digest"],
            marker=row["marker_text"],
            payload=_decode_object(row["payload_json"]) or {},
            created_at=_decode_time(row["created_at"]),  # type: ignore[arg-type]
        )

    @staticmethod
    def _linear_ingress_receipt_from_row(
        row: sqlite3.Row,
    ) -> VerifiedLinearIngressReceipt:
        marker = _decode_object(row["source_marker_json"])
        return VerifiedLinearIngressReceipt(
            receipt_id=row["receipt_id"],
            project_id=row["project_id"],
            provider=row["provider"],
            authenticated_app_id=row["authenticated_app_id"],
            mentioned_app_id=row["mentioned_app_id"],
            author_id=row["author_id"],
            credential_identity=row["credential_identity"],
            workspace_id=row["workspace_id"],
            issue_id=row["issue_id"],
            thread_id=row["thread_id"],
            comment_id=row["comment_id"],
            webhook_event_id=row["webhook_event_id"],
            task_id=row["task_id"],
            source_marker=(
                SessionNotificationSourceMarker.model_validate(marker)
                if marker is not None
                else None
            ),
            command_digest=row["command_digest"],
            observed_payload_digest=row["observed_payload_digest"],
            binding_digest=row["binding_digest"],
            verified_at=_decode_time(row["verified_at"]),  # type: ignore[arg-type]
        )

    @staticmethod
    def _require_operation(
        connection: sqlite3.Connection,
        operation_id: str,
    ) -> None:
        operation = connection.execute(
            "SELECT operation_id FROM operations WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        if operation is None:
            raise _error(
                "operation_not_found",
                "Notification action operation was not found.",
                context={"operation_id": operation_id},
            )

    def get_notification_reply(
        self,
        reply_id: str,
    ) -> SessionNotificationReply | None:
        with self._read_lock():
            row = self._connection.execute(
                "SELECT * FROM notification_replies WHERE reply_id = ?",
                (reply_id,),
            ).fetchone()
            return self._notification_reply_from_row(row) if row else None

    @staticmethod
    def _notification_reply_from_row(
        row: sqlite3.Row,
    ) -> SessionNotificationReply:
        return SessionNotificationReply(
            reply_id=row["reply_id"],
            notification_id=row["notification_id"],
            project_id=row["project_id"],
            task_id=row["task_id"],
            session_id=row["session_id"],
            actor_id=row["actor_id"],
            body=row["body_text"],
            payload_digest=row["payload_digest"],
            created_at=_decode_time(row["created_at"]),  # type: ignore[arg-type]
        )

    def get_notification_reply_outbox(
        self,
        outbox_id: str,
    ) -> OutboxRecord | None:
        with self._read_lock():
            row = self._connection.execute(
                "SELECT * FROM notification_reply_outbox WHERE outbox_id = ?",
                (outbox_id,),
            ).fetchone()
            return self._outbox_from_row(row) if row else None

    def list_notification_reply_outbox(
        self,
        *,
        state: str | None = None,
    ) -> tuple[OutboxRecord, ...]:
        query = "SELECT * FROM notification_reply_outbox"
        parameters: list[object] = []
        if state is not None:
            if state not in {"pending", "delivered", "dead_letter"}:
                raise ValueError("invalid notification outbox state")
            query += " WHERE state = ?"
            parameters.append(state)
        query += " ORDER BY created_at, outbox_id"
        with self._read_lock():
            rows = self._connection.execute(query, parameters).fetchall()
            return tuple(self._outbox_from_row(row) for row in rows)

    def _published_notification_reply(
        self,
        reply_id: str,
    ) -> PublishedNotificationReply:
        reply = self.get_notification_reply(reply_id)
        outbox = self.get_notification_reply_outbox(
            f"notification-reply:{reply_id}"
        )
        if reply is None or outbox is None:
            raise _error(
                "runtime_store_corrupt",
                "Notification reply is missing its durable reply or outbox record.",
            )
        notification = self.get_notification(reply.notification_id)
        if notification is None:
            raise _error(
                "runtime_store_corrupt",
                "Notification reply references a missing Session notification.",
            )
        return PublishedNotificationReply(
            notification=notification,
            reply=reply,
            outbox=outbox,
        )

    def publish_status_update(
        self,
        project_id: str,
        update: StatusUpdate,
    ) -> PublishedStatus:
        _require_text(project_id, "project_id")
        self._validate_status_semantics(update)
        payload_bytes = canonical_json_bytes(update)
        payload_json = payload_bytes.decode("utf-8")
        payload_digest = "sha256:" + hashlib.sha256(payload_bytes).hexdigest()
        observed_at = _encode_time(update.observed_at)
        outbox_id = f"status-update:{update.update_id}"
        with self._write_transaction() as connection:
            session = connection.execute(
                "SELECT project_id, task_id FROM runtime_sessions WHERE session_id = ?",
                (update.session_id,),
            ).fetchone()
            if session is None:
                raise _error(
                    "status_session_not_found",
                    "StatusUpdate Session was not found in the runtime store.",
                    context={"session_id": update.session_id},
                )
            if session["project_id"] != project_id or session["task_id"] != update.task_id:
                raise _error(
                    "status_session_mismatch",
                    "StatusUpdate does not match its runtime Session identity.",
                    context={"update_id": update.update_id},
                )
            existing = connection.execute(
                "SELECT * FROM status_updates WHERE update_id = ?",
                (update.update_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["project_id"] != project_id
                    or existing["payload_digest"] != payload_digest
                    or existing["payload_json"] != payload_json
                ):
                    raise _error(
                        "status_update_conflict",
                        "StatusUpdate ID is already bound to different content.",
                        context={"update_id": update.update_id},
                    )
            else:
                connection.execute(
                    """
                    INSERT INTO status_updates(
                        update_id, project_id, task_id, session_id, status,
                        observed_at, payload_digest, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        update.update_id,
                        project_id,
                        update.task_id,
                        update.session_id,
                        update.status.value,
                        observed_at,
                        payload_digest,
                        payload_json,
                    ),
                )
            connection.execute(
                """
                INSERT OR IGNORE INTO outbox_events(
                    outbox_id, project_id, topic, aggregate_id,
                    payload_json, created_at, state
                ) VALUES (?, ?, 'status_update.v1', ?, ?, ?, 'pending')
                """,
                (outbox_id, project_id, update.update_id, payload_json, observed_at),
            )
            self._upsert_attention(connection, project_id, update)
        return self._published_status(update.update_id)

    @staticmethod
    def _validate_status_semantics(update: StatusUpdate) -> None:
        blocker_fields_are_paired = (
            (update.blocker_category is None) == (update.blocker_detail is None)
        )
        if not blocker_fields_are_paired:
            raise _error(
                "invalid_status_update",
                "StatusUpdate blocker_category and blocker_detail must be paired.",
                context={"update_id": update.update_id},
            )
        if update.status == StatusKind.BLOCKED and update.blocker_category is None:
            raise _error(
                "invalid_status_update",
                "Blocked StatusUpdate requires blocker_category and blocker_detail.",
                context={"update_id": update.update_id},
            )
        if update.status != StatusKind.BLOCKED and update.blocker_category is not None:
            raise _error(
                "invalid_status_update",
                "StatusUpdate blocker fields are only valid for blocked status.",
                context={"update_id": update.update_id},
            )
        if update.status == StatusKind.NEEDS_INPUT and update.decision_needed is None:
            raise _error(
                "invalid_status_update",
                "Needs-input StatusUpdate requires a structured decision request.",
                context={"update_id": update.update_id},
            )

    def _upsert_attention(
        self,
        connection: sqlite3.Connection,
        project_id: str,
        update: StatusUpdate,
    ) -> None:
        kind, identity, priority = _attention_descriptor(update)
        dedupe_key = attention_dedupe_key(project_id, update)
        evidence_digest = _attention_evidence_digest(update)
        observed_at = _encode_time(update.observed_at)
        existing = connection.execute(
            "SELECT * FROM attention_items WHERE dedupe_key = ?",
            (dedupe_key,),
        ).fetchone()
        if existing is None:
            connection.execute(
                """
                INSERT INTO attention_items(
                    dedupe_key, project_id, task_id, session_id, kind,
                    identity_text, priority, state, generation, evidence_digest,
                    current_update_id, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', 1, ?, ?, ?, ?)
                """,
                (
                    dedupe_key,
                    project_id,
                    update.task_id,
                    update.session_id,
                    kind,
                    identity,
                    priority,
                    evidence_digest,
                    update.update_id,
                    observed_at,
                    observed_at,
                ),
            )
            return
        current_order = (existing["last_seen_at"], existing["current_update_id"])
        incoming_order = (observed_at, update.update_id)
        if incoming_order <= current_order:
            return
        material_changed = existing["evidence_digest"] != evidence_digest
        if material_changed:
            connection.execute(
                """
                UPDATE attention_items
                SET priority = ?, state = 'open', generation = generation + 1,
                    evidence_digest = ?, current_update_id = ?, last_seen_at = ?,
                    acknowledged_by = NULL, acknowledged_at = NULL,
                    acknowledged_update_id = NULL, snoozed_by = NULL,
                    snoozed_until = NULL, snoozed_update_id = NULL,
                    resolved_by = NULL, resolved_at = NULL,
                    resolved_update_id = NULL, resolution_reason = NULL
                WHERE dedupe_key = ?
                """,
                (
                    priority,
                    evidence_digest,
                    update.update_id,
                    observed_at,
                    dedupe_key,
                ),
            )
        elif existing["state"] == "acknowledged":
            connection.execute(
                """
                UPDATE attention_items
                SET state = 'open', current_update_id = ?, last_seen_at = ?,
                    acknowledged_by = NULL, acknowledged_at = NULL,
                    acknowledged_update_id = NULL
                WHERE dedupe_key = ?
                """,
                (update.update_id, observed_at, dedupe_key),
            )
        else:
            connection.execute(
                """
                UPDATE attention_items
                SET current_update_id = ?, last_seen_at = ?
                WHERE dedupe_key = ?
                """,
                (update.update_id, observed_at, dedupe_key),
            )

    def get_status_update(self, update_id: str) -> StatusUpdate | None:
        with self._read_lock():
            row = self._connection.execute(
                "SELECT payload_json FROM status_updates WHERE update_id = ?",
                (update_id,),
            ).fetchone()
            return self._status_from_row(row) if row else None

    def list_status_updates(
        self,
        project_id: str,
        *,
        session_id: str | None = None,
    ) -> tuple[StatusUpdate, ...]:
        query = "SELECT payload_json FROM status_updates WHERE project_id = ?"
        parameters: list[object] = [project_id]
        if session_id is not None:
            query += " AND session_id = ?"
            parameters.append(session_id)
        query += " ORDER BY observed_at, update_id"
        with self._read_lock():
            rows = self._connection.execute(query, parameters).fetchall()
            return tuple(self._status_from_row(row) for row in rows)

    @staticmethod
    def _status_from_row(row: sqlite3.Row) -> StatusUpdate:
        return StatusUpdate.model_validate_json(row["payload_json"])

    def get_outbox(self, outbox_id: str) -> OutboxRecord | None:
        with self._read_lock():
            row = self._connection.execute(
                "SELECT * FROM outbox_events WHERE outbox_id = ?",
                (outbox_id,),
            ).fetchone()
            return self._outbox_from_row(row) if row else None

    def list_outbox(
        self,
        project_id: str | None = None,
        *,
        pending_only: bool = False,
    ) -> tuple[OutboxRecord, ...]:
        clauses: list[str] = []
        parameters: list[object] = []
        if project_id is not None:
            clauses.append("project_id = ?")
            parameters.append(project_id)
        if pending_only:
            clauses.append("state = 'pending'")
        query = "SELECT * FROM outbox_events"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at, outbox_id"
        with self._read_lock():
            rows = self._connection.execute(query, parameters).fetchall()
            return tuple(self._outbox_from_row(row) for row in rows)

    @staticmethod
    def _outbox_from_row(row: sqlite3.Row) -> OutboxRecord:
        return OutboxRecord(
            outbox_id=row["outbox_id"],
            project_id=row["project_id"],
            topic=row["topic"],
            aggregate_id=row["aggregate_id"],
            payload=_decode_object(row["payload_json"]) or {},
            created_at=_decode_time(row["created_at"]),  # type: ignore[arg-type]
            state=row["state"],
        )

    def _published_status(self, update_id: str) -> PublishedStatus:
        update = self.get_status_update(update_id)
        outbox = self.get_outbox(f"status-update:{update_id}")
        if update is None or outbox is None:
            raise _error(
                "runtime_store_corrupt",
                "Published status is missing its update or outbox record.",
            )
        project_id = outbox.project_id
        attention = self.get_attention(attention_dedupe_key(project_id, update))
        if attention is None:
            raise _error(
                "runtime_store_corrupt",
                "Published status is missing its attention projection.",
            )
        return PublishedStatus(update=update, outbox=outbox, attention=attention)

    def get_attention(self, dedupe_key: str) -> AttentionItem | None:
        with self._read_lock():
            row = self._connection.execute(
                "SELECT * FROM attention_items WHERE dedupe_key = ?",
                (dedupe_key,),
            ).fetchone()
            return self._attention_from_row(self._connection, row) if row else None

    def list_inbox(
        self,
        project_id: str,
        *,
        as_of: datetime | None = None,
        include_resolved: bool = False,
    ) -> tuple[AttentionItem, ...]:
        parameters: list[object] = [project_id]
        query = "SELECT * FROM attention_items WHERE project_id = ?"
        if not include_resolved:
            observed_at = _encode_time(as_of or datetime.now(UTC))
            query += (
                " AND (state IN ('open', 'acknowledged')"
                " OR (state = 'snoozed' AND snoozed_until <= ?))"
            )
            parameters.append(observed_at)
        query += " ORDER BY priority, last_seen_at, dedupe_key"
        with self._read_lock():
            rows = self._connection.execute(query, parameters).fetchall()
            return tuple(
                self._attention_from_row(self._connection, row) for row in rows
            )

    @staticmethod
    def _attention_from_row(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> AttentionItem:
        update_row = connection.execute(
            "SELECT payload_json FROM status_updates WHERE update_id = ?",
            (row["current_update_id"],),
        ).fetchone()
        if update_row is None:
            raise _error(
                "runtime_store_corrupt",
                "Attention item references a missing StatusUpdate.",
            )
        return AttentionItem(
            dedupe_key=row["dedupe_key"],
            project_id=row["project_id"],
            task_id=row["task_id"],
            session_id=row["session_id"],
            kind=row["kind"],
            identity=row["identity_text"],
            priority=int(row["priority"]),
            state=row["state"],
            generation=int(row["generation"]),
            evidence_digest=row["evidence_digest"],
            current_update=StatusUpdate.model_validate_json(update_row["payload_json"]),
            first_seen_at=_decode_time(row["first_seen_at"]),  # type: ignore[arg-type]
            last_seen_at=_decode_time(row["last_seen_at"]),  # type: ignore[arg-type]
            acknowledged_by=row["acknowledged_by"],
            acknowledged_at=_decode_time(row["acknowledged_at"]),
            snoozed_by=row["snoozed_by"],
            snoozed_until=_decode_time(row["snoozed_until"]),
            resolved_by=row["resolved_by"],
            resolved_at=_decode_time(row["resolved_at"]),
            resolution_reason=row["resolution_reason"],
        )

    def ack_attention(
        self,
        dedupe_key: str,
        actor: str,
        observed_at: datetime,
        *,
        expected_generation: int | None = None,
        operation_id: str | None = None,
    ) -> AttentionItem:
        return self._apply_attention_action(
            dedupe_key,
            "ack",
            actor,
            observed_at,
            expected_generation=expected_generation,
            operation_id=operation_id,
        )

    def snooze_attention(
        self,
        dedupe_key: str,
        actor: str,
        until: datetime,
        observed_at: datetime,
        *,
        expected_generation: int | None = None,
        operation_id: str | None = None,
    ) -> AttentionItem:
        if until.astimezone(UTC) <= observed_at.astimezone(UTC):
            raise _error(
                "invalid_snooze",
                "Snooze deadline must be later than its observation time.",
            )
        return self._apply_attention_action(
            dedupe_key,
            "snooze",
            actor,
            observed_at,
            expected_generation=expected_generation,
            operation_id=operation_id,
            detail={"until": _encode_time(until)},
        )

    def resolve_attention(
        self,
        dedupe_key: str,
        actor: str,
        reason: str,
        observed_at: datetime,
        *,
        expected_generation: int | None = None,
        operation_id: str | None = None,
    ) -> AttentionItem:
        _require_text(reason, "reason")
        return self._apply_attention_action(
            dedupe_key,
            "resolve",
            actor,
            observed_at,
            expected_generation=expected_generation,
            operation_id=operation_id,
            detail={"reason": reason},
        )

    def _apply_attention_action(
        self,
        dedupe_key: str,
        action: str,
        actor: str,
        observed_at: datetime,
        *,
        expected_generation: int | None,
        operation_id: str | None,
        detail: Mapping[str, Any] | None = None,
    ) -> AttentionItem:
        _require_text(dedupe_key, "dedupe_key")
        _require_text(actor, "actor")
        timestamp = _encode_time(observed_at)
        copied_detail, detail_json = _json_object(detail)
        del copied_detail
        with self._write_transaction() as connection:
            row = connection.execute(
                "SELECT * FROM attention_items WHERE dedupe_key = ?",
                (dedupe_key,),
            ).fetchone()
            if row is None:
                raise _error(
                    "attention_not_found",
                    "Attention item was not found.",
                    context={"dedupe_key": dedupe_key},
                )
            generation = int(row["generation"])
            if operation_id is not None:
                operation = connection.execute(
                    "SELECT operation_id FROM operations WHERE operation_id = ?",
                    (operation_id,),
                ).fetchone()
                if operation is None:
                    raise _error(
                        "operation_not_found",
                        "Attention action operation was not found.",
                        context={"operation_id": operation_id},
                    )
            if expected_generation is not None and expected_generation != generation:
                raise _error(
                    "stale_attention",
                    "Attention item changed before the requested action.",
                    context={"dedupe_key": dedupe_key},
                )
            action_material = {
                "dedupe_key": dedupe_key,
                "action": action,
                "actor": actor,
                "source_update_id": row["current_update_id"],
                "operation_id": operation_id,
                "generation": generation,
                "detail": json.loads(detail_json),
            }
            action_key = "sha256:" + hashlib.sha256(
                canonical_json_bytes(action_material)
            ).hexdigest()
            known_action = connection.execute(
                "SELECT action_key FROM attention_actions WHERE action_key = ?",
                (action_key,),
            ).fetchone()
            if known_action is not None:
                pass
            elif row["state"] == "resolved":
                raise _error(
                    "attention_resolved",
                    "Resolved attention remains closed until new evidence arrives.",
                    context={"dedupe_key": dedupe_key},
                )
            else:
                connection.execute(
                    """
                    INSERT INTO attention_actions(
                        action_key, dedupe_key, action, actor, source_update_id,
                        operation_id, generation, observed_at, detail_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        action_key,
                        dedupe_key,
                        action,
                        actor,
                        row["current_update_id"],
                        operation_id,
                        generation,
                        timestamp,
                        detail_json,
                    ),
                )
                if action == "ack":
                    connection.execute(
                        """
                        UPDATE attention_items
                        SET state = 'acknowledged', acknowledged_by = ?,
                            acknowledged_at = ?, acknowledged_update_id = ?,
                            snoozed_by = NULL, snoozed_until = NULL,
                            snoozed_update_id = NULL
                        WHERE dedupe_key = ?
                        """,
                        (actor, timestamp, row["current_update_id"], dedupe_key),
                    )
                elif action == "snooze":
                    connection.execute(
                        """
                        UPDATE attention_items
                        SET state = 'snoozed', snoozed_by = ?, snoozed_until = ?,
                            snoozed_update_id = ?
                        WHERE dedupe_key = ?
                        """,
                        (
                            actor,
                            json.loads(detail_json)["until"],
                            row["current_update_id"],
                            dedupe_key,
                        ),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE attention_items
                        SET state = 'resolved', resolved_by = ?, resolved_at = ?,
                            resolved_update_id = ?, resolution_reason = ?,
                            snoozed_by = NULL, snoozed_until = NULL,
                            snoozed_update_id = NULL
                        WHERE dedupe_key = ?
                        """,
                        (
                            actor,
                            timestamp,
                            row["current_update_id"],
                            json.loads(detail_json)["reason"],
                            dedupe_key,
                        ),
                    )
        item = self.get_attention(dedupe_key)
        assert item is not None
        return item
