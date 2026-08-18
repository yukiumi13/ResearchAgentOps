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
    SimpleDocumentSiteAsset,
    SimpleDocumentSiteExcludedPath,
    SimpleDocumentSiteManifest,
    SimpleDocumentSitePage,
    SimpleDocumentSiteSection,
)
from researchctl.integrations.mkdocs import ResearchctlPlugin
from researchctl.serialization import canonical_digest

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _digest(path: Path) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


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


# --------------------------------------------------------------------------
# Directory-first manifests
# --------------------------------------------------------------------------


def _simple_manifest(tmp_path: Path, *, state: str = "clean") -> tuple[Path, Path]:
    """A hand-sealed version 2 manifest covering every publishable role."""

    project = tmp_path / "project"
    docs = project / "docs"
    (docs / "design").mkdir(parents=True)
    (docs / "profiling").mkdir()
    (docs / "runbooks/cluster/deep").mkdir(parents=True)
    (project / "mkdocs.yml").write_text("site_name: Test\n", encoding="utf-8")
    (docs / "CODEOWNERS").write_text("* @docs-team\n", encoding="utf-8")
    # A root page carrying an envelope: the frontmatter must not reach the site.
    (docs / "README.md").write_text(
        "---\nstatus: active\n---\n\n# Overview\n", encoding="utf-8"
    )
    (docs / "design/splice.yaml").write_text("title: Splice\n", encoding="utf-8")
    # A generated page may still carry a project-owned envelope.
    (docs / "design/splice.md").write_text(
        "---\nlifecycle: draft\n---\n\n# Splice the encoder\n", encoding="utf-8"
    )
    (docs / "profiling/memory.yaml").write_text("question: memory\n", encoding="utf-8")
    (docs / "profiling/memory.md").write_text("# Memory at 16k\n", encoding="utf-8")
    (docs / "runbooks/evaluation.md").write_text(
        "---\n"
        "status: active\n"
        "tags: [evaluation, cluster]\n"
        "reviewed_on: 2026-05-04\n"
        "locked: true\n"
        "---\n"
        "\n# Evaluation\n\nHow to evaluate.\n",
        encoding="utf-8",
    )
    (docs / "runbooks/retired.md").write_text("# Retired plan\n", encoding="utf-8")
    (docs / "runbooks/cluster/gpu-nodes.md").write_text("# GPU nodes\n", encoding="utf-8")
    (docs / "runbooks/cluster/old-plan.md").write_text("# Old plan\n", encoding="utf-8")
    (docs / "runbooks/cluster/deep/tuning.md").write_text("# Tuning\n", encoding="utf-8")
    (docs / "runbooks/cluster/plot.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    def page(relative: str, title: str, **overrides: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "path": f"docs/{relative}",
            "kind": "ordinary",
            "title": title,
            "status": "active",
            "git_history_present": False,
            "content_digest": _digest(docs / relative),
        }
        if "/" in relative:
            section, _, nested = relative.partition("/")
            payload["section"] = section
            payload["section_relative_path"] = nested
        else:
            payload["kind"] = "root"
        payload.update(overrides)
        return payload

    # Manifest order is the builder's: active before retired, root before
    # sections, then policy section order, then path.
    pages = [
        page("README.md", "Overview", owners=("@docs-team",)),
        page(
            "design/splice.md",
            "Splice the encoder",
            kind="structured",
            status=None,
            source_path="docs/design/splice.yaml",
            source_digest=_digest(docs / "design/splice.yaml"),
            contract="design-document",
            classification="design/architecture:document",
            lifecycle="draft",
            owners=("@docs-team",),
            tags=("encoder",),
        ),
        # An analysis brief has no envelope, so it has no lifecycle to show.
        page(
            "profiling/memory.md",
            "Memory at 16k",
            kind="structured",
            status=None,
            source_path="docs/profiling/memory.yaml",
            source_digest=_digest(docs / "profiling/memory.yaml"),
            contract="analysis-brief",
            owners=("@perf-team",),
        ),
        page("runbooks/cluster/deep/tuning.md", "Tuning", owners=("@docs-team",)),
        # Nobody owns it, nobody reviewed it, Git has never seen it.
        page("runbooks/cluster/gpu-nodes.md", "GPU nodes"),
        page(
            "runbooks/evaluation.md",
            "Evaluation",
            owners=("@docs-team", "ops@example.com"),
            tags=("evaluation", "cluster"),
            reviewed_on="2026-05-04",
            locked=True,
            git_history_present=True,
            last_edited_at="2026-06-07T08:09:10Z",
        ),
        page(
            "runbooks/cluster/old-plan.md",
            "Old plan",
            status="deprecated",
            in_history=True,
        ),
        page("runbooks/retired.md", "Retired plan", status="deprecated", in_history=True),
    ]
    payload: dict[str, object] = {
        "schema_version": "0.1",
        "manifest_kind": "simple_document_site_manifest",
        "policy_version": 2,
        "document_root": "docs",
        "repository_head": "1" * 40,
        "repository_state": state,
        "repository_remote": "https://github.com/acme/site.git",
        "policy_digest": "sha256:" + "2" * 64,
        # "experiments" holds nothing, so it must not become a heading.
        "sections": [
            SimpleDocumentSiteSection(path=name).model_dump(mode="json")
            for name in ("design", "profiling", "experiments", "runbooks")
        ],
        "pages": [
            SimpleDocumentSitePage.model_validate(item).model_dump(mode="json")
            for item in pages
        ],
        "assets": [
            SimpleDocumentSiteAsset(
                path="docs/runbooks/cluster/plot.png",
                section="runbooks",
                section_relative_path="cluster/plot.png",
                content_digest=_digest(docs / "runbooks/cluster/plot.png"),
            ).model_dump(mode="json")
        ],
        "excluded_paths": [
            SimpleDocumentSiteExcludedPath(
                path="docs/CODEOWNERS",
                reason="codeowners",
            ).model_dump(mode="json"),
            SimpleDocumentSiteExcludedPath(
                path="docs/design/splice.yaml",
                reason="structured_source",
                page_path="docs/design/splice.md",
            ).model_dump(mode="json"),
            SimpleDocumentSiteExcludedPath(
                path="docs/profiling/memory.yaml",
                reason="structured_source",
                page_path="docs/profiling/memory.md",
            ).model_dump(mode="json"),
        ],
    }
    payload["manifest_digest"] = canonical_digest(payload)
    manifest = SimpleDocumentSiteManifest.model_validate(payload)
    manifest_path = project / "site-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return project, manifest_path


DISCOVERED_URIS = [
    "README.md",
    "CODEOWNERS",
    "design/splice.yaml",
    "design/splice.md",
    "profiling/memory.yaml",
    "profiling/memory.md",
    "runbooks/evaluation.md",
    "runbooks/retired.md",
    "runbooks/cluster/gpu-nodes.md",
    "runbooks/cluster/old-plan.md",
    "runbooks/cluster/deep/tuning.md",
    "runbooks/cluster/plot.png",
]


EXPECTED_VERSION_TWO_NAV = [
    {"Overview": [{"Overview": "README.md"}]},
    {"Design": [{"Splice the encoder": "design/splice.md"}]},
    {"Profiling": [{"Memory at 16k": "profiling/memory.md"}]},
    {
        "Runbooks": [
            {"Evaluation": "runbooks/evaluation.md"},
            {
                "Cluster": [
                    {"GPU nodes": "runbooks/cluster/gpu-nodes.md"},
                    {"Deep": [{"Tuning": "runbooks/cluster/deep/tuning.md"}]},
                ]
            },
        ]
    },
    {
        "History": [
            {
                "Runbooks": [
                    {"Retired plan": "runbooks/retired.md"},
                    {"Cluster": [{"Old plan": "runbooks/cluster/old-plan.md"}]},
                ]
            }
        ]
    },
]


def test_version_two_navigation_nests_directories_and_retires_pages(
    tmp_path: Path,
) -> None:
    project, manifest = _simple_manifest(tmp_path)
    _plugin_instance, config = _plugin(project, manifest)

    assert config["nav"] == EXPECTED_VERSION_TWO_NAV


def test_version_two_publishes_only_listed_pages_and_assets(tmp_path: Path) -> None:
    project, manifest = _simple_manifest(tmp_path)
    plugin, config = _plugin(project, manifest)

    filtered = plugin.on_files(
        [SimpleNamespace(src_uri=uri) for uri in DISCOVERED_URIS],
        config=config,
    )

    # The canonical sources and the CODEOWNERS file are excluded; the static
    # asset is published because the manifest listed and digested it.
    assert [file.src_uri for file in filtered] == [
        "README.md",
        "design/splice.md",
        "profiling/memory.md",
        "runbooks/evaluation.md",
        "runbooks/retired.md",
        "runbooks/cluster/gpu-nodes.md",
        "runbooks/cluster/old-plan.md",
        "runbooks/cluster/deep/tuning.md",
        "runbooks/cluster/plot.png",
    ]


@pytest.mark.parametrize(
    "unlisted",
    ["stray.md", "runbooks/cluster/stray.png"],
)
def test_version_two_rejects_anything_the_manifest_never_saw(
    tmp_path: Path,
    unlisted: str,
) -> None:
    project, manifest = _simple_manifest(tmp_path)
    plugin, config = _plugin(project, manifest)

    with pytest.raises(ConfigurationError, match="absent from"):
        plugin.on_files([SimpleNamespace(src_uri=unlisted)], config=config)


@pytest.mark.parametrize(
    ("relative", "message"),
    [
        ("docs/runbooks/evaluation.md", "site page changed after manifest validation"),
        ("docs/design/splice.yaml", "structured source changed after manifest validation"),
        (
            "docs/runbooks/cluster/plot.png",
            "static asset changed after manifest validation",
        ),
    ],
)
def test_version_two_rejects_drift_in_every_published_role(
    tmp_path: Path,
    relative: str,
    message: str,
) -> None:
    project, manifest = _simple_manifest(tmp_path)
    (project / relative).write_bytes(b"drifted\n")

    with pytest.raises(ConfigurationError, match=message):
        _plugin(project, manifest)


def test_version_two_requires_a_clean_repository(tmp_path: Path) -> None:
    project, manifest = _simple_manifest(tmp_path, state="dirty")

    with pytest.raises(ConfigurationError, match="dirty repository"):
        _plugin(project, manifest)

    # The same manifest is publishable once the operator accepts a dirty tree.
    _plugin_instance, config = _plugin(project, manifest, require_clean=False)
    assert config["nav"][0] == {"Overview": [{"Overview": "README.md"}]}


@pytest.mark.parametrize("kind", ["site_manifest_v3", None])
def test_an_undeclared_manifest_kind_is_refused(tmp_path: Path, kind: str | None) -> None:
    project, manifest = _simple_manifest(tmp_path)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    if kind is None:
        del payload["manifest_kind"]
    else:
        payload["manifest_kind"] = kind
    manifest.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    # The kind is never inferred from the fields present, so a manifest whose
    # shape is otherwise valid is still refused.
    with pytest.raises(ConfigurationError, match="no supported manifest_kind"):
        _plugin(project, manifest)


SOURCE_URL = "https://github.com/acme/site/blob/" + "1" * 40


def _rendered(plugin, config, project: Path, uri: str) -> str:  # type: ignore[no-untyped-def]
    page = SimpleNamespace(file=SimpleNamespace(src_uri=uri))
    markdown = (project / "docs" / uri).read_text(encoding="utf-8")
    return plugin.on_page_markdown(markdown, page=page, config=config, files=[])


def test_invalid_page_frontmatter_is_a_named_configuration_error(tmp_path: Path) -> None:
    project, manifest = _simple_manifest(tmp_path)
    plugin, config = _plugin(project, manifest)
    page = SimpleNamespace(file=SimpleNamespace(src_uri="runbooks/evaluation.md"))

    with pytest.raises(
        ConfigurationError,
        match=r"page frontmatter is invalid: runbooks/evaluation\.md .*ParserError.*line 1",
    ):
        plugin.on_page_markdown(
            "---\ntags: [unterminated\n---\n# Evaluation\n",
            page=page,
            config=config,
            files=[],
        )


def test_invalid_version_one_frontmatter_uses_the_same_error_boundary(
    tmp_path: Path,
) -> None:
    project, manifest = _manifest(tmp_path)
    plugin, config = _plugin(project, manifest)
    page = SimpleNamespace(file=SimpleNamespace(src_uri="guide/run.md"))

    with pytest.raises(
        ConfigurationError,
        match=r"page frontmatter is invalid: guide/run\.md .*ParserError.*line 1",
    ):
        plugin.on_page_markdown(
            "---\ntags: [unterminated\n---\n# Run\n",
            page=page,
            config=config,
            files=[],
        )


def test_version_two_metadata_states_only_what_the_manifest_knows(
    tmp_path: Path,
) -> None:
    project, manifest = _simple_manifest(tmp_path)
    plugin, config = _plugin(project, manifest)

    assert _rendered(plugin, config, project, "runbooks/evaluation.md") == (
        "<!-- researchctl-site-metadata:simple-document-site-manifest.v1 -->\n"
        "> Document metadata: Owned by `@docs-team`, `ops@example.com` | "
        "Reviewed `2026-05-04` | Edited `2026-06-07` | "
        "Tags `evaluation`, `cluster` | Status `active` | Locked Yes\n"
        "\n"
        "# Evaluation\n\nHow to evaluate.\n"
    )
    # Absence is stated, never left for the reader to infer.
    assert _rendered(plugin, config, project, "runbooks/cluster/gpu-nodes.md") == (
        "<!-- researchctl-site-metadata:simple-document-site-manifest.v1 -->\n"
        "> Document metadata: Owned by Unassigned | Reviewed Not recorded | "
        "Edited Not recorded in Git | Tags None | Status `active` | Locked No\n"
        "\n"
        "# GPU nodes\n"
    )
    # A root page is treated exactly like an ordinary one, envelope included.
    assert _rendered(plugin, config, project, "README.md") == (
        "<!-- researchctl-site-metadata:simple-document-site-manifest.v1 -->\n"
        "> Document metadata: Owned by `@docs-team` | Reviewed Not recorded | "
        "Edited Not recorded in Git | Tags None | Status `active` | Locked No\n"
        "\n"
        "# Overview\n"
    )


def test_a_structured_page_shows_a_lifecycle_only_when_its_contract_has_one(
    tmp_path: Path,
) -> None:
    project, manifest = _simple_manifest(tmp_path)
    plugin, config = _plugin(project, manifest)

    assert _rendered(plugin, config, project, "design/splice.md") == (
        "<!-- researchctl-site-metadata:simple-document-site-manifest.v1 -->\n"
        "> Document metadata: Owned by `@docs-team` | Reviewed Not recorded | "
        "Edited Not recorded in Git | Tags `encoder` | Lifecycle `draft` | "
        "Locked No | Source [`docs/design/splice.yaml`]"
        f"({SOURCE_URL}/docs/design/splice.yaml)\n"
        "\n"
        "# Splice the encoder\n"
    )
    # An analysis brief has no envelope. Nothing invents a lifecycle or a
    # status for it, and the v1 classification taxonomy stays out of v2.
    brief = _rendered(plugin, config, project, "profiling/memory.md")
    assert brief == (
        "<!-- researchctl-site-metadata:simple-document-site-manifest.v1 -->\n"
        "> Document metadata: Owned by `@perf-team` | Reviewed Not recorded | "
        "Edited Not recorded in Git | Tags None | Locked No | "
        "Source [`docs/profiling/memory.yaml`]"
        f"({SOURCE_URL}/docs/profiling/memory.yaml)\n"
        "\n"
        "# Memory at 16k\n"
    )
    assert "Lifecycle" not in brief
    assert "Status" not in brief
    assert "Classification" not in brief


def test_mkdocs_core_builds_a_version_two_site_strictly(tmp_path: Path) -> None:
    project, manifest = _simple_manifest(tmp_path)
    config = load_config(config_file=str(project / "mkdocs.yml"), strict=True)
    plugin = ResearchctlPlugin()
    errors, warnings = plugin.load_config(
        {"manifest": str(manifest), "require_clean": True}
    )
    assert errors == []
    assert warnings == []
    config.plugins["researchctl"] = plugin
    plugin.on_config(config)
    assert config["nav"] == EXPECTED_VERSION_TWO_NAV

    build(config)

    site = project / "site"
    pages = {
        "index.html": "Overview",
        "design/splice/index.html": "Splice the encoder",
        "profiling/memory/index.html": "Memory at 16k",
        "runbooks/evaluation/index.html": "Evaluation",
        "runbooks/cluster/gpu-nodes/index.html": "GPU nodes",
        "runbooks/cluster/deep/tuning/index.html": "Tuning",
        "runbooks/retired/index.html": "Retired plan",
        "runbooks/cluster/old-plan/index.html": "Old plan",
    }
    rendered = {}
    for relative, heading in pages.items():
        html_text = (site / relative).read_text(encoding="utf-8")
        assert heading in html_text, relative
        assert "researchctl-site-metadata:simple-document-site-manifest.v1" in html_text
        rendered[relative] = html_text

    # The nav MkDocs built is the nav the manifest projected, empty section and
    # History group included.
    overview = rendered["index.html"]
    for heading in ("Design", "Profiling", "Runbooks", "History"):
        assert heading in overview
    assert "Experiments" not in overview

    # Frontmatter is source metadata and never reaches the page.
    assert "status: active" not in rendered["runbooks/evaluation/index.html"]
    assert "reviewed_on" not in rendered["runbooks/evaluation/index.html"]
    assert "lifecycle: draft" not in rendered["design/splice/index.html"]

    assert "Owned by" in rendered["runbooks/evaluation/index.html"]
    assert "Locked Yes" in rendered["runbooks/evaluation/index.html"]
    assert f"{SOURCE_URL}/docs/design/splice.yaml" in rendered["design/splice/index.html"]
    assert "Lifecycle" not in rendered["profiling/memory/index.html"]

    # Listed assets ship byte for byte; excluded paths never leave the tree.
    assert (site / "runbooks/cluster/plot.png").read_bytes() == (
        project / "docs/runbooks/cluster/plot.png"
    ).read_bytes()
    for absent in ("CODEOWNERS", "design/splice.yaml", "profiling/memory.yaml"):
        assert not (site / absent).exists(), absent


def test_only_files_discovered_in_the_docs_directory_face_the_closed_world(
    tmp_path: Path,
) -> None:
    project, manifest = _simple_manifest(tmp_path)
    plugin, config = _plugin(project, manifest)

    # MkDocs adds theme files to the same collection before on_files runs. They
    # come from the theme's directory and the manifest never lists them.
    retained = plugin.on_files(
        [SimpleNamespace(src_uri="css/base.css", src_dir=str(tmp_path / "theme"))],
        config=config,
    )
    assert [file.src_uri for file in retained] == ["css/base.css"]

    with pytest.raises(ConfigurationError, match="absent from"):
        plugin.on_files(
            [SimpleNamespace(src_uri="stray.md", src_dir=str(project / "docs"))],
            config=config,
        )


def test_an_unreadable_published_file_is_reported_as_a_configuration_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, manifest = _simple_manifest(tmp_path)

    def refuse_to_read(self: Path) -> bytes:
        raise OSError(13, "Permission denied")

    # The path still exists and is still a regular file; only the read fails.
    monkeypatch.setattr(Path, "read_bytes", refuse_to_read)

    with pytest.raises(
        ConfigurationError,
        match=r"site page could not be read: docs/README\.md \(PermissionError\)",
    ):
        _plugin(project, manifest)


def test_version_one_navigation_and_exclusions_survive_the_version_two_wiring(
    tmp_path: Path,
) -> None:
    project, manifest = _manifest(tmp_path)
    plugin, config = _plugin(project, manifest)

    assert config["nav"] == [
        {"Overview": [{"Overview": "README.md"}]},
        {"Guide": [{"Run": "guide/run.md"}]},
        {"Brief": [{"Result": "brief/result.md"}]},
    ]
    filtered = plugin.on_files(
        [
            SimpleNamespace(src_uri="README.md"),
            SimpleNamespace(src_uri="brief/result.yaml"),
            SimpleNamespace(src_uri="guide/run.md"),
            SimpleNamespace(src_uri="brief/result.md"),
        ],
        config=config,
    )
    assert [file.src_uri for file in filtered] == [
        "README.md",
        "guide/run.md",
        "brief/result.md",
    ]
    # A classification-route manifest cannot enumerate assets, so its silence
    # about a static file is not a verdict and the file is still published.
    static = plugin.on_files(
        [SimpleNamespace(src_uri="guide/diagram.png")],
        config=config,
    )
    assert [file.src_uri for file in static] == ["guide/diagram.png"]
    # Unlisted Markdown stays refused, exactly as before.
    with pytest.raises(ConfigurationError, match="absent from"):
        plugin.on_files([SimpleNamespace(src_uri="guide/unlisted.md")], config=config)


def test_repository_site_output_is_confined_to_ignored_build_tree() -> None:
    config = (PROJECT_ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    ignored = {
        line.strip()
        for line in (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert "site_dir: build/site" in config
    assert "build/" in ignored
