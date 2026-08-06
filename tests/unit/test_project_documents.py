from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from researchctl.cli import app
from researchctl.domain.models import (
    DesignDocument,
    DocumentLayoutPolicy,
    ProjectStatusSummary,
)
from researchctl.serialization import dump_yaml, load_yaml
from researchctl.services.project_documents import (
    DESIGN_DOCUMENT_RENDERER_ID,
    DOCUMENT_INDEX_RENDERER_ID,
    PROJECT_AGENT_GUIDE_RENDERER_IDS,
    PROJECT_STATUS_RENDERER_ID,
    lint_document_tree,
    load_markdown_frontmatter,
    render_design_document,
    render_document_index,
    render_project_agent_guide,
    render_project_status_summary,
    render_standalone_document_policy_template,
)


DOCUMENT_ID = "document_20260804T120000Z_" + "1" * 24
BASIS_COMMIT = "2" * 40
SESSION_ID = "session_20260804T110000Z_" + "3" * 24


def _design(**overrides: object) -> DesignDocument:
    payload: dict[str, object] = {
        "document_id": DOCUMENT_ID,
        "document_kind": "design_document",
        "classification": "design/architecture:proposal",
        "slug": "document-governance",
        "title": "Document governance",
        "status": "proposed",
        "basis_commit": BASIS_COMMIT,
        "revision": 1,
        "authored_by": {
            "role": "agent",
            "actor_id": f"agent-{SESSION_ID}",
            "session_id": SESSION_ID,
        },
        "created_at": "2026-08-04T12:00:00Z",
        "updated_at": "2026-08-04T12:00:00Z",
        "sources": [
            {
                "key": "requirements",
                "kind": "repository_path",
                "location": "docs/REQUIREMENT_LEDGER.md",
            }
        ],
        "problem": "Project documents become ambiguous when type and path drift.",
        "context": "Agents and humans author documents through several interfaces.",
        "goals": ["Make document type, authority, and placement deterministic."],
        "non_goals": ["Move machine-consumed artifacts solely for aesthetics."],
        "constraints": ["Standalone lint must work without researchctl init."],
        "options": [
            {
                "key": "routes",
                "summary": "Manager-owned classification routes",
                "benefits": ["Paths are deterministic."],
                "drawbacks": ["New types require review."],
                "disposition": "selected",
                "rationale": "It separates content changes from taxonomy changes.",
            },
            {
                "key": "freeform",
                "summary": "Free-form directories",
                "benefits": ["No setup is required."],
                "drawbacks": ["The hierarchy drifts."],
                "disposition": "rejected",
                "rationale": "It does not provide enforceable organization.",
            },
        ],
        "components": [
            {
                "key": "linter",
                "responsibility": "Validate document source, route, and render pairs.",
                "interfaces": ["researchctl doc tree"],
            }
        ],
        "workflows": [
            {
                "name": "Agent document proposal",
                "steps": [
                    "Agent selects one accepted classification.",
                    "CI validates the derived path and deterministic render.",
                ],
            }
        ],
        "security_considerations": [
            "An Agent cannot add a route in the same document proposal."
        ],
        "failure_modes": [
            {
                "condition": "A document uses an unknown classification.",
                "behavior": "Lint fails closed.",
                "recovery": "A manager reviews a separate route policy proposal.",
            }
        ],
        "migration_steps": ["Declare existing files as finite legacy entries."],
        "validation": [
            {
                "case": "Generated Markdown is edited.",
                "expected": "Tree lint fails.",
                "evidence": "Byte comparison test.",
            }
        ],
    }
    payload.update(overrides)
    return DesignDocument.model_validate(payload)


def _status(**overrides: object) -> ProjectStatusSummary:
    payload: dict[str, object] = {
        "document_id": DOCUMENT_ID,
        "document_kind": "project_status_summary",
        "classification": "status/project:snapshot",
        "slug": "current-state",
        "title": "Current project state",
        "status": "proposed",
        "basis_commit": BASIS_COMMIT,
        "revision": 1,
        "authored_by": {
            "role": "trusted_automation",
            "actor_id": "researchctl-status-renderer",
        },
        "created_at": "2026-08-04T12:00:00Z",
        "updated_at": "2026-08-04T12:00:00Z",
        "as_of": "2026-08-04T12:00:00Z",
        "sources": [
            {
                "key": "tests",
                "kind": "repository_path",
                "location": "tests/unit/test_project_documents.py",
            }
        ],
        "executive_summary": "Document contracts are locally implemented.",
        "capabilities": [
            {
                "key": "document-lint",
                "title": "Document lint",
                "status": "verified_local",
                "summary": "Schema and tree checks run locally.",
                "evidence_keys": ["tests"],
            }
        ],
        "next_steps": ["Run one repository canary."],
    }
    payload.update(overrides)
    return ProjectStatusSummary.model_validate(payload)


def _custom_policy() -> DocumentLayoutPolicy:
    return DocumentLayoutPolicy.model_validate(
        {
            "root": "docs",
            "root_files": ["docs/README.md"],
            "max_depth": 3,
            "routes": [
                {
                    "classification": "research/evidence:ledger",
                    "document_type": "ledger",
                    "directory": "docs/ledger",
                    "contract": "markdown-frontmatter",
                    "rationale": "Existing result ledgers are stored here.",
                },
                {
                    "classification": "research/analysis:brief",
                    "document_type": "brief",
                    "directory": "docs/brief",
                    "contract": "analysis-brief",
                    "rationale": "Existing measurement briefs are stored here.",
                },
                {
                    "classification": "operations/project:runbook",
                    "document_type": "runbook",
                    "directory": "docs/runbook",
                    "contract": "markdown-frontmatter",
                    "rationale": "Existing operational procedures are stored here.",
                },
                {
                    "classification": "reference/project:frozen",
                    "document_type": "reference",
                    "directory": "docs/reference",
                    "contract": "markdown-frontmatter",
                    "rationale": "Existing frozen references are stored here.",
                },
                {
                    "classification": "guide/engineering:document",
                    "document_type": "guide",
                    "directory": "docs/guide",
                    "contract": "markdown-frontmatter",
                    "rationale": "Existing engineering guides are stored here.",
                },
                {
                    "classification": "lineage/experiment:index",
                    "document_type": "lineage",
                    "directory": "docs/lineage",
                    "contract": "markdown-frontmatter",
                    "rationale": "Existing experiment lineage indexes are stored here.",
                },
                {
                    "classification": "archive/project:superseded",
                    "document_type": "archive",
                    "directory": "docs/archive",
                    "contract": "markdown-frontmatter",
                    "rationale": "Existing superseded documents are stored here.",
                },
            ],
        }
    )


def _frontmatter(
    *,
    document_type: str = "ledger",
    derived_from: str = "",
    validity: str = "valid",
) -> str:
    relation = f"    - {derived_from}\n" if derived_from else "    []\n"
    return (
        "---\n"
        f"type: {document_type}\n"
        "title: Evidence ledger\n"
        "owner: person:yl2708\n"
        "last_updated: 2026-08-04\n"
        f"validity: {validity}\n"
        "tags: [evaluation]\n"
        "references: []\n"
        "relations:\n"
        "  supersedes: []\n"
        f"  derived_from:\n{relation}"
        "  see_also: []\n"
        "---\n"
        "# Evidence ledger\n"
    )


def test_design_and_status_renderers_show_versioned_markers() -> None:
    design = render_design_document(_design()).decode("utf-8")
    status = render_project_status_summary(_status()).decode("utf-8")

    assert f"researchctl-renderer:{DESIGN_DOCUMENT_RENDERER_ID}" in design
    assert f"researchctl-renderer:{PROJECT_STATUS_RENDERER_ID}" in status
    assert "<!-- researchctl-generated:" in design + status
    assert "Megatron&\\#x27;s" not in render_design_document(
        _design(title="Megatron's design")
    ).decode("utf-8")
    assert design.index("## Goals") < design.index("## Failure Modes")
    assert "| Document lint | `verified_local` |" in status


def test_agent_guide_renderer_teaches_the_effective_standalone_workflow() -> None:
    guide = render_project_agent_guide(_custom_policy(), "claude").decode("utf-8")

    assert "researchctl-agent-guide:" in guide
    assert f"researchctl-renderer:{PROJECT_AGENT_GUIDE_RENDERER_IDS['claude']}" in guide
    assert "`.researchctl-docs.yaml`" in guide
    assert "`researchctl doc tree --project .`" in guide
    assert "commit is not acceptance" in guide
    assert "does not require `researchctl init`" in guide
    assert "| `ledger` | `research/evidence:ledger` |" in guide


def test_standalone_policy_template_is_explicit_and_schema_valid() -> None:
    rendered = render_standalone_document_policy_template("claude")
    payload = load_yaml(rendered.decode("utf-8"))
    policy = DocumentLayoutPolicy.model_validate(payload)

    assert set(payload) == {
        "agent_guides",
        "classification_depth",
        "generated_index",
        "legacy_files",
        "machine_artifact_roots",
        "max_depth",
        "root",
        "root_files",
        "routes",
    }
    assert policy.generated_index == "docs/INDEX.md"
    assert policy.root_files == ("docs/README.md", "docs/INDEX.md")
    assert policy.classification_depth.minimum == 2
    assert policy.classification_depth.maximum == 4
    assert policy.agent_guides[0].path == "CLAUDE.md"
    assert policy.legacy_files == ()
    assert policy.machine_artifact_roots == ()
    assert all(route.rationale.startswith("TEMPLATE:") for route in policy.routes)


def test_markdown_frontmatter_requires_canonical_session_owner() -> None:
    valid = _frontmatter().replace(
        "owner: person:yl2708",
        f"owner: session:{SESSION_ID}",
    )
    frontmatter, body = load_markdown_frontmatter(valid, path="docs/ledger/a.md")
    assert frontmatter.owner == f"session:{SESSION_ID}"
    assert body == "# Evidence ledger\n"

    invalid = valid.replace(f"session:{SESSION_ID}", "session:human-label")
    with pytest.raises(ValidationError):
        load_markdown_frontmatter(invalid, path="docs/ledger/a.md")


def test_markdown_claim_provenance_is_keyed_and_distinguishes_estimates() -> None:
    valid = _frontmatter().replace(
        "references: []\n",
        (
            "references: []\n"
            "sources:\n"
            "  - key: disk-model\n"
            "    kind: repository_path\n"
            "    location: data/disk-model.csv\n"
            "provenance:\n"
            "  - key: expected-disk\n"
            "    value: 2.4 TB\n"
            "    basis: estimated\n"
            "    source_keys: [disk-model]\n"
            "    method: checkpoints * bytes-per-checkpoint\n"
        ),
    ).replace("# Evidence ledger\n", "# Evidence ledger\n\nExpected disk: 2.4 TB.\n")

    frontmatter, body = load_markdown_frontmatter(valid, path="docs/ledger/a.md")

    assert frontmatter.provenance[0].basis == "estimated"
    assert frontmatter.provenance[0].source_keys == ("disk-model",)
    assert "2.4 TB" in body

    without_method = valid.replace(
        "    method: checkpoints * bytes-per-checkpoint\n",
        "",
    )
    with pytest.raises(ValidationError, match="requires method"):
        load_markdown_frontmatter(without_method, path="docs/ledger/a.md")

    unknown_source = valid.replace("source_keys: [disk-model]", "source_keys: [missing]")
    with pytest.raises(ValidationError) as raised:
        load_markdown_frontmatter(unknown_source, path="docs/ledger/a.md")
    message = str(raised.value)
    assert "unused declared sources: disk-model" in message
    assert "undeclared source keys: missing" in message


def test_document_contract_rejects_incomplete_design_and_unproven_status() -> None:
    design = _design().model_dump(mode="json")
    design["options"][1]["disposition"] = "selected"
    with pytest.raises(ValidationError, match="exactly one selected option"):
        DesignDocument.model_validate(design)

    status = _status().model_dump(mode="json")
    status["capabilities"][0]["evidence_keys"] = ["missing"]
    with pytest.raises(ValidationError, match="use every declared source"):
        ProjectStatusSummary.model_validate(status)


def test_custom_policy_supports_seven_project_types_and_rejects_overlap() -> None:
    policy = _custom_policy()
    assert tuple(route.document_type for route in policy.routes) == (
        "ledger",
        "brief",
        "runbook",
        "reference",
        "guide",
        "lineage",
        "archive",
    )

    payload = policy.model_dump(mode="json")
    payload["routes"][1]["directory"] = "docs/ledger/nested"
    with pytest.raises(ValidationError, match="must not overlap"):
        DocumentLayoutPolicy.model_validate(payload)

    payload = policy.model_dump(mode="json")
    payload["machine_artifact_roots"] = [
        {"directory": "data", "allowed_extensions": [".json", ".md"]}
    ]
    with pytest.raises(ValidationError, match="cannot allow Markdown"):
        DocumentLayoutPolicy.model_validate(payload)

    payload = policy.model_dump(mode="json")
    payload["agent_guides"] = [
        {"path": "docs/CLAUDE.md", "format": "claude"}
    ]
    with pytest.raises(ValidationError, match="outside the document root"):
        DocumentLayoutPolicy.model_validate(payload)

    payload = policy.model_dump(mode="json")
    payload["agent_guides"] = [
        {"path": "CLAUDE.txt", "format": "claude"}
    ]
    with pytest.raises(ValidationError, match="must be Markdown"):
        DocumentLayoutPolicy.model_validate(payload)

    payload = policy.model_dump(mode="json")
    payload["routes"][0]["classification"] = "research:ledger"
    with pytest.raises(ValidationError, match="classification depth"):
        DocumentLayoutPolicy.model_validate(payload)

    payload["classification_depth"] = {"minimum": 1, "maximum": 4}
    customized = DocumentLayoutPolicy.model_validate(payload)
    assert customized.routes[0].classification == "research:ledger"

    payload["classification_depth"] = {"minimum": 4, "maximum": 2}
    with pytest.raises(ValidationError, match="minimum cannot exceed maximum"):
        DocumentLayoutPolicy.model_validate(payload)

    payload = policy.model_dump(mode="json")
    payload["routes"][1]["generated_markdown_frontmatter"] = {
        "required_fields": ["type", "name", "status"]
    }
    customized = DocumentLayoutPolicy.model_validate(payload)
    assert customized.routes[1].generated_markdown_frontmatter is not None
    assert customized.routes[1].generated_markdown_frontmatter.required_fields == (
        "type",
        "name",
        "status",
    )

    payload["routes"][1]["generated_markdown_frontmatter"] = {
        "required_fields": ["type", "type"]
    }
    with pytest.raises(ValidationError, match="frontmatter fields must be unique"):
        DocumentLayoutPolicy.model_validate(payload)

    payload = policy.model_dump(mode="json")
    payload["routes"][0]["generated_markdown_frontmatter"] = {
        "required_fields": ["type"]
    }
    with pytest.raises(ValidationError, match="manual markdown-frontmatter routes"):
        DocumentLayoutPolicy.model_validate(payload)


def test_tree_lint_enforces_frontmatter_type_path_and_references(tmp_path: Path) -> None:
    policy = _custom_policy()
    (tmp_path / "docs/ledger").mkdir(parents=True)
    (tmp_path / "docs/README.md").write_text("# Index\n", encoding="utf-8")
    ledger = tmp_path / "docs/ledger/result.md"
    ledger.write_text(_frontmatter(), encoding="utf-8")

    passed = lint_document_tree(tmp_path, policy)
    assert passed.passed
    assert passed.checked_files == 2

    ledger.write_text(_frontmatter(document_type="guide"), encoding="utf-8")
    invalid = lint_document_tree(tmp_path, policy)
    assert not invalid.passed
    assert any(finding.code == "document_type_path_mismatch" for finding in invalid.findings)


def test_document_relations_are_repository_root_relative_with_legacy_guidance(
    tmp_path: Path,
) -> None:
    policy = _custom_policy()
    (tmp_path / "docs/ledger").mkdir(parents=True)
    (tmp_path / "docs/guide").mkdir()
    (tmp_path / "docs/README.md").write_text("# Index\n", encoding="utf-8")
    target = tmp_path / "docs/guide/target.md"
    target.write_text(_frontmatter(document_type="guide"), encoding="utf-8")
    source = tmp_path / "docs/ledger/source.md"
    source.write_text(
        _frontmatter().replace(
            "  see_also: []",
            "  see_also: [docs/guide/target.md]",
        ),
        encoding="utf-8",
    )

    assert lint_document_tree(tmp_path, policy).passed

    source.write_text(
        source.read_text(encoding="utf-8").replace(
            "docs/guide/target.md",
            "guide/target.md",
        ),
        encoding="utf-8",
    )
    legacy = lint_document_tree(tmp_path, policy)

    finding = next(
        item for item in legacy.findings if item.code == "document_relation_path_legacy"
    )
    assert "replace 'guide/target.md' with 'docs/guide/target.md'" in finding.message


def test_tree_lint_rejects_unknown_paths_and_orphan_generated_markdown(
    tmp_path: Path,
) -> None:
    policy = DocumentLayoutPolicy()
    (tmp_path / "docs/design").mkdir(parents=True)
    (tmp_path / "docs/unknown").mkdir()
    (tmp_path / "docs/design/orphan.md").write_text("orphan\n", encoding="utf-8")
    (tmp_path / "docs/unknown/note.md").write_text("note\n", encoding="utf-8")

    result = lint_document_tree(tmp_path, policy)

    assert not result.passed
    codes = {finding.code for finding in result.findings}
    assert "document_path_unclassified" in codes
    assert "document_render_orphaned" in codes


def test_tree_lint_enforces_generated_index_and_machine_artifact_boundary(
    tmp_path: Path,
) -> None:
    payload = _custom_policy().model_dump(mode="json")
    payload["generated_index"] = "docs/README.md"
    payload["machine_artifact_roots"] = [
        {
            "directory": "data",
            "allowed_extensions": [".csv", ".json", ".jsonl", ".xlsx", ".py"],
        }
    ]
    policy = DocumentLayoutPolicy.model_validate(payload)
    (tmp_path / "docs/ledger").mkdir(parents=True)
    (tmp_path / "data").mkdir()
    (tmp_path / "docs/README.md").write_bytes(render_document_index(policy))
    (tmp_path / "docs/ledger/result.md").write_text(_frontmatter(), encoding="utf-8")
    (tmp_path / "data/results.json").write_text("{}\n", encoding="utf-8")

    passed = lint_document_tree(tmp_path, policy)

    assert passed.passed
    assert passed.checked_files == 3
    assert f"researchctl-renderer:{DOCUMENT_INDEX_RENDERER_ID}" in (
        tmp_path / "docs/README.md"
    ).read_text(encoding="utf-8")

    (tmp_path / "docs/README.md").write_text("# stale\n", encoding="utf-8")
    (tmp_path / "data/narrative.md").write_text("# prose\n", encoding="utf-8")
    invalid = lint_document_tree(tmp_path, policy)

    codes = {finding.code for finding in invalid.findings}
    assert "document_index_mismatch" in codes
    assert "machine_artifact_extension_invalid" in codes


def test_tree_lint_compares_frozen_documents_with_explicit_baseline(
    tmp_path: Path,
) -> None:
    policy = _custom_policy()
    current = tmp_path / "current"
    baseline = tmp_path / "baseline"
    for root in (current, baseline):
        (root / "docs/reference").mkdir(parents=True)
        (root / "docs/README.md").write_text("# Index\n", encoding="utf-8")
        (root / "docs/reference/locked.md").write_text(
            _frontmatter(document_type="reference", validity="frozen"),
            encoding="utf-8",
        )

    passed = lint_document_tree(
        current,
        policy,
        baseline_root=baseline,
        baseline_policy=policy,
    )
    assert passed.passed

    (current / "docs/reference/locked.md").write_text(
        _frontmatter(document_type="reference", validity="frozen") + "changed\n",
        encoding="utf-8",
    )
    invalid = lint_document_tree(
        current,
        policy,
        baseline_root=baseline,
        baseline_policy=policy,
    )
    assert any(finding.code == "frozen_document_modified" for finding in invalid.findings)

    missing_root_baseline = tmp_path / "missing-root-baseline"
    missing_root_baseline.mkdir()
    missing_root = lint_document_tree(
        current,
        policy,
        baseline_root=missing_root_baseline,
        baseline_policy=policy,
    )
    assert any(
        finding.code == "document_baseline_root_missing"
        for finding in missing_root.findings
    )


def test_tree_lint_fails_closed_for_malformed_baseline_frontmatter(
    tmp_path: Path,
) -> None:
    policy = _custom_policy()
    current = tmp_path / "current"
    baseline = tmp_path / "baseline"
    (current / "docs").mkdir(parents=True)
    (current / "docs/README.md").write_text("# Index\n", encoding="utf-8")
    (baseline / "docs/reference").mkdir(parents=True)
    (baseline / "docs/reference/locked.md").write_text(
        "---\nvalidity: [frozen\n---\n# Locked\n",
        encoding="utf-8",
    )

    result = lint_document_tree(
        current,
        policy,
        baseline_root=baseline,
        baseline_document_root="docs",
    )

    finding = next(
        item for item in result.findings if item.code == "document_baseline_invalid"
    )
    assert finding.path == "docs/reference/locked.md"
    assert "line 1, column" in finding.message


def test_tree_lint_requires_each_configured_root_file(tmp_path: Path) -> None:
    policy = _custom_policy()
    (tmp_path / "docs/ledger").mkdir(parents=True)
    (tmp_path / "docs/ledger/result.md").write_text(_frontmatter(), encoding="utf-8")

    result = lint_document_tree(tmp_path, policy)

    assert any(finding.code == "document_root_file_missing" for finding in result.findings)


def test_tree_lint_requires_current_configured_agent_guide(tmp_path: Path) -> None:
    payload = _custom_policy().model_dump(mode="json")
    payload["agent_guides"] = [{"path": "CLAUDE.md", "format": "claude"}]
    policy = DocumentLayoutPolicy.model_validate(payload)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/README.md").write_text("# Index\n", encoding="utf-8")

    missing = lint_document_tree(tmp_path, policy)
    assert any(finding.code == "agent_guide_missing" for finding in missing.findings)

    (tmp_path / "CLAUDE.md").write_bytes(render_project_agent_guide(policy, "claude"))
    passed = lint_document_tree(tmp_path, policy)
    assert passed.passed
    assert passed.checked_files == 2

    stale = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8").replace(
        "research/evidence:ledger",
        "research/evidence:stale",
    )
    (tmp_path / "CLAUDE.md").write_text(stale, encoding="utf-8")
    invalid = lint_document_tree(tmp_path, policy)
    assert any(finding.code == "agent_guide_mismatch" for finding in invalid.findings)


def test_doc_cli_works_in_uninitialized_repository_with_standalone_policy(
    tmp_path: Path,
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    policy = _custom_policy()
    (tmp_path / ".researchctl-docs.yaml").write_text(
        dump_yaml(policy),
        encoding="utf-8",
    )
    (tmp_path / "docs/ledger").mkdir(parents=True)
    (tmp_path / "docs/README.md").write_text("# Index\n", encoding="utf-8")
    (tmp_path / "docs/ledger/result.md").write_text(
        _frontmatter(),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        ["doc", "tree", "--project", str(tmp_path), "--json"],
    )

    assert result.exit_code == 0
    assert '"success": true' in result.stdout
    assert '"checked_files": 2' in result.stdout
    assert not (tmp_path / ".research").exists()

    doctor = CliRunner().invoke(app, ["doctor", str(tmp_path), "--json"])
    assert doctor.exit_code == 0
    assert '"name": "mode:standalone-documents"' in doctor.stdout
    assert "Generated schema file is missing" not in doctor.stdout

    index = CliRunner().invoke(
        app,
        ["doc", "index", "--project", str(tmp_path)],
    )
    assert index.exit_code == 0
    assert f"researchctl-renderer:{DOCUMENT_INDEX_RENDERER_ID}" in index.stdout


def test_doc_tree_human_policy_error_names_file_field_and_yaml_location(
    tmp_path: Path,
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    payload = _custom_policy().model_dump(mode="json")
    payload["routes"][0].pop("rationale")
    policy_path = tmp_path / ".researchctl-docs.yaml"
    policy_path.write_text(dump_yaml(payload), encoding="utf-8")

    result = CliRunner().invoke(
        app,
        ["doc", "tree", "--project", str(tmp_path)],
    )

    assert result.exit_code == 2
    assert f"Document policy schema validation failed in {policy_path}" in result.stderr
    assert "invalid: routes.0.rationale [validation_error]" in result.stderr
    assert "Field required" in result.stderr
    assert "line " in result.stderr and "column " in result.stderr
    assert "researchctl doc tree --project PROJECT" in result.stderr


def test_doc_cli_uses_current_policy_for_a_pre_policy_baseline(
    tmp_path: Path,
) -> None:
    current = tmp_path / "current"
    baseline = tmp_path / "baseline"
    for repository in (current, baseline):
        subprocess.run(["git", "init", "-q", str(repository)], check=True)
    (current / "docs").mkdir()
    (current / "docs/README.md").write_text("# Index\n", encoding="utf-8")
    (current / ".researchctl-docs.yaml").write_text(
        dump_yaml(_custom_policy()),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        [
            "doc",
            "tree",
            "--project",
            str(current),
            "--baseline-project",
            str(baseline),
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert '"success": true' in result.stdout
    assert not (baseline / ".research").exists()


def test_doc_cli_reads_legacy_baseline_policy_without_current_schema_validation(
    tmp_path: Path,
) -> None:
    current = tmp_path / "current"
    baseline = tmp_path / "baseline"
    policy = _custom_policy()
    legacy_payload = policy.model_dump(mode="json")
    for route in legacy_payload["routes"]:
        route.pop("rationale")

    for repository in (current, baseline):
        subprocess.run(["git", "init", "-q", str(repository)], check=True)
        (repository / "docs/reference").mkdir(parents=True)
        (repository / "docs/README.md").write_text("# Index\n", encoding="utf-8")
        (repository / "docs/reference/locked.md").write_text(
            _frontmatter(document_type="reference", validity="frozen"),
            encoding="utf-8",
        )
    (current / ".researchctl-docs.yaml").write_text(
        dump_yaml(policy),
        encoding="utf-8",
    )
    (baseline / ".researchctl-docs.yaml").write_text(
        dump_yaml(legacy_payload),
        encoding="utf-8",
    )

    passed = CliRunner().invoke(
        app,
        [
            "doc",
            "tree",
            "--project",
            str(current),
            "--baseline-project",
            str(baseline),
            "--json",
        ],
    )

    assert passed.exit_code == 0, passed.stdout
    assert '"success": true' in passed.stdout

    (current / "docs/reference/locked.md").write_text(
        _frontmatter(document_type="reference", validity="frozen") + "changed\n",
        encoding="utf-8",
    )
    rejected = CliRunner().invoke(
        app,
        [
            "doc",
            "tree",
            "--project",
            str(current),
            "--baseline-project",
            str(baseline),
            "--json",
        ],
    )

    assert rejected.exit_code == 2
    assert "frozen_document_modified" in rejected.stdout


@pytest.mark.parametrize(
    "baseline_policy",
    (
        "root: [docs\n",
        "root: ../docs\n",
    ),
)
def test_doc_cli_fails_closed_for_unsafe_baseline_policy(
    tmp_path: Path,
    baseline_policy: str,
) -> None:
    current = tmp_path / "current"
    baseline = tmp_path / "baseline"
    for repository in (current, baseline):
        subprocess.run(["git", "init", "-q", str(repository)], check=True)
    (current / "docs").mkdir()
    (current / "docs/README.md").write_text("# Index\n", encoding="utf-8")
    (current / ".researchctl-docs.yaml").write_text(
        dump_yaml(_custom_policy()),
        encoding="utf-8",
    )
    (baseline / ".researchctl-docs.yaml").write_text(
        baseline_policy,
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        [
            "doc",
            "tree",
            "--project",
            str(current),
            "--baseline-project",
            str(baseline),
            "--json",
        ],
    )

    assert result.exit_code == 2
    assert "document_baseline_policy_invalid" in result.stdout


def test_doc_cli_fails_closed_for_symlinked_baseline_policy(tmp_path: Path) -> None:
    current = tmp_path / "current"
    baseline = tmp_path / "baseline"
    for repository in (current, baseline):
        subprocess.run(["git", "init", "-q", str(repository)], check=True)
    (current / "docs").mkdir()
    (current / "docs/README.md").write_text("# Index\n", encoding="utf-8")
    (current / ".researchctl-docs.yaml").write_text(
        dump_yaml(_custom_policy()),
        encoding="utf-8",
    )
    target = baseline / "policy.yaml"
    target.write_text("root: docs\n", encoding="utf-8")
    (baseline / ".researchctl-docs.yaml").symlink_to(target.name)

    result = CliRunner().invoke(
        app,
        [
            "doc",
            "tree",
            "--project",
            str(current),
            "--baseline-project",
            str(baseline),
            "--json",
        ],
    )

    assert result.exit_code == 2
    assert "document_baseline_policy_invalid" in result.stdout


def test_doc_cli_renders_and_lints_policy_before_repository_adoption(
    tmp_path: Path,
) -> None:
    candidate = tmp_path / ".researchctl-docs.yaml"

    rendered = CliRunner().invoke(
        app,
        [
            "doc",
            "policy-template",
            "--agent-format",
            "claude",
            "--output-file",
            str(candidate),
        ],
    )
    assert rendered.exit_code == 0
    assert "Rendered:" in rendered.stdout

    linted = CliRunner().invoke(
        app,
        ["doc", "policy-lint", str(candidate), "--json"],
    )
    assert linted.exit_code == 2
    assert "document_route_rationale_template" in linted.stdout

    payload = load_yaml(candidate.read_text(encoding="utf-8"))
    routes = payload["routes"]
    assert isinstance(routes, list)
    for route in routes:
        assert isinstance(route, dict)
        route["rationale"] = f"Existing artifact justifies {route['classification']}."
    candidate.write_text(dump_yaml(payload), encoding="utf-8")
    adopted = CliRunner().invoke(
        app,
        ["doc", "policy-lint", str(candidate), "--json"],
    )
    assert adopted.exit_code == 0
    assert '"success": true' in adopted.stdout
    assert '"routes": 5' in adopted.stdout
    assert not (tmp_path / ".research").exists()


def test_doc_contract_discovery_schema_scaffold_and_route_dispatch(
    tmp_path: Path,
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".researchctl-docs.yaml").write_text(
        dump_yaml(_custom_policy()),
        encoding="utf-8",
    )
    (tmp_path / "docs/runbook").mkdir(parents=True)
    (tmp_path / "docs/brief").mkdir()
    (tmp_path / "docs/README.md").write_text("# Index\n", encoding="utf-8")
    runner = CliRunner()

    contracts = runner.invoke(app, ["doc", "contracts"])
    schema = runner.invoke(
        app,
        ["doc", "schema", "--contract", "markdown-frontmatter"],
    )
    runbook = tmp_path / "docs/runbook/operate.md"
    scaffold = runner.invoke(
        app,
        [
            "doc",
            "scaffold",
            "--project",
            str(tmp_path),
            "--type",
            "runbook",
            "--title",
            "Operate Megatron's worker",
            "--output-file",
            str(runbook),
        ],
    )
    checked = runner.invoke(
        app,
        ["doc", "check", str(runbook), "--project", str(tmp_path), "--json"],
    )

    assert contracts.exit_code == 0
    assert "Contract: analysis-brief" in contracts.stdout
    assert "Standalone check (no policy): researchctl brief lint PATH" in contracts.stdout
    assert "Routed check (policy required): researchctl doc check PATH --project ." in (
        contracts.stdout
    )
    assert "researchctl brief render PATH" in contracts.stdout
    assert "canonical model JSON, not the YAML file bytes" in contracts.stdout
    assert schema.exit_code == 0
    assert '"pattern": "^(?:session:session_' in schema.stdout
    assert "Always quote numeric-looking YAML values" in schema.stdout
    assert scaffold.exit_code == 0
    observed = runbook.read_text(encoding="utf-8")
    assert "owner: person:TODO" in observed
    assert "Operate Megatron's worker" in observed
    assert "sources: []" in observed
    assert "provenance: []" in observed
    assert '#   value: "11677"' in observed
    assert "#   see_also: [docs/runbooks/evaluation.md]" in observed
    assert checked.exit_code == 0
    assert '"contract": "markdown-frontmatter"' in checked.stdout

    numeric_provenance = observed.replace(
        "sources: []",
        (
            "sources:\n"
            "  - key: count-log\n"
            "    kind: repository_path\n"
            "    location: data/count.json"
        ),
        1,
    ).replace(
        "provenance: []",
        (
            "provenance:\n"
            "  - key: measured-count\n"
            "    value: 11677\n"
            "    basis: measured\n"
            "    source_keys: [count-log]"
        ),
        1,
    )
    runbook.write_text(numeric_provenance, encoding="utf-8")
    invalid_number = runner.invoke(
        app,
        ["doc", "check", str(runbook), "--project", str(tmp_path)],
    )
    assert invalid_number.exit_code == 2
    assert "invalid: provenance.0.value [validation_error]" in invalid_number.stderr
    assert "Input should be a valid string" in invalid_number.stderr
    assert "line " in invalid_number.stderr and "column " in invalid_number.stderr
    assert "researchctl doc check PATH" in invalid_number.stderr
    tree_validation = lint_document_tree(tmp_path, _custom_policy())
    tree_finding = next(
        item
        for item in tree_validation.findings
        if item.code == "document_schema_validation_error"
    )
    assert tree_finding.path.endswith(":provenance.0.value")
    assert "line " in tree_finding.message and "column " in tree_finding.message

    runbook.write_text(
        observed.replace(
            "sources: []",
            (
                "sources:\n"
                "  - key: disk-model\n"
                "    kind: repository_path\n"
                "    location: data/disk-model.csv"
            ),
        ).replace(
            "provenance: []",
            (
                "provenance:\n"
                "  - key: expected-disk\n"
                "    value: 2.4 TB\n"
                "    basis: estimated\n"
                "    source_keys: [disk-model]\n"
                "    method: checkpoints * bytes-per-checkpoint"
            ),
        ),
        encoding="utf-8",
    )
    missing_value = runner.invoke(
        app,
        ["doc", "check", str(runbook), "--project", str(tmp_path), "--json"],
    )
    assert missing_value.exit_code == 2
    assert "document_provenance_value_missing" in missing_value.stdout

    brief_source = tmp_path / "docs/brief/memory.yaml"
    brief_output = tmp_path / "docs/brief/memory.md"
    brief_scaffold = runner.invoke(
        app,
        [
            "doc",
            "scaffold",
            "--project",
            str(tmp_path),
            "--type",
            "brief",
            "--title",
            "Does recompute change memory use?",
            "--output-file",
            str(brief_source),
        ],
    )
    brief_check = runner.invoke(
        app,
        ["doc", "check", str(brief_source), "--project", str(tmp_path)],
    )
    brief_render = runner.invoke(
        app,
        [
            "doc",
            "render",
            str(brief_source),
            "--project",
            str(tmp_path),
            "--output-file",
            str(brief_output),
        ],
    )
    assert brief_scaffold.exit_code == 0
    assert brief_check.exit_code == 0
    assert "Prose:" in brief_check.stdout and "/350 English words" in brief_check.stdout
    assert brief_render.exit_code == 0
    assert "researchctl-generated:research-analysis-brief.v4" in brief_output.read_text(
        encoding="utf-8"
    )


def test_routed_render_preserves_project_frontmatter_and_owns_only_the_body(
    tmp_path: Path,
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    payload = _custom_policy().model_dump(mode="json")
    payload["routes"][1]["generated_markdown_frontmatter"] = {
        "required_fields": [
            "type",
            "name",
            "last_update",
            "status",
            "sources",
            "summary",
        ]
    }
    policy = DocumentLayoutPolicy.model_validate(payload)
    (tmp_path / ".researchctl-docs.yaml").write_text(
        dump_yaml(policy),
        encoding="utf-8",
    )
    (tmp_path / "docs/brief").mkdir(parents=True)
    (tmp_path / "docs/README.md").write_text("# Index\n", encoding="utf-8")
    source = tmp_path / "docs/brief/memory.yaml"
    output = tmp_path / "docs/brief/memory.md"
    runner = CliRunner()
    scaffold = runner.invoke(
        app,
        [
            "doc",
            "scaffold",
            "--project",
            str(tmp_path),
            "--type",
            "brief",
            "--title",
            "Does recompute change memory use?",
            "--output-file",
            str(source),
        ],
    )
    assert scaffold.exit_code == 0

    missing_envelope = runner.invoke(
        app,
        [
            "doc",
            "render",
            str(source),
            "--project",
            str(tmp_path),
            "--output-file",
            str(output),
        ],
    )
    assert missing_envelope.exit_code == 2
    assert "document_generated_frontmatter_missing" in missing_envelope.stderr

    frontmatter = (
        "---\n"
        "type: experiment\n"
        "name: Memory brief\n"
        "last_update: '2026-08-06'\n"
        "status: active\n"
        "sources: [memory.yaml]\n"
        "summary: Memory comparison\n"
        "---\n\n"
    )
    output.write_text(frontmatter, encoding="utf-8")
    rendered = runner.invoke(
        app,
        [
            "doc",
            "render",
            str(source),
            "--project",
            str(tmp_path),
            "--output-file",
            str(output),
        ],
    )
    assert rendered.exit_code == 0
    observed = output.read_text(encoding="utf-8")
    assert observed.startswith(frontmatter)
    assert "researchctl-generated:research-analysis-brief.v4" in observed

    tree = runner.invoke(
        app,
        ["doc", "tree", "--project", str(tmp_path), "--json"],
    )
    assert tree.exit_code == 0
    assert '"success": true' in tree.stdout

    output.write_text(observed.replace("summary: Memory comparison\n", ""), encoding="utf-8")
    invalid_frontmatter = runner.invoke(
        app,
        ["doc", "tree", "--project", str(tmp_path), "--json"],
    )
    assert invalid_frontmatter.exit_code == 2
    assert "document_generated_frontmatter_invalid" in invalid_frontmatter.stdout

    output.write_text(observed.replace("## Answer", "## Hand-edited answer"), encoding="utf-8")
    conflict = runner.invoke(
        app,
        [
            "doc",
            "render",
            str(source),
            "--project",
            str(tmp_path),
            "--output-file",
            str(output),
        ],
    )
    assert conflict.exit_code == 2
    assert "document_output_conflict" in conflict.stderr


def test_policy_template_can_render_to_standard_output() -> None:
    result = CliRunner().invoke(
        app,
        ["doc", "policy-template", "--output-file", "/dev/stdout"],
    )

    assert result.exit_code == 0
    assert result.stdout.startswith("# researchctl standalone document policy candidate")
    assert "document_output_conflict" not in result.stdout


def test_doc_cli_upserts_configured_agent_guide_without_initializing(
    tmp_path: Path,
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    payload = _custom_policy().model_dump(mode="json")
    payload["agent_guides"] = [{"path": "CLAUDE.md", "format": "claude"}]
    policy = DocumentLayoutPolicy.model_validate(payload)
    (tmp_path / ".researchctl-docs.yaml").write_text(
        dump_yaml(policy),
        encoding="utf-8",
    )
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/README.md").write_text("# Index\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("# Existing instructions\n", encoding="utf-8")

    rendered = CliRunner().invoke(
        app,
        [
            "doc",
            "agent-guide",
            "--project",
            str(tmp_path),
            "--output-file",
            "CLAUDE.md",
        ],
    )
    assert rendered.exit_code == 0
    assert "Updated:" in rendered.stdout
    observed = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
    assert observed.startswith("# Existing instructions\n")
    assert "researchctl-agent-guide:" in observed
    assert not (tmp_path / ".research").exists()

    legacy = render_project_agent_guide(policy, "claude").decode("utf-8").replace(
        "project-document-agent-guide.claude:begin",
        "project-document-agent-guide.claude.v1:begin",
    ).replace(
        "project-document-agent-guide.claude:end",
        "project-document-agent-guide.claude.v1:end",
    )
    (tmp_path / "CLAUDE.md").write_text(observed + "\n" + legacy, encoding="utf-8")
    migrated = CliRunner().invoke(
        app,
        [
            "doc",
            "agent-guide",
            "--project",
            str(tmp_path),
            "--output-file",
            "CLAUDE.md",
        ],
    )
    assert migrated.exit_code == 0
    assert "Updated:" in migrated.stdout
    observed = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
    assert ".claude.v1:begin" not in observed
    assert observed.count("researchctl-agent-guide:project-document-agent-guide.claude") == 2
    assert not observed.endswith("\n\n")

    repeated = CliRunner().invoke(
        app,
        [
            "doc",
            "agent-guide",
            "--project",
            str(tmp_path),
            "--output-file",
            "CLAUDE.md",
        ],
    )
    assert repeated.exit_code == 0
    assert "Unchanged:" in repeated.stdout

    tree = CliRunner().invoke(
        app,
        ["doc", "tree", "--project", str(tmp_path), "--json"],
    )
    assert tree.exit_code == 0
    assert '"checked_files": 2' in tree.stdout

    outside_contract = CliRunner().invoke(
        app,
        [
            "doc",
            "agent-guide",
            "--project",
            str(tmp_path),
            "--output-file",
            "UNCONFIGURED.md",
        ],
    )
    assert outside_contract.exit_code == 2
    assert "agent_guide_target_unconfigured" in outside_contract.stderr


def test_doc_cli_fails_closed_without_an_explicit_policy(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "docs").mkdir()

    result = CliRunner().invoke(
        app,
        ["doc", "tree", "--project", str(tmp_path), "--json"],
    )

    assert result.exit_code == 2
    assert '"code": "document_policy_missing"' in result.stdout
    assert not (tmp_path / ".research").exists()
