from __future__ import annotations

import json
import sqlite3
import stat
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import researchctl.runtime.store as store_module
from researchctl.domain.enums import SessionState
from researchctl.domain.models import StatusUpdate
from researchctl.errors import RCPError
from researchctl.runtime import (
    RuntimeSession,
    RuntimeStore,
    attention_dedupe_key,
    hash_session_token,
)
from researchctl.serialization import canonical_digest, canonical_json_bytes

NOW = datetime(2026, 8, 3, 12, 0, 0, tzinfo=UTC)
PROJECT_ID = "project_20260803T120000Z_" + "a" * 24
TASK_ID = "task_20260803T120000Z_" + "b" * 24
SESSION_ID = "session_20260803T120000Z_" + "c" * 24
CONTINUED_SESSION_ID = "session_20260803T120000Z_" + "d" * 24
OPERATION_ID = "operation_20260803T120000Z_" + "e" * 24
OTHER_OPERATION_ID = "operation_20260803T120000Z_" + "f" * 24
REQUEST_DIGEST = "sha256:" + "1" * 64
OTHER_REQUEST_DIGEST = "sha256:" + "2" * 64


def _session(
    *,
    session_id: str = SESSION_ID,
    state: SessionState = SessionState.ACTIVE,
    created_at: datetime = NOW,
    updated_at: datetime = NOW,
    continued_from: str | None = None,
    actor_token_digest: str | None = None,
) -> RuntimeSession:
    return RuntimeSession(
        session_id=session_id,
        project_id=PROJECT_ID,
        task_id=TASK_ID,
        state=state,
        created_at=created_at,
        updated_at=updated_at,
        host="host-a",
        branch=f"research/{session_id}",
        worktree_path=f".research/worktrees/{session_id}",
        continued_from=continued_from,
        actor_token_digest=actor_token_digest,
        metadata={"adapter": "codex"},
    )


def _blocked_update(
    *,
    update_id: str = "update_20260803T120000Z_" + "3" * 24,
    observed_at: datetime = NOW,
    summary: str = "Environment preflight is blocked.",
    detail: str = "CUDA toolkit is missing.",
    evidence_value: str = "preflight-1",
) -> StatusUpdate:
    return StatusUpdate(
        update_id=update_id,
        task_id=TASK_ID,
        session_id=SESSION_ID,
        status="blocked",
        summary=summary,
        observed_at=observed_at,
        evidence=({"kind": "log", "value": evidence_value},),
        blocker_category="environment",
        blocker_detail=detail,
        suggested_next_action="Install the pinned environment.",
    )


def _assert_error(exc: pytest.ExceptionInfo[RCPError], code: str) -> RCPError:
    assert exc.value.code == code
    assert exc.value.exit_code == 2
    return exc.value


def test_store_initializes_versioned_secure_sqlite_and_rejects_symlinks(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runtime.sqlite3"

    with RuntimeStore(database, busy_timeout_ms=1_234) as store:
        assert store.schema_version == 3
        assert store.settings() == {
            "journal_mode": "wal",
            "foreign_keys": 1,
            "busy_timeout": 1_234,
            "synchronous": 2,
        }
        assert stat.S_IMODE(database.stat().st_mode) == 0o600

    connection = sqlite3.connect(database)
    try:
        migrations = connection.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version"
        ).fetchall()
    finally:
        connection.close()
    assert migrations == [
        (1, "initial"),
        (2, "session_notifications"),
        (3, "linear_delivery"),
    ]

    target = tmp_path / "outside.sqlite3"
    target.write_bytes(b"must-not-be-opened")
    linked = tmp_path / "linked.sqlite3"
    linked.symlink_to(target)

    with pytest.raises(RCPError) as raised:
        RuntimeStore(linked)

    _assert_error(raised, "unsafe_runtime_database_path")
    assert target.read_bytes() == b"must-not-be-opened"


def test_store_preserves_v1_data_through_v2_notification_migration(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runtime-v1.sqlite3"
    timestamp = NOW.isoformat().replace("+00:00", "Z")
    session = _session()
    update = _blocked_update()
    outbox_id = f"status-update:{update.update_id}"
    status_json = canonical_json_bytes(update).decode("utf-8")
    status_digest = canonical_digest(update)
    outbox_payload = json.loads(status_json)

    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, name TEXT NOT NULL)"
        )
        for statement in store_module._MIGRATION_1:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_migrations(version, name) VALUES (1, 'initial')"
        )
        connection.execute("PRAGMA user_version=1")
        connection.execute(
            """
            INSERT INTO operations(
                operation_id, project_id, command, idempotency_key,
                request_digest, state, started_at, finished_at,
                terminal_result, result_json
            ) VALUES (?, ?, 'status.publish', 'legacy-status', ?, 'terminal',
                      ?, ?, 'published', ?)
            """,
            (
                OPERATION_ID,
                PROJECT_ID,
                REQUEST_DIGEST,
                timestamp,
                timestamp,
                json.dumps({"update_id": update.update_id}, sort_keys=True),
            ),
        )
        connection.executemany(
            """
            INSERT INTO operation_events(
                operation_id, sequence, kind, observed_at, payload_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                (OPERATION_ID, 0, "operation_started", timestamp, "{}"),
                (OPERATION_ID, 1, "operation_finished", timestamp, "{}"),
            ),
        )
        connection.execute(
            """
            INSERT INTO runtime_sessions(
                session_id, project_id, task_id, state, created_at, updated_at,
                host, branch, worktree_path, continued_from,
                actor_token_digest, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)
            """,
            (
                session.session_id,
                session.project_id,
                session.task_id,
                session.state.value,
                timestamp,
                timestamp,
                session.host,
                session.branch,
                session.worktree_path,
                json.dumps(session.metadata, sort_keys=True),
            ),
        )
        connection.execute(
            """
            INSERT INTO status_updates(
                update_id, project_id, task_id, session_id, status,
                observed_at, payload_digest, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                update.update_id,
                PROJECT_ID,
                update.task_id,
                update.session_id,
                update.status.value,
                timestamp,
                status_digest,
                status_json,
            ),
        )
        connection.execute(
            """
            INSERT INTO outbox_events(
                outbox_id, project_id, topic, aggregate_id,
                payload_json, created_at, state
            ) VALUES (?, ?, 'status_update.v1', ?, ?, ?, 'pending')
            """,
            (
                outbox_id,
                PROJECT_ID,
                update.update_id,
                json.dumps(outbox_payload, sort_keys=True),
                timestamp,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    with RuntimeStore(database) as store:
        assert store.schema_version == store_module._SCHEMA_VERSION
        assert store.settings()["foreign_keys"] == 1
        operation = store.get_operation(OPERATION_ID)
        assert operation is not None
        assert operation.command == "status.publish"
        assert operation.result == {"update_id": update.update_id}
        assert [event.kind for event in operation.events] == [
            "operation_started",
            "operation_finished",
        ]
        assert store.get_session(SESSION_ID) == session
        assert store.get_status_update(update.update_id) == update
        outbox = store.get_outbox(outbox_id)
        assert outbox is not None
        assert outbox.topic == "status_update.v1"
        assert outbox.aggregate_id == update.update_id
        assert outbox.payload == outbox_payload
        assert outbox.state == "pending"

    connection = sqlite3.connect(database)
    try:
        migrations = connection.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version"
        ).fetchall()
        user_version = connection.execute("PRAGMA user_version").fetchone()[0]
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
        notification_table = connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name = 'session_notifications'
            """
        ).fetchone()
    finally:
        connection.close()

    assert migrations[:2] == [(1, "initial"), (2, "session_notifications")]
    assert user_version == store_module._SCHEMA_VERSION
    assert foreign_key_errors == []
    assert notification_table == ("session_notifications",)


def test_store_fails_closed_for_a_future_migration(tmp_path: Path) -> None:
    database = tmp_path / "future.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, name TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO schema_migrations(version, name) VALUES (4, 'future')"
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(RCPError) as raised:
        RuntimeStore(database)

    error = _assert_error(raised, "runtime_schema_too_new")
    assert error.context == {"found": 4, "supported": 3}


def test_begin_operation_is_idempotent_and_commits_started_event(
    tmp_path: Path,
) -> None:
    with RuntimeStore(tmp_path / "runtime.sqlite3") as store:
        first = store.begin_operation(
            PROJECT_ID,
            "session.start",
            "start-session-c",
            REQUEST_DIGEST,
            OPERATION_ID,
            NOW,
        )

        assert first.operation_id == OPERATION_ID
        assert first.state == "running"
        assert [(event.sequence, event.kind) for event in first.events] == [
            (0, "operation_started")
        ]
        assert first.events[0].payload["request_digest"] == REQUEST_DIGEST

        replay = store.begin_operation(
            PROJECT_ID,
            "session.start",
            "start-session-c",
            REQUEST_DIGEST,
            OTHER_OPERATION_ID,
            NOW + timedelta(seconds=1),
        )

        assert replay == first
        assert len(store.list_operations(PROJECT_ID)) == 1

        with pytest.raises(RCPError) as conflict:
            store.begin_operation(
                PROJECT_ID,
                "session.start",
                "start-session-c",
                OTHER_REQUEST_DIGEST,
                OTHER_OPERATION_ID,
                NOW + timedelta(seconds=2),
            )
        _assert_error(conflict, "idempotency_conflict")

        with pytest.raises(RCPError) as collision:
            store.begin_operation(
                PROJECT_ID,
                "session.pause",
                "pause-session-c",
                OTHER_REQUEST_DIGEST,
                OPERATION_ID,
                NOW + timedelta(seconds=3),
            )
        _assert_error(collision, "operation_id_conflict")


def test_operation_events_append_and_terminal_result_is_absorbing(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runtime.sqlite3"
    store = RuntimeStore(database)
    store.begin_operation(
        PROJECT_ID,
        "session.start",
        "start-session-c",
        REQUEST_DIGEST,
        OPERATION_ID,
        NOW,
    )
    event = store.append_operation_event(
        OPERATION_ID,
        "worktree_created",
        NOW + timedelta(seconds=1),
        {"path": ".research/worktrees/session-c"},
    )
    assert event.sequence == 1
    finished = store.finish_operation(
        OPERATION_ID,
        "active",
        NOW + timedelta(seconds=2),
        {"session_id": SESSION_ID},
    )

    assert finished.state == "terminal"
    assert finished.terminal_result == "active"
    assert finished.result == {"session_id": SESSION_ID}
    assert [(item.sequence, item.kind) for item in finished.events] == [
        (0, "operation_started"),
        (1, "worktree_created"),
        (2, "operation_finished"),
    ]
    assert (
        store.finish_operation(
            OPERATION_ID,
            "active",
            NOW + timedelta(seconds=20),
            {"session_id": SESSION_ID},
        )
        == finished
    )

    with pytest.raises(RCPError) as append_error:
        store.append_operation_event(
            OPERATION_ID,
            "agent_started",
            NOW + timedelta(seconds=3),
        )
    _assert_error(append_error, "operation_terminal")
    with pytest.raises(RCPError) as finish_error:
        store.finish_operation(
            OPERATION_ID,
            "failed",
            NOW + timedelta(seconds=3),
            {"reason": "different"},
        )
    _assert_error(finish_error, "operation_terminal")

    store.close()
    reopened = RuntimeStore(database)
    try:
        assert reopened.get_operation(OPERATION_ID) == finished
        assert reopened.list_operations(PROJECT_ID) == (finished,)
    finally:
        reopened.close()


def test_runtime_sessions_persist_continue_and_keep_lost_terminal(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runtime.sqlite3"
    token = "session-token-that-must-never-be-stored"
    digest = hash_session_token(token)
    store = RuntimeStore(database)
    original = store.save_session(_session(actor_token_digest=digest))

    assert store.get_session(SESSION_ID) == original
    assert store.authenticate_session(SESSION_ID, token) == original
    database_material = b"".join(
        candidate.read_bytes()
        for candidate in (
            database,
            Path(str(database) + "-wal"),
            Path(str(database) + "-shm"),
        )
        if candidate.exists()
    )
    assert token.encode() not in database_material

    for supplied in ("wrong-token", ""):
        with pytest.raises(RCPError) as unauthorized:
            store.authenticate_session(SESSION_ID, supplied)
        error = _assert_error(unauthorized, "unauthorized_actor")
        rendered = f"{error} {error.context!r}"
        if supplied:
            assert supplied not in rendered
        assert digest not in rendered

    lost = store.update_session_state(
        SESSION_ID,
        SessionState.LOST,
        NOW + timedelta(seconds=1),
    )
    assert lost.state is SessionState.LOST
    with pytest.raises(RCPError) as terminal:
        store.update_session_state(
            SESSION_ID,
            SessionState.ACTIVE,
            NOW + timedelta(seconds=2),
        )
    _assert_error(terminal, "session_terminal")

    continued = store.save_session(
        _session(
            session_id=CONTINUED_SESSION_ID,
            created_at=NOW + timedelta(seconds=3),
            updated_at=NOW + timedelta(seconds=3),
            continued_from=SESSION_ID,
            actor_token_digest=hash_session_token("new-session-token"),
        )
    )
    assert continued.continued_from == SESSION_ID
    assert store.get_session(SESSION_ID) == lost
    expected_sessions = (lost, continued)
    assert store.list_sessions(PROJECT_ID) == expected_sessions

    store.close()
    reopened = RuntimeStore(database)
    try:
        assert reopened.list_sessions(PROJECT_ID) == expected_sessions
        with pytest.raises(RCPError) as unauthorized:
            reopened.authenticate_session(SESSION_ID, "wrong-token")
        _assert_error(unauthorized, "unauthorized_actor")
    finally:
        reopened.close()


def test_authentication_always_uses_constant_time_digest_comparison(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []
    real_compare = store_module.hmac.compare_digest

    def observed_compare(first: str, second: str) -> bool:
        calls.append((first, second))
        return real_compare(first, second)

    monkeypatch.setattr(store_module.hmac, "compare_digest", observed_compare)
    with RuntimeStore(tmp_path / "runtime.sqlite3") as store:
        store.save_session(
            _session(actor_token_digest=hash_session_token("correct-token"))
        )
        with pytest.raises(RCPError):
            store.authenticate_session(SESSION_ID, "incorrect-token")
        with pytest.raises(RCPError):
            store.authenticate_session(CONTINUED_SESSION_ID, "incorrect-token")

    assert len(calls) == 2
    assert all(len(first) == len(second) for first, second in calls)


def test_session_token_rotation_is_explicit_and_state_guarded(tmp_path: Path) -> None:
    first_digest = hash_session_token("preparing-token")
    second_digest = hash_session_token("rotated-token")
    third_digest = hash_session_token("forbidden-token")
    with RuntimeStore(tmp_path / "runtime.sqlite3") as store:
        preparing = store.save_session(
            _session(
                state=SessionState.PREPARING,
                actor_token_digest=first_digest,
            )
        )

        with pytest.raises(RCPError) as implicit_rotation:
            store.save_session(
                replace(
                    preparing,
                    actor_token_digest=second_digest,
                    updated_at=NOW + timedelta(seconds=1),
                )
            )
        _assert_error(implicit_rotation, "session_identity_conflict")

        rotated = store.rotate_session_token(
            SESSION_ID,
            second_digest,
            NOW + timedelta(seconds=1),
        )

        assert rotated.actor_token_digest == second_digest
        assert store.authenticate_session(SESSION_ID, "rotated-token") == rotated
        with pytest.raises(RCPError) as old_token:
            store.authenticate_session(SESSION_ID, "preparing-token")
        _assert_error(old_token, "unauthorized_actor")

        active = store.update_session_state(
            SESSION_ID,
            SessionState.ACTIVE,
            NOW + timedelta(seconds=2),
        )
        assert active.state is SessionState.ACTIVE
        with pytest.raises(RCPError) as forbidden:
            store.rotate_session_token(
                SESSION_ID,
                third_digest,
                NOW + timedelta(seconds=3),
            )
        _assert_error(forbidden, "session_token_rotation_forbidden")
        assert store.get_session(SESSION_ID) == active


@pytest.mark.parametrize(
    "update",
    [
        _blocked_update(
            update_id="update_20260803T120000Z_" + "4" * 24,
        ).model_copy(update={"blocker_detail": None}),
        StatusUpdate(
            update_id="update_20260803T120000Z_" + "5" * 24,
            task_id=TASK_ID,
            session_id=SESSION_ID,
            status="needs_input",
            summary="Choose a recovery option.",
            observed_at=NOW,
            decision_needed={
                "question": "Which environment should be used?",
                "options": ["Use the pinned image."],
            },
        ).model_copy(update={"decision_needed": None}),
    ],
)
def test_status_publication_rejects_semantically_incomplete_updates(
    tmp_path: Path,
    update: StatusUpdate,
) -> None:
    with RuntimeStore(tmp_path / "runtime.sqlite3") as store:
        store.save_session(_session())

        with pytest.raises(RCPError) as raised:
            store.publish_status_update(PROJECT_ID, update)

        _assert_error(raised, "invalid_status_update")
        assert store.list_status_updates(PROJECT_ID) == ()
        assert store.list_outbox(PROJECT_ID) == ()
        assert store.list_inbox(PROJECT_ID) == ()


def test_status_outbox_and_attention_publish_atomically_and_idempotently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    update = _blocked_update()
    with RuntimeStore(tmp_path / "runtime.sqlite3") as store:
        store.save_session(_session())

        def injected_failure(*_: object) -> None:
            raise RuntimeError("injected attention failure")

        with monkeypatch.context() as scoped:
            scoped.setattr(RuntimeStore, "_upsert_attention", injected_failure)
            with pytest.raises(RuntimeError, match="injected attention failure"):
                store.publish_status_update(PROJECT_ID, update)

        assert store.get_status_update(update.update_id) is None
        assert store.list_outbox(PROJECT_ID) == ()
        assert store.list_inbox(PROJECT_ID) == ()

        published = store.publish_status_update(PROJECT_ID, update)
        replay = store.publish_status_update(PROJECT_ID, update)

        assert replay == published
        assert published.outbox.outbox_id == f"status-update:{update.update_id}"
        assert published.outbox.state == "pending"
        assert published.attention.dedupe_key == attention_dedupe_key(
            PROJECT_ID,
            update,
        )
        assert len(store.list_status_updates(PROJECT_ID)) == 1
        assert len(store.list_outbox(PROJECT_ID)) == 1
        assert len(store.list_inbox(PROJECT_ID)) == 1

        conflicting = update.model_copy(update={"summary": "Different content."})
        with pytest.raises(RCPError) as raised:
            store.publish_status_update(PROJECT_ID, conflicting)
        _assert_error(raised, "status_update_conflict")


def test_inbox_actions_preserve_updates_and_material_evidence_reopens(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runtime.sqlite3"
    store = RuntimeStore(database)
    store.save_session(_session())
    store.begin_operation(
        PROJECT_ID,
        "inbox.resolve",
        "resolve-environment-blocker",
        REQUEST_DIGEST,
        OPERATION_ID,
        NOW,
    )
    first_update = _blocked_update()
    published = store.publish_status_update(PROJECT_ID, first_update)
    key = published.attention.dedupe_key
    original_updates = store.list_status_updates(PROJECT_ID)

    acknowledged = store.ack_attention(
        key,
        "manager@example.invalid",
        NOW + timedelta(minutes=1),
        expected_generation=1,
        operation_id=OPERATION_ID,
    )
    assert acknowledged.state == "acknowledged"
    assert store.list_status_updates(PROJECT_ID) == original_updates

    snoozed = store.snooze_attention(
        key,
        "manager@example.invalid",
        NOW + timedelta(hours=1),
        NOW + timedelta(minutes=2),
        expected_generation=1,
        operation_id=OPERATION_ID,
    )
    assert snoozed.state == "snoozed"
    assert store.list_inbox(
        PROJECT_ID,
        as_of=NOW + timedelta(minutes=30),
    ) == ()
    assert store.list_inbox(
        PROJECT_ID,
        as_of=NOW + timedelta(hours=2),
    ) == (snoozed,)

    resolved = store.resolve_attention(
        key,
        "manager@example.invalid",
        "Environment was repaired.",
        NOW + timedelta(minutes=3),
        expected_generation=1,
        operation_id=OPERATION_ID,
    )
    assert resolved.state == "resolved"
    assert store.list_inbox(PROJECT_ID, as_of=NOW + timedelta(hours=2)) == ()
    assert store.list_status_updates(PROJECT_ID) == original_updates

    unchanged_update = _blocked_update(
        update_id="update_20260803T120100Z_" + "6" * 24,
        observed_at=NOW + timedelta(minutes=4),
    )
    unchanged = store.publish_status_update(PROJECT_ID, unchanged_update)
    assert unchanged.attention.state == "resolved"
    assert unchanged.attention.generation == 1

    material_update = _blocked_update(
        update_id="update_20260803T120200Z_" + "7" * 24,
        observed_at=NOW + timedelta(minutes=5),
        summary="Environment preflight found a second failure.",
        detail="CUDA toolkit and compiler are missing.",
        evidence_value="preflight-2",
    )
    reopened = store.publish_status_update(PROJECT_ID, material_update)

    assert reopened.attention.dedupe_key == key
    assert reopened.attention.state == "open"
    assert reopened.attention.generation == 2
    assert reopened.attention.resolved_by is None
    assert first_update == store.get_status_update(first_update.update_id)
    assert len(store.list_status_updates(PROJECT_ID)) == 3

    with pytest.raises(RCPError) as stale:
        store.resolve_attention(
            key,
            "manager@example.invalid",
            "Stale browser action.",
            NOW + timedelta(minutes=6),
            expected_generation=1,
            operation_id=OPERATION_ID,
        )
    _assert_error(stale, "stale_attention")

    expected_updates = store.list_status_updates(PROJECT_ID)
    expected_outbox = store.list_outbox(PROJECT_ID)
    expected_inbox = store.list_inbox(PROJECT_ID)
    store.close()

    reopened_store = RuntimeStore(database)
    try:
        assert reopened_store.list_status_updates(PROJECT_ID) == expected_updates
        assert reopened_store.list_outbox(PROJECT_ID) == expected_outbox
        assert reopened_store.list_inbox(PROJECT_ID) == expected_inbox
    finally:
        reopened_store.close()
