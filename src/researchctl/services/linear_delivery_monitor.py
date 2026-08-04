from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Literal, Protocol

from researchctl.serialization import canonical_digest


class LinearDeliveryHealthReason(StrEnum):
    PENDING_AGE_EXCEEDED = "pending_age_exceeded"
    ATTEMPT_LIMIT_REACHED = "attempt_limit_reached"
    DEAD_LETTER = "dead_letter"


class LinearDeliveryReceiptState(StrEnum):
    MISSING = "missing"
    PRESENT = "present"


LinearDeliveryTopic = Literal[
    "linear.accepted-result.v1",
    "linear.session-reply.v1",
]
LinearDeliveryState = Literal["pending", "delivered", "dead_letter"]

_SUPPORTED_TOPICS = frozenset(
    {
        "linear.accepted-result.v1",
        "linear.session-reply.v1",
    }
)
_SUPPORTED_STATES = frozenset({"pending", "delivered", "dead_letter"})
_EXCEPTION_KIND: Literal["linear_delivery_health"] = "linear_delivery_health"
_MANAGER_ROUTE: Literal["manager_exception"] = "manager_exception"


def _require_text(value: object, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")


def _optional_text(value: object, field: str) -> None:
    if value is not None:
        _require_text(value, field)


def _utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    timespec = "microseconds" if value.microsecond else "seconds"
    return value.astimezone(UTC).isoformat(timespec=timespec).replace("+00:00", "Z")


def _duration_microseconds(value: timedelta) -> int:
    return (
        value.days * 24 * 60 * 60 * 1_000_000
        + value.seconds * 1_000_000
        + value.microseconds
    )


@dataclass(frozen=True, slots=True)
class LinearDeliveryHealthPolicy:
    max_pending_age: timedelta
    max_attempts: int
    dead_letter_immediate: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.max_pending_age, timedelta):
            raise TypeError("max_pending_age must be a timedelta")
        if self.max_pending_age <= timedelta(0):
            raise ValueError("max_pending_age must be greater than zero")
        if type(self.max_attempts) is not int or self.max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")
        if type(self.dead_letter_immediate) is not bool:
            raise TypeError("dead_letter_immediate must be a bool")


@dataclass(frozen=True, slots=True)
class LinearDeliveryObservation:
    project_id: str
    topic: LinearDeliveryTopic
    outbox_id: str
    state: LinearDeliveryState
    created_at: datetime
    status_updated_at: datetime | None
    attempt_count: int
    last_error_code: str | None
    last_claim_id: str | None
    receipt_id: str | None

    def __post_init__(self) -> None:
        _require_text(self.project_id, "project_id")
        _require_text(self.outbox_id, "outbox_id")
        if self.topic not in _SUPPORTED_TOPICS:
            raise ValueError(f"unsupported Linear delivery topic: {self.topic!r}")
        if self.state not in _SUPPORTED_STATES:
            raise ValueError(f"unsupported Linear delivery state: {self.state!r}")
        if type(self.attempt_count) is not int or self.attempt_count < 0:
            raise ValueError("attempt_count must be a non-negative integer")
        _optional_text(self.last_error_code, "last_error_code")
        _optional_text(self.last_claim_id, "last_claim_id")
        _optional_text(self.receipt_id, "receipt_id")

        created_at = _utc(self.created_at, "created_at")
        object.__setattr__(self, "created_at", created_at)
        if self.status_updated_at is not None:
            status_updated_at = _utc(self.status_updated_at, "status_updated_at")
            if status_updated_at < created_at:
                raise ValueError("status_updated_at cannot precede created_at")
            object.__setattr__(self, "status_updated_at", status_updated_at)

    @property
    def receipt_state(self) -> LinearDeliveryReceiptState:
        if self.receipt_id is None:
            return LinearDeliveryReceiptState.MISSING
        return LinearDeliveryReceiptState.PRESENT


class DeliveryObservationSource(Protocol):
    """Narrow read-only input boundary for delivery health evaluation."""

    def list_delivery_observations(
        self,
        *,
        project_id: str,
    ) -> tuple[LinearDeliveryObservation, ...]: ...


@dataclass(frozen=True, slots=True)
class LinearDeliveryManagerException:
    project_id: str
    topic: LinearDeliveryTopic
    outbox_id: str
    state: LinearDeliveryState
    reasons: tuple[LinearDeliveryHealthReason, ...]
    observed_at: datetime
    created_at: datetime
    status_updated_at: datetime | None
    age: timedelta
    attempt_count: int
    last_error_code: str | None
    last_claim_id: str | None
    receipt_state: LinearDeliveryReceiptState
    receipt_id: str | None
    dedupe_key: str
    evidence_digest: str
    kind: Literal["linear_delivery_health"] = field(
        init=False,
        default=_EXCEPTION_KIND,
    )
    route: Literal["manager_exception"] = field(
        init=False,
        default=_MANAGER_ROUTE,
    )

    @property
    def age_seconds(self) -> float:
        return self.age.total_seconds()

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "route": self.route,
            "project_id": self.project_id,
            "topic": self.topic,
            "outbox_id": self.outbox_id,
            "state": self.state,
            "reasons": [reason.value for reason in self.reasons],
            "observed_at": _timestamp(self.observed_at),
            "created_at": _timestamp(self.created_at),
            "status_updated_at": (
                _timestamp(self.status_updated_at)
                if self.status_updated_at is not None
                else None
            ),
            "age_seconds": self.age_seconds,
            "attempt_count": self.attempt_count,
            "last_error_code": self.last_error_code,
            "last_claim_id": self.last_claim_id,
            "receipt_state": self.receipt_state.value,
            "receipt_id": self.receipt_id,
            "dedupe_key": self.dedupe_key,
            "evidence_digest": self.evidence_digest,
        }


class LinearDeliveryHealthMonitor:
    """Evaluate durable delivery observations without mutating canonical state."""

    def __init__(
        self,
        *,
        source: DeliveryObservationSource,
        policy: LinearDeliveryHealthPolicy,
        clock: Callable[[], datetime],
    ) -> None:
        self._source = source
        self._policy = policy
        self._clock = clock

    def evaluate(self, *, project_id: str) -> tuple[LinearDeliveryManagerException, ...]:
        _require_text(project_id, "project_id")
        observations = tuple(
            self._source.list_delivery_observations(project_id=project_id)
        )
        observed_at = _utc(self._clock(), "delivery health clock")

        identities: set[tuple[str, str]] = set()
        for index, observation in enumerate(observations):
            if not isinstance(observation, LinearDeliveryObservation):
                raise TypeError(
                    "delivery observation source returned an invalid item "
                    f"at index {index}"
                )
            if observation.project_id != project_id:
                raise ValueError(
                    "delivery observation source returned an item outside the "
                    "requested project"
                )
            identity = (observation.topic, observation.outbox_id)
            if identity in identities:
                raise ValueError(
                    "delivery observation source returned a duplicate topic/outbox identity"
                )
            identities.add(identity)
            if observation.created_at > observed_at:
                raise ValueError("delivery observation created_at is later than evaluation time")

        ordered = sorted(observations, key=lambda item: (item.topic, item.outbox_id))

        exceptions = [
            item
            for observation in ordered
            if (item := self._evaluate_observation(observation, observed_at)) is not None
        ]
        return tuple(exceptions)

    def _evaluate_observation(
        self,
        observation: LinearDeliveryObservation,
        observed_at: datetime,
    ) -> LinearDeliveryManagerException | None:
        if observation.state == "delivered":
            return None

        age = observed_at - observation.created_at
        reasons: list[LinearDeliveryHealthReason] = []
        if (
            observation.state == "pending"
            and age >= self._policy.max_pending_age
        ):
            reasons.append(LinearDeliveryHealthReason.PENDING_AGE_EXCEEDED)
        if observation.attempt_count >= self._policy.max_attempts:
            reasons.append(LinearDeliveryHealthReason.ATTEMPT_LIMIT_REACHED)
        if (
            observation.state == "dead_letter"
            and self._policy.dead_letter_immediate
        ):
            reasons.append(LinearDeliveryHealthReason.DEAD_LETTER)
        if not reasons:
            return None

        reason_tuple = tuple(reasons)
        dedupe_key = canonical_digest(
            {
                "kind": _EXCEPTION_KIND,
                "project_id": observation.project_id,
                "topic": observation.topic,
                "outbox_id": observation.outbox_id,
            }
        )
        evidence_digest = canonical_digest(
            {
                "kind": _EXCEPTION_KIND,
                "project_id": observation.project_id,
                "topic": observation.topic,
                "outbox_id": observation.outbox_id,
                "state": observation.state,
                "created_at": _timestamp(observation.created_at),
                "status_updated_at": (
                    _timestamp(observation.status_updated_at)
                    if observation.status_updated_at is not None
                    else None
                ),
                "attempt_count": observation.attempt_count,
                "last_error_code": observation.last_error_code,
                "last_claim_id": observation.last_claim_id,
                "receipt_state": observation.receipt_state.value,
                "receipt_id": observation.receipt_id,
                "reasons": [reason.value for reason in reason_tuple],
                "policy": {
                    "max_pending_age_microseconds": _duration_microseconds(
                        self._policy.max_pending_age
                    ),
                    "max_attempts": self._policy.max_attempts,
                    "dead_letter_immediate": self._policy.dead_letter_immediate,
                },
            }
        )
        return LinearDeliveryManagerException(
            project_id=observation.project_id,
            topic=observation.topic,
            outbox_id=observation.outbox_id,
            state=observation.state,
            reasons=reason_tuple,
            observed_at=observed_at,
            created_at=observation.created_at,
            status_updated_at=observation.status_updated_at,
            age=age,
            attempt_count=observation.attempt_count,
            last_error_code=observation.last_error_code,
            last_claim_id=observation.last_claim_id,
            receipt_state=observation.receipt_state,
            receipt_id=observation.receipt_id,
            dedupe_key=dedupe_key,
            evidence_digest=evidence_digest,
        )
