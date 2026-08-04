from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from researchctl.services.linear_delivery_monitor import (
    DeliveryObservationSource,
    LinearDeliveryHealthMonitor,
    LinearDeliveryHealthPolicy,
    LinearDeliveryHealthReason,
    LinearDeliveryObservation,
    LinearDeliveryReceiptState,
)


NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
PROJECT_ID = "project_20260803T120000Z_" + "a" * 24


@dataclass(slots=True)
class FakeObservationSource(DeliveryObservationSource):
    observations: tuple[LinearDeliveryObservation, ...]
    requested_projects: list[str]

    def list_delivery_observations(
        self,
        *,
        project_id: str,
    ) -> tuple[LinearDeliveryObservation, ...]:
        self.requested_projects.append(project_id)
        return self.observations


def _observation(
    outbox_id: str,
    *,
    topic: str = "linear.accepted-result.v1",
    state: str = "pending",
    age: timedelta = timedelta(minutes=1),
    attempt_count: int = 0,
    last_error_code: str | None = None,
    last_claim_id: str | None = None,
    receipt_id: str | None = None,
    project_id: str = PROJECT_ID,
) -> LinearDeliveryObservation:
    return LinearDeliveryObservation(
        project_id=project_id,
        topic=topic,  # type: ignore[arg-type]
        outbox_id=outbox_id,
        state=state,  # type: ignore[arg-type]
        created_at=NOW - age,
        status_updated_at=NOW - timedelta(seconds=5),
        attempt_count=attempt_count,
        last_error_code=last_error_code,
        last_claim_id=last_claim_id,
        receipt_id=receipt_id,
    )


def _monitor(
    observations: tuple[LinearDeliveryObservation, ...],
    *,
    policy: LinearDeliveryHealthPolicy | None = None,
    now: datetime = NOW,
) -> tuple[LinearDeliveryHealthMonitor, FakeObservationSource]:
    source = FakeObservationSource(observations, [])
    return (
        LinearDeliveryHealthMonitor(
            source=source,
            policy=policy
            or LinearDeliveryHealthPolicy(
                max_pending_age=timedelta(minutes=15),
                max_attempts=3,
            ),
            clock=lambda: now,
        ),
        source,
    )


def test_young_pending_and_delivered_records_do_not_raise_exceptions() -> None:
    monitor, source = _monitor(
        (
            _observation("young-pending", age=timedelta(minutes=14, seconds=59)),
            _observation(
                "delivered",
                state="delivered",
                age=timedelta(days=4),
                attempt_count=20,
                receipt_id="linear-receipt-1",
            ),
        )
    )

    assert monitor.evaluate(project_id=PROJECT_ID) == ()
    assert source.requested_projects == [PROJECT_ID]


def test_pending_age_boundary_produces_typed_manager_exception() -> None:
    monitor, _ = _monitor(
        (_observation("old-pending", age=timedelta(minutes=15)),)
    )

    (exception,) = monitor.evaluate(project_id=PROJECT_ID)

    assert exception.kind == "linear_delivery_health"
    assert exception.route == "manager_exception"
    assert exception.reasons == (
        LinearDeliveryHealthReason.PENDING_AGE_EXCEEDED,
    )
    assert exception.age == timedelta(minutes=15)
    assert exception.age_seconds == 900
    assert exception.attempt_count == 0
    assert exception.last_error_code is None
    assert exception.receipt_state is LinearDeliveryReceiptState.MISSING
    assert exception.receipt_id is None
    assert exception.dedupe_key.startswith("sha256:")
    assert exception.evidence_digest.startswith("sha256:")
    assert exception.as_dict()["age_seconds"] == 900


def test_attempt_boundary_carries_error_claim_and_receipt_evidence() -> None:
    monitor, _ = _monitor(
        (
            _observation(
                "retried",
                topic="linear.session-reply.v1",
                attempt_count=3,
                last_error_code="linear_delivery_unavailable",
                last_claim_id="claim-3",
                receipt_id="linear-receipt-observed",
            ),
        )
    )

    (exception,) = monitor.evaluate(project_id=PROJECT_ID)

    assert exception.topic == "linear.session-reply.v1"
    assert exception.reasons == (
        LinearDeliveryHealthReason.ATTEMPT_LIMIT_REACHED,
    )
    assert exception.attempt_count == 3
    assert exception.last_error_code == "linear_delivery_unavailable"
    assert exception.last_claim_id == "claim-3"
    assert exception.receipt_state is LinearDeliveryReceiptState.PRESENT
    assert exception.receipt_id == "linear-receipt-observed"


def test_dead_letter_is_immediate_by_default_and_policy_can_disable_it() -> None:
    observation = _observation("dead", state="dead_letter")
    immediate, _ = _monitor((observation,))

    (exception,) = immediate.evaluate(project_id=PROJECT_ID)
    assert exception.reasons == (LinearDeliveryHealthReason.DEAD_LETTER,)

    disabled, _ = _monitor(
        (observation,),
        policy=LinearDeliveryHealthPolicy(
            max_pending_age=timedelta(minutes=15),
            max_attempts=3,
            dead_letter_immediate=False,
        ),
    )
    assert disabled.evaluate(project_id=PROJECT_ID) == ()


def test_disabled_dead_letter_immediacy_does_not_hide_attempt_exhaustion() -> None:
    monitor, _ = _monitor(
        (_observation("dead-retried", state="dead_letter", attempt_count=3),),
        policy=LinearDeliveryHealthPolicy(
            max_pending_age=timedelta(minutes=15),
            max_attempts=3,
            dead_letter_immediate=False,
        ),
    )

    (exception,) = monitor.evaluate(project_id=PROJECT_ID)
    assert exception.reasons == (
        LinearDeliveryHealthReason.ATTEMPT_LIMIT_REACHED,
    )


def test_sort_and_digests_are_stable_across_source_order_and_clock_age() -> None:
    accepted = _observation("z-event", age=timedelta(hours=1))
    reply = _observation(
        "a-reply",
        topic="linear.session-reply.v1",
        age=timedelta(hours=1),
    )
    first, _ = _monitor((reply, accepted))
    later, _ = _monitor((accepted, reply), now=NOW + timedelta(minutes=5))

    first_result = first.evaluate(project_id=PROJECT_ID)
    later_result = later.evaluate(project_id=PROJECT_ID)

    assert [(item.topic, item.outbox_id) for item in first_result] == [
        ("linear.accepted-result.v1", "z-event"),
        ("linear.session-reply.v1", "a-reply"),
    ]
    assert [item.dedupe_key for item in later_result] == [
        item.dedupe_key for item in first_result
    ]
    assert [item.evidence_digest for item in later_result] == [
        item.evidence_digest for item in first_result
    ]
    assert later_result[0].age > first_result[0].age


def test_combined_reasons_have_fixed_order_and_change_evidence_not_identity() -> None:
    older = _observation(
        "event",
        age=timedelta(hours=1),
        attempt_count=3,
    )
    age_only = _observation("event", age=timedelta(hours=1), attempt_count=2)
    combined_monitor, _ = _monitor((older,))
    age_monitor, _ = _monitor((age_only,))

    (combined,) = combined_monitor.evaluate(project_id=PROJECT_ID)
    (single,) = age_monitor.evaluate(project_id=PROJECT_ID)

    assert combined.reasons == (
        LinearDeliveryHealthReason.PENDING_AGE_EXCEEDED,
        LinearDeliveryHealthReason.ATTEMPT_LIMIT_REACHED,
    )
    assert combined.dedupe_key == single.dedupe_key
    assert combined.evidence_digest != single.evidence_digest


def test_source_project_leak_duplicates_and_naive_clock_fail_closed() -> None:
    other_project = "project_20260803T120000Z_" + "b" * 24
    leaked, _ = _monitor((_observation("leaked", project_id=other_project),))
    with pytest.raises(ValueError, match="outside the requested project"):
        leaked.evaluate(project_id=PROJECT_ID)

    duplicate = _observation("duplicate")
    duplicated, _ = _monitor((duplicate, duplicate))
    with pytest.raises(ValueError, match="duplicate topic/outbox"):
        duplicated.evaluate(project_id=PROJECT_ID)

    naive, _ = _monitor((duplicate,), now=NOW.replace(tzinfo=None))
    with pytest.raises(ValueError, match="timezone-aware"):
        naive.evaluate(project_id=PROJECT_ID)


def test_source_item_type_is_checked_before_deterministic_sorting() -> None:
    source = FakeObservationSource((object(),), [])  # type: ignore[arg-type]
    monitor = LinearDeliveryHealthMonitor(
        source=source,
        policy=LinearDeliveryHealthPolicy(
            max_pending_age=timedelta(minutes=15),
            max_attempts=3,
        ),
        clock=lambda: NOW,
    )

    with pytest.raises(TypeError, match="invalid item at index 0"):
        monitor.evaluate(project_id=PROJECT_ID)


@pytest.mark.parametrize(
    ("kwargs", "error_type"),
    [
        ({"max_pending_age": timedelta(0), "max_attempts": 1}, ValueError),
        ({"max_pending_age": timedelta(seconds=1), "max_attempts": 0}, ValueError),
        ({"max_pending_age": timedelta(seconds=1), "max_attempts": True}, ValueError),
        (
            {
                "max_pending_age": timedelta(seconds=1),
                "max_attempts": 1,
                "dead_letter_immediate": 1,
            },
            TypeError,
        ),
    ],
)
def test_policy_rejects_non_strict_thresholds(
    kwargs: dict[str, object],
    error_type: type[Exception],
) -> None:
    with pytest.raises(error_type):
        LinearDeliveryHealthPolicy(**kwargs)  # type: ignore[arg-type]


def test_observation_rejects_invalid_state_timestamp_and_attempts() -> None:
    with pytest.raises(ValueError, match="unsupported Linear delivery state"):
        _observation("bad-state", state="retryable")
    with pytest.raises(ValueError, match="non-negative integer"):
        _observation("bad-attempts", attempt_count=-1)
    with pytest.raises(ValueError, match="timezone-aware"):
        LinearDeliveryObservation(
            project_id=PROJECT_ID,
            topic="linear.accepted-result.v1",
            outbox_id="naive",
            state="pending",
            created_at=NOW.replace(tzinfo=None),
            status_updated_at=None,
            attempt_count=0,
            last_error_code=None,
            last_claim_id=None,
            receipt_id=None,
        )
