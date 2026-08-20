from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from researchctl.cli import app
from researchctl.domain.models import (
    SimpleDocumentLayoutPolicy,
    SimpleDocumentSiteAsset,
    SimpleDocumentSiteExcludedPath,
    SimpleDocumentSiteManifest,
    SimpleDocumentSitePage,
    SimpleDocumentSiteSection,
)
from researchctl.errors import RCPError
from researchctl.repository import discover_repository, last_commit_timestamp
from researchctl.schema import SCHEMA_MODELS, generate_schema_files
from researchctl.serialization import canonical_digest, dump_yaml
from researchctl.services import simple_document_site
from researchctl.services.document_policy import build_effective_policy
from researchctl.services.simple_document_site import (
    build_simple_document_site_manifest,
    render_simple_document_site_manifest,
)

# The Git identity is fixed so a commit's bytes never depend on the machine.
_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "Manifest Fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.com",
    "GIT_COMMITTER_NAME": "Manifest Fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.com",
}

SECTIONS: list[dict[str, Any]] = [
    {
        "path": "design",
        "structured": {
            "contract": "design-document",
            "classification": "design/architecture:document",
        },
    },
    {"path": "runbooks"},
]


def _policy(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "version": 2,
        "root": "docs",
        "sections": [dict(section) for section in SECTIONS],
        "root_pages": ["README.md"],
        "max_depth": 3,
        "ownership": {"source": "codeowners", "required": True},
    }
    payload.update(overrides)
    return payload


def _simple(payload: dict[str, Any]) -> SimpleDocumentLayoutPolicy:
    effective = build_effective_policy(payload)
    assert effective.simple is not None
    return effective.simple


def _git(root: Path, *args: str, when: str | None = None) -> None:
    """Run one Git command with an explicit date, never through a shell."""

    environment = {**os.environ, **_GIT_IDENTITY}
    if when is not None:
        environment["GIT_AUTHOR_DATE"] = when
        environment["GIT_COMMITTER_DATE"] = when
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        env=environment,
    )


def _commit(root: Path, *paths: str, when: str) -> None:
    _git(root, "add", "--", *paths)
    _git(root, "commit", "-q", "-m", f"add {' '.join(paths)}", when=when)


def _write(root: Path, relative: str, content: str) -> Path:
    destination = root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8")
    return destination


def _structured_pair(root: Path, *, section: str, contract: str, title: str, stem: str) -> None:
    """Author one canonical source and its render through the real commands."""

    runner = CliRunner()
    source = root / "docs" / section / f"{stem}.yaml"
    source.parent.mkdir(parents=True, exist_ok=True)
    scaffolded = runner.invoke(
        app,
        [
            "doc", "scaffold",
            "--type", section,
            "--title", title,
            "--contract", contract,
            "--project", str(root),
            "--output-file", str(source),
        ],
    )
    assert scaffolded.exit_code == 0, scaffolded.stdout + str(scaffolded.stderr)
    # Envelope tags must reach the manifest, so the fixture sets one.
    source.write_text(
        source.read_text(encoding="utf-8").replace("tags: []\n", "tags:\n- encoder\n"),
        encoding="utf-8",
    )
    rendered = runner.invoke(
        app,
        [
            "doc", "render", str(source),
            "--project", str(root),
            "--output-file", str(source.with_suffix(".md")),
        ],
    )
    assert rendered.exit_code == 0, rendered.stdout


@pytest.fixture
def site_repository(tmp_path: Path) -> Path:
    """A committed version 2 repository holding one of every publishable thing."""

    root = tmp_path / "repository"
    root.mkdir()
    _git(root, "init", "-q", ".")
    _write(root, ".researchctl-docs.yaml", dump_yaml(_policy()))
    # The effective CODEOWNERS lives inside the document root, so it is review
    # configuration rather than a page and must appear as an exclusion.
    _write(root, "docs/CODEOWNERS", "* @docs-team\n")
    _write(root, "docs/README.md", "# Documentation\n")
    _write(root, "docs/runbooks/evaluation.md", "# Evaluation\n\nHow to evaluate.\n")
    _write(
        root,
        "docs/runbooks/cluster/gpu-nodes.md",
        "---\n"
        "status: active\n"
        "tags: [cluster]\n"
        "reviewed_on: 2026-05-04\n"
        "locked: true\n"
        "depends_on: [docs/runbooks/evaluation.md]\n"
        "---\n"
        "\n# GPU nodes\n\nSee [evaluation](../evaluation.md) and ![plot](plot.png).\n",
    )
    _write(root, "docs/runbooks/cluster/plot.png", "\x89PNG\r\n\x1a\n")
    _write(
        root,
        "docs/runbooks/retired.md",
        "---\nstatus: deprecated\n---\n\n# Retired runbook\n",
    )
    _structured_pair(
        root,
        section="design",
        contract="design-document",
        title="Splice the encoder",
        stem="splice-the-encoder",
    )
    # The render is committed first and alone, so the source's later commit is
    # the only way the page can report the newer date.
    _commit(root, "docs/design/splice-the-encoder.md", when="2026-03-01T00:00:00+00:00")
    _commit(
        root,
        ".researchctl-docs.yaml",
        "docs/CODEOWNERS",
        "docs/README.md",
        "docs/runbooks",
        "docs/design/splice-the-encoder.yaml",
        when="2026-06-07T08:09:10+00:00",
    )
    return root


def _manifest(root: Path) -> SimpleDocumentSiteManifest:
    return build_simple_document_site_manifest(root, _simple(_policy()))


def test_the_manifest_projects_a_whole_version_two_tree(site_repository: Path) -> None:
    # An untracked page proves history absence without touching the rest.
    _write(site_repository, "docs/runbooks/draft-note.md", "# Draft note\n")

    manifest = _manifest(site_repository)
    pages = {page.path: page for page in manifest.pages}

    assert manifest.manifest_kind == "simple_document_site_manifest"
    assert manifest.policy_version == 2
    assert manifest.document_root == "docs"
    assert manifest.repository_state == "dirty"
    assert [section.path for section in manifest.sections] == ["design", "runbooks"]
    assert manifest.policy_digest == canonical_digest(_simple(_policy()))

    # Root first, then sections in policy order, with History last.
    assert [page.path for page in manifest.pages] == [
        "docs/README.md",
        "docs/design/splice-the-encoder.md",
        "docs/runbooks/cluster/gpu-nodes.md",
        "docs/runbooks/draft-note.md",
        "docs/runbooks/evaluation.md",
        "docs/runbooks/retired.md",
    ]

    root_page = pages["docs/README.md"]
    assert (root_page.kind, root_page.section, root_page.section_relative_path) == (
        "root",
        None,
        None,
    )
    assert root_page.title == "Documentation"
    assert root_page.status == "active"

    nested = pages["docs/runbooks/cluster/gpu-nodes.md"]
    assert nested.kind == "ordinary"
    assert nested.section == "runbooks"
    # Section plus relative path is the whole hierarchy a nav needs.
    assert nested.section_relative_path == "cluster/gpu-nodes.md"
    assert nested.tags == ("cluster",)
    assert nested.owners == ("@docs-team",)
    assert nested.reviewed_on is not None and nested.reviewed_on.isoformat() == "2026-05-04"
    assert nested.locked is True
    assert nested.depends_on == ("docs/runbooks/evaluation.md",)
    assert nested.links == (
        "docs/runbooks/evaluation.md",
        "docs/runbooks/cluster/plot.png",
    )
    assert nested.git_history_present is True
    assert nested.last_edited_at is not None
    assert nested.last_edited_at.isoformat() == "2026-06-07T08:09:10+00:00"

    untracked = pages["docs/runbooks/draft-note.md"]
    assert untracked.git_history_present is False
    assert untracked.last_edited_at is None

    retired = pages["docs/runbooks/retired.md"]
    assert (retired.status, retired.in_history) == ("deprecated", True)
    assert nested.in_history is False

    structured = pages["docs/design/splice-the-encoder.md"]
    assert structured.kind == "structured"
    assert structured.source_path == "docs/design/splice-the-encoder.yaml"
    assert structured.contract == "design-document"
    assert structured.classification == "design/architecture:document"
    assert structured.title == "Splice the encoder"
    assert structured.lifecycle == "draft"
    assert structured.tags == ("encoder",)
    assert structured.status is None
    assert structured.source_digest is not None
    # The canonical source is what anyone edits, so its history wins over the
    # render's older commit.
    assert structured.last_edited_at is not None
    assert structured.last_edited_at.isoformat() == "2026-06-07T08:09:10+00:00"

    assert [asset.path for asset in manifest.assets] == ["docs/runbooks/cluster/plot.png"]
    asset = manifest.assets[0]
    assert (asset.section, asset.section_relative_path) == ("runbooks", "cluster/plot.png")

    assert [(item.path, item.reason, item.page_path) for item in manifest.excluded_paths] == [
        ("docs/CODEOWNERS", "codeowners", None),
        (
            "docs/design/splice-the-encoder.yaml",
            "structured_source",
            "docs/design/splice-the-encoder.md",
        ),
    ]

    # Deterministic bytes and a digest that authenticates the whole payload.
    again = _manifest(site_repository)
    first = render_simple_document_site_manifest(manifest)
    assert first == render_simple_document_site_manifest(again)
    assert manifest.manifest_digest == again.manifest_digest
    payload = json.loads(first)
    recomputed = canonical_digest(
        {key: value for key, value in payload.items() if key != "manifest_digest"}
    )
    assert payload["manifest_digest"] == manifest.manifest_digest == recomputed


def test_an_invalid_tree_refuses_to_build_a_manifest(site_repository: Path) -> None:
    _write(site_repository, "docs/runbooks/untitled.md", "No level-one heading here.\n")

    with pytest.raises(RCPError) as error:
        _manifest(site_repository)

    assert error.value.code == "document_site_tree_invalid"
    assert "document_title_missing" in json.dumps(error.value.context)


@pytest.mark.parametrize(
    ("mutation", "changed_fields"),
    [
        # Invalidate the tree just before the confirming lint runs, so it is
        # that lint, not an earlier byte read, that catches the race.
        pytest.param("second_lint", True, id="final-lint-invalidated"),
        # A valid CODEOWNERS edit breaks no rule at all; only comparing the
        # whole projection notices that every page changed hands.
        pytest.param("codeowners_owner", True, id="owners-reassigned-mid-build"),
        # A page edited after its digest was taken, chosen so the projection is
        # identical and only the byte re-read can see it.
        pytest.param("page_bytes", False, id="page-bytes-drifted"),
    ],
)
def test_a_tree_that_changes_during_generation_fails_closed(
    site_repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    changed_fields: bool,
) -> None:
    real_lint = simple_document_site.lint_simple_document_tree
    calls = {"count": 0}

    def mutating_lint(repository: Path, policy: SimpleDocumentLayoutPolicy):  # type: ignore[no-untyped-def]
        calls["count"] += 1
        if calls["count"] == 2:
            if mutation == "second_lint":
                (site_repository / "docs/runbooks/evaluation.md").unlink()
            elif mutation == "codeowners_owner":
                _write(site_repository, "docs/CODEOWNERS", "* @platform-team\n")
        result = real_lint(repository, policy)
        if calls["count"] == 2 and mutation == "page_bytes":
            # Same title, same links, same status: nothing the lint reports.
            _write(
                site_repository,
                "docs/runbooks/evaluation.md",
                "# Evaluation\n\nHow to evaluate, revised.\n",
            )
        return result

    monkeypatch.setattr(simple_document_site, "lint_simple_document_tree", mutating_lint)

    with pytest.raises(RCPError) as error:
        _manifest(site_repository)

    assert error.value.code == "document_site_tree_changed"
    context = error.value.context
    if changed_fields:
        assert context["changed_fields"]
        # Concise facts, not two copies of every finding.
        assert set(context["initial"]) == {
            "terminal_result",
            "checked_files",
            "documents",
            "structured_documents",
            "assets",
            "codeowners_path",
            "invalid_findings",
        }
        if mutation == "codeowners_owner":
            assert "document_facts" in context["changed_fields"]
            # Both lints pass; only the resolved owners moved.
            assert context["initial"]["terminal_result"] == "passed"
            assert context["final"]["terminal_result"] == "passed"
    else:
        assert context == {"path": "docs/runbooks/evaluation.md"}


def test_generated_links_keep_every_safely_resolved_target() -> None:
    # The shipped renderers emit no Markdown links today, so this pins the
    # resolver the manifest uses rather than a particular rendered document.
    links = simple_document_site._rendered_links(
        relative="docs/design/thing.md",
        content=(
            b"# Thing\n\n"
            b"[gone](evidence/missing.md)\n"
            b"[here](../runbooks/evaluation.md)\n"
            b"[outside the document root](../../CONTRIBUTING.md)\n"
            b"[external](https://example.com/a.md)\n"
            b"[fragment](#section)\n"
            b"[site absolute](/cloud/guide)\n"
            b"[escaping the repository](../../../elsewhere.md)\n"
        ),
    )

    # A resolvable target is recorded whether or not it exists right now:
    # dropping it would hide a broken link from whatever builds the site.
    assert links == (
        "docs/design/evidence/missing.md",
        "docs/runbooks/evaluation.md",
        "CONTRIBUTING.md",
    )


def _valid_manifest_payload() -> dict[str, Any]:
    """Build one minimal, consistent payload the way the service builds one."""

    page = SimpleDocumentSitePage(
        path="docs/design/thing.md",
        kind="structured",
        section="design",
        section_relative_path="thing.md",
        source_path="docs/design/thing.yaml",
        contract="design-document",
        classification="design/architecture:document",
        title="Thing",
        lifecycle="draft",
        git_history_present=False,
        content_digest="sha256:" + "b" * 64,
        source_digest="sha256:" + "c" * 64,
    )
    payload: dict[str, Any] = {
        "schema_version": "0.1",
        "manifest_kind": "simple_document_site_manifest",
        "policy_version": 2,
        "document_root": "docs",
        "repository_head": None,
        "repository_state": "clean",
        "repository_remote": None,
        "policy_digest": "sha256:" + "a" * 64,
        "sections": [SimpleDocumentSiteSection(path="design").model_dump(mode="json")],
        "pages": [page.model_dump(mode="json")],
        "assets": [],
        "excluded_paths": [
            SimpleDocumentSiteExcludedPath(
                path="docs/design/thing.yaml",
                reason="structured_source",
                page_path="docs/design/thing.md",
            ).model_dump(mode="json")
        ],
    }
    payload["manifest_digest"] = canonical_digest(payload)
    return payload


@pytest.mark.parametrize(
    ("mutate", "fragment"),
    [
        pytest.param(
            lambda payload: payload["pages"][0].update({"section": "runbooks"}),
            "section",
            id="page-section-not-declared",
        ),
        pytest.param(
            lambda payload: payload["pages"][0].update(
                {"section_relative_path": "elsewhere/thing.md"}
            ),
            "section-relative path",
            id="section-relative-path-mismatch",
        ),
        pytest.param(
            lambda payload: payload.update({"excluded_paths": []}),
            "one-to-one",
            id="missing-source-exclusion",
        ),
        pytest.param(
            # Nothing else constrains repository_state, so only the digest can
            # notice that the payload was edited after it was sealed.
            lambda payload: payload.update({"repository_state": "dirty"}),
            "digest",
            id="stale-digest",
        ),
        pytest.param(
            lambda payload: payload["pages"][0].update({"locked": True}),
            "no review date, lock, or dependency",
            id="structured-page-claims-a-lock",
        ),
        pytest.param(
            lambda payload: payload["excluded_paths"].append(
                {"path": "elsewhere/notes.yaml", "reason": "codeowners"}
            ),
            "only excludes paths under its root",
            id="exclusion-outside-the-root",
        ),
    ],
)
def test_the_model_rejects_an_inconsistent_projection(mutate: Any, fragment: str) -> None:
    payload = _valid_manifest_payload()
    assert SimpleDocumentSiteManifest.model_validate(payload)

    mutate(payload)
    with pytest.raises(ValidationError) as error:
        SimpleDocumentSiteManifest.model_validate(payload)

    assert fragment in str(error.value)


def test_markdown_is_recognised_by_suffix_case_the_way_the_tree_does() -> None:
    # The tree classifies on a lowercased suffix, so README.MD is an ordinary
    # page there and must be one here too.
    page = SimpleDocumentSitePage(
        path="docs/README.MD",
        kind="root",
        title="Readme",
        status="active",
        git_history_present=False,
        content_digest="sha256:" + "d" * 64,
    )
    assert page.path == "docs/README.MD"

    with pytest.raises(ValidationError, match="never a static asset"):
        SimpleDocumentSiteAsset(
            path="docs/runbooks/README.MD",
            section="runbooks",
            section_relative_path="README.MD",
            content_digest="sha256:" + "d" * 64,
        )


def test_both_version_two_schemas_are_registered() -> None:
    assert SCHEMA_MODELS["simple-document-layout-policy"] is SimpleDocumentLayoutPolicy
    assert SCHEMA_MODELS["simple-document-site-manifest"] is SimpleDocumentSiteManifest


def test_the_history_helper_separates_committed_from_uncommitted_paths(
    site_repository: Path,
) -> None:
    _write(site_repository, "docs/runbooks/uncommitted.md", "# Uncommitted\n")
    git_repository = discover_repository(site_repository)

    committed = last_commit_timestamp(git_repository, "docs/runbooks/evaluation.md")
    uncommitted = last_commit_timestamp(git_repository, "docs/runbooks/uncommitted.md")

    assert committed.present is True
    assert committed.last_edited_at is not None
    assert committed.last_edited_at.isoformat() == "2026-06-07T08:09:10+00:00"
    # No filesystem fallback: a path Git has never recorded has no edit time.
    assert uncommitted == type(uncommitted)(present=False, last_edited_at=None)


def test_the_history_helper_separates_an_unborn_repository_from_a_broken_one(
    tmp_path: Path,
) -> None:
    root = tmp_path / "unborn"
    root.mkdir()
    _git(root, "init", "--quiet")
    _write(root, "docs/README.md", "# Readme\n")
    git_repository = discover_repository(root)

    # An unborn branch is a real answer: nothing has been committed yet.
    unborn = last_commit_timestamp(git_repository, "docs/README.md")
    assert unborn.present is False
    assert unborn.last_edited_at is None

    # A HEAD Git cannot resolve looks identical to "unborn" through rev-parse
    # alone, so the checked status has to turn it into a failure instead of
    # reporting every document as never edited.
    (root / ".git" / "HEAD").write_text("not-a-valid-ref\n", encoding="utf-8")
    with pytest.raises(RCPError) as error:
        last_commit_timestamp(git_repository, "docs/README.md")

    assert error.value.code == "git_command_failed"


# --------------------------------------------------------------------------
# CLI wiring
# --------------------------------------------------------------------------


def test_the_cli_streams_and_replaces_a_version_two_manifest(
    site_repository: Path,
    tmp_path: Path,
) -> None:
    runner = CliRunner()

    streamed = runner.invoke(app, ["doc", "site-manifest", "-C", str(site_repository)])

    assert streamed.exit_code == 0, streamed.stdout + str(streamed.stderr)
    payload = json.loads(streamed.stdout)
    assert payload["manifest_kind"] == "simple_document_site_manifest"
    assert payload["policy_version"] == 2
    # The command publishes exactly what the service builds, with nothing added.
    assert streamed.stdout.encode("utf-8") == render_simple_document_site_manifest(
        _manifest(site_repository)
    )

    output = tmp_path / "site-manifest.json"
    first = runner.invoke(
        app,
        ["doc", "site-manifest", "-C", str(site_repository), "--output-file", str(output)],
    )
    assert first.exit_code == 0, first.stdout + str(first.stderr)
    written = output.read_bytes()
    assert json.loads(written)["repository_state"] == "clean"

    repeated = runner.invoke(
        app,
        ["doc", "site-manifest", "-C", str(site_repository), "--output-file", str(output)],
    )
    # An unchanged tree produces the same bytes, so replacement is a no-op.
    assert repeated.exit_code == 0, repeated.stdout + str(repeated.stderr)
    assert "Unchanged:" in repeated.stdout
    assert output.read_bytes() == written

    _write(site_repository, "docs/runbooks/draft-note.md", "# Draft note\n")
    updated = runner.invoke(
        app,
        ["doc", "site-manifest", "-C", str(site_repository), "--output-file", str(output)],
    )
    assert updated.exit_code == 0, updated.stdout + str(updated.stderr)
    assert "Updated:" in updated.stdout
    assert json.loads(output.read_bytes())["repository_state"] == "dirty"


def test_require_clean_refuses_a_dirty_version_two_repository(
    site_repository: Path,
) -> None:
    runner = CliRunner()
    command = ["doc", "site-manifest", "-C", str(site_repository), "--require-clean"]

    accepted = runner.invoke(app, command)
    assert accepted.exit_code == 0, accepted.stdout + str(accepted.stderr)
    assert json.loads(accepted.stdout)["repository_state"] == "clean"

    _write(site_repository, "docs/runbooks/draft-note.md", "# Draft note\n")
    rejected = runner.invoke(app, command)

    # The same code and remediation both policy versions have always used.
    assert rejected.exit_code == 2
    assert "document_site_repository_dirty" in rejected.stderr
    assert "Commit or discard the relevant changes" in rejected.stderr


def test_an_explicit_policy_file_drives_the_version_two_manifest(
    site_repository: Path,
    tmp_path: Path,
) -> None:
    # A section the in-repository policy does not declare, written outside the
    # repository so the tree stays clean.
    external = tmp_path / "external-docs.yaml"
    external.write_text(
        dump_yaml(_policy(sections=[*SECTIONS, {"path": "notes"}])),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        [
            "doc", "site-manifest",
            "-C", str(site_repository),
            "--policy-file", str(external),
        ],
    )

    assert result.exit_code == 0, result.stdout + str(result.stderr)
    payload = json.loads(result.stdout)
    assert [section["path"] for section in payload["sections"]] == [
        "design",
        "runbooks",
        "notes",
    ]
    assert payload["policy_digest"] != _manifest(site_repository).policy_digest


@pytest.mark.parametrize(
    ("contract", "title"),
    [
        ("simple-document-layout-policy", "SimpleDocumentLayoutPolicy"),
        ("simple-document-site-manifest", "SimpleDocumentSiteManifest"),
    ],
)
def test_doc_schema_discovers_both_version_two_contracts(
    contract: str,
    title: str,
) -> None:
    result = CliRunner().invoke(app, ["doc", "schema", "--contract", contract])

    assert result.exit_code == 0, result.stdout + str(result.stderr)
    assert result.stdout.encode("utf-8") == generate_schema_files()[
        f"{contract}.schema.json"
    ]
    assert json.loads(result.stdout)["title"] == title
