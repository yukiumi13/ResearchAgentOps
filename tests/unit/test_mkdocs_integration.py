from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("mkdocs")

from mkdocs.commands.build import build
from mkdocs.config import load_config
from mkdocs.exceptions import ConfigurationError

from researchctl.domain.models import (
    DocumentSiteExcludedPath,
    DocumentSiteManifest,
    DocumentSitePage,
)
from researchctl.integrations.mkdocs import ResearchctlPlugin
from researchctl.serialization import canonical_digest

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _manifest(tmp_path: Path, *, state: str = "clean") -> tuple[Path, Path]:
    project = tmp_path / "project"
    docs = project / "docs"
    (docs / "guide").mkdir(parents=True)
    (docs / "brief").mkdir()
    (docs / "README.md").write_text("# Overview\n", encoding="utf-8")
    (docs / "guide/run.md").write_text(
        "---\ntype: guide\nowner: person:manager\n---\n\n# Run\n",
        encoding="utf-8",
    )
    (docs / "brief/result.md").write_text("# Result\n", encoding="utf-8")
    (docs / "brief/result.yaml").write_text("question: result\n", encoding="utf-8")
    (project / "mkdocs.yml").write_text("site_name: Test\n", encoding="utf-8")

    def digest(path: Path) -> str:
        import hashlib

        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()

    pages = [
        {
            "path": "docs/README.md",
            "kind": "root",
            "title": "Overview",
            "content_digest": digest(docs / "README.md"),
        },
        {
            "path": "docs/guide/run.md",
            "kind": "manual",
            "title": "Run",
            "document_type": "guide",
            "classification": "guide/project:document",
            "contract": "markdown-frontmatter",
            "validity": "frozen",
            "route_order": 0,
            "content_digest": digest(docs / "guide/run.md"),
        },
        {
            "path": "docs/brief/result.md",
            "source_path": "docs/brief/result.yaml",
            "kind": "structured",
            "title": "Result",
            "document_type": "brief",
            "classification": "research/analysis:brief",
            "contract": "analysis-brief",
            "generated": True,
            "route_order": 1,
            "content_digest": digest(docs / "brief/result.md"),
            "source_digest": digest(docs / "brief/result.yaml"),
        },
    ]
    payload: dict[str, object] = {
        "schema_version": "0.1",
        "manifest_kind": "document_site_manifest",
        "document_root": "docs",
        "repository_head": "1" * 40,
        "repository_state": state,
        "repository_remote": "https://github.com/acme/site.git",
        "policy_digest": "sha256:" + "2" * 64,
        "pages": pages,
        "excluded_paths": [
            {
                "path": "docs/brief/result.yaml",
                "reason": "structured_source",
                "page_path": "docs/brief/result.md",
            }
        ],
    }
    payload["pages"] = [
        DocumentSitePage.model_validate(item).model_dump(mode="json") for item in pages
    ]
    payload["excluded_paths"] = [
        DocumentSiteExcludedPath.model_validate(item).model_dump(mode="json")
        for item in payload["excluded_paths"]
    ]
    payload["manifest_digest"] = canonical_digest(payload)
    manifest = DocumentSiteManifest.model_validate(payload)
    manifest_path = project / "site-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return project, manifest_path


def _plugin(project: Path, manifest: Path, *, require_clean: bool = True):
    plugin = ResearchctlPlugin()
    errors, warnings = plugin.load_config(
        {"manifest": str(manifest), "require_clean": require_clean}
    )
    assert errors == []
    assert warnings == []
    config = {
        "config_file_path": str(project / "mkdocs.yml"),
        "docs_dir": str(project / "docs"),
    }
    plugin.on_config(config)
    return plugin, config


def test_mkdocs_plugin_generates_nav_filters_sources_and_injects_metadata(
    tmp_path: Path,
) -> None:
    project, manifest = _manifest(tmp_path)
    plugin, config = _plugin(project, manifest)

    assert config["nav"] == [
        {"Overview": [{"Overview": "README.md"}]},
        {"Guide": [{"Run": "guide/run.md"}]},
        {"Brief": [{"Result": "brief/result.md"}]},
    ]
    files = [
        SimpleNamespace(src_uri="README.md"),
        SimpleNamespace(src_uri="guide/run.md"),
        SimpleNamespace(src_uri="brief/result.md"),
        SimpleNamespace(src_uri="brief/result.yaml"),
    ]
    filtered = plugin.on_files(files, config=config)
    assert [file.src_uri for file in filtered] == [
        "README.md",
        "guide/run.md",
        "brief/result.md",
    ]

    page = SimpleNamespace(file=SimpleNamespace(src_uri="brief/result.md"))
    rendered = plugin.on_page_markdown(
        "# Result\n", page=page, config=config, files=filtered
    )
    assert "Classification `research/analysis:brief`" in rendered
    assert "Source [`docs/brief/result.yaml`]" in rendered
    assert "/blob/" + "1" * 40 + "/docs/brief/result.yaml" in rendered

    manual_page = SimpleNamespace(file=SimpleNamespace(src_uri="guide/run.md"))
    manual_source = (project / "docs/guide/run.md").read_text(encoding="utf-8")
    manual = plugin.on_page_markdown(
        manual_source,
        page=manual_page,
        config=config,
        files=filtered,
    )
    assert "Validity `frozen`" in manual
    assert "owner: person:manager" not in manual
    assert manual.endswith("# Run\n")


def test_mkdocs_plugin_rejects_dirty_manifest_digest_drift_and_unlisted_markdown(
    tmp_path: Path,
) -> None:
    project, manifest = _manifest(tmp_path, state="dirty")
    with pytest.raises(ConfigurationError, match="dirty repository"):
        _plugin(project, manifest)

    plugin, config = _plugin(project, manifest, require_clean=False)
    with pytest.raises(ConfigurationError, match="absent from"):
        plugin.on_files(
            [SimpleNamespace(src_uri="unlisted.md")],
            config=config,
        )

    (project / "docs/guide/run.md").write_text("# Drifted\n", encoding="utf-8")
    fresh = ResearchctlPlugin()
    errors, _warnings = fresh.load_config(
        {"manifest": str(manifest), "require_clean": False}
    )
    assert errors == []
    with pytest.raises(ConfigurationError, match="changed after manifest validation"):
        fresh.on_config(config)


def test_mkdocs_core_builds_the_manifest_projection_strictly(tmp_path: Path) -> None:
    project, manifest = _manifest(tmp_path)
    config = load_config(config_file=str(project / "mkdocs.yml"), strict=True)
    plugin = ResearchctlPlugin()
    errors, warnings = plugin.load_config(
        {"manifest": str(manifest), "require_clean": True}
    )
    assert errors == []
    assert warnings == []
    config.plugins["researchctl"] = plugin
    plugin.on_config(config)

    build(config)

    overview = (project / "site/index.html").read_text(encoding="utf-8")
    result = (project / "site/brief/result/index.html").read_text(encoding="utf-8")
    assert "Overview" in overview
    assert "researchctl-site-metadata:document-site-manifest.v1" in result
    assert "docs/brief/result.yaml" in result


def test_repository_site_output_is_confined_to_ignored_build_tree() -> None:
    config = (PROJECT_ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    ignored = {
        line.strip()
        for line in (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert "site_dir: build/site" in config
    assert "build/" in ignored
