from __future__ import annotations

from researchctl.errors import RCPError
from researchctl.runtime.store import RuntimeStore
from researchctl.services.linear_delivery_monitor import LinearDeliveryObservation


class RuntimeLinearDeliveryObservationSource:
    """Adapt the durable SQLite outboxes to the read-only health monitor."""

    def __init__(self, runtime: RuntimeStore, *, snapshot_limit: int = 1000) -> None:
        if (
            not isinstance(snapshot_limit, int)
            or isinstance(snapshot_limit, bool)
            or not 1 <= snapshot_limit <= 1000
        ):
            raise ValueError("snapshot_limit must be between 1 and 1000")
        self._runtime = runtime
        self._snapshot_limit = snapshot_limit

    def list_delivery_observations(
        self,
        *,
        project_id: str,
    ) -> tuple[LinearDeliveryObservation, ...]:
        records = self._runtime.list_linear_deliveries(
            project_id,
            limit=self._snapshot_limit,
        )
        if len(records) == self._snapshot_limit:
            raise RCPError(
                code="linear_delivery_health_snapshot_incomplete",
                message="The bounded Linear delivery health snapshot may be incomplete.",
                remediation=(
                    "Narrow or archive delivery history before evaluating health; "
                    "pagination is required above the current snapshot limit."
                ),
                context={"limit": self._snapshot_limit},
            )
        return tuple(
            LinearDeliveryObservation(
                project_id=record.project_id,
                topic=record.topic,
                outbox_id=record.outbox_id,
                state=record.state,
                created_at=record.created_at,
                status_updated_at=record.status_updated_at,
                attempt_count=record.attempt_count,
                last_error_code=record.last_error_code,
                last_claim_id=record.last_claim_id,
                receipt_id=(
                    record.receipt.receipt_id if record.receipt is not None else None
                ),
            )
            for record in records
        )
