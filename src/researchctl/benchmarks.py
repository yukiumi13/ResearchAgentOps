from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from math import ceil, isfinite
from time import perf_counter_ns
from typing import Any, Protocol, TypeVar

from researchctl.runtime import AttentionItem
from researchctl.serialization import canonical_json_bytes

INBOX_BENCHMARK_NAME = "inbox-50-v1"
INBOX_ITEM_COUNT = 50
MIN_PRODUCTION_SAMPLE_COUNT = 200
MIN_PRODUCTION_WINDOW_SECONDS = 30 * 60
LOCAL_INBOX_P95_OBJECTIVE_MS = 500.0
LOCAL_INBOX_P99_OBJECTIVE_MS = 1_000.0

ResultT = TypeVar("ResultT")


class InboxReader(Protocol):
    def list_inbox(
        self,
        project_id: str,
        *,
        as_of: datetime | None = None,
        include_resolved: bool = False,
    ) -> tuple[AttentionItem, ...]: ...


@dataclass(frozen=True, slots=True)
class LatencyMeasurement:
    sample_count: int
    successful_sample_count: int
    failure_count: int
    p50_ms: float | None
    p95_ms: float | None
    p99_ms: float | None

    def __post_init__(self) -> None:
        counts = (
            self.sample_count,
            self.successful_sample_count,
            self.failure_count,
        )
        if any(not isinstance(value, int) or isinstance(value, bool) for value in counts):
            raise TypeError("measurement counts must be integers")
        if self.sample_count < 1:
            raise ValueError("measurement sample_count must be at least 1")
        if self.successful_sample_count < 0 or self.failure_count < 0:
            raise ValueError("measurement counts cannot be negative")
        if self.successful_sample_count + self.failure_count != self.sample_count:
            raise ValueError(
                "successful_sample_count plus failure_count must equal sample_count"
            )
        percentiles = (self.p50_ms, self.p95_ms, self.p99_ms)
        if self.successful_sample_count == 0:
            if any(value is not None for value in percentiles):
                raise ValueError("failed measurements cannot report percentiles")
            return
        if any(
            value is None or not isfinite(value) or value < 0
            for value in percentiles
        ):
            raise ValueError("successful measurements require finite percentiles")
        p50, p95, p99 = percentiles
        if not (p50 <= p95 <= p99):
            raise ValueError("measurement percentiles must be monotonic")

    def as_dict(self) -> dict[str, int | float | None]:
        return {
            "sample_count": self.sample_count,
            "successful_sample_count": self.successful_sample_count,
            "failure_count": self.failure_count,
            "p50_ms": self.p50_ms,
            "p95_ms": self.p95_ms,
            "p99_ms": self.p99_ms,
        }


@dataclass(frozen=True, slots=True)
class InboxBenchmarkReport:
    started_at: datetime
    finished_at: datetime
    duration_seconds: float
    sample_count: int
    commit: str
    host: str
    fixture_digest: str
    warm_list: LatencyMeasurement
    inbox_render: LatencyMeasurement
    synthetic_timing: bool = False

    def __post_init__(self) -> None:
        _require_aware_time(self.started_at, "started_at")
        _require_aware_time(self.finished_at, "finished_at")
        if self.finished_at < self.started_at:
            raise ValueError("benchmark finished_at cannot precede started_at")
        if not isfinite(self.duration_seconds) or self.duration_seconds < 0:
            raise ValueError("benchmark duration_seconds must be finite and non-negative")
        if isinstance(self.sample_count, bool) or self.sample_count < 1:
            raise ValueError("benchmark sample_count must be at least 1")
        if not isinstance(self.synthetic_timing, bool):
            raise TypeError("synthetic_timing must be a boolean")
        for name, measurement in self.measurements.items():
            if not isinstance(measurement, LatencyMeasurement):
                raise TypeError(f"{name} must be a LatencyMeasurement")
            if measurement.sample_count != self.sample_count:
                raise ValueError(f"{name} sample_count must match the report")
        if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", self.commit) is None:
            raise ValueError("commit must be a 40- or 64-character Git object ID")
        if not self.host.strip():
            raise ValueError("host must be non-empty")
        if re.fullmatch(r"sha256:[0-9a-f]{64}", self.fixture_digest) is None:
            raise ValueError("fixture_digest must be a SHA-256 digest")

    @property
    def measurements(self) -> dict[str, LatencyMeasurement]:
        return {
            "warm_list": self.warm_list,
            "inbox_render": self.inbox_render,
        }

    @property
    def failure_count(self) -> int:
        return sum(item.failure_count for item in self.measurements.values())

    @property
    def preliminary(self) -> bool:
        return True

    @property
    def production_slo_eligible(self) -> bool:
        return False

    def as_dict(self) -> dict[str, Any]:
        return {
            "benchmark": INBOX_BENCHMARK_NAME,
            "preliminary": self.preliminary,
            "production_slo_eligible": self.production_slo_eligible,
            "production_slo_requirement": {
                "minimum_sample_count": MIN_PRODUCTION_SAMPLE_COUNT,
                "minimum_window_seconds": MIN_PRODUCTION_WINDOW_SECONDS,
                "p95_objective_ms": LOCAL_INBOX_P95_OBJECTIVE_MS,
                "p99_objective_ms": LOCAL_INBOX_P99_OBJECTIVE_MS,
                "reason": (
                    "This diagnostic component baseline cannot pass a production "
                    "SLO. Production evaluation requires an end-to-end inbox path "
                    "with at least 200 samples over at least 30 minutes."
                ),
            },
            "measurement_scope": "diagnostic_components",
            "sample_count": self.sample_count,
            "window": {
                "started_at": _format_timestamp(self.started_at),
                "finished_at": _format_timestamp(self.finished_at),
                "duration_seconds": self.duration_seconds,
            },
            "commit": self.commit,
            "host": self.host,
            "fixture_digest": self.fixture_digest,
            "cache_state": "warm",
            "timing_source": "synthetic" if self.synthetic_timing else "system",
            "item_count": INBOX_ITEM_COUNT,
            "failure_count": self.failure_count,
            "partial_results": self.failure_count > 0,
            "queue_time_ms": None,
            "measurements": {
                name: measurement.as_dict()
                for name, measurement in sorted(self.measurements.items())
            },
        }


def run_inbox_benchmark(
    runtime: InboxReader,
    project_id: str,
    *,
    as_of: datetime,
    sample_count: int,
    commit: str,
    host: str,
    wall_clock: Callable[[], datetime] | None = None,
    timer_ns: Callable[[], int] = perf_counter_ns,
) -> InboxBenchmarkReport:
    """Measure warm inbox reads and the real JSON data-rendering path separately."""

    if sample_count < 1:
        raise ValueError("sample_count must be at least 1")
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone aware")
    if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit) is None:
        raise ValueError("commit must be a 40- or 64-character Git object ID")
    if not host.strip():
        raise ValueError("host must be non-empty")

    clock = wall_clock or (lambda: datetime.now(UTC))
    fixture_items = _require_inbox_fixture(
        runtime.list_inbox(project_id, as_of=as_of),
    )
    fixture_payload = _render_inbox_payload(fixture_items)
    fixture_digest = "sha256:" + hashlib.sha256(fixture_payload).hexdigest()

    # These warmups exercise both paths once without contributing a sample.
    _require_fixture_digest(
        runtime.list_inbox(project_id, as_of=as_of),
        fixture_digest,
    )
    _render_inbox_payload(fixture_items)

    started_at = _require_aware_time(clock(), "wall_clock")
    window_started_ns = timer_ns()
    warm_list = _measure(
        sample_count,
        lambda: runtime.list_inbox(project_id, as_of=as_of),
        lambda items: _require_fixture_digest(items, fixture_digest),
        timer_ns,
    )
    inbox_render = _measure(
        sample_count,
        lambda: _render_inbox_payload(fixture_items),
        lambda payload: _require_payload_digest(payload, fixture_digest),
        timer_ns,
    )
    window_finished_ns = timer_ns()
    finished_at = _require_aware_time(clock(), "wall_clock")
    elapsed_ns = window_finished_ns - window_started_ns
    if elapsed_ns < 0:
        raise RuntimeError("benchmark timer moved backwards")

    return InboxBenchmarkReport(
        started_at=started_at,
        finished_at=finished_at,
        duration_seconds=round(elapsed_ns / 1_000_000_000, 9),
        sample_count=sample_count,
        commit=commit,
        host=host,
        fixture_digest=fixture_digest,
        warm_list=warm_list,
        inbox_render=inbox_render,
        synthetic_timing=wall_clock is not None or timer_ns is not perf_counter_ns,
    )


def _measure(
    sample_count: int,
    operation: Callable[[], ResultT],
    validator: Callable[[ResultT], object],
    timer_ns: Callable[[], int],
) -> LatencyMeasurement:
    elapsed_ms: list[float] = []
    failure_count = 0
    for _ in range(sample_count):
        started_ns = timer_ns()
        result: ResultT | None = None
        operation_succeeded = False
        try:
            result = operation()
            operation_succeeded = True
        except Exception:
            failure_count += 1
        finished_ns = timer_ns()
        duration_ns = finished_ns - started_ns
        if duration_ns < 0:
            raise RuntimeError("benchmark timer moved backwards")
        if operation_succeeded:
            try:
                validator(result)  # type: ignore[arg-type]
            except Exception:
                failure_count += 1
            else:
                elapsed_ms.append(duration_ns / 1_000_000)

    return LatencyMeasurement(
        sample_count=sample_count,
        successful_sample_count=len(elapsed_ms),
        failure_count=failure_count,
        p50_ms=_nearest_rank(elapsed_ms, 0.50),
        p95_ms=_nearest_rank(elapsed_ms, 0.95),
        p99_ms=_nearest_rank(elapsed_ms, 0.99),
    )


def _nearest_rank(samples: Sequence[float], percentile: float) -> float | None:
    if not samples:
        return None
    if not 0 < percentile <= 1:
        raise ValueError("percentile must be in the interval (0, 1]")
    ordered = sorted(samples)
    rank = max(1, ceil(percentile * len(ordered)))
    return round(ordered[rank - 1], 6)


def _require_inbox_fixture(items: tuple[AttentionItem, ...]) -> tuple[AttentionItem, ...]:
    if len(items) != INBOX_ITEM_COUNT:
        raise ValueError(
            f"{INBOX_BENCHMARK_NAME} requires exactly {INBOX_ITEM_COUNT} visible "
            f"attention items; observed {len(items)}"
        )
    return items


def _render_inbox_payload(items: tuple[AttentionItem, ...]) -> bytes:
    # Imported lazily so this benchmark can later be wired into the root CLI
    # without creating an import cycle during command registration.
    from researchctl.phase2_cli import _result_data

    data = _result_data(items)
    rendered_items = data.get("items")
    if not isinstance(rendered_items, list) or len(rendered_items) != INBOX_ITEM_COUNT:
        raise ValueError("inbox rendering did not preserve the 50-item fixture")
    return canonical_json_bytes(data)


def _require_fixture_digest(
    items: tuple[AttentionItem, ...],
    expected_digest: str,
) -> None:
    fixture = _require_inbox_fixture(items)
    _require_payload_digest(_render_inbox_payload(fixture), expected_digest)


def _require_payload_digest(payload: bytes, expected_digest: str) -> None:
    observed_digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    if observed_digest != expected_digest:
        raise ValueError("inbox fixture changed during benchmark sampling")


def _require_aware_time(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must return a timezone-aware datetime")
    return value.astimezone(UTC)


def _format_timestamp(value: datetime) -> str:
    source = value.astimezone(UTC)
    timespec = "microseconds" if source.microsecond else "seconds"
    return source.isoformat(timespec=timespec).replace("+00:00", "Z")
