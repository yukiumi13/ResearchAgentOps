# ADR 0008: Canonical Data and Schema Upgrades

Status: Accepted
Date: 2026-08-02

## Context

Byte-for-byte generated state and content hashes are unreliable without a
canonical representation. Generic YAML loaders accept duplicate keys, aliases,
implicit values, and non-finite numbers that can change meaning across tools.

## Decision

Pydantic v2 models with extra fields forbidden define protocol records. JSON
Schema is generated from those models.

Git records use deterministic YAML for review. Digests use canonical JSON:
UTF-8, sorted keys, compact separators, JSON-mode values, and no non-finite
numbers. Timestamps are RFC 3339 UTC. Paths are repository-relative POSIX paths.

The YAML loader is safe and rejects duplicate keys. Unknown major schema
versions fail closed. A minor migration is previewed by researchctl upgrade
--check and applied only through an explicit manager change. Accepted records
are never silently migrated.

Managed projects pin protocol, CLI, schema, and trusted CI action versions.

## Consequences

Hashing and regeneration are stable across hosts. Upgrades become reviewable
protocol changes. Readable YAML remains a view over strict typed data rather
than an extensible bag of fields.
