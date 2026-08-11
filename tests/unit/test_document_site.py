from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from researchctl.cli import app
from researchctl.domain.models import (
    AnalysisBrief,
    DocumentLayoutPolicy,
    DocumentSiteManifest,
    ProjectStatusSummary,
)
from researchctl.errors import RCPError
from researchctl.serialization import canonical_digest, dump_yaml
from researchctl.services.project_documents import (
    build_document_site_manifest,
    render_document_site_manifest,
    render_project_status_summary,
)
from researchctl.services.research_writing import render_analysis_brief

DOCUMENT_ID = "document_20260810T120000Z_" + "1" * 24
BASIS_COMMIT = "2" * 40


def _git(repository: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repository), *args], check=True, capture_output=True)


def _policy() -> DocumentLayoutPolicy:
    return DocumentLayoutPolicy.model_validate(
        {
            "root": "docs",
            "root_files": ["docs/README.md"],
            "routes": [
                {
                    "classification": "guide/project:document",
                    "document_type": "guide",
                    "directory": "docs/guide",
                    "contract": "markdown-frontmatter",
                    "rationale": "The fixture keeps manually authored guides here.",
                },
                {
                    "classification": "research/analysis:brief",
                    "document_type": "brief",
                    "directory": "docs/brief",
                    "contract": "analysis-brief",
                    "rationale": "The fixture publishes concise measurement briefs here.",
                },
                {
                    "classification": "status/project:snapshot",
                    "document_type": "status",
                    "directory": "docs/status",
                    "contract": "project-status-summary",
                    "rationale": "The fixture publishes structured current state here.",
                },
                {
                    "classification": "archive/project:superseded",
                    "document_type": "archive",
                    "directory": "docs/archive",
                    "contract": "markdown-frontmatter",
                    "rationale": "The fixture retains invalid historical notes here.",
                },
            ],
            "legacy_files": [
                {
                    "path": "docs/OLD.md",
                    "classification": "guide/project:document",
                    "migration_target": "docs/guide/old.md",
                    "reason": "Pre-contract prose.",
                },
                {
                    "path": "docs/legacy.json",
                    "classification": "guide/project:document",
                    "migration_target": "docs/guide/legacy.md",
                    "reason": "Pre-contract machine-readable reference.",
                },
            ],
            "max_depth": 2,
        }
    )


def _manual(*, title: str, document_type: str, validity: str = "valid") -> str:
    invalid_reason = (
        "invalid_reason: Replaced by accepted evidence.\n" if validity == "invalid" else ""
    )
    return (
        "---\n"
        f"type: {document_type}\n"
        f"title: {title}\n"
        "owner: person:manager\n"
        "last_updated: 2026-08-10\n"
        f"validity: {validity}\n"
        f"{invalid_reason}"
        "tags: []\n"
        "references: []\n"
        "relations:\n"
        "  supersedes: []\n"
        "  derived_from: []\n"
        "  see_also: []\n"
        "---\n\n"
        f"# {title}\n"
    )


def _status() -> ProjectStatusSummary:
    return ProjectStatusSummary.model_validate(
        {
            "document_id": DOCUMENT_ID,
            "document_kind": "project_status_summary",
            "classification": "status/project:snapshot",
            "slug": "current",
            "title": "Current status",
            "status": "proposed",
            "basis_commit": BASIS_COMMIT,
            "revision": 1,
            "authored_by": {
                "role": "trusted_automation",
                "actor_id": "site-test",
            },
            "created_at": "2026-08-10T12:00:00Z",
            "updated_at": "2026-08-10T12:00:00Z",
            "as_of": "2026-08-10T12:00:00Z",
            "sources": [
                {
                    "key": "readme",
                    "kind": "repository_path",
                    "location": "docs/README.md",
                }
            ],
            "executive_summary": "The site manifest is under validation.",
            "capabilities": [
                {
                    "key": "site",
                    "title": "Document site",
                    "status": "designed",
                    "summary": "The projection is implemented locally.",
                    "evidence_keys": ["readme"],
                    "missing": ["A protected publication pilot remains."],
                }
            ],
            "next_steps": ["Run the strict site build."],
        }
    )


def _brief() -> AnalysisBrief:
    return AnalysisBrief.model_validate(
        {
            "question": "Does the manifest preserve the validated page set?",
            "answer": "Yes, for this fixture.",
            "protocol": "Compare manifest paths and digests with fixture bytes.",
            "metrics": [{"key": "pages", "label": "Pages"}],
            "evidence": [
                {
                    "setting": "Fixture",
                    "values": {"pages": "6"},
                    "source_keys": ["tree"],
                }
            ],
            "interpretation": ["Every published Markdown path is explicit."],
            "limitations": ["This is a local fixture."],
            "sources": [{"key": "tree", "location": "docs/README.md"}],
        }
    )


def _repository(tmp_path: Path) -> tuple[Path, DocumentLayoutPolicy]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "site@example.test")
    _git(repository, "config", "user.name", "Site Test")
    _git(repository, "remote", "add", "origin", "https://token@github.com/acme/site.git")
    policy = _policy()
    (repository / "docs/guide").mkdir(parents=True)
    (repository / "docs/brief").mkdir()
    (repository / "docs/status").mkdir()
    (repository / "docs/archive").mkdir()
    (repository / "docs/README.md").write_text("# Project overview\n", encoding="utf-8")
    (repository / "docs/guide/operate.md").write_text(
        _manual(title="Operate", document_type="guide", validity="frozen"),
        encoding="utf-8",
    )
    (repository / "docs/archive/obsolete.md").write_text(
        _manual(title="Obsolete", document_type="archive", validity="invalid"),
        encoding="utf-8",
    )
    (repository / "docs/OLD.md").write_text("# Legacy note\n", encoding="utf-8")
    (repository / "docs/legacy.json").write_text("{}\n", encoding="utf-8")

    brief = _brief()
    (repository / "docs/brief/result.yaml").write_text(dump_yaml(brief), encoding="utf-8")
    (repository / "docs/brief/result.md").write_bytes(render_analysis_brief(brief))
    status = _status()
    (repository / "docs/status/current.yaml").write_text(
        dump_yaml(status), encoding="utf-8"
    )
    (repository / "docs/status/current.md").write_bytes(
        render_project_status_summary(status)
    )
    (repository / ".researchctl-docs.yaml").write_text(dump_yaml(policy), encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-qm", "fixture")
    return repository, policy


def test_site_manifest_is_deterministic_and_preserves_governed_metadata(
    tmp_path: Path,
) -> None:
    repository, policy = _repository(tmp_path)

    first = build_document_site_manifest(repository, policy)
    second = build_document_site_manifest(repository, policy)

    assert first == second
    assert render_document_site_manifest(first) == render_document_site_manifest(second)
    assert first.repository_state == "clean"
    assert first.repository_remote == "https://github.com/acme/site.git"
    assert first.repository_head is not None
    assert first.manifest_digest.startswith("sha256:")
    assert [page.path for page in first.pages] == [
        "docs/README.md",
        "docs/guide/operate.md",
        "docs/brief/result.md",
        "docs/status/current.md",
        "docs/archive/obsolete.md",
        "docs/OLD.md",
    ]

    pages = {page.path: page for page in first.pages}
    assert pages["docs/guide/operate.md"].validity == "frozen"
    assert pages["docs/status/current.md"].lifecycle == "proposed"
    assert pages["docs/status/current.md"].source_path == "docs/status/current.yaml"
    assert pages["docs/archive/obsolete.md"].history_kind == "archive"
    assert pages["docs/OLD.md"].history_kind == "legacy"
    expected_digest = "sha256:" + hashlib.sha256(
        (repository / "docs/status/current.yaml").read_bytes()
    ).hexdigest()
    assert pages["docs/status/current.md"].source_digest == expected_digest
    assert [(item.path, item.reason) for item in first.excluded_paths] == [
        ("docs/brief/result.yaml", "structured_source"),
        ("docs/legacy.json", "legacy_non_markdown"),
        ("docs/status/current.yaml", "structured_source"),
    ]

    inconsistent = first.model_dump(mode="json")
    inconsistent["excluded_paths"] = [
        item
        for item in inconsistent["excluded_paths"]
        if item["path"] != "docs/status/current.yaml"
    ]
    digest_payload = {
        key: value for key, value in inconsistent.items() if key != "manifest_digest"
    }
    inconsistent["manifest_digest"] = canonical_digest(digest_payload)
    with pytest.raises(ValidationError, match="one-to-one mapping"):
        DocumentSiteManifest.model_validate(inconsistent)


def test_site_manifest_records_dirty_state_and_refuses_an_invalid_tree(tmp_path: Path) -> None:
    repository, policy = _repository(tmp_path)
    (repository / "scratch.txt").write_text("dirty\n", encoding="utf-8")
    assert build_document_site_manifest(repository, policy).repository_state == "dirty"

    (repository / "docs/unknown").mkdir()
    (repository / "docs/unknown/note.md").write_text("# Unknown\n", encoding="utf-8")
    with pytest.raises(RCPError, match="requires a valid governed document tree") as caught:
        build_document_site_manifest(repository, policy)
    assert caught.value.code == "document_site_tree_invalid"


def test_site_manifest_cli_streams_json_and_safely_replaces_output(tmp_path: Path) -> None:
    repository, _policy_value = _repository(tmp_path)
    runner = CliRunner()

    streamed = runner.invoke(app, ["doc", "site-manifest", "-C", str(repository)])
    assert streamed.exit_code == 0
    assert json.loads(streamed.stdout)["manifest_kind"] == "document_site_manifest"

    output = tmp_path / "site-manifest.json"
    rendered = runner.invoke(
        app,
        ["doc", "site-manifest", "-C", str(repository), "--output-file", str(output)],
    )
    assert rendered.exit_code == 0
    clean_payload = json.loads(output.read_text(encoding="utf-8"))
    assert clean_payload["repository_state"] == "clean"

    (repository / "scratch.txt").write_text("dirty\n", encoding="utf-8")
    updated = runner.invoke(
        app,
        ["doc", "site-manifest", "-C", str(repository), "--output-file", str(output)],
    )
    assert updated.exit_code == 0
    assert "Updated:" in updated.stdout
    assert json.loads(output.read_text(encoding="utf-8"))["repository_state"] == "dirty"

    rejected = runner.invoke(
        app,
        ["doc", "site-manifest", "-C", str(repository), "--require-clean"],
    )
    assert rejected.exit_code == 2
    assert "document_site_repository_dirty" in rejected.stderr

    schema = runner.invoke(
        app,
        ["doc", "schema", "--contract", "document-site-manifest"],
    )
    assert schema.exit_code == 0
    assert json.loads(schema.stdout)["title"] == "DocumentSiteManifest"
