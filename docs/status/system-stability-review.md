# System stability review

> Renderer: `researchctl-renderer:project-status-summary.v2`
<!-- researchctl-generated:project-status-summary.v2;source=sha256:cdceb06781aa5b9f5fc095c9285e5eb18815c35b518adf3e4784f09bc75905aa;body=sha256:5a9ad6ec4e0f4a4d78f11c01fc7526c03805f06b04e51b115ee77f529a48f5cf -->

- Document: `document_20260812T230402Z_d47c9274452ad42422fcb092`
- Classification: `status/project:snapshot`
- Status: `proposed`
- Revision: `3`
- Basis commit: `ef51eb85ca6e85db9f050a89c3e2301892eff6b5`
- Author: `codex-system-review` (`external_agent`)
- Updated: `2026-08-13T05:53:56+00:00`
- As of: `2026-08-13T05:53:56+00:00`

## Summary

The local core is a release candidate, not a production-stable control plane. Historical and post-export requirements are traceable, 931 tests pass, the Python lint baseline is clean, and measured local paths are responsive. Live protected deployment and external transports remain the dominant stability gaps.

## Capabilities

| Capability | Status | Current behavior | Evidence | Missing |
| --- | --- | --- | --- | --- |
| Requirement and workflow coverage | `verified_local` | All 77 historical prompt anchors map to 33 stable scenarios, every scenario has one of 16 workflow rows and an honest status, and later requirements are tracked separately rather than hidden in the historical count. | `historical`, `scenarios`, `requirements`, `workflows`, `traceability-tests` | - |
| Local governed workflow | `partial` | Typed records, local Sessions and Runs, Submission and Impact proposals, deterministic validation, document contracts, and recovery behavior have executable local coverage. | `source-tree` | Protected-repository, GitHub App author, Plan reviewer, Linear, SSH, and live provider pilots remain. |
| Source quality gate | `verified_local` | The review removed the 183-finding Ruff baseline, fixed runtime annotation introspection and a host-dependent tmux integration fixture, and adds full-repository Ruff plus pytest to the exact-head source workflow. | `ci-workflow` | - |
| Local performance envelope | `partial` | Cold CLI and document commands take about 0.55 to 0.68 seconds on cm04. A 50-item, 200-sample warm diagnostic measured inbox reads at p95 0.777 ms and rendering at p95 1.975 ms with zero failures. | `benchmark-contract` | The required 30-minute end-to-end production window and external queue measurements have not run. |
| Python language fitness | `verified_local` | No measured local path justifies a rewrite. Python matches Pydantic, Typer, SQLite, subprocess, and MkDocs boundaries; network, GitHub queue, Git, and remote process latency dominate the unfinished workflows. | `benchmark-contract`, `source-tree` | - |
| Validated document library | `verified_local` | The policy remains taxonomy authority while a digest-bound manifest feeds an optional strict MkDocs build without a second navigation truth. | `document-site` | - |
| Protected manager acceptance | `deployment_pending` | The repository contains exact-head workflows, CODEOWNERS, governance models, and audit/apply adapters, but GitHub currently reports no active ruleset or classic main protection. | `governance` | Audit required checks, latest CODEOWNER approval, strict base currency, and protected-main rules. |
| Distinct GitHub proposal identity | `partial` | GitHub App 4577593 and installation 153350892 were observed on the selected ResearchAgentOps repository with exactly metadata read, contents write, and pull requests write. The local broker implementation has adversarial credential and identity tests. | `github-app-installation`, `proposal-broker-tests` | Manager acceptance, an Agent-inaccessible broker service principal, and a live rcp-agent\[bot\] canary remain. |

## Active Work

- **Accept and deploy the distinct GitHub App proposal broker** (`active`, owner `person:yukiumi13`): Review the broker PR, deploy it under an Agent-inaccessible principal, then run the first bot-authored canary.

## Risks

- **`high`:** Green workflows are advisory while main has no active GitHub merge rule. Mitigation: Apply the reviewed governance policy and verify it with researchctl github doctor.
- **`medium`:** RuntimeStore, ApplicationService, domain models, CI dispatch, and document linting are large change surfaces; lint\_document\_tree alone is 469 lines. Mitigation: Split only observed domain and transaction hotspots behind existing facades, with characterization tests and no second database or state machine.
- **`medium`:** Fake ports and local adapters do not prove real GitHub, Linear, SSH, or provider behavior. Mitigation: Run the R1 protected-repository shadow pilot before expanding the product surface.
- **`high`:** A mode-0400 private key remains readable to an Agent process that shares the broker Unix UID. Mitigation: Move the key and one-shot host to a distinct principal before any live proposal canary.

## Next Steps

- Review and merge the App proposal broker without treating the implementing Agent as acceptance.
- Deploy the broker under a distinct principal and run one rcp-agent\[bot\] canary before applying protection.
- Apply and audit protected main governance after the App-authored canary.
- Run the live Plan reviewer and Linear shadow pilots from one exact release commit.
- Split the document tree validator and notification transactions only after hotspot-specific characterization tests exist.
- Retain Python until a recorded workload violates the supported latency or scale envelope.

## Evidence

- `historical` (`repository_path`): `docs/HISTORICAL_PROMPT_MANIFEST.json`
- `scenarios` (`repository_path`): `docs/USER_SCENARIOS.md`
- `requirements` (`repository_path`): `docs/reference/requirement-ledger.md`
- `workflows` (`repository_path`): `docs/WORKFLOW_COVERAGE.md`
- `traceability-tests` (`repository_path`): `tests/unit/test_traceability.py`
- `source-tree` (`git_commit`): `ef51eb85ca6e85db9f050a89c3e2301892eff6b5`
- `benchmark-contract` (`repository_path`): `tests/unit/test_benchmarks.py`
- `ci-workflow` (`repository_path`): `.github/workflows/research-source-tests.yml`
- `governance` (`repository_path`): `docs/adr/0015-github-proposal-identity-and-protected-acceptance.md`
- `document-site` (`repository_path`): `tests/unit/test_document_site.py`
- `github-app-installation` (`external`): `github-app:4577593/installation:153350892/repository:yukiumi13/ResearchAgentOps`
- `proposal-broker-tests` (`repository_path`): `tests/unit/test_github_app_broker.py`
