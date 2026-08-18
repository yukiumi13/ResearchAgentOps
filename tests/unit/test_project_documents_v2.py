from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from researchctl.cli import app
from researchctl.domain.models import (
    SIMPLE_MARKDOWN_FRONTMATTER_FIELDS,
    SimpleDocumentLayoutPolicy,
)
from researchctl.errors import RCPError
from researchctl.serialization import dump_yaml
from researchctl.services.document_policy import (
    build_effective_policy,
    select_policy_version,
)
from researchctl.services.markdown_source import first_heading_title, link_destinations
from researchctl.services.project_documents_v2 import (
    lint_simple_document_tree,
    resolve_repository_link,
    scaffold_simple_document,
)

LEGACY_ROUTE: dict[str, Any] = {
    "classification": "reference/project:document",
    "document_type": "reference",
    "directory": "docs/reference",
    "contract": "markdown-frontmatter",
    "rationale": "The fixture keeps long-lived references here.",
}


def _policy(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "version": 2,
        "root": "docs",
        "sections": [{"path": "design"}, {"path": "runbooks"}],
        "root_pages": ["README.md"],
        "max_depth": 3,
    }
    payload.update(overrides)
    return payload


def _repository(tmp_path: Path, policy: dict[str, Any] | None = None) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".researchctl-docs.yaml").write_text(
        dump_yaml(policy if policy is not None else _policy()),
        encoding="utf-8",
    )
    (tmp_path / "docs").mkdir(exist_ok=True)
    (tmp_path / "docs/README.md").write_text("# Documentation\n", encoding="utf-8")
    for section in (policy if policy is not None else _policy())["sections"]:
        (tmp_path / "docs" / section["path"]).mkdir(parents=True, exist_ok=True)
    return tmp_path


def _simple(payload: dict[str, Any] | None = None) -> SimpleDocumentLayoutPolicy:
    effective = build_effective_policy(payload if payload is not None else _policy())
    assert effective.simple is not None
    return effective.simple


def _codes(result: Any) -> list[str]:
    return [finding.code for finding in result.findings]


def _invalid(result: Any) -> list[str]:
    return [finding.code for finding in result.findings if finding.kind == "invalid"]


# --------------------------------------------------------------------------
# Policy shape and version dispatch
# --------------------------------------------------------------------------


def test_section_directory_is_the_only_statement_of_document_type() -> None:
    policy = _simple(
        _policy(
            sections=[
                {
                    "path": "design",
                    "structured": {
                        "contract": "design-document",
                        "classification": "design/architecture:document",
                    },
                },
                {"path": "runbooks"},
            ]
        )
    )

    assert [section.path for section in policy.sections] == ["design", "runbooks"]
    assert policy.section_directory(policy.sections[0]) == "docs/design"
    assert policy.root_page_paths() == ("docs/README.md",)
    # An ordinary section carries no label, type, or directory of its own.
    assert policy.sections[1].structured is None
    assert set(policy.sections[1].model_dump()) == {"path", "structured"}
    # Only a structured envelope may keep a compatibility classification.
    assert policy.sections[0].structured is not None
    assert policy.sections[0].structured.classification == "design/architecture:document"


def test_section_lookup_ignores_root_files_and_unknown_directories() -> None:
    policy = _simple()

    assert policy.section_for_path("docs/design/a.md") is not None
    assert policy.section_for_path("docs/design/nested/deep/a.md") is not None
    assert policy.section_for_path("docs/README.md") is None
    assert policy.section_for_path("docs/reference/a.md") is None
    assert policy.section_for_path("other/design/a.md") is None


def test_absent_or_explicit_version_one_keeps_the_original_contract() -> None:
    legacy_payload = {"routes": [LEGACY_ROUTE], "root_files": []}

    assert select_policy_version(legacy_payload) == 1
    assert select_policy_version({**legacy_payload, "version": 1}) == 1

    without_version = build_effective_policy(legacy_payload)
    with_version = build_effective_policy({**legacy_payload, "version": 1})

    assert without_version.version == 1
    assert without_version.simple is None
    assert without_version.legacy is not None
    # Declaring version 1 explicitly must not change the validated policy.
    assert with_version.legacy == without_version.legacy


def test_version_two_selects_the_simple_contract() -> None:
    effective = build_effective_policy(_policy())

    assert effective.version == 2
    assert effective.is_simple
    assert effective.legacy is None
    assert effective.root == "docs"


@pytest.mark.parametrize("declared", [0, 3, 99, "2", True, None, 2.0])
def test_unsupported_policy_version_fails_closed(declared: object) -> None:
    with pytest.raises(RCPError) as error:
        build_effective_policy({"version": declared, "sections": [{"path": "design"}]})

    assert error.value.code == "document_policy_version_unsupported"
    assert error.value.context["supported_versions"] == [1, 2]


def test_version_two_command_is_refused_on_a_version_one_policy() -> None:
    effective = build_effective_policy({"routes": [LEGACY_ROUTE], "root_files": []})

    with pytest.raises(RCPError) as error:
        effective.require_simple(command="doc tree")

    assert error.value.code == "document_policy_version_unsupported_command"


def test_version_one_payload_declared_as_version_two_reports_both_problems() -> None:
    with pytest.raises(ValidationError) as error:
        build_effective_policy({"version": 2, "routes": [LEGACY_ROUTE]})

    locations = {detail["loc"][0] for detail in error.value.errors()}
    assert locations == {"sections", "routes"}


def test_simple_policy_rejects_ambiguous_layouts() -> None:
    with pytest.raises(ValidationError):
        _simple(_policy(sections=[{"path": "design"}, {"path": "design"}]))
    with pytest.raises(ValidationError):
        _simple(_policy(root_pages=["design/README.md"]))
    with pytest.raises(ValidationError):
        _simple(_policy(root_pages=["README.txt"]))
    with pytest.raises(ValidationError):
        _simple(_policy(sections=[]))
    with pytest.raises(ValidationError):
        # A section is one directory name, never a nested path.
        _simple(_policy(sections=[{"path": "design/deep"}]))
    with pytest.raises(ValidationError):
        _simple(_policy(agent_guides=[{"path": "docs/CLAUDE.md", "format": "claude"}]))


# --------------------------------------------------------------------------
# Markdown parsing
# --------------------------------------------------------------------------


def test_title_comes_from_the_first_level_one_heading() -> None:
    assert first_heading_title("# Real Title\n\n# Later\n") == "Real Title"
    assert first_heading_title("## Sub\n\n# Real\n") == "Real"
    assert first_heading_title("Setext\n======\n") == "Setext"
    assert first_heading_title("# Title with `code` and *emphasis*") == (
        "Title with code and emphasis"
    )
    assert first_heading_title("no heading at all\n") is None
    # A heading inside a fenced block is code, not a title.
    assert first_heading_title("```\n# Not A Title\n```\n") is None
    # So is an indented code block.
    assert first_heading_title("    # Not A Title\n") is None


def test_link_extraction_uses_the_parser_and_skips_code() -> None:
    text = (
        "# Title\n\n"
        "[inline](./a.md) and ![image](img/b.png) and [ref][r].\n\n"
        "`[incode](./never.md)` stays code.\n\n"
        "```\n[fenced](./never.md)\n```\n\n"
        "[r]: ./c.md\n"
    )

    assert link_destinations(text) == ("./a.md", "img/b.png", "./c.md")


@pytest.mark.parametrize(
    ("destination", "expected"),
    [
        ("https://example.com/a.md", None),
        ("//example.com/a.md", None),
        ("mailto:a@example.com", None),
        ("#anchor-only", None),
        ("./sibling.md", "docs/design/sibling.md"),
        ("nested/child.md", "docs/design/nested/child.md"),
        ("../runbooks/run.md", "docs/runbooks/run.md"),
        ("/src/module.py", None),
        ("/cloud/xcloud/guide", None),
        ("/learning/deepmind/xmanager2", None),
        ("sibling.md#section", "docs/design/sibling.md"),
        ("with%20space.md", "docs/design/with space.md"),
        ("../../../escape.md", "../escape.md"),
    ],
)
def test_link_resolution_maps_only_repository_local_targets(
    destination: str,
    expected: str | None,
) -> None:
    assert (
        resolve_repository_link(
            document_relative="docs/design/doc.md",
            destination=destination,
        )
        == expected
    )


# --------------------------------------------------------------------------
# Ordinary Markdown documents
# --------------------------------------------------------------------------


def test_document_without_frontmatter_is_active_untagged_and_unreviewed(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    (repository / "docs/design/plain.md").write_text(
        "# Plain Design\n\nBody text.\n",
        encoding="utf-8",
    )

    result = lint_simple_document_tree(repository, _simple())

    assert result.passed
    assert _invalid(result) == []
    assert result.documents == 2


def test_missing_level_one_heading_is_the_only_title_failure(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    (repository / "docs/design/untitled.md").write_text(
        "## Only a subheading\n\nBody.\n",
        encoding="utf-8",
    )

    result = lint_simple_document_tree(repository, _simple())

    assert _invalid(result) == ["document_title_missing"]


def test_metadata_is_read_and_locked_is_separate_from_status(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    (repository / "docs/design/meta.md").write_text(
        "---\n"
        "status: draft\n"
        "tags: [alpha, beta]\n"
        "reviewed_on: 2026-08-01\n"
        "locked: true\n"
        "---\n"
        "\n"
        "# Meta\n",
        encoding="utf-8",
    )

    result = lint_simple_document_tree(repository, _simple())
    assert result.passed

    checked = CliRunner().invoke(
        app,
        [
            "doc",
            "check",
            str(repository / "docs/design/meta.md"),
            "--project",
            str(repository),
            "--json",
        ],
    )
    data = json.loads(checked.stdout)["data"]
    assert checked.exit_code == 0
    assert data["status"] == "draft"
    assert data["locked"] is True
    assert data["tags"] == ["alpha", "beta"]
    assert data["reviewed_on"] == "2026-08-01"
    assert data["title"] == "Meta"


@pytest.mark.parametrize(
    ("field", "fragment"),
    [
        ("title", "first level-one heading"),
        ("type", "section directory is the document type"),
        ("owner", "CODEOWNERS"),
        ("last_updated", "derived from Git history"),
        ("validity", "status: draft|active|deprecated|archived"),
        ("classification", "section directory is the taxonomy"),
        ("relations", "depends_on and superseded_by"),
    ],
)
def test_classification_route_frontmatter_names_its_replacement(
    tmp_path: Path,
    field: str,
    fragment: str,
) -> None:
    repository = _repository(tmp_path)
    (repository / "docs/design/legacy.md").write_text(
        f"---\n{field}: placeholder\n---\n\n# Legacy\n",
        encoding="utf-8",
    )

    result = lint_simple_document_tree(repository, _simple())

    finding = next(
        item
        for item in result.findings
        if item.code == "document_legacy_frontmatter_field"
    )
    assert finding.path == f"docs/design/legacy.md:{field}"
    assert fragment in finding.message


def test_unknown_frontmatter_field_lists_the_accepted_fields(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    (repository / "docs/design/typo.md").write_text(
        "---\nstatuss: draft\n---\n\n# Typo\n",
        encoding="utf-8",
    )

    result = lint_simple_document_tree(repository, _simple())

    finding = next(
        item
        for item in result.findings
        if item.code == "document_frontmatter_unknown_field"
    )
    assert finding.path == "docs/design/typo.md:statuss"
    for accepted in SIMPLE_MARKDOWN_FRONTMATTER_FIELDS:
        assert accepted in finding.message


def test_frontmatter_schema_errors_name_the_offending_field(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    (repository / "docs/design/bad-status.md").write_text(
        "---\nstatus: retired\n---\n\n# Bad Status\n",
        encoding="utf-8",
    )
    (repository / "docs/design/bad-tags.md").write_text(
        "---\ntags: [same, same]\n---\n\n# Bad Tags\n",
        encoding="utf-8",
    )

    result = lint_simple_document_tree(repository, _simple())

    paths = {
        finding.path
        for finding in result.findings
        if finding.code == "document_schema_validation_error"
    }
    assert "docs/design/bad-status.md:status" in paths
    assert any(path.startswith("docs/design/bad-tags.md") for path in paths)


def test_non_mapping_frontmatter_fails_closed(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    (repository / "docs/design/listy.md").write_text(
        "---\n- one\n- two\n---\n\n# Listy\n",
        encoding="utf-8",
    )

    result = lint_simple_document_tree(repository, _simple())

    assert "document_frontmatter_invalid" in _invalid(result)


def test_superseded_by_needs_an_archived_status_and_an_existing_target(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    (repository / "docs/design/new.md").write_text("# New\n", encoding="utf-8")
    (repository / "docs/design/old.md").write_text(
        "---\nsuperseded_by: docs/design/new.md\n---\n\n# Old\n",
        encoding="utf-8",
    )
    (repository / "docs/design/gone.md").write_text(
        "---\nstatus: archived\nsuperseded_by: docs/design/absent.md\n---\n\n# Gone\n",
        encoding="utf-8",
    )

    result = lint_simple_document_tree(repository, _simple())

    assert "document_superseded_status_mismatch" in _invalid(result)
    assert "document_superseded_by_target_missing" in _invalid(result)

    (repository / "docs/design/old.md").write_text(
        "---\nstatus: deprecated\nsuperseded_by: docs/design/new.md\n---\n\n# Old\n",
        encoding="utf-8",
    )
    (repository / "docs/design/gone.md").unlink()
    assert lint_simple_document_tree(repository, _simple()).passed


def test_declared_dependencies_must_exist(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    (repository / "src").mkdir()
    (repository / "src/module.py").write_text("x = 1\n", encoding="utf-8")
    (repository / "docs/design/deps.md").write_text(
        "---\ndepends_on: [src/module.py, src/absent.py]\n---\n\n# Deps\n",
        encoding="utf-8",
    )

    result = lint_simple_document_tree(repository, _simple())

    assert _invalid(result) == ["document_depends_on_target_missing"]


def test_repository_local_links_are_checked_and_external_links_ignored(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    (repository / "docs/runbooks/run.md").write_text("# Run\n", encoding="utf-8")
    (repository / "docs/design/links.md").write_text(
        "# Links\n\n"
        "[ok](../runbooks/run.md), [external](https://example.com/x.md), "
        "[anchor](#here), [broken](./absent.md).\n\n"
        "```\n[fenced](./also-absent.md)\n```\n",
        encoding="utf-8",
    )

    result = lint_simple_document_tree(repository, _simple())

    broken = [
        finding
        for finding in result.findings
        if finding.code == "document_link_target_missing"
    ]
    assert len(broken) == 1
    assert broken[0].path == "docs/design/links.md:./absent.md"


def test_links_leaving_the_repository_are_a_warning_not_a_failure(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    (repository / "docs/design/outside.md").write_text(
        "# Outside\n\n[up](../../../elsewhere.md)\n",
        encoding="utf-8",
    )

    result = lint_simple_document_tree(repository, _simple())

    assert result.passed
    assert "document_link_outside_repository" in _codes(result)


# --------------------------------------------------------------------------
# Layout rules
# --------------------------------------------------------------------------


def test_nested_directories_need_no_new_section(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    (repository / "docs/runbooks/container/gpu").mkdir(parents=True)
    (repository / "docs/runbooks/container/gpu/launch.md").write_text(
        "# Launch\n",
        encoding="utf-8",
    )

    result = lint_simple_document_tree(repository, _simple())

    assert result.passed
    assert result.documents == 2


def test_unknown_section_names_the_accepted_sections(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    (repository / "docs/reference").mkdir()
    (repository / "docs/reference/guide.md").write_text("# Guide\n", encoding="utf-8")

    result = lint_simple_document_tree(repository, _simple())

    finding = next(
        item for item in result.findings if item.code == "document_section_unknown"
    )
    assert finding.path == "docs/reference/guide.md"
    assert "'reference' is not an accepted section" in finding.message
    assert "design, runbooks" in finding.message


def test_depth_limit_is_measured_below_the_section(tmp_path: Path) -> None:
    repository = _repository(tmp_path, _policy(max_depth=1))
    (repository / "docs/design/one").mkdir(parents=True)
    (repository / "docs/design/one/ok.md").write_text("# Ok\n", encoding="utf-8")
    (repository / "docs/design/one/two").mkdir(parents=True)
    (repository / "docs/design/one/two/deep.md").write_text("# Deep\n", encoding="utf-8")

    result = lint_simple_document_tree(repository, _simple(_policy(max_depth=1)))

    invalid = [
        finding for finding in result.findings if finding.kind == "invalid"
    ]
    assert [finding.path for finding in invalid] == ["docs/design/one/two/deep.md"]
    assert invalid[0].code == "document_path_too_deep"


def test_root_files_must_be_declared_root_pages(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    (repository / "docs/stray.md").write_text("# Stray\n", encoding="utf-8")

    result = lint_simple_document_tree(repository, _simple())

    finding = next(
        item
        for item in result.findings
        if item.code == "document_root_file_unclassified"
    )
    assert finding.path == "docs/stray.md"


def test_missing_root_page_and_missing_section_directory_are_distinguished(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    (repository / "docs/README.md").unlink()
    (repository / "docs/runbooks").rmdir()

    result = lint_simple_document_tree(repository, _simple())

    assert "document_root_page_missing" in _invalid(result)
    warnings = {
        finding.code for finding in result.findings if finding.kind == "warning"
    }
    assert "document_section_directory_missing" in warnings


def test_structured_section_content_is_reported_as_unvalidated(tmp_path: Path) -> None:
    payload = _policy(
        sections=[
            {
                "path": "design",
                "structured": {
                    "contract": "design-document",
                    "classification": "design/architecture:document",
                },
            },
            {"path": "runbooks"},
        ]
    )
    repository = _repository(tmp_path, payload)
    (repository / "docs/design/thing.yaml").write_text("{}\n", encoding="utf-8")
    (repository / "docs/design/thing.md").write_text("# Thing\n", encoding="utf-8")

    result = lint_simple_document_tree(repository, _simple(payload))

    assert result.passed
    warning = next(
        item
        for item in result.findings
        if item.code == "document_structured_section_unvalidated"
    )
    assert warning.path == "docs/design"
    assert result.documents == 1


def test_ordinary_sections_reject_non_markdown_files(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    (repository / "docs/design/data.yaml").write_text("a: 1\n", encoding="utf-8")

    result = lint_simple_document_tree(repository, _simple())

    assert _invalid(result) == ["document_extension_invalid"]


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def test_doc_policy_lint_reports_the_simple_policy_shape(tmp_path: Path) -> None:
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text(
        dump_yaml(
            _policy(
                sections=[
                    {
                "path": "design",
                "structured": {
                    "contract": "design-document",
                    "classification": "design/architecture:document",
                },
            },
                    {"path": "runbooks"},
                ],
                ownership={"source": "codeowners", "required": True},
            )
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(app, ["doc", "policy-lint", str(policy_file), "--json"])

    assert result.exit_code == 0
    data = json.loads(result.stdout)["data"]
    assert data["policy_version"] == 2
    assert data["sections"] == ["design", "runbooks"]
    assert data["structured_sections"] == ["design"]
    assert data["ownership"] == {"source": "codeowners", "required": True}


def test_doc_policy_lint_rejects_an_unsupported_version(tmp_path: Path) -> None:
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text(dump_yaml(_policy(version=7)), encoding="utf-8")

    result = CliRunner().invoke(app, ["doc", "policy-lint", str(policy_file), "--json"])

    assert result.exit_code == 2
    assert '"code": "document_policy_version_unsupported"' in result.stdout


def test_doc_tree_validates_a_version_two_repository(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    (repository / "docs/design/one.md").write_text("# One\n", encoding="utf-8")

    result = CliRunner().invoke(
        app,
        ["doc", "tree", "--project", str(repository), "--json"],
    )

    assert result.exit_code == 0
    data = json.loads(result.stdout)["data"]
    assert data["policy_version"] == 2
    assert data["documents"] == 2
    assert data["terminal_result"] == "passed"
    assert not (repository / ".research").exists()


def test_doc_tree_human_output_names_the_policy_version(tmp_path: Path) -> None:
    repository = _repository(tmp_path)

    result = CliRunner().invoke(app, ["doc", "tree", "--project", str(repository)])

    assert result.exit_code == 0
    assert "Policy version: 2" in result.stdout


def test_doc_check_rejects_a_path_outside_every_section(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    (repository / "docs/reference").mkdir()
    (repository / "docs/reference/guide.md").write_text("# Guide\n", encoding="utf-8")

    result = CliRunner().invoke(
        app,
        [
            "doc",
            "check",
            str(repository / "docs/reference/guide.md"),
            "--project",
            str(repository),
            "--json",
        ],
    )

    assert result.exit_code == 2
    assert '"code": "document_section_unknown"' in result.stdout


def test_doc_check_reports_links_and_dependencies(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    (repository / "docs/runbooks/run.md").write_text("# Run\n", encoding="utf-8")
    (repository / "docs/design/linked.md").write_text(
        "---\ndepends_on: [docs/runbooks/run.md]\n---\n\n"
        "# Linked\n\n[run](../runbooks/run.md)\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        [
            "doc",
            "check",
            str(repository / "docs/design/linked.md"),
            "--project",
            str(repository),
            "--json",
        ],
    )

    assert result.exit_code == 0
    data = json.loads(result.stdout)["data"]
    assert data["section"] == "design"
    assert data["document_type"] == "design"
    assert data["contract"] == "markdown"
    assert data["depends_on"] == ["docs/runbooks/run.md"]
    assert data["links"] == ["docs/runbooks/run.md"]


def test_doc_scaffold_emits_a_document_that_passes_tree_lint(tmp_path: Path) -> None:
    repository = _repository(tmp_path)

    scaffold = CliRunner().invoke(
        app,
        [
            "doc",
            "scaffold",
            "--type",
            "design",
            "--title",
            "Latent Encoder Split",
            "--project",
            str(repository),
        ],
    )
    assert scaffold.exit_code == 0
    assert "# Latent Encoder Split" in scaffold.stdout
    # The heading is the title; the frontmatter block never restates it.
    assert "title:" not in scaffold.stdout.split("---\n", maxsplit=2)[1]

    (repository / "docs/design/latent-encoder-split.md").write_text(
        scaffold.stdout,
        encoding="utf-8",
    )
    tree = CliRunner().invoke(
        app,
        ["doc", "tree", "--project", str(repository), "--json"],
    )
    assert tree.exit_code == 0


def test_doc_scaffold_rejects_a_directory_that_is_not_a_section(tmp_path: Path) -> None:
    repository = _repository(tmp_path)

    result = CliRunner().invoke(
        app,
        [
            "doc",
            "scaffold",
            "--type",
            "reference",
            "--title",
            "Nope",
            "--project",
            str(repository),
        ],
    )

    assert result.exit_code == 2
    assert "document_type_unaccepted" in result.stdout + str(result.stderr)


def test_scaffold_output_is_a_valid_minimal_document() -> None:
    rendered = scaffold_simple_document(title="Example").decode("utf-8")

    assert rendered.startswith("---\n")
    assert "status: draft" in rendered
    assert first_heading_title(rendered.split("---\n", maxsplit=2)[2]) == "Example"


@pytest.mark.parametrize(
    "command",
    [
        ["doc", "index"],
        ["doc", "site-manifest"],
        ["doc", "agent-guide"],
    ],
)
def test_version_two_fails_closed_for_unimplemented_commands(
    tmp_path: Path,
    command: list[str],
) -> None:
    repository = _repository(tmp_path)

    result = CliRunner().invoke(app, [*command, "--project", str(repository)])

    assert result.exit_code == 2
    assert "document_policy_version_unsupported_command" in (
        result.stdout + str(result.stderr)
    )


def test_version_two_refuses_a_frozen_baseline_until_it_is_implemented(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "head")
    baseline = _repository(tmp_path / "base")

    result = CliRunner().invoke(
        app,
        [
            "doc",
            "tree",
            "--project",
            str(repository),
            "--baseline-project",
            str(baseline),
            "--json",
        ],
    )

    assert result.exit_code == 2
    assert '"code": "document_baseline_unsupported"' in result.stdout


# --------------------------------------------------------------------------
# Version 1 regression
# --------------------------------------------------------------------------


def _legacy_repository(tmp_path: Path, *, version_key: bool) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    payload: dict[str, Any] = {
        "root": "docs",
        "root_files": ["docs/README.md"],
        "routes": [LEGACY_ROUTE],
    }
    if version_key:
        payload["version"] = 1
    (tmp_path / ".researchctl-docs.yaml").write_text(
        dump_yaml(payload),
        encoding="utf-8",
    )
    (tmp_path / "docs/reference").mkdir(parents=True)
    (tmp_path / "docs/README.md").write_text("# Docs\n", encoding="utf-8")
    (tmp_path / "docs/reference/note.md").write_text(
        "---\n"
        "type: reference\n"
        "title: Note\n"
        "owner: person:reviewer\n"
        "last_updated: 2026-08-01\n"
        "validity: valid\n"
        "---\n"
        "\n"
        "# Note\n",
        encoding="utf-8",
    )
    return tmp_path


def _tree_data(repository: Path) -> dict[str, Any]:
    result = CliRunner().invoke(
        app,
        ["doc", "tree", "--project", str(repository), "--json"],
    )
    assert result.exit_code == 0, result.stdout
    payload: dict[str, Any] = json.loads(result.stdout)["data"]
    return payload


def test_version_one_tree_output_is_unchanged_and_carries_no_version_key(
    tmp_path: Path,
) -> None:
    implicit = _tree_data(_legacy_repository(tmp_path / "implicit", version_key=False))
    explicit = _tree_data(_legacy_repository(tmp_path / "explicit", version_key=True))

    assert implicit == explicit
    assert implicit == {
        "root": "docs",
        "checked_files": 2,
        "structured_documents": 0,
        "terminal_result": "passed",
        "findings": [],
    }


def test_version_one_policy_lint_output_is_unchanged(tmp_path: Path) -> None:
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text(
        dump_yaml({"root": "docs", "root_files": [], "routes": [LEGACY_ROUTE]}),
        encoding="utf-8",
    )

    result = CliRunner().invoke(app, ["doc", "policy-lint", str(policy_file), "--json"])

    assert result.exit_code == 0
    data = json.loads(result.stdout)["data"]
    assert "policy_version" not in data
    assert set(data) == {
        "path",
        "terminal_result",
        "routes",
        "agent_guides",
        "classification_depth",
        "max_depth",
    }


def test_structured_sections_refuse_check_and_scaffold_for_now(tmp_path: Path) -> None:
    payload = _policy(
        sections=[
            {
                "path": "design",
                "structured": {
                    "contract": "design-document",
                    "classification": "design/architecture:document",
                },
            },
            {"path": "runbooks"},
        ]
    )
    repository = _repository(tmp_path, payload)
    (repository / "docs/design/thing.yaml").write_text("{}\n", encoding="utf-8")

    checked = CliRunner().invoke(
        app,
        [
            "doc",
            "check",
            str(repository / "docs/design/thing.yaml"),
            "--project",
            str(repository),
            "--json",
        ],
    )
    assert checked.exit_code == 2
    assert '"code": "document_structured_check_unsupported"' in checked.stdout

    scaffolded = CliRunner().invoke(
        app,
        [
            "doc",
            "scaffold",
            "--type",
            "design",
            "--title",
            "Thing",
            "--project",
            str(repository),
        ],
    )
    assert scaffolded.exit_code == 2
    assert "document_structured_scaffold_unsupported" in (
        scaffolded.stdout + str(scaffolded.stderr)
    )


# --------------------------------------------------------------------------
# Frontmatter delimiters must fail closed, never degrade to "no frontmatter"
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "raw"),
    [
        ("unclosed", "---\nstatus: deprecated\n# Unclosed frontmatter\n"),
        ("unclosed_crlf", "---\r\nstatus: deprecated\r\n# Unclosed\r\n"),
        ("empty_block", "---\n---\n\n# Empty Block\n"),
        ("no_trailing_newline", "---\nstatus: draft\n---"),
        ("delimiter_only", "---\n"),
        ("corrupt_yaml", "---\nstatus: [unterminated\n---\n\n# Corrupt\n"),
        ("sequence_root", "---\n- one\n- two\n---\n\n# Sequence\n"),
        ("scalar_root", "---\njust a string\n---\n\n# Scalar\n"),
    ],
)
def test_declared_frontmatter_that_cannot_form_an_envelope_fails_closed(
    tmp_path: Path,
    name: str,
    raw: str,
) -> None:
    repository = _repository(tmp_path / name)
    (repository / "docs/design/broken.md").write_text(raw, encoding="utf-8", newline="")

    result = lint_simple_document_tree(repository, _simple())

    assert not result.passed, name
    assert "document_frontmatter_invalid" in _invalid(result), name


def test_valid_crlf_frontmatter_still_parses(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    (repository / "docs/design/windows.md").write_text(
        "---\r\nstatus: draft\r\ntags: [crlf]\r\n---\r\n\r\n# Windows Line Endings\r\n",
        encoding="utf-8",
        newline="",
    )

    result = lint_simple_document_tree(repository, _simple())

    assert result.passed
    assert _invalid(result) == []


def test_the_unclosed_frontmatter_probe_is_reported_by_the_cli(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    (repository / "docs/design/probe.md").write_text(
        "---\nstatus: deprecated\n# Unclosed frontmatter\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        ["doc", "tree", "--project", str(repository), "--json"],
    )

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["success"] is False
    codes = [finding["code"] for finding in payload["data"]["findings"]]
    assert "document_frontmatter_invalid" in codes


def test_a_document_with_no_delimiter_is_untouched(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    (repository / "docs/design/plain.md").write_text(
        "# Plain\n\nA thematic break below is body content.\n\n---\n\nMore.\n",
        encoding="utf-8",
    )

    result = lint_simple_document_tree(repository, _simple())

    assert result.passed


# --------------------------------------------------------------------------
# A configured ownership source cannot be silently ignored
# --------------------------------------------------------------------------


def test_absent_ownership_configuration_reports_nothing(tmp_path: Path) -> None:
    repository = _repository(tmp_path)

    result = lint_simple_document_tree(repository, _simple())

    assert result.passed
    assert "document_ownership_not_implemented" not in _codes(result)


def test_optional_ownership_warns_once(tmp_path: Path) -> None:
    payload = _policy(ownership={"source": "codeowners", "required": False})
    repository = _repository(tmp_path, payload)
    (repository / "docs/design/a.md").write_text("# A\n", encoding="utf-8")
    (repository / "docs/design/b.md").write_text("# B\n", encoding="utf-8")

    result = lint_simple_document_tree(repository, _simple(payload))

    ownership = [
        finding
        for finding in result.findings
        if finding.code == "document_ownership_not_implemented"
    ]
    assert result.passed
    assert len(ownership) == 1
    assert ownership[0].kind == "warning"
    assert ownership[0].path == "ownership"


def test_required_ownership_fails_closed_until_codeowners_lands(
    tmp_path: Path,
) -> None:
    payload = _policy(ownership={"source": "codeowners", "required": True})
    repository = _repository(tmp_path, payload)
    (repository / "docs/design/a.md").write_text("# A\n", encoding="utf-8")

    result = lint_simple_document_tree(repository, _simple(payload))

    ownership = [
        finding
        for finding in result.findings
        if finding.code == "document_ownership_not_implemented"
    ]
    assert not result.passed
    assert len(ownership) == 1
    assert ownership[0].kind == "invalid"
    assert "ownership.required is true" in ownership[0].message


def test_doc_tree_fails_when_required_ownership_cannot_be_resolved(
    tmp_path: Path,
) -> None:
    repository = _repository(
        tmp_path,
        _policy(ownership={"source": "codeowners", "required": True}),
    )

    result = CliRunner().invoke(
        app,
        ["doc", "tree", "--project", str(repository), "--json"],
    )

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["success"] is False
    assert [finding["code"] for finding in payload["data"]["findings"]] == [
        "document_ownership_not_implemented"
    ]


# --------------------------------------------------------------------------
# Classification-route scaffold options must not be silently ignored
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("--owner", "person:ignored"),
        ("--supersedes", "docs/design/old.md"),
        ("--derived-from", "docs/design/base.md"),
        ("--see-also", "docs/runbooks/run.md"),
    ],
)
def test_version_two_scaffold_rejects_each_classification_route_option(
    tmp_path: Path,
    option: str,
    value: str,
) -> None:
    repository = _repository(tmp_path)

    result = CliRunner().invoke(
        app,
        [
            "doc",
            "scaffold",
            "--type",
            "design",
            "--title",
            "Thing",
            "--project",
            str(repository),
            option,
            value,
        ],
    )

    combined = result.stdout + str(result.stderr)
    assert result.exit_code == 2
    assert "document_scaffold_option_unsupported" in combined
    assert option in combined


def test_version_two_scaffold_names_every_supplied_legacy_option(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)

    result = CliRunner().invoke(
        app,
        [
            "doc",
            "scaffold",
            "--type",
            "design",
            "--title",
            "Thing",
            "--project",
            str(repository),
            "--owner",
            "person:ignored",
            "--see-also",
            "docs/runbooks/run.md",
        ],
    )

    combined = result.stdout + str(result.stderr)
    assert result.exit_code == 2
    assert "--owner" in combined
    assert "--see-also" in combined
    assert "CODEOWNERS" in combined


def test_version_one_scaffold_keeps_its_default_owner(tmp_path: Path) -> None:
    repository = _legacy_repository(tmp_path, version_key=False)

    result = CliRunner().invoke(
        app,
        [
            "doc",
            "scaffold",
            "--type",
            "reference",
            "--title",
            "Note Two",
            "--project",
            str(repository),
        ],
    )

    assert result.exit_code == 0
    assert "owner: person:TODO" in result.stdout


def test_version_one_scaffold_still_accepts_an_explicit_owner(tmp_path: Path) -> None:
    repository = _legacy_repository(tmp_path, version_key=False)

    result = CliRunner().invoke(
        app,
        [
            "doc",
            "scaffold",
            "--type",
            "reference",
            "--title",
            "Note Three",
            "--project",
            str(repository),
            "--owner",
            "person:reviewer",
        ],
    )

    assert result.exit_code == 0
    assert "owner: person:reviewer" in result.stdout


# --------------------------------------------------------------------------
# Structured classification is bound to the contract that has the field
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "contract",
    ["design-document", "project-status-summary"],
)
def test_classified_structured_contracts_require_a_classification(
    contract: str,
) -> None:
    with pytest.raises(ValidationError) as error:
        _simple(_policy(sections=[{"path": "design", "structured": {"contract": contract}}]))

    assert "require a policy-level classification" in str(error.value)

    accepted = _simple(
        _policy(
            sections=[
                {
                    "path": "design",
                    "structured": {
                        "contract": contract,
                        "classification": "design/architecture:document",
                    },
                }
            ]
        )
    )
    assert accepted.sections[0].structured is not None
    assert accepted.sections[0].structured.contract == contract


def test_analysis_brief_sections_cannot_declare_a_classification() -> None:
    accepted = _simple(
        _policy(
            sections=[{"path": "experiments", "structured": {"contract": "analysis-brief"}}]
        )
    )
    assert accepted.sections[0].structured is not None
    assert accepted.sections[0].structured.classification is None

    with pytest.raises(ValidationError) as error:
        _simple(
            _policy(
                sections=[
                    {
                        "path": "experiments",
                        "structured": {
                            "contract": "analysis-brief",
                            "classification": "analysis/experiment:brief",
                        },
                    }
                ]
            )
        )

    assert "no classification field to compare against" in str(error.value)


# --------------------------------------------------------------------------
# Site-absolute links are presentation, not repository dependencies
# --------------------------------------------------------------------------


def test_site_absolute_links_are_ignored_rather_than_reported_missing(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    (repository / "docs/design/g3doc.md").write_text(
        "# Imported Guide\n\n"
        "See [xcloud](/cloud/xcloud/guide.md) and "
        "[learning](/learning/deepmind/xmanager2/gcp/examples) and "
        "[depot](/depot/google3/third_party/py/xmanager).\n",
        encoding="utf-8",
    )

    result = lint_simple_document_tree(repository, _simple())

    assert result.passed
    assert _codes(result) == []


def test_relative_links_remain_repository_dependencies(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    (repository / "docs/runbooks/run.md").write_text("# Run\n", encoding="utf-8")
    (repository / "docs/design/mixed.md").write_text(
        "# Mixed\n\n[site](/cloud/guide.md) and [repo](../runbooks/run.md) "
        "and [gone](./absent.md)\n",
        encoding="utf-8",
    )

    result = lint_simple_document_tree(repository, _simple())

    broken = [
        finding
        for finding in result.findings
        if finding.code == "document_link_target_missing"
    ]
    assert [finding.path for finding in broken] == ["docs/design/mixed.md:./absent.md"]

    checked = CliRunner().invoke(
        app,
        [
            "doc",
            "check",
            str(repository / "docs/design/mixed.md"),
            "--project",
            str(repository),
            "--json",
        ],
    )
    assert json.loads(checked.stdout)["data"]["links"] == ["docs/runbooks/run.md"]


# --------------------------------------------------------------------------
# root_pages name direct children of the root
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "page",
    ["nested/README.md", "design/README.md", "a/b/c.md", "deep/nested/index.md"],
)
def test_root_pages_reject_any_multi_segment_path(page: str) -> None:
    with pytest.raises(ValidationError) as error:
        _simple(_policy(root_pages=[page]))

    assert "direct children of the root" in str(error.value)


def test_root_pages_accept_a_direct_child() -> None:
    policy = _simple(_policy(root_pages=["README.md", "GLOSSARY.md"]))

    assert policy.root_page_paths() == ("docs/README.md", "docs/GLOSSARY.md")

    # A leading "./" is normalized away by the repository path type, so it still
    # names a direct child rather than a second segment.
    assert _simple(_policy(root_pages=["./README.md"])).root_pages == ("README.md",)
