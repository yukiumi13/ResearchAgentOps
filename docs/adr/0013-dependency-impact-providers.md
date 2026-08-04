# ADR 0013: Dependency Engines Produce Evidence; RCP Owns Impact Decisions

Status: Accepted
Date: 2026-08-03

## Context

Report impact resembles affected-target and lineage analysis, but its terminal
question is different. Build and data tools can determine that a declared input,
target, stage, dataset, or asset changed. They cannot decide whether a research
claim should be waived, rerun, kept stale, invalidated, or superseded, and they
do not own the accepted Report revision or human review.

Several mature tools cover useful subsets:

- DVC declares stage dependencies and outputs and can identify stale pipeline
  stages or reproduce them. It is useful when a repository already has stable
  DVC pipelines or needs cross-host data/artifact caching.
- Bazel, Pants, Nx, and similar build graph tools can calculate affected code
  targets and tests. They are useful only when the repository already maintains
  that graph; RCP must not impose a new build system during initialization.
- dbt state selection can identify modified SQL models and their graph
  neighborhood in an existing dbt project.
- Dagster asset lineage, freshness policies, and asset checks can emit asset
  change observations when Dagster is already the orchestrator. Requiring its
  service and metadata database for the RCP core would violate the complexity
  budget.
- OpenLineage or a lineage catalog can provide observed job/dataset lineage and
  immutable dataset identities. Observation is not proof that an undeclared
  resource stayed unchanged.
- CODEOWNERS, SLSA, and in-toto cover ownership or provenance, not semantic
  dependency impact.

## Decision

RCP keeps a small provider-neutral governance core:

```text
change evidence provider
  -> typed paths/resources/environments plus source receipt
  -> dependency matcher
  -> ReportImpact proposal
  -> protected-base regeneration
  -> human decision and protected merge
```

The built-in provider remains immutable Git tree comparison because every RCP
repository already has Git. It supports exact paths and trailing `/**` without
requiring another manifest language or service.

The current implementation separates `DeclaredDependencyImpactEvaluator` from
the Report proposal builder. Git supplies immutable changed-path evidence. A
typed `DependencyChangeReceipt` can additionally carry exact resource and
environment observations. The builder rejects an evaluator that changes the
evidence set, returns an undeclared dependency, omits receipt uncertainty, or
changes its identity mid-call.

Every receipt binds provider ID/version, the digest of its structured provider
query, Report basis tree, exact target commit/tree, sorted observations,
observation evidence digests, observation time, and its own canonical digest.
Known observations require basis and target identities; `changed` and
`unchanged` must agree with those identities. `unknown` requires a reason and
never counts as unchanged.

Every generated `ReportImpact` records `change_provider_id`,
`dependency_evaluator_id`, the complete receipts it consumed, matched external
dependencies, and any unresolved dependencies. All are covered by the Impact
digest, rendered review artifact, and machine-readable result. Batch source
digests also bind every child Report source digest.

A path overlap may conservatively propose `stale` while external evidence is
unresolved. A no-overlap proposal may advance the validation basis only when
every declared resource and environment has a known receipt observation. The
default Git batch records uncovered Reports as `unresolved_report_ids` and does
not create a validity revision for them.

Future DVC/build-graph/dbt/Dagster/OpenLineage integrations are optional live
evidence adapters for this receipt contract. An adapter may add candidate
matches but cannot write Report applicability, create a waiver, start a Run,
accept a PR, or treat missing lineage as a no-change result.

RCP will not parse arbitrary third-party CLI prose. An adapter must consume a
versioned structured API/output or reject the observation. Repository policy
must explicitly enable the provider; automatic tool detection cannot expand the
dependency boundary.

## Consequences

Repositories with no dependency framework retain a small Git-native path. A
repository that already uses DVC, Bazel, dbt, Dagster, or OpenLineage can reuse
its graph without transferring governance authority to that tool. The typed
receipt and uncertainty semantics are implemented. Third-party provider
adapters, trusted receipt acquisition, and protected-base provider replay remain
unimplemented.

This boundary also makes performance work straightforward: cache Git diffs by
validation basis, batch structured provider queries, and index immutable event
identities before considering a language rewrite or mandatory orchestration
service.

## References

- DVC pipelines: https://dvc.org/doc/user-guide/pipelines/defining-pipelines
- Bazel query guide: https://bazel.build/query/guide
- dbt state selection: https://docs.getdbt.com/reference/node-selection/methods#the-state-method
- Dagster assets: https://docs.dagster.io/guides/build/assets/
- OpenLineage object model: https://openlineage.io/docs/spec/object-model/
