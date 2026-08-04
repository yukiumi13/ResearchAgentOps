from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from researchctl.domain.models import DependencyChangeReceipt, DependencySet


GIT_TREE_CHANGE_PROVIDER_ID = "researchctl.git-tree-diff.v1"
PATH_DEPENDENCY_EVALUATOR_ID = "researchctl.path-dependency.v1"
DECLARED_DEPENDENCY_EVALUATOR_ID = "researchctl.declared-dependency.v2"


def path_dependency_matches(dependency: str, changed_path: str) -> bool:
    """Match an exact file or the only supported recursive prefix form."""

    if dependency.endswith("/**"):
        prefix = dependency[:-3]
        return changed_path == prefix or changed_path.startswith(f"{prefix}/")
    return changed_path == dependency


@dataclass(frozen=True, slots=True)
class DependencyImpactEvaluation:
    evaluator_id: str
    changed_paths: tuple[str, ...]
    matched_path_dependencies: tuple[str, ...]
    matched_resource_dependencies: tuple[str, ...]
    matched_environment_dependencies: tuple[str, ...]
    unresolved_resource_dependencies: tuple[str, ...]
    unresolved_environment_dependencies: tuple[str, ...]
    receipt_digests: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.evaluator_id or not self.evaluator_id.isascii():
            raise ValueError("dependency evaluator ID must be non-empty ASCII")
        for label, values in (
            ("changed_paths", self.changed_paths),
            ("matched_path_dependencies", self.matched_path_dependencies),
            ("matched_resource_dependencies", self.matched_resource_dependencies),
            (
                "matched_environment_dependencies",
                self.matched_environment_dependencies,
            ),
            (
                "unresolved_resource_dependencies",
                self.unresolved_resource_dependencies,
            ),
            (
                "unresolved_environment_dependencies",
                self.unresolved_environment_dependencies,
            ),
            ("receipt_digests", self.receipt_digests),
        ):
            if values != tuple(sorted(values)) or len(values) != len(set(values)):
                raise ValueError(f"dependency evaluation {label} must be unique and sorted")


class DependencyImpactEvaluator(Protocol):
    evaluator_id: str

    def evaluate(
        self,
        *,
        dependencies: DependencySet,
        changed_paths: tuple[str, ...],
        dependency_receipts: tuple[DependencyChangeReceipt, ...],
    ) -> DependencyImpactEvaluation: ...


class PathDependencyImpactEvaluator:
    """Evaluate the Git-native exact/trailing-recursive dependency subset."""

    evaluator_id = PATH_DEPENDENCY_EVALUATOR_ID

    def evaluate(
        self,
        *,
        dependencies: DependencySet,
        changed_paths: tuple[str, ...],
        dependency_receipts: tuple[DependencyChangeReceipt, ...] = (),
    ) -> DependencyImpactEvaluation:
        if dependency_receipts:
            raise ValueError("path-only evaluator does not accept provider receipts")
        canonical_paths = tuple(sorted(set(changed_paths)))
        matched = tuple(
            dependency
            for dependency in dependencies.paths
            if any(
                path_dependency_matches(dependency, path)
                for path in canonical_paths
            )
        )
        return DependencyImpactEvaluation(
            evaluator_id=self.evaluator_id,
            changed_paths=canonical_paths,
            matched_path_dependencies=matched,
            matched_resource_dependencies=(),
            matched_environment_dependencies=(),
            unresolved_resource_dependencies=dependencies.resources,
            unresolved_environment_dependencies=dependencies.environments,
            receipt_digests=(),
        )


class DeclaredDependencyImpactEvaluator:
    """Evaluate paths plus exact, typed resource/environment observations."""

    evaluator_id = DECLARED_DEPENDENCY_EVALUATOR_ID

    def evaluate(
        self,
        *,
        dependencies: DependencySet,
        changed_paths: tuple[str, ...],
        dependency_receipts: tuple[DependencyChangeReceipt, ...] = (),
    ) -> DependencyImpactEvaluation:
        path_evaluation = PathDependencyImpactEvaluator().evaluate(
            dependencies=DependencySet(paths=dependencies.paths),
            changed_paths=changed_paths,
        )
        declared = {
            ("resource", value) for value in dependencies.resources
        } | {
            ("environment", value) for value in dependencies.environments
        }
        observations: dict[tuple[str, str], str] = {}
        for receipt in dependency_receipts:
            for observation in receipt.observations:
                key = (observation.kind, observation.dependency)
                if key not in declared:
                    raise ValueError(
                        "dependency receipt contains an undeclared dependency"
                    )
                if key in observations:
                    raise ValueError(
                        "dependency receipts contain duplicate observations"
                    )
                observations[key] = observation.state

        def values(kind: str, state: str) -> tuple[str, ...]:
            return tuple(
                dependency
                for observed_kind, dependency in sorted(observations)
                if observed_kind == kind and observations[
                    (observed_kind, dependency)
                ] == state
            )

        matched_resources = values("resource", "changed")
        matched_environments = values("environment", "changed")
        unresolved_resources = tuple(
            dependency
            for dependency in dependencies.resources
            if observations.get(("resource", dependency)) in {None, "unknown"}
        )
        unresolved_environments = tuple(
            dependency
            for dependency in dependencies.environments
            if observations.get(("environment", dependency)) in {None, "unknown"}
        )
        return DependencyImpactEvaluation(
            evaluator_id=self.evaluator_id,
            changed_paths=path_evaluation.changed_paths,
            matched_path_dependencies=(
                path_evaluation.matched_path_dependencies
            ),
            matched_resource_dependencies=matched_resources,
            matched_environment_dependencies=matched_environments,
            unresolved_resource_dependencies=unresolved_resources,
            unresolved_environment_dependencies=unresolved_environments,
            receipt_digests=tuple(
                sorted(receipt.receipt_digest for receipt in dependency_receipts)
            ),
        )
