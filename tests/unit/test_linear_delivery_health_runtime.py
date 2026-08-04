from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from researchctl.errors import RCPError
from researchctl.services.linear_delivery_health_runtime import (
    RuntimeLinearDeliveryObservationSource,
)


NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


class FakeRuntime:
    def __init__(self, records: tuple[SimpleNamespace, ...]) -> None:
        self.records = records
        self.calls: list[tuple[str, int]] = []

    def list_linear_deliveries(self, project_id: str, *, limit: int):
        self.calls.append((project_id, limit))
        return self.records[:limit]


def record(*, receipt_id: str | None = "receipt-1") -> SimpleNamespace:
    return SimpleNamespace(
        project_id="project_20260803T120000Z_aaaaaaaaaaaaaaaaaaaaaaaa",
        topic="linear.accepted-result.v1",
        outbox_id="outbox-1",
        state="pending",
        created_at=NOW,
        status_updated_at=None,
        attempt_count=2,
        last_error_code="linear_unavailable",
        last_claim_id="claim-1",
        receipt=(
            SimpleNamespace(receipt_id=receipt_id) if receipt_id is not None else None
        ),
    )


def test_runtime_source_maps_delivery_without_payload_or_comment_body() -> None:
    runtime = FakeRuntime((record(), record(receipt_id=None)))
    runtime.records[1].outbox_id = "outbox-2"
    source = RuntimeLinearDeliveryObservationSource(runtime, snapshot_limit=10)

    observations = source.list_delivery_observations(
        project_id="project_20260803T120000Z_aaaaaaaaaaaaaaaaaaaaaaaa"
    )

    assert runtime.calls == [
        ("project_20260803T120000Z_aaaaaaaaaaaaaaaaaaaaaaaa", 10)
    ]
    assert [item.outbox_id for item in observations] == ["outbox-1", "outbox-2"]
    assert [item.receipt_id for item in observations] == ["receipt-1", None]
    assert not hasattr(observations[0], "payload")


def test_runtime_source_fails_closed_when_snapshot_reaches_bound() -> None:
    runtime = FakeRuntime((record(),))
    source = RuntimeLinearDeliveryObservationSource(runtime, snapshot_limit=1)

    with pytest.raises(RCPError) as caught:
        source.list_delivery_observations(
            project_id="project_20260803T120000Z_aaaaaaaaaaaaaaaaaaaaaaaa"
        )

    assert caught.value.code == "linear_delivery_health_snapshot_incomplete"
    assert caught.value.context == {"limit": 1}


@pytest.mark.parametrize("limit", [0, 1001, True])
def test_runtime_source_rejects_invalid_snapshot_limit(limit: object) -> None:
    with pytest.raises(ValueError):
        RuntimeLinearDeliveryObservationSource(
            FakeRuntime(()),
            snapshot_limit=limit,  # type: ignore[arg-type]
        )
