# System stability review

> Renderer: `researchctl-renderer:project-status-summary.v2`
<!-- researchctl-generated:project-status-summary.v2;source=sha256:d25eb10f0537e24b5012243a5e880815927ab14389f45f8c8191950fba6ebad5;body=sha256:035f78f396a2482554784c65ccd470e20a824397db110aef7d40802c8b9a4eb8 -->

- Document: `document_20260812T230402Z_d47c9274452ad42422fcb092`
- Classification: `status/project:snapshot`
- Status: `proposed`
- Revision: `2`
- Basis commit: `7bd5309acfc136176c8cab7e57c83eacfa652535`
- Author: `codex-system-review` (`external_agent`)
- Updated: `2026-08-13T03:32:30+00:00`
- As of: `2026-08-13T03:32:30+00:00`

## Summary

The local core is a release candidate, not a production-stable control plane. Historical and post-export requirements are traceable, 910 tests pass, the Python lint baseline is clean, and measured local paths are responsive. Live governance and external transports remain the dominant stability gaps.

## Capabilities

| Capability | Status | Current behavior | Evidence | Missing |
| --- | --- | --- | --- | --- |
| Requirement and workflow coverage | `verified_local` | All 77 historical prompt anchors map to 33 stable scenarios, every scenario has one of 16 workflow rows and an honest status, and later requirements are tracked separately rather than hidden in the historical count. | `historical`, `scenarios`, `requirements`, `workflows`, `traceability-tests` | - |
| Local governed workflow | `partial` | Typed records, local Sessions and Runs, Submission and Impact proposals, deterministic validation, document contracts, and recovery behavior have executable local coverage. | `source-tree` | Protected-repository, GitHub App author, Plan reviewer, Linear, SSH, and live provider pilots remain. |
| Source quality gate | `verified_local` | The review removed the 183-finding Ruff baseline, fixed runtime annotation introspection and a host-dependent tmux integration fixture, and adds full-repository Ruff plus pytest to the exact-head source workflow. | `ci-workflow` | - |
| Local performance envelope | `partial` | Cold CLI and document commands take about 0.55 to 0.68 seconds on cm04. A 50-item, 200-sample warm diagnostic measured inbox reads at p95 0.777 ms and rendering at p95 1.975 ms with zero failures. | `benchmark-contract` | The required 30-minute end-to-end production window and external queue measurements have not run. |
| Python language fitness | `verified_local` | No measured local path justifies a rewrite. Python matches Pydantic, Typer, SQLite, subprocess, and MkDocs boundaries; network, GitHub queue, Git, and remote process latency dominate the unfinished workflows. | `benchmark-contract`, `source-tree` | - |
| Validated document library | `verified_local` | The policy remains taxonomy authority while a digest-bound manifest feeds an optional strict MkDocs build without a second navigation truth. | `document-site` | - |
| Protected manager acceptance | `deployment_pending` | The repository contains exact-head workflows, CODEOWNERS, governance models, and audit/apply adapters, but GitHub currently reports no active ruleset or classic main protection. | `governance` | Install and audit required checks, latest CODEOWNER approval, strict base currency, and a distinct Agent App principal. |

## Active Work

- **Install and validate the distinct GitHub App proposal identity** (`active`, owner `person:yukiumi13`): Create the least-privilege App, install it only on ResearchAgentOps, and record its non-secret identities.
- **Migrate project documentation through accepted researchctl routes** (`ready_for_review`, owner `person:yukiumi13`): Review the minimal legacy-policy exception removal and the routed requirement, design, and runbook documents.

## Risks

- **`high`:** Green workflows are advisory while main has no active GitHub merge rule. Mitigation: Apply the reviewed governance policy and verify it with researchctl github doctor.
- **`medium`:** RuntimeStore, ApplicationService, domain models, CI dispatch, and document linting are large change surfaces; lint\_document\_tree alone is 469 lines. Mitigation: Split only observed domain and transaction hotspots behind existing facades, with characterization tests and no second database or state machine.
- **`medium`:** Fake ports and local adapters do not prove real GitHub, Linear, SSH, or provider behavior. Mitigation: Run the R1 protected-repository shadow pilot before expanding the product surface.

## Next Steps

- Review the document-management dogfood proposal after exact-head checks pass.
- Apply and audit protected main governance with a distinct Agent App author.
- Run the live Plan reviewer and Linear shadow pilots from one exact release commit.
- Split the document tree validator and notification transactions only after hotspot-specific characterization tests exist.
- Retain Python until a recorded workload violates the supported latency or scale envelope.

## Evidence

- `historical` (`repository_path`): `docs/HISTORICAL_PROMPT_MANIFEST.json`
- `scenarios` (`repository_path`): `docs/USER_SCENARIOS.md`
- `requirements` (`repository_path`): `docs/reference/requirement-ledger.md`
- `workflows` (`repository_path`): `docs/WORKFLOW_COVERAGE.md`
- `traceability-tests` (`repository_path`): `tests/unit/test_traceability.py`
- `source-tree` (`git_commit`): `7bd5309acfc136176c8cab7e57c83eacfa652535`
- `benchmark-contract` (`repository_path`): `tests/unit/test_benchmarks.py`
- `ci-workflow` (`repository_path`): `.github/workflows/research-source-tests.yml`
- `governance` (`repository_path`): `docs/adr/0015-github-proposal-identity-and-protected-acceptance.md`
- `document-site` (`repository_path`): `tests/unit/test_document_site.py`
