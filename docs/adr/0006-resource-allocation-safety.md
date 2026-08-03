# ADR 0006: Cooperative GPU Allocation Safety

Status: Accepted
Date: 2026-08-02

## Context

A database transaction can prevent two RCP requests from receiving the same GPU
record, but it cannot stop an external process or a partitioned old runner.
Expiring a lease solely because heartbeats stop can create a real split brain.

## Decision

The controller is a cooperative allocator for RCP-managed jobs. It does not
replace an institutional scheduler.

An allocation requires transactional uniqueness, idempotency response storage,
a monotonically increasing fencing generation, a host-local lifetime lock,
startup acknowledgement, monotonic heartbeat sequence, fresh inventory, and an
immediate pre-launch physical check.

For each physical `gpu_uuid`, the database permits at most one non-terminal
assignment globally. Generation is excluded from that uniqueness key: it fences
stale launch, heartbeat, and release calls but cannot legalize a second live
assignment.

Automatic placement uses `offer -> target preflight -> transactional claim`.
The offer is non-binding advice. Claim is the linearization point and occurs only
after target protocol, environment, input, executable, and disk preflight. A
failed preflight discards the offer without assigning the GPU.

Heartbeat loss moves the allocation through suspect to quarantined. The GPU is
not available until a fresh observation proves the old process is gone and the
host lock is free. Force release without that observation is an explicit
dangerous manager action and is audited.

Before the controller phase, manual and static allocators require explicit host
and GPU selection and disable host auto-selection.

## Consequences

The safety claim is limited and testable: RCP will not intentionally double-book
a managed GPU, including during a network partition. It makes no claim about
unmanaged processes and must surface externally_busy state.
