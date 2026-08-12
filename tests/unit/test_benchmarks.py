from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from researchctl.benchmarks import (
    INBOX_ITEM_COUNT,
    MIN_PRODUCTION_SAMPLE_COUNT,
    MIN_PRODUCTION_WINDOW_SECONDS,
    InboxBenchmarkReport,
    LatencyMeasurement,
    _nearest_rank,
    run_inbox_benchmark,
)
from researchctl.domain.enums import SessionState, StatusKind
from researchctl.domain.models import StatusUpdate
from researchctl.runtime import RuntimeSession, RuntimeStore

NOW = datetime(2026, 8, 3, 12, 0, 0, tzinfo=UTC)
AS_OF = NOW + timedelta(hours=1)
PROJECT_ID = "project_20260803T120000Z_" + "a" * 24
COMMIT = "b" * 40


def _record_id(kind: str, index: int) -> str:
    return f"{kind}_20260803T120000Z_{index:024x}"


def _populate_inbox(store: RuntimeStore) -> None:
    for index in range(1, INBOX_ITEM_COUNT + 1):
        task_id = _record_id("task", index)
        session_id = _record_id("session", index)
        observed_at = NOW + timedelta(seconds=index)
        store.save_session(
            RuntimeSession(
                session_id=session_id,
                project_id=PROJECT_ID,
                task_id=task_id,
                state=SessionState.ACTIVE,
                created_at=observed_at,
                updated_at=observed_at,
                host="benchmark-host",
                branch=f"research/task/BENCH-{index}/{session_id}",
                worktree_path=f".research/worktrees/{session_id}",
                metadata={"fixture_index": index},
            )
        )

        common = {
            "update_id": _record_id("update", index),
            "task_id": task_id,
            "session_id": session_id,
            "summary": f"Benchmark attention item {index}.",
            "observed_at": observed_at,
            "evidence": ({"kind": "fixture", "value": f"item-{index:02d}"},),
        }
        variant = index % 3
        if variant == 0:
            update = StatusUpdate(
                **common,
                status=StatusKind.BLOCKED,
                blocker_category=f"environment-{index:02d}",
                blocker_detail=f"Fixture blocker {index} requires attention.",
                suggested_next_action="Inspect the benchmark fixture.",
            )
        elif variant == 1:
            update = StatusUpdate(
                **common,
                status=StatusKind.NEEDS_INPUT,
                decision_needed={
                    "question": f"Which fixture option should item {index} use?",
                    "options": ("option-a", "option-b"),
                },
            )
        else:
            update = StatusUpdate(
                **common,
                status=StatusKind.READY_FOR_REVIEW,
                suggested_next_action="Review the benchmark evidence.",
            )
        store.publish_status_update(PROJECT_ID, update)


def test_inbox_benchmark_reports_repeatable_preliminary_50_item_baseline(
    tmp_path: Path,
) -> None:
    with RuntimeStore(tmp_path / "runtime.sqlite3") as store:
        _populate_inbox(store)
        visible = store.list_inbox(PROJECT_ID, as_of=AS_OF)
        assert len(visible) == INBOX_ITEM_COUNT

        first = run_inbox_benchmark(
            store,
            PROJECT_ID,
            as_of=AS_OF,
            sample_count=10,
            commit=COMMIT,
            host="local-test-host",
        )
        second = run_inbox_benchmark(
            store,
            PROJECT_ID,
            as_of=AS_OF,
            sample_count=10,
            commit=COMMIT,
            host="local-test-host",
        )

    report = first.as_dict()
    assert first.fixture_digest == second.fixture_digest
    assert report["benchmark"] == "inbox-50-v1"
    assert report["preliminary"] is True
    assert report["production_slo_eligible"] is False
    assert "production_slo_passed" not in report
    assert report["measurement_scope"] == "diagnostic_components"
    assert report["sample_count"] == 10
    assert report["commit"] == COMMIT
    assert report["host"] == "local-test-host"
    assert report["cache_state"] == "warm"
    assert report["timing_source"] == "system"
    assert report["item_count"] == INBOX_ITEM_COUNT
    assert report["fixture_digest"].startswith("sha256:")
    assert len(report["fixture_digest"]) == 71
    assert report["failure_count"] == 0
    assert report["partial_results"] is False

    requirement = report["production_slo_requirement"]
    assert requirement["minimum_sample_count"] == MIN_PRODUCTION_SAMPLE_COUNT
    assert requirement["minimum_window_seconds"] == MIN_PRODUCTION_WINDOW_SECONDS
    assert "200 samples" in requirement["reason"]
    assert "30 minutes" in requirement["reason"]
    assert report["window"]["duration_seconds"] >= 0

    assert set(report["measurements"]) == {"warm_list", "inbox_render"}
    for measurement in report["measurements"].values():
        assert measurement["sample_count"] == 10
        assert measurement["successful_sample_count"] == 10
        assert measurement["failure_count"] == 0
        assert measurement["p50_ms"] <= measurement["p95_ms"]
        assert measurement["p95_ms"] <= measurement["p99_ms"]

    json.dumps(report, allow_nan=False, sort_keys=True)


def test_inbox_benchmark_rejects_a_non_50_item_fixture(tmp_path: Path) -> None:
    with (
        RuntimeStore(tmp_path / "runtime.sqlite3") as store,
        pytest.raises(ValueError, match="requires exactly 50 visible attention items"),
    ):
        run_inbox_benchmark(
            store,
            PROJECT_ID,
            as_of=AS_OF,
            sample_count=1,
            commit=COMMIT,
            host="local-test-host",
        )


def test_nearest_rank_percentiles_are_dependency_free_and_deterministic() -> None:
    samples = [9.0, 1.0, 3.0, 7.0, 5.0]

    assert _nearest_rank(samples, 0.50) == 5.0
    assert _nearest_rank(samples, 0.95) == 9.0
    assert _nearest_rank(samples, 0.99) == 9.0
    assert _nearest_rank([], 0.95) is None


def test_report_invariants_fail_closed_and_never_issue_a_production_pass() -> None:
    measurement = LatencyMeasurement(
        sample_count=200,
        successful_sample_count=200,
        failure_count=0,
        p50_ms=1.0,
        p95_ms=2.0,
        p99_ms=3.0,
    )
    report = InboxBenchmarkReport(
        started_at=NOW,
        finished_at=NOW + timedelta(minutes=30),
        duration_seconds=MIN_PRODUCTION_WINDOW_SECONDS,
        sample_count=200,
        commit=COMMIT,
        host="local-test-host",
        fixture_digest="sha256:" + "c" * 64,
        warm_list=measurement,
        inbox_render=measurement,
        synthetic_timing=True,
    ).as_dict()

    assert report["preliminary"] is True
    assert report["production_slo_eligible"] is False
    assert report["timing_source"] == "synthetic"
    assert "production_slo_passed" not in report

    with pytest.raises(ValueError, match="must equal sample_count"):
        LatencyMeasurement(
            sample_count=2,
            successful_sample_count=0,
            failure_count=1,
            p50_ms=None,
            p95_ms=None,
            p99_ms=None,
        )
    with pytest.raises(TypeError, match="warm_list must be a LatencyMeasurement"):
        InboxBenchmarkReport(
            started_at=NOW,
            finished_at=NOW,
            duration_seconds=0,
            sample_count=200,
            commit=COMMIT,
            host="local-test-host",
            fixture_digest="sha256:" + "c" * 64,
            warm_list=None,  # type: ignore[arg-type]
            inbox_render=measurement,
        )
    with pytest.raises(ValueError, match="sample_count must match the report"):
        InboxBenchmarkReport(
            started_at=NOW,
            finished_at=NOW,
            duration_seconds=0,
            sample_count=201,
            commit=COMMIT,
            host="local-test-host",
            fixture_digest="sha256:" + "c" * 64,
            warm_list=measurement,
            inbox_render=measurement,
        )


def test_inbox_benchmark_detects_fixture_changes_outside_the_timed_read(
    tmp_path: Path,
) -> None:
    with RuntimeStore(tmp_path / "runtime.sqlite3") as store:
        _populate_inbox(store)
        items = store.list_inbox(PROJECT_ID, as_of=AS_OF)

    changed = (replace(items[0], generation=items[0].generation + 1), *items[1:])

    class ChangingReader:
        def __init__(self) -> None:
            self.calls = 0

        def list_inbox(
            self,
            project_id: str,
            *,
            as_of: datetime | None = None,
            include_resolved: bool = False,
        ) -> tuple:
            assert project_id == PROJECT_ID
            assert as_of == AS_OF
            assert include_resolved is False
            self.calls += 1
            return changed if self.calls == 3 else items

    report = run_inbox_benchmark(
        ChangingReader(),
        PROJECT_ID,
        as_of=AS_OF,
        sample_count=1,
        commit=COMMIT,
        host="local-test-host",
    ).as_dict()

    assert report["measurements"]["warm_list"]["failure_count"] == 1
    assert report["measurements"]["warm_list"]["successful_sample_count"] == 0
    assert report["measurements"]["inbox_render"]["failure_count"] == 0
    assert report["failure_count"] == 1
    assert report["partial_results"] is True
