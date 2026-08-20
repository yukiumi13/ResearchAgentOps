from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from researchctl.cli import app
from researchctl.constants import PROJECT_POLICY_PATH
from researchctl.domain.models import (
    SIMPLE_MARKDOWN_FRONTMATTER_FIELDS,
    DocumentLayoutPolicy,
    ProjectPolicy,
    SimpleDocumentLayoutPolicy,
)
from researchctl.errors import RCPError
from researchctl.serialization import dump_yaml, load_yaml
from researchctl.services.agent_guides import (
    agent_guide_markers,
    render_simple_agent_guide,
)
from researchctl.services.codeowners import (
    CODEOWNERS_LOCATIONS,
    CODEOWNERS_MAX_BYTES,
    discover_codeowners,
    parse_codeowners,
)
from researchctl.services.document_policy import (
    build_effective_policy,
    select_policy_version,
)
from researchctl.services.generated_markdown import claims_generated_markdown
from researchctl.services.markdown_source import (
    blockquote_texts,
    first_heading_title,
    html_block_texts,
    link_destinations,
)
from researchctl.services.project_documents import (
    PROJECT_AGENT_GUIDE_RENDERER_IDS,
    render_project_agent_guide,
    render_standalone_document_policy_template,
)
from researchctl.services.project_documents_v2 import (
    check_simple_document,
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
MANAGED_AGENT_POLICY = {
    "accepted_paths_denied": [
        ".research/decisions/**",
        ".research/policies/**",
        ".research/project.yaml",
        ".research/impacts/**",
        ".research/reports/**",
        ".research/tasks/**",
    ]
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


def _managed_repository(
    root: Path,
    policy: dict[str, Any] | None = None,
) -> Path:
    """Wrap the same v2 layout in the managed Project policy envelope."""

    repository = _repository(root, policy)
    (repository / ".researchctl-docs.yaml").unlink()
    policy_path = repository / PROJECT_POLICY_PATH
    policy_path.parent.mkdir(parents=True)
    policy_path.write_text(
        dump_yaml(
            ProjectPolicy.model_validate(
                {
                    "schema_version": "0.1",
                    "agent": MANAGED_AGENT_POLICY,
                    "document_layout": policy if policy is not None else _policy(),
                }
            )
        ),
        encoding="utf-8",
    )
    return repository


def _simple(payload: dict[str, Any] | None = None) -> SimpleDocumentLayoutPolicy:
    effective = build_effective_policy(payload if payload is not None else _policy())
    assert effective.simple is not None
    return effective.simple


STRUCTURED_SECTIONS: list[dict[str, Any]] = [
    {
        "path": "design",
        "structured": {
            "contract": "design-document",
            "classification": "design/architecture:document",
        },
    },
    {"path": "profiling", "structured": {"contract": "analysis-brief"}},
    {"path": "runbooks"},
]


def _structured_policy(**overrides: Any) -> dict[str, Any]:
    return _policy(sections=[dict(section) for section in STRUCTURED_SECTIONS], **overrides)


def _write_structured_pair(
    repository: Path,
    *,
    section: str,
    contract: str,
    title: str,
    stem: str,
) -> tuple[Path, Path]:
    """Author one canonical source and its render through the real commands."""

    runner = CliRunner()
    source = repository / "docs" / section / f"{stem}.yaml"
    scaffolded = runner.invoke(
        app,
        [
            "doc",
            "scaffold",
            "--type",
            section,
            "--title",
            title,
            "--contract",
            contract,
            "--project",
            str(repository),
            "--output-file",
            str(source),
        ],
    )
    assert scaffolded.exit_code == 0, scaffolded.stdout
    render = source.with_suffix(".md")
    rendered = runner.invoke(
        app,
        [
            "doc",
            "render",
            str(source),
            "--project",
            str(repository),
            "--output-file",
            str(render),
        ],
    )
    assert rendered.exit_code == 0, rendered.stdout
    return source, render


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


def test_managed_project_policy_defaults_to_v1_and_accepts_explicit_v2() -> None:
    agent = ProjectPolicy.model_validate(
        {
            "schema_version": "0.1",
            "agent": MANAGED_AGENT_POLICY,
        }
    ).agent

    legacy = ProjectPolicy(agent=agent)
    simple = ProjectPolicy.model_validate(
        {
            **legacy.model_dump(mode="json"),
            "document_layout": _policy(),
        }
    )

    assert isinstance(legacy.document_layout, DocumentLayoutPolicy)
    assert isinstance(simple.document_layout, SimpleDocumentLayoutPolicy)
    assert simple.document_layout.version == 2

    with pytest.raises(ValidationError):
        ProjectPolicy.model_validate(
            {
                **legacy.model_dump(mode="json"),
                "document_layout": {
                    "version": 3,
                    "sections": [{"path": "design"}],
                },
            }
        )


def test_managed_policy_keeps_v1_diagnostics_and_rejects_unknown_versions(
    tmp_path: Path,
) -> None:
    repository = _managed_repository(tmp_path)
    policy_path = repository / PROJECT_POLICY_PATH
    payload = load_yaml(policy_path.read_text(encoding="utf-8"))
    payload["document_layout"] = {"max_dept": 3}
    policy_path.write_text(dump_yaml(payload), encoding="utf-8")
    runner = CliRunner()

    typo = runner.invoke(
        app,
        ["doc", "tree", "--project", str(repository), "--json"],
    )

    assert typo.exit_code == 2
    details = json.loads(typo.stdout)["errors"][0]["context"]["details"]
    assert [detail["loc"] for detail in details] == [
        ["document_layout", "max_dept"]
    ]
    expected_line = next(
        number
        for number, line in enumerate(
            policy_path.read_text(encoding="utf-8").splitlines(),
            start=1,
        )
        if line.strip() == "max_dept: 3"
    )
    assert details[0]["line"] == expected_line
    assert "function-after" not in typo.stdout

    payload["document_layout"] = {
        "version": 3,
        "sections": [{"path": "design"}],
    }
    policy_path.write_text(dump_yaml(payload), encoding="utf-8")
    unsupported = runner.invoke(
        app,
        ["doc", "tree", "--project", str(repository), "--json"],
    )

    assert unsupported.exit_code == 2
    error = json.loads(unsupported.stdout)["errors"][0]
    assert error["code"] == "document_policy_version_unsupported"
    assert error["context"]["declared_version"] == 3


def test_managed_v2_policy_drives_the_same_directory_first_tree(
    tmp_path: Path,
) -> None:
    repository = _managed_repository(tmp_path)
    (repository / "docs/design/overview.md").write_text(
        "# Managed design overview\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        ["doc", "tree", "--project", str(repository), "--json"],
    )

    assert result.exit_code == 0, result.stdout
    data = json.loads(result.stdout)["data"]
    assert data["policy_version"] == 2
    assert data["terminal_result"] == "passed"
    assert [item["path"] for item in data["document_facts"]] == [
        "docs/README.md",
        "docs/design/overview.md",
    ]


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


def test_a_structured_pair_and_ordinary_markdown_share_one_section(
    tmp_path: Path,
) -> None:
    payload = _structured_policy()
    repository = _repository(tmp_path, payload)
    _write_structured_pair(
        repository,
        section="profiling",
        contract="analysis-brief",
        title="Full SFT memory at 16k",
        stem="full-sft-memory-at-16k",
    )
    # A hand-written note with a different stem stays an ordinary document.
    (repository / "docs/profiling/encoder-performance-tuning.md").write_text(
        "# Encoder performance tuning\n\nNotes beside the brief.\n",
        encoding="utf-8",
    )

    result = lint_simple_document_tree(repository, _simple(payload))

    assert result.passed, _invalid(result)
    assert result.structured_documents == 1
    # docs/README.md plus the hand-written note; the render is not a document.
    assert result.documents == 2
    assert [facts.path for facts in result.document_facts] == [
        "docs/README.md",
        "docs/profiling/encoder-performance-tuning.md",
    ]
    structured = result.structured_facts[0]
    assert structured.source_path == "docs/profiling/full-sft-memory-at-16k.yaml"
    assert structured.render_path == "docs/profiling/full-sft-memory-at-16k.md"
    assert structured.contract == "analysis-brief"
    assert structured.classification is None


def test_ordinary_sections_publish_non_markdown_files_as_assets(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    (repository / "docs/design/diagrams").mkdir()
    (repository / "docs/design/diagrams/flow.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (repository / "docs/design/report.pdf").write_bytes(b"%PDF-1.4\n")
    (repository / "docs/design/data.yaml").write_text("a: 1\n", encoding="utf-8")

    result = lint_simple_document_tree(repository, _simple())

    # No content sniffing and no MIME check: a non-Markdown file is published
    # as it stands.
    assert result.passed
    assert _invalid(result) == []
    assert result.assets == 3
    assert result.asset_paths == (
        "docs/design/data.yaml",
        "docs/design/diagrams/flow.png",
        "docs/design/report.pdf",
    )
    assert result.documents == 1


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
    assert data["contract"] == "simple-markdown-frontmatter"
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


def _versioned_repository(root: Path, *, version: int) -> Path:
    """Build a repository whose only document is locked by its own contract."""

    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "docs/design").mkdir(parents=True, exist_ok=True)
    if version == 2:
        (root / ".researchctl-docs.yaml").write_text(
            dump_yaml(_policy()),
            encoding="utf-8",
        )
        (root / "docs/README.md").write_text("# Documentation\n", encoding="utf-8")
        (root / "docs/runbooks").mkdir(exist_ok=True)
        body = "---\nlocked: true\n---\n\n# Locked\n\nImmobilized text.\n"
    else:
        (root / ".researchctl-docs.yaml").write_text(
            dump_yaml(
                {
                    "root": "docs",
                    "routes": [dict(LEGACY_ROUTE, document_type="design", directory="docs/design")],
                }
            ),
            encoding="utf-8",
        )
        body = (
            "---\n"
            "type: design\n"
            "title: Locked\n"
            "owner: person:manager\n"
            "last_updated: 2026-01-01\n"
            "validity: frozen\n"
            "---\n"
            "\n# Locked\n\nImmobilized text.\n"
        )
    (root / "docs/design/locked.md").write_text(body, encoding="utf-8")
    return root


@pytest.mark.parametrize("head_version", [1, 2])
@pytest.mark.parametrize("baseline_version", [1, 2])
def test_a_locked_baseline_document_is_protected_across_policy_versions(
    tmp_path: Path,
    head_version: int,
    baseline_version: int,
) -> None:
    baseline = _versioned_repository(
        tmp_path / "base", version=baseline_version
    )
    head = _versioned_repository(tmp_path / "head", version=head_version)
    # The baseline's own locked bytes are what the head must preserve.
    locked = (baseline / "docs/design/locked.md").read_bytes()
    (head / "docs/design/locked.md").write_bytes(locked)

    runner = CliRunner()
    unchanged = runner.invoke(
        app,
        [
            "doc",
            "tree",
            "--project",
            str(head),
            "--baseline-project",
            str(baseline),
            "--json",
        ],
    )
    assert "frozen_document_modified" not in unchanged.stdout

    (head / "docs/design/locked.md").write_bytes(locked + b"Appended.\n")
    modified = runner.invoke(
        app,
        [
            "doc",
            "tree",
            "--project",
            str(head),
            "--baseline-project",
            str(baseline),
            "--json",
        ],
    )
    assert modified.exit_code == 2
    assert '"code": "frozen_document_modified"' in modified.stdout

    (head / "docs/design/locked.md").unlink()
    deleted = runner.invoke(
        app,
        [
            "doc",
            "tree",
            "--project",
            str(head),
            "--baseline-project",
            str(baseline),
            "--json",
        ],
    )
    assert deleted.exit_code == 2
    assert '"code": "frozen_document_modified"' in deleted.stdout

    (head / "docs/design/locked.md").symlink_to(head / "docs/README.md")
    replaced = runner.invoke(
        app,
        [
            "doc",
            "tree",
            "--project",
            str(head),
            "--baseline-project",
            str(baseline),
            "--json",
        ],
    )
    assert replaced.exit_code == 2
    assert '"code": "frozen_document_modified"' in replaced.stdout


def test_managed_v2_baseline_keeps_locked_documents_immutable(
    tmp_path: Path,
) -> None:
    baseline = _managed_repository(tmp_path / "base")
    head = _managed_repository(tmp_path / "head")
    locked_path = "docs/design/locked.md"
    locked = b"---\nlocked: true\n---\n\n# Locked\n\nAccepted bytes.\n"
    (baseline / locked_path).write_bytes(locked)
    (head / locked_path).write_bytes(locked + b"Changed.\n")
    (baseline / PROJECT_POLICY_PATH).write_text(
        dump_yaml(
            {
                "document_layout": {
                    "version": 99,
                    "root": "docs",
                    "sections": "future-shape",
                },
                "unknown_future_policy_field": True,
            }
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        [
            "doc",
            "tree",
            "--project",
            str(head),
            "--baseline-project",
            str(baseline),
            "--json",
        ],
    )

    assert result.exit_code == 2
    codes = [
        finding["code"]
        for finding in json.loads(result.stdout)["data"]["findings"]
    ]
    assert "frozen_document_modified" in codes
    assert "document_baseline_policy_invalid" not in codes


def test_an_unlocked_baseline_document_places_no_restriction(tmp_path: Path) -> None:
    baseline = _repository(tmp_path / "base")
    (baseline / "docs/design/free.md").write_text("# Free\n", encoding="utf-8")
    head = _repository(tmp_path / "head")
    (head / "docs/design/free.md").write_text("# Free, rewritten\n", encoding="utf-8")

    result = CliRunner().invoke(
        app,
        [
            "doc",
            "tree",
            "--project",
            str(head),
            "--baseline-project",
            str(baseline),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert "frozen_document_modified" not in result.stdout


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


def test_doc_check_accepts_both_kinds_inside_a_structured_section(
    tmp_path: Path,
) -> None:
    payload = _structured_policy()
    repository = _repository(tmp_path, payload)
    source, _render = _write_structured_pair(
        repository,
        section="design",
        contract="design-document",
        title="Splice the encoder",
        stem="splice-the-encoder",
    )
    note = repository / "docs/design/encoder-notes.md"
    note.write_text("# Encoder notes\n", encoding="utf-8")

    runner = CliRunner()
    structured = runner.invoke(
        app,
        ["doc", "check", str(source), "--project", str(repository), "--json"],
    )
    assert structured.exit_code == 0, structured.stdout
    structured_data = json.loads(structured.stdout)["data"]
    assert structured_data["kind"] == "structured"
    assert structured_data["contract"] == "design-document"
    assert structured_data["terminal_result"] == "passed"

    ordinary = runner.invoke(
        app,
        ["doc", "check", str(note), "--project", str(repository), "--json"],
    )
    assert ordinary.exit_code == 0, ordinary.stdout
    ordinary_data = json.loads(ordinary.stdout)["data"]
    assert ordinary_data["kind"] == "markdown"
    assert ordinary_data["contract"] == "simple-markdown-frontmatter"
    assert ordinary_data["title"] == "Encoder notes"


def test_scaffold_defaults_to_markdown_inside_a_structured_section(
    tmp_path: Path,
) -> None:
    payload = _structured_policy()
    repository = _repository(tmp_path, payload)

    result = CliRunner().invoke(
        app,
        [
            "doc",
            "scaffold",
            "--type",
            "design",
            "--title",
            "Encoder notes",
            "--project",
            str(repository),
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert result.stdout.startswith("---\n")
    assert "# Encoder notes" in result.stdout
    assert "document_id" not in result.stdout


def test_scaffold_contract_must_match_the_configured_structured_contract(
    tmp_path: Path,
) -> None:
    payload = _structured_policy()
    repository = _repository(tmp_path, payload)

    mismatch = CliRunner().invoke(
        app,
        [
            "doc",
            "scaffold",
            "--type",
            "design",
            "--title",
            "Thing",
            "--contract",
            "analysis-brief",
            "--project",
            str(repository),
        ],
    )
    assert mismatch.exit_code == 2
    assert "document_structured_contract_mismatch" in (
        mismatch.stdout + str(mismatch.stderr)
    )

    unconfigured = CliRunner().invoke(
        app,
        [
            "doc",
            "scaffold",
            "--type",
            "runbooks",
            "--title",
            "Thing",
            "--contract",
            "analysis-brief",
            "--project",
            str(repository),
        ],
    )
    assert unconfigured.exit_code == 2
    assert "document_structured_contract_unconfigured" in (
        unconfigured.stdout + str(unconfigured.stderr)
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
    (repository / ".github").mkdir()
    # Even a defective CODEOWNERS is silent when the policy configures no
    # ownership source at all.
    (repository / ".github/CODEOWNERS").write_text("!nope @a\n", encoding="utf-8")

    result = lint_simple_document_tree(repository, _simple())

    assert result.passed
    assert not [code for code in _codes(result) if "codeowners" in code]
    assert not [code for code in _codes(result) if code == "document_owner_unresolved"]
    assert result.document_facts[0].owners == ()


def test_optional_ownership_warns_when_no_codeowners_exists(tmp_path: Path) -> None:
    payload = _policy(ownership={"source": "codeowners", "required": False})
    repository = _repository(tmp_path, payload)
    (repository / "docs/design/a.md").write_text("# A\n", encoding="utf-8")
    (repository / "docs/design/b.md").write_text("# B\n", encoding="utf-8")

    result = lint_simple_document_tree(repository, _simple(payload))

    ownership = [
        finding
        for finding in result.findings
        if finding.code == "document_codeowners_missing"
    ]
    assert result.passed
    assert len(ownership) == 1
    assert ownership[0].kind == "warning"
    for location in CODEOWNERS_LOCATIONS:
        assert location in ownership[0].message


def test_required_ownership_fails_when_no_codeowners_exists(tmp_path: Path) -> None:
    payload = _policy(ownership={"source": "codeowners", "required": True})
    repository = _repository(tmp_path, payload)
    (repository / "docs/design/a.md").write_text("# A\n", encoding="utf-8")

    result = lint_simple_document_tree(repository, _simple(payload))

    ownership = [
        finding
        for finding in result.findings
        if finding.code == "document_codeowners_missing"
    ]
    assert not result.passed
    assert len(ownership) == 1
    assert ownership[0].kind == "invalid"


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
    assert payload["data"]["codeowners_path"] is None
    assert [finding["code"] for finding in payload["data"]["findings"]] == [
        "document_codeowners_missing"
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

# --------------------------------------------------------------------------
# Ownership resolves from CODEOWNERS and nowhere else
# --------------------------------------------------------------------------


def test_codeowners_accepts_github_owner_forms_and_rejects_the_rest() -> None:
    ruleset = parse_codeowners(
        "\n".join(
            [
                "# A comment, then a blank line.",
                "",
                "*                    @fallback",
                "docs/design/         @docs-team @org/writers ops@example.com",
                "docs/runbooks/*.md   @org/sre",
                "docs/unowned/",
                "!docs/negated        @a",
                "[owners]",
                "docs/bad             not-an-owner",
            ]
        ),
        path=".github/CODEOWNERS",
    )

    assert [rule.pattern for rule in ruleset.rules] == [
        "*",
        "docs/design/",
        "docs/runbooks/*.md",
        "docs/unowned/",
    ]
    assert ruleset.owners_for("docs/design/a.md") == (
        "@docs-team",
        "@org/writers",
        "ops@example.com",
    )
    assert ruleset.owners_for("docs/runbooks/r.md") == ("@org/sre",)
    assert ruleset.owners_for("src/main.py") == ("@fallback",)
    # A rule with no owners is legal and un-assigns ownership.
    assert ruleset.owners_for("docs/unowned/z.md") == ()
    assert [problem.line for problem in ruleset.problems] == [7, 8, 9]
    assert "negated" in ruleset.problems[0].message
    assert "GitLab" in ruleset.problems[1].message
    assert "not-an-owner" in ruleset.problems[2].message


def test_codeowners_resolution_uses_the_last_matching_rule() -> None:
    ruleset = parse_codeowners(
        "\n".join(
            [
                "docs/design/ @first",
                "docs/design/ @second",
                "* @catch-all",
                "docs/design/a.md @specific",
            ]
        ),
        path="CODEOWNERS",
    )

    # GitHub reads the file top to bottom and the last match wins, so a broad
    # rule placed late overrides the narrow rules above it.
    assert ruleset.owners_for("docs/design/b.md") == ("@catch-all",)
    assert ruleset.owners_for("docs/design/a.md") == ("@specific",)


def test_codeowners_discovery_follows_github_precedence(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    (repository / "docs/CODEOWNERS").write_text("* @docs-copy\n", encoding="utf-8")
    assert discover_codeowners(repository).path == "docs/CODEOWNERS"

    (repository / "CODEOWNERS").write_text("* @root\n", encoding="utf-8")
    assert discover_codeowners(repository).path == "CODEOWNERS"

    (repository / ".github").mkdir()
    (repository / ".github/CODEOWNERS").write_text("* @github\n", encoding="utf-8")
    discovery = discover_codeowners(repository)
    assert discovery.path == ".github/CODEOWNERS"
    assert discovery.ruleset is not None
    assert discovery.ruleset.owners_for("docs/README.md") == ("@github",)


def test_a_codeowners_symlink_fails_instead_of_falling_through(
    tmp_path: Path,
) -> None:
    payload = _policy(ownership={"source": "codeowners", "required": False})
    repository = _repository(tmp_path, payload)
    (repository / "CODEOWNERS").write_text("* @root\n", encoding="utf-8")
    (repository / ".github").mkdir()
    (repository / ".github/CODEOWNERS").symlink_to(repository / "CODEOWNERS")

    result = lint_simple_document_tree(repository, _simple(payload))

    # Falling through to the lower-precedence file would resolve owners GitHub
    # never consults, so an untrustworthy candidate is a hard error.
    unreadable = [
        finding
        for finding in result.findings
        if finding.code == "document_codeowners_unreadable"
    ]
    assert not result.passed
    assert len(unreadable) == 1
    assert unreadable[0].path == ".github/CODEOWNERS"
    assert result.codeowners_path is None


def test_resolved_owners_reach_the_document_and_structured_facts(
    tmp_path: Path,
) -> None:
    payload = _structured_policy(ownership={"source": "codeowners", "required": True})
    repository = _repository(tmp_path, payload)
    (repository / ".github").mkdir()
    (repository / ".github/CODEOWNERS").write_text(
        "* @fallback\ndocs/profiling/ @perf-team ops@example.com\n",
        encoding="utf-8",
    )
    _write_structured_pair(
        repository,
        section="profiling",
        contract="analysis-brief",
        title="Full SFT memory at 16k",
        stem="full-sft-memory-at-16k",
    )
    (repository / "docs/profiling/encoder-performance-tuning.md").write_text(
        "# Encoder performance tuning\n", encoding="utf-8"
    )

    result = lint_simple_document_tree(repository, _simple(payload))

    assert result.passed, _invalid(result)
    assert result.codeowners_path == ".github/CODEOWNERS"
    owners = {facts.path: facts.owners for facts in result.document_facts}
    assert owners["docs/README.md"] == ("@fallback",)
    assert owners["docs/profiling/encoder-performance-tuning.md"] == (
        "@perf-team",
        "ops@example.com",
    )
    # A generated page is owned through the Markdown readers actually see.
    assert result.structured_facts[0].owners == ("@perf-team", "ops@example.com")


@pytest.mark.parametrize(
    ("required", "kind", "passed"),
    [(True, "invalid", False), (False, "warning", True)],
)
def test_an_unmatched_document_is_reported_by_the_required_flag(
    tmp_path: Path,
    required: bool,
    kind: str,
    passed: bool,
) -> None:
    payload = _policy(ownership={"source": "codeowners", "required": required})
    repository = _repository(tmp_path, payload)
    (repository / ".github").mkdir()
    (repository / ".github/CODEOWNERS").write_text(
        "docs/design/ @docs-team\n", encoding="utf-8"
    )
    (repository / "docs/design/a.md").write_text("# A\n", encoding="utf-8")

    result = lint_simple_document_tree(repository, _simple(payload))

    unresolved = [
        finding
        for finding in result.findings
        if finding.code == "document_owner_unresolved"
    ]
    assert result.passed is passed
    # docs/design/a.md matches; only the root page is left unowned.
    assert [finding.path for finding in unresolved] == ["docs/README.md"]
    assert unresolved[0].kind == kind


def test_the_effective_codeowners_inside_the_root_is_neither_page_nor_asset(
    tmp_path: Path,
) -> None:
    payload = _policy(ownership={"source": "codeowners", "required": True})
    repository = _repository(tmp_path, payload)
    (repository / "docs/CODEOWNERS").write_text("* @docs-team\n", encoding="utf-8")

    result = lint_simple_document_tree(repository, _simple(payload))

    assert result.passed, _invalid(result)
    assert result.codeowners_path == "docs/CODEOWNERS"
    assert result.assets == 0
    assert "docs/CODEOWNERS" not in [facts.path for facts in result.document_facts]
    assert "document_root_file_unclassified" not in _codes(result)


def test_a_shadowed_codeowners_inside_the_root_is_reported_as_such(
    tmp_path: Path,
) -> None:
    payload = _policy(ownership={"source": "codeowners", "required": True})
    repository = _repository(tmp_path, payload)
    (repository / ".github").mkdir()
    (repository / ".github/CODEOWNERS").write_text("* @github\n", encoding="utf-8")
    (repository / "docs/CODEOWNERS").write_text("* @ignored\n", encoding="utf-8")

    result = lint_simple_document_tree(repository, _simple(payload))

    shadowed = [
        finding
        for finding in result.findings
        if finding.code == "document_codeowners_shadowed"
    ]
    assert not result.passed
    assert [finding.path for finding in shadowed] == ["docs/CODEOWNERS"]
    assert ".github/CODEOWNERS" in shadowed[0].message


def test_codeowners_syntax_defects_are_reported_with_their_line(
    tmp_path: Path,
) -> None:
    payload = _policy(ownership={"source": "codeowners", "required": False})
    repository = _repository(tmp_path, payload)
    (repository / ".github").mkdir()
    (repository / ".github/CODEOWNERS").write_text(
        "* @ok\n!docs/x @a\n", encoding="utf-8"
    )

    result = lint_simple_document_tree(repository, _simple(payload))

    syntax = [
        finding
        for finding in result.findings
        if finding.code == "document_codeowners_syntax_invalid"
    ]
    # A defect is invalid even when ownership itself is optional: the file is
    # present and says something GitHub will not do.
    assert not result.passed
    assert [finding.path for finding in syntax] == [".github/CODEOWNERS:2"]


# --------------------------------------------------------------------------
# Static assets
# --------------------------------------------------------------------------


def test_nested_yaml_in_a_structured_section_is_an_asset(tmp_path: Path) -> None:
    payload = _structured_policy()
    repository = _repository(tmp_path, payload)
    (repository / "docs/design/fixtures").mkdir()
    (repository / "docs/design/fixtures/data.yaml").write_text(
        "a: 1\n", encoding="utf-8"
    )

    result = lint_simple_document_tree(repository, _simple(payload))

    # Only a direct child is canonical, so nested YAML is published, not parsed.
    assert result.passed, _invalid(result)
    assert result.asset_paths == ("docs/design/fixtures/data.yaml",)
    assert result.structured_documents == 0


def test_an_uppercase_yaml_source_is_not_a_canonical_structured_source(
    tmp_path: Path,
) -> None:
    payload = _structured_policy()
    repository = _repository(tmp_path, payload)
    (repository / "docs/design/encoder.YAML").write_text("a: 1\n", encoding="utf-8")

    result = lint_simple_document_tree(repository, _simple(payload))

    # The render path is derived by stripping a literal ".yaml", so treating
    # .YAML as canonical would name a render docs/design/encoder.YAML.md that
    # nothing builds and no manifest can describe.
    assert not result.passed
    assert _invalid(result) == ["document_structured_extension_invalid"]
    assert result.structured_documents == 0
    assert result.asset_paths == ()

    # The single-path route has to reach the same verdict as the tree.
    with pytest.raises(RCPError) as error:
        check_simple_document(
            repository,
            _simple(payload),
            source=repository / "docs/design/encoder.YAML",
            relative="docs/design/encoder.YAML",
        )

    assert error.value.code == "document_structured_extension_invalid"


def test_markdown_links_to_assets_are_existence_checked(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    (repository / "docs/design/flow.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (repository / "docs/design/a.md").write_text(
        "# A\n\n![flow](flow.png)\n\n![gone](missing.png)\n",
        encoding="utf-8",
    )

    result = lint_simple_document_tree(repository, _simple())

    missing = [
        finding
        for finding in result.findings
        if finding.code == "document_link_target_missing"
    ]
    assert len(missing) == 1
    assert missing[0].path == "docs/design/a.md:missing.png"
    facts = next(
        item for item in result.document_facts if item.path == "docs/design/a.md"
    )
    assert facts.links == ("docs/design/flow.png",)


def test_root_direct_files_still_require_an_explicit_root_page(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    (repository / "docs/notes.md").write_text("# Notes\n", encoding="utf-8")
    (repository / "docs/logo.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    result = lint_simple_document_tree(repository, _simple())

    unclassified = [
        finding
        for finding in result.findings
        if finding.code == "document_root_file_unclassified"
    ]
    assert [finding.path for finding in unclassified] == [
        "docs/logo.png",
        "docs/notes.md",
    ]
    assert result.assets == 0


def test_symlinks_remain_a_hard_error_in_a_section(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    (repository / "docs/design/real.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (repository / "docs/design/link.png").symlink_to(
        repository / "docs/design/real.png"
    )

    result = lint_simple_document_tree(repository, _simple())

    assert "document_symlink_forbidden" in _invalid(result)


# --------------------------------------------------------------------------
# Structured and plain coexistence
# --------------------------------------------------------------------------


def test_a_markerless_same_stem_markdown_file_is_an_ambiguity_error(
    tmp_path: Path,
) -> None:
    payload = _structured_policy()
    repository = _repository(tmp_path, payload)
    source, render = _write_structured_pair(
        repository,
        section="profiling",
        contract="analysis-brief",
        title="Full SFT memory at 16k",
        stem="full-sft-memory-at-16k",
    )
    assert source.exists()
    render.write_text("# Full SFT memory at 16k\n\nHand written.\n", encoding="utf-8")

    result = lint_simple_document_tree(repository, _simple(payload))

    # A stale render must not be able to pass itself off as an ordinary
    # document; an ordinary document uses a different stem.
    assert _invalid(result) == ["document_render_marker_missing"]
    assert render.relative_to(repository).as_posix() not in [
        facts.path for facts in result.document_facts
    ]


def test_a_damaged_render_marker_is_not_treated_as_ordinary_markdown(
    tmp_path: Path,
) -> None:
    payload = _structured_policy()
    repository = _repository(tmp_path, payload)
    _source, render = _write_structured_pair(
        repository,
        section="profiling",
        contract="analysis-brief",
        title="Full SFT memory at 16k",
        stem="full-sft-memory-at-16k",
    )
    damaged = re.sub(
        r"body=sha256:[0-9a-f]{64}",
        "body=sha256:" + "0" * 64,
        render.read_text(encoding="utf-8"),
    )
    render.write_text(damaged, encoding="utf-8")

    result = lint_simple_document_tree(repository, _simple(payload))

    assert _invalid(result) == ["document_render_marker_invalid"]
    assert result.documents == 1


def test_a_generated_render_without_a_source_is_an_orphan(tmp_path: Path) -> None:
    payload = _structured_policy()
    repository = _repository(tmp_path, payload)
    source, render = _write_structured_pair(
        repository,
        section="profiling",
        contract="analysis-brief",
        title="Full SFT memory at 16k",
        stem="full-sft-memory-at-16k",
    )
    source.unlink()

    result = lint_simple_document_tree(repository, _simple(payload))

    assert _invalid(result) == ["document_render_orphaned"]
    assert result.findings[-1].path == render.relative_to(repository).as_posix()


def test_an_envelope_classification_must_match_its_section(tmp_path: Path) -> None:
    payload = _structured_policy()
    repository = _repository(tmp_path, payload)
    source, _render = _write_structured_pair(
        repository,
        section="design",
        contract="design-document",
        title="Splice the encoder",
        stem="splice-the-encoder",
    )
    source.write_text(
        source.read_text(encoding="utf-8").replace(
            "classification: design/architecture:document",
            "classification: implementation/architecture:note",
        ),
        encoding="utf-8",
    )

    result = lint_simple_document_tree(repository, _simple(payload))

    assert "document_classification_section_mismatch" in _invalid(result)


def test_a_structured_source_needs_a_generated_pair(tmp_path: Path) -> None:
    payload = _structured_policy()
    repository = _repository(tmp_path, payload)
    _source, render = _write_structured_pair(
        repository,
        section="profiling",
        contract="analysis-brief",
        title="Full SFT memory at 16k",
        stem="full-sft-memory-at-16k",
    )
    render.unlink()

    result = lint_simple_document_tree(repository, _simple(payload))

    assert _invalid(result) == ["document_render_missing"]


# --------------------------------------------------------------------------
# The configured contract, not the file, selects the model
# --------------------------------------------------------------------------


MIXED_ENVELOPE_SECTIONS: list[dict[str, Any]] = [
    {
        "path": "design",
        "structured": {
            "contract": "design-document",
            "classification": "design/architecture:document",
        },
    },
    {
        "path": "status",
        "structured": {
            "contract": "project-status-summary",
            # Deliberately the same label as the design section, so a status
            # summary filed under design would satisfy every classification
            # check and the document kind is the only thing left to catch it.
            "classification": "design/architecture:document",
        },
    },
    {"path": "profiling", "structured": {"contract": "analysis-brief"}},
]


def _misfiled_status_summary(tmp_path: Path) -> tuple[Path, Path]:
    """Author a valid status summary, then file it under the design section."""

    payload = _policy(sections=[dict(section) for section in MIXED_ENVELOPE_SECTIONS])
    repository = _repository(tmp_path, payload)
    source, render = _write_structured_pair(
        repository,
        section="status",
        contract="project-status-summary",
        title="Where the work stands",
        stem="where-the-work-stands",
    )
    misfiled = repository / "docs/design/where-the-work-stands.yaml"
    misfiled.write_bytes(source.read_bytes())
    misfiled.with_suffix(".md").write_bytes(render.read_bytes())
    source.unlink()
    render.unlink()
    return repository, misfiled


def test_the_tree_rejects_a_valid_document_of_the_wrong_kind(tmp_path: Path) -> None:
    repository, _misfiled = _misfiled_status_summary(tmp_path)
    payload = _policy(sections=[dict(section) for section in MIXED_ENVELOPE_SECTIONS])

    result = lint_simple_document_tree(repository, _simple(payload))

    # Classification, slug, and render all agree; only the kind is wrong.
    assert _invalid(result) == ["document_contract_kind_mismatch"]
    assert result.structured_documents == 0


def test_doc_check_rejects_a_valid_document_of_the_wrong_kind(
    tmp_path: Path,
) -> None:
    repository, misfiled = _misfiled_status_summary(tmp_path)

    result = CliRunner().invoke(
        app,
        ["doc", "check", str(misfiled), "--project", str(repository), "--json"],
    )

    assert result.exit_code == 2
    data = json.loads(result.stdout)["data"]
    assert [finding["code"] for finding in data["findings"]] == [
        "document_contract_kind_mismatch"
    ]


def test_doc_render_rejects_a_valid_document_of_the_wrong_kind(
    tmp_path: Path,
) -> None:
    repository, misfiled = _misfiled_status_summary(tmp_path)

    result = CliRunner().invoke(
        app,
        ["doc", "render", str(misfiled), "--project", str(repository)],
    )

    assert result.exit_code == 2
    assert "document_contract_kind_mismatch" in (result.stdout + str(result.stderr))


def test_an_envelope_cannot_satisfy_an_analysis_brief_section(
    tmp_path: Path,
) -> None:
    payload = _policy(sections=[dict(section) for section in MIXED_ENVELOPE_SECTIONS])
    repository = _repository(tmp_path, payload)
    source, _render = _write_structured_pair(
        repository,
        section="design",
        contract="design-document",
        title="Splice the encoder",
        stem="splice-the-encoder",
    )
    (repository / "docs/profiling").mkdir(exist_ok=True)
    misfiled = repository / "docs/profiling/splice-the-encoder.yaml"
    misfiled.write_bytes(source.read_bytes())

    findings, _facts = check_simple_document(
        repository,
        _simple(payload),
        source=misfiled,
        relative="docs/profiling/splice-the-encoder.yaml",
    )

    assert [finding.code for finding in findings if finding.kind == "invalid"] == [
        "document_contract_kind_mismatch"
    ]


# --------------------------------------------------------------------------
# Generated Markdown is an output, never a checkable source
# --------------------------------------------------------------------------


@pytest.mark.parametrize("damage", [None, "prefix", "comment"])
def test_doc_check_refuses_renderer_owned_markdown(
    tmp_path: Path,
    damage: str | None,
) -> None:
    payload = _structured_policy()
    repository = _repository(tmp_path, payload)
    _source, render = _write_structured_pair(
        repository,
        section="profiling",
        contract="analysis-brief",
        title="Full SFT memory at 16k",
        stem="full-sft-memory-at-16k",
    )
    text = render.read_text(encoding="utf-8")
    if damage == "prefix":
        text = text.replace("<!-- researchctl-generated:", "<!-- researchctl-generated")
    elif damage == "comment":
        # Only the visible renderer header survives.
        text = "\n".join(
            line for line in text.splitlines() if "researchctl-generated" not in line
        )
    render.write_text(text, encoding="utf-8")

    result = CliRunner().invoke(
        app,
        ["doc", "check", str(render), "--project", str(repository), "--json"],
    )

    assert result.exit_code == 2
    reported = json.loads(result.stdout)["errors"]
    assert [error["code"] for error in reported] == [
        "document_generated_markdown_not_checkable"
    ]
    # The diagnostic must point at the canonical source, not just say "no".
    assert (
        reported[0]["context"]["canonical_source"]
        == "docs/profiling/full-sft-memory-at-16k.yaml"
    )
    assert "researchctl doc render" in reported[0]["remediation"]


@pytest.mark.parametrize("damage", ["prefix", "comment"])
def test_a_damaged_renderer_claim_never_falls_back_to_ordinary_markdown(
    tmp_path: Path,
    damage: str,
) -> None:
    payload = _structured_policy()
    repository = _repository(tmp_path, payload)
    source, render = _write_structured_pair(
        repository,
        section="profiling",
        contract="analysis-brief",
        title="Full SFT memory at 16k",
        stem="full-sft-memory-at-16k",
    )
    text = render.read_text(encoding="utf-8")
    if damage == "prefix":
        text = text.replace("<!-- researchctl-generated:", "<!-- researchctl-generated")
    else:
        text = "\n".join(
            line for line in text.splitlines() if "researchctl-generated" not in line
        )
    render.write_text(text, encoding="utf-8")

    paired = lint_simple_document_tree(repository, _simple(payload))
    assert _invalid(paired) == ["document_render_marker_invalid"]
    assert paired.documents == 1

    # The same damaged file with no canonical source is an orphan, not prose.
    source.unlink()
    orphaned = lint_simple_document_tree(repository, _simple(payload))
    assert _invalid(orphaned) == ["document_render_orphaned"]
    assert orphaned.documents == 1


# --------------------------------------------------------------------------
# Omitted ownership means no ownership
# --------------------------------------------------------------------------


def test_valid_codeowners_rules_are_ignored_when_ownership_is_omitted(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    (repository / ".github").mkdir()
    (repository / ".github/CODEOWNERS").write_text(
        "* @fallback\ndocs/design/ @docs-team\n", encoding="utf-8"
    )
    (repository / "docs/design/a.md").write_text("# A\n", encoding="utf-8")

    result = lint_simple_document_tree(repository, _simple())

    # The policy has not adopted CODEOWNERS, so nothing is resolved from it.
    assert result.passed, _invalid(result)
    assert result.codeowners_path is None
    assert {facts.owners for facts in result.document_facts} == {()}
    assert not [code for code in _codes(result) if "codeowners" in code]
    assert "document_owner_unresolved" not in _codes(result)


def test_a_shadowed_codeowners_is_silent_when_ownership_is_omitted(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    (repository / ".github").mkdir()
    (repository / ".github/CODEOWNERS").write_text("* @github\n", encoding="utf-8")
    (repository / "docs/CODEOWNERS").write_text("* @ignored\n", encoding="utf-8")

    result = lint_simple_document_tree(repository, _simple())

    assert "document_codeowners_shadowed" not in _codes(result)
    # With no ownership source configured, the stray file is just an
    # undeclared file in the document root.
    assert _invalid(result) == ["document_root_file_unclassified"]
    assert result.findings[0].path == "docs/CODEOWNERS"


def test_an_effective_codeowners_in_the_root_is_excluded_without_ownership(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    (repository / "docs/CODEOWNERS").write_text("* @docs-team\n", encoding="utf-8")

    result = lint_simple_document_tree(repository, _simple())

    # Exclusion is the one thing discovery is still used for: GitHub reads this
    # file, so it is review configuration rather than a page.
    assert result.passed, _invalid(result)
    assert result.codeowners_path is None
    assert result.assets == 0
    assert "docs/CODEOWNERS" not in [facts.path for facts in result.document_facts]


# --------------------------------------------------------------------------
# The JSON result carries the facts a validated tree produced
# --------------------------------------------------------------------------


def test_doc_tree_json_exposes_facts_and_asset_paths(tmp_path: Path) -> None:
    payload = _structured_policy(ownership={"source": "codeowners", "required": True})
    repository = _repository(tmp_path, payload)
    (repository / ".github").mkdir()
    (repository / ".github/CODEOWNERS").write_text("* @docs-team\n", encoding="utf-8")
    _write_structured_pair(
        repository,
        section="profiling",
        contract="analysis-brief",
        title="Full SFT memory at 16k",
        stem="full-sft-memory-at-16k",
    )
    (repository / "docs/design/flow.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (repository / "docs/design/a.md").write_text(
        "---\nstatus: draft\ntags: [encoder]\n---\n\n# A\n\n![flow](flow.png)\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        ["doc", "tree", "--project", str(repository), "--json"],
    )

    assert result.exit_code == 0, result.stdout
    data = json.loads(result.stdout)["data"]
    assert data["asset_paths"] == ["docs/design/flow.png"]
    assert [facts["path"] for facts in data["document_facts"]] == [
        "docs/README.md",
        "docs/design/a.md",
    ]
    authored = data["document_facts"][1]
    assert authored["title"] == "A"
    assert authored["status"] == "draft"
    assert authored["tags"] == ["encoder"]
    assert authored["owners"] == ["@docs-team"]
    assert authored["links"] == ["docs/design/flow.png"]
    assert data["structured_facts"] == [
        {
            "source_path": "docs/profiling/full-sft-memory-at-16k.yaml",
            "render_path": "docs/profiling/full-sft-memory-at-16k.md",
            "section": "profiling",
            "contract": "analysis-brief",
            "classification": None,
            "title": "Full SFT memory at 16k",
            "lifecycle": None,
            # An analysis brief has no envelope, so it carries no tags.
            "tags": [],
            "owners": ["@docs-team"],
        }
    ]
    # The JSON envelope must stay serializable end to end.
    assert json.loads(json.dumps(data)) == data


# --------------------------------------------------------------------------
# Baseline enforcement must not depend on a schema-valid baseline policy
# --------------------------------------------------------------------------


def test_a_stale_baseline_policy_still_protects_locked_bytes(tmp_path: Path) -> None:
    baseline = _versioned_repository(tmp_path / "base", version=2)
    # Readable YAML that exposes root, but is not valid under any current
    # model: an unknown policy version, an unknown key, and a sections value of
    # the wrong type. Requiring a schema-valid baseline here would deadlock the
    # very change set that repairs the policy.
    (baseline / ".researchctl-docs.yaml").write_text(
        dump_yaml(
            {
                "version": 99,
                "root": "docs",
                "sections": "not-a-list",
                "unknown_future_key": {"nested": True},
            }
        ),
        encoding="utf-8",
    )
    head = _versioned_repository(tmp_path / "head", version=2)
    locked = (baseline / "docs/design/locked.md").read_bytes()
    (head / "docs/design/locked.md").write_bytes(locked + b"Appended.\n")

    result = CliRunner().invoke(
        app,
        [
            "doc",
            "tree",
            "--project",
            str(head),
            "--baseline-project",
            str(baseline),
            "--json",
        ],
    )

    assert result.exit_code == 2
    codes = [
        finding["code"] for finding in json.loads(result.stdout)["data"]["findings"]
    ]
    assert "frozen_document_modified" in codes
    assert "document_baseline_policy_invalid" not in codes


# --------------------------------------------------------------------------
# A renderer claim is a shape, not a mention
# --------------------------------------------------------------------------


DOCUMENTATION_ABOUT_MARKERS = """# How generated pages are marked

Every generated page carries a `researchctl-generated` provenance comment and
the visible `researchctl-renderer` header above it. Do not edit either by hand.

A rendered analysis brief begins like this:

```markdown
> Renderer: `researchctl-renderer:research-analysis-brief.v4`
<!-- researchctl-generated:research-analysis-brief.v4;source=sha256:abc;body=sha256:def -->
```

The marker line is what `researchctl doc tree` reads to tell a render from an
ordinary document.
"""


def test_prose_and_code_samples_about_the_marker_stay_ordinary() -> None:
    # Documentation about the contract is not an instance of the contract.
    assert claims_generated_markdown(DOCUMENTATION_ABOUT_MARKERS.encode()) is False


@pytest.mark.parametrize(
    "line",
    [
        "<!-- researchctl-generated:research-analysis-brief.v4;body=sha256:a -->",
        "<!-- researchctl-generated research-analysis-brief.v4 -->",
        "<!--researchctl-generated",
        "<!--   researchctl-generated;truncated",
        "> Renderer: `researchctl-renderer:research-analysis-brief.v4`",
    ],
)
def test_a_damaged_or_intact_claim_is_still_a_claim(line: str) -> None:
    assert claims_generated_markdown(f"# Title\n\n{line}\n".encode()) is True


@pytest.mark.parametrize(
    "line",
    [
        "The `researchctl-generated` comment records the source digest.",
        "Look for researchctl-generated at the top of the file.",
        "See researchctl-renderer for the renderer identifier.",
        "> This quoted note mentions researchctl-renderer but is not a Renderer header.",
        "    <!-- researchctl-generated:v1 -->",
    ],
)
def test_a_mention_of_the_marker_is_not_a_claim(line: str) -> None:
    assert claims_generated_markdown(f"# Title\n\n{line}\n".encode()) is False


def test_a_runbook_about_markers_is_an_ordinary_document(tmp_path: Path) -> None:
    payload = _structured_policy()
    repository = _repository(tmp_path, payload)
    (repository / "docs/runbooks").mkdir(exist_ok=True)
    note = repository / "docs/runbooks/generated-pages.md"
    note.write_text(DOCUMENTATION_ABOUT_MARKERS, encoding="utf-8")

    result = lint_simple_document_tree(repository, _simple(payload))

    assert result.passed, _invalid(result)
    assert "docs/runbooks/generated-pages.md" in [
        facts.path for facts in result.document_facts
    ]
    assert "document_render_orphaned" not in _codes(result)

    checked = CliRunner().invoke(
        app,
        ["doc", "check", str(note), "--project", str(repository), "--json"],
    )
    assert checked.exit_code == 0, checked.stdout
    assert json.loads(checked.stdout)["data"]["title"] == "How generated pages are marked"


# --------------------------------------------------------------------------
# CODEOWNERS syntax GitHub does not accept, and the size GitHub refuses
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "fragment"),
    [
        ("!docs/x @a", "negated"),
        ("docs/[a-z]*.md @a", "character ranges"),
        ("*.[md] @a", "character ranges"),
        ("\\#docs/x @a", "escapes a leading"),
    ],
)
def test_the_parser_rejects_syntax_github_does_not_support(
    line: str,
    fragment: str,
) -> None:
    ruleset = parse_codeowners(f"* @fallback\n{line}\n", path="CODEOWNERS")

    assert [rule.pattern for rule in ruleset.rules] == ["*"]
    assert [problem.line for problem in ruleset.problems] == [2]
    assert fragment in ruleset.problems[0].message


def test_a_gitlab_section_header_keeps_its_own_diagnostic() -> None:
    ruleset = parse_codeowners(
        "[owners]\n^[Docs]\n[Docs][2]\n", path="CODEOWNERS"
    )

    assert ruleset.rules == ()
    assert [problem.line for problem in ruleset.problems] == [1, 2, 3]
    assert all("GitLab" in problem.message for problem in ruleset.problems)


def test_an_oversized_codeowners_does_not_fall_through_to_lower_precedence(
    tmp_path: Path,
) -> None:
    payload = _policy(ownership={"source": "codeowners", "required": True})
    repository = _repository(tmp_path, payload)
    (repository / "CODEOWNERS").write_text("* @root\n", encoding="utf-8")
    (repository / ".github").mkdir()
    (repository / ".github/CODEOWNERS").write_bytes(
        b"#" * (CODEOWNERS_MAX_BYTES - 1) + b"\n"
    )

    discovery = discover_codeowners(repository)
    assert discovery.path == ".github/CODEOWNERS"
    assert discovery.ruleset is None
    assert discovery.error is not None
    assert str(CODEOWNERS_MAX_BYTES) in discovery.error

    result = lint_simple_document_tree(repository, _simple(payload))

    # GitHub loads no owners from an oversized file, so resolving them from the
    # root CODEOWNERS would report ownership the repository does not have.
    assert not result.passed
    assert _invalid(result) == ["document_codeowners_unreadable"]
    assert result.codeowners_path is None
    assert {facts.owners for facts in result.document_facts} == {()}


def test_a_codeowners_just_under_the_limit_is_read(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    rule = b"* @fallback\n"
    padding = b"#" + b"a" * (CODEOWNERS_MAX_BYTES - len(rule) - 3) + b"\n"
    (repository / "CODEOWNERS").write_bytes(padding + rule)
    assert (repository / "CODEOWNERS").stat().st_size == CODEOWNERS_MAX_BYTES - 1

    discovery = discover_codeowners(repository)

    assert discovery.error is None
    assert discovery.ruleset is not None
    assert discovery.ruleset.owners_for("docs/README.md") == ("@fallback",)


NESTED_FENCE_SAMPLE = """# Quoting a rendered page

To show the whole header, the sample is fenced with four backticks so the inner
three-backtick fence stays literal:

````markdown
```
> Renderer: `researchctl-renderer:research-analysis-brief.v4`
<!-- researchctl-generated:research-analysis-brief.v4;body=sha256:abc -->
```
````

That is the entire generated header.
"""


def test_a_four_backtick_fence_keeps_a_nested_marker_as_code() -> None:
    content = NESTED_FENCE_SAMPLE.encode()

    # The inner three-backtick line must not close the four-backtick fence, so
    # the marker never reaches the document as a block.
    assert html_block_texts(NESTED_FENCE_SAMPLE) == ()
    assert blockquote_texts(NESTED_FENCE_SAMPLE) == ()
    assert claims_generated_markdown(content) is False


def test_a_document_quoting_a_render_in_a_nested_fence_stays_ordinary(
    tmp_path: Path,
) -> None:
    payload = _structured_policy()
    repository = _repository(tmp_path, payload)
    (repository / "docs/runbooks").mkdir(exist_ok=True)
    note = repository / "docs/runbooks/quoting-renders.md"
    note.write_text(NESTED_FENCE_SAMPLE, encoding="utf-8")

    result = lint_simple_document_tree(repository, _simple(payload))

    assert result.passed, _invalid(result)
    facts = next(
        item
        for item in result.document_facts
        if item.path == "docs/runbooks/quoting-renders.md"
    )
    assert facts.title == "Quoting a rendered page"
    assert "document_render_orphaned" not in _codes(result)


def test_a_real_render_is_claimed_through_both_of_its_markers(
    tmp_path: Path,
) -> None:
    payload = _structured_policy()
    repository = _repository(tmp_path, payload)
    _source, render = _write_structured_pair(
        repository,
        section="profiling",
        contract="analysis-brief",
        title="Full SFT memory at 16k",
        stem="full-sft-memory-at-16k",
    )
    text = render.read_text(encoding="utf-8")

    assert claims_generated_markdown(text.encode()) is True
    # The provenance comment is a real HTML block and the visible header is a
    # real block quote, so losing either one still leaves a claim.
    assert any(
        block.strip().startswith("<!-- researchctl-generated")
        for block in html_block_texts(text)
    )
    assert any("researchctl-renderer" in quote for quote in blockquote_texts(text))


# --------------------------------------------------------------------------
# Managed Agent guide
# --------------------------------------------------------------------------

GUIDE_TARGET: list[dict[str, str]] = [{"path": "CLAUDE.md", "format": "claude"}]
PROJECT_RULES = "# Project rules\n\nRun the tests before pushing.\n"


def _guide_policy(**overrides: Any) -> dict[str, Any]:
    return _structured_policy(agent_guides=[dict(GUIDE_TARGET[0])], **overrides)


def _guide(payload: dict[str, Any]) -> str:
    return render_simple_agent_guide(_simple(payload), "claude").decode("utf-8")


def test_the_version_two_agent_guide_renders_to_stdout(tmp_path: Path) -> None:
    payload = _guide_policy()
    repository = _repository(tmp_path, payload)

    result = CliRunner().invoke(app, ["doc", "agent-guide", "--project", str(repository)])

    assert result.exit_code == 0, result.stdout + str(result.stderr)
    assert result.stdout == _guide(payload)


def test_the_version_two_guide_states_the_directory_first_contract() -> None:
    payload = _guide_policy(ownership={"source": "codeowners", "required": True})
    guide = _guide(payload)

    # The markers are the version 1 markers, so raising a policy replaces the
    # same managed block instead of leaving two behind. Only the renderer id
    # says which contract the block describes.
    assert guide.startswith(
        "<!-- researchctl-agent-guide:project-document-agent-guide.claude:begin -->"
    )
    assert guide.rstrip("\n").endswith(
        "<!-- researchctl-agent-guide:project-document-agent-guide.claude:end -->"
    )
    assert "researchctl-renderer:simple-document-agent-guide.claude.v1" in guide
    assert "project-document-agent-guide.claude.v5" not in guide

    for statement in (
        "An ordinary document satisfies the `simple-markdown-frontmatter` contract",
        "Markdown whose section directory is its type",
        "no `a/b:c` classification",
        "Its title is the first\nlevel-one heading in the file",
        "owners come from CODEOWNERS, which is the\nonly review authority",
        "edited comes from Git",
        "Frontmatter is optional and a document with none is valid",
        "A structured YAML contract is opt-in per section",
        "a direct child of the section directory",
        "regenerate it, never edit it by hand",
        "repository-root-relative path",
        "researchctl doc contracts --project .",
        "researchctl doc schema --contract simple-markdown-frontmatter",
        "researchctl doc schema --contract CONTRACT",
        "researchctl doc scaffold --type SECTION --title TITLE",
        "researchctl doc check PATH",
        "researchctl doc render PATH --output-file PATH.md",
        "researchctl doc tree --project .",
        "an Agent-authored\ncommit is not acceptance",
        "must not be\nhidden inside a content proposal",
        "no `researchctl init`, no Session, no SQLite",
    ):
        assert statement in guide, statement

    # The guide lists exactly the fields the frontmatter model accepts.
    for field in SIMPLE_MARKDOWN_FRONTMATTER_FIELDS:
        assert f"- `{field}` --" in guide, field
    assert guide.count(" -- ") == len(SIMPLE_MARKDOWN_FRONTMATTER_FIELDS)

    # Every section names the ordinary contract, so no reader concludes that a
    # section without a structured contract accepts nothing.
    assert (
        "| Section | Ordinary contract | Structured contract | "
        "Classification compatibility |"
    ) in guide
    assert (
        "| `design` | `simple-markdown-frontmatter` | `design-document` | "
        "`design/architecture:document` |"
    ) in guide
    assert "| `profiling` | `simple-markdown-frontmatter` | `analysis-brief` | - |" in (
        guide
    )
    assert "| `runbooks` | `simple-markdown-frontmatter` | - | - |" in guide
    assert guide.count("| `simple-markdown-frontmatter` |") == 3
    assert (
        "Directory depth below a section: at most 3. "
        "Accepted root pages: `README.md`. Ownership: CODEOWNERS, required."
    ) in guide


def test_the_guide_upserts_one_block_and_leaves_the_project_rules_alone(
    tmp_path: Path,
) -> None:
    payload = _guide_policy()
    repository = _repository(tmp_path, payload)
    (repository / "CLAUDE.md").write_text(PROJECT_RULES, encoding="utf-8")
    runner = CliRunner()
    command = [
        "doc", "agent-guide",
        "--project", str(repository),
        "--output-file", "CLAUDE.md",
    ]

    inserted = runner.invoke(app, command)

    assert inserted.exit_code == 0, inserted.stdout + str(inserted.stderr)
    assert "Updated:" in inserted.stdout
    expected = PROJECT_RULES + "\n" + _guide(payload)
    assert (repository / "CLAUDE.md").read_text(encoding="utf-8") == expected

    repeated = runner.invoke(app, command)

    # Rendering twice is a no-op, so the command is safe to run in CI.
    assert repeated.exit_code == 0, repeated.stdout + str(repeated.stderr)
    assert "Unchanged:" in repeated.stdout
    assert (repository / "CLAUDE.md").read_text(encoding="utf-8") == expected
    assert lint_simple_document_tree(repository, _simple(payload)).passed


def test_doc_tree_enforces_the_configured_version_two_guide(tmp_path: Path) -> None:
    payload = _guide_policy()
    repository = _repository(tmp_path, payload)

    absent = lint_simple_document_tree(repository, _simple(payload))
    assert _invalid(absent) == ["agent_guide_missing"]

    (repository / "CLAUDE.md").write_text(PROJECT_RULES, encoding="utf-8")
    unmanaged = lint_simple_document_tree(repository, _simple(payload))
    assert _invalid(unmanaged) == ["agent_guide_mismatch"]

    (repository / "CLAUDE.md").write_text(
        PROJECT_RULES + "\n" + _guide(payload), encoding="utf-8"
    )
    managed = lint_simple_document_tree(repository, _simple(payload))
    assert managed.passed, _invalid(managed)
    # docs/README.md plus the guide researchctl just checked.
    assert managed.checked_files == 2


def test_upgrading_to_version_two_replaces_the_version_one_block_in_place(
    tmp_path: Path,
) -> None:
    payload = _guide_policy()
    repository = _repository(tmp_path, payload)

    # The block a version 1 policy left behind, produced by the real version 1
    # renderer rather than written by hand.
    legacy_policy = DocumentLayoutPolicy.model_validate(
        {
            "root": "docs",
            "root_files": [],
            "routes": [LEGACY_ROUTE],
            "agent_guides": [dict(GUIDE_TARGET[0])],
        }
    )
    legacy_block = render_project_agent_guide(legacy_policy, "claude").decode("utf-8")
    trailer = "\n## Local conventions\n\nAsk before renaming a section.\n"
    (repository / "CLAUDE.md").write_text(
        PROJECT_RULES + "\n" + legacy_block + trailer, encoding="utf-8"
    )

    upgraded = CliRunner().invoke(
        app,
        [
            "doc", "agent-guide",
            "--project", str(repository),
            "--output-file", "CLAUDE.md",
        ],
    )

    assert upgraded.exit_code == 0, upgraded.stdout + str(upgraded.stderr)
    observed = (repository / "CLAUDE.md").read_text(encoding="utf-8")

    # Project-owned text on both sides of the managed block survives byte for byte.
    assert observed == PROJECT_RULES + "\n" + _guide(payload) + trailer
    # Sharing the marker identity is what makes this a replacement rather than
    # a second block appended below the first.
    begin, end = agent_guide_markers("claude")
    assert observed.count(begin) == 1
    assert observed.count(end) == 1
    assert PROJECT_AGENT_GUIDE_RENDERER_IDS["claude"] not in observed
    assert "researchctl-renderer:simple-document-agent-guide.claude.v1" in observed
    assert lint_simple_document_tree(repository, _simple(payload)).passed


def test_any_edit_to_the_managed_block_is_reported_as_drift(tmp_path: Path) -> None:
    payload = _guide_policy()
    repository = _repository(tmp_path, payload)
    rendered = _guide(payload)
    guide_path = repository / "CLAUDE.md"
    guide_path.write_text(rendered, encoding="utf-8")
    assert lint_simple_document_tree(repository, _simple(payload)).passed

    # One character inside the block is enough.
    tampered = rendered.replace("no Session,", "no session,", 1)
    assert tampered != rendered
    guide_path.write_text(tampered, encoding="utf-8")
    assert _invalid(lint_simple_document_tree(repository, _simple(payload))) == [
        "agent_guide_mismatch"
    ]

    # So is leaving the block alone while the policy moves underneath it.
    guide_path.write_text(rendered, encoding="utf-8")
    widened = _policy(
        sections=[*(dict(section) for section in STRUCTURED_SECTIONS), {"path": "notes"}],
        agent_guides=[dict(GUIDE_TARGET[0])],
    )
    assert _invalid(lint_simple_document_tree(repository, _simple(widened))) == [
        "agent_guide_mismatch"
    ]


# --------------------------------------------------------------------------
# Discovering the directory-first contract, and adopting a policy that uses it
# --------------------------------------------------------------------------


def _contract_block(stdout: str, contract: str) -> str:
    """Return only the human paragraph describing one contract."""

    blocks = stdout.split("Contract: ")
    matching = [block for block in blocks if block.startswith(f"{contract}\n")]
    assert len(matching) == 1, f"{contract} appears {len(matching)} times"
    return matching[0]


def test_the_contract_doc_check_reports_is_the_one_doc_schema_prints(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    note = repository / "docs/runbooks/operate.md"
    note.write_text("# Operate the worker\n", encoding="utf-8")
    runner = CliRunner()

    checked = runner.invoke(
        app,
        ["doc", "check", str(note), "--project", str(repository), "--json"],
    )

    assert checked.exit_code == 0, checked.stdout
    reported = json.loads(checked.stdout)["data"]["contract"]
    assert reported == "simple-markdown-frontmatter"

    # The name is not decoration: whatever `doc check` prints has to be a
    # registered schema, or an author cannot look up what it accepts.
    schema = runner.invoke(app, ["doc", "schema", "--contract", reported])

    assert schema.exit_code == 0, schema.stdout + str(schema.stderr)
    document = json.loads(schema.stdout)
    assert document["title"] == "SimpleMarkdownFrontmatter"
    assert document["$id"].endswith(f":{reported}")
    assert sorted(document["properties"]) == sorted(SIMPLE_MARKDOWN_FRONTMATTER_FIELDS)
    assert "required" not in document


def test_doc_contracts_describes_the_directory_first_ordinary_contract() -> None:
    runner = CliRunner()

    human = runner.invoke(app, ["doc", "contracts"])
    machine = runner.invoke(app, ["doc", "contracts", "--json"])

    assert human.exit_code == 0, human.stdout
    assert machine.exit_code == 0, machine.stdout
    block = _contract_block(human.stdout, "simple-markdown-frontmatter")
    assert "Source: Markdown with optional YAML frontmatter" in block
    assert "Required: none" in block
    assert (
        "Optional: depends_on, locked, reviewed_on, status, superseded_by, tags"
    ) in block
    assert "Note: Frontmatter is optional; a document with none is valid." in block
    assert "Note: The title is the first level-one heading" in block
    assert "Note: The document type is its section directory" in block
    assert "Note: Owners come from CODEOWNERS" in block
    assert "Note: The last edited date comes from Git" in block
    assert "Check: researchctl doc check PATH --project ." in block
    assert "Schema: researchctl doc schema --contract simple-markdown-frontmatter" in (
        block
    )
    assert "Render: none (manual Markdown is canonical)" in block
    # There is no standalone form of this check, so the listing must not offer
    # one; the classification-route contracts keep theirs.
    assert "Standalone check" not in block
    assert "Standalone check (no policy): researchctl brief lint PATH" in human.stdout
    # `doc check` rejects sources and provenance frontmatter here, so the
    # listing must not send an author toward the version 1 answer.
    assert "keyed sources" not in block
    assert "Provenance: Cite with ordinary Markdown links or depends_on" in block
    assert "keyed sources" in _contract_block(human.stdout, "markdown-frontmatter")

    contracts = json.loads(machine.stdout)["data"]["contracts"]
    entry = next(
        item for item in contracts if item["contract"] == "simple-markdown-frontmatter"
    )
    assert entry["required_fields"] == []
    assert entry["optional_fields"] == sorted(SIMPLE_MARKDOWN_FRONTMATTER_FIELDS)
    assert entry["standalone_check_command"] is None
    assert entry["check_command"] == "researchctl doc check PATH --project ."
    assert entry["render_command"] is None
    assert entry["routed_render_command"] is None
    assert len(entry["authoring_facts"]) == 6


def test_doc_contracts_binds_each_section_to_the_contracts_it_accepts(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path, _structured_policy())
    runner = CliRunner()

    human = runner.invoke(app, ["doc", "contracts", "--project", str(repository)])
    machine = runner.invoke(app, ["doc", "contracts", "-C", str(repository), "--json"])

    assert human.exit_code == 0, human.stdout
    assert machine.exit_code == 0, machine.stdout
    assert "Effective policy: version 2" in human.stdout
    assert "Section: design (docs/design)" in human.stdout
    assert "Classification: design/architecture:document" in human.stdout
    assert human.stdout.count("Ordinary: simple-markdown-frontmatter") == 3

    summary = json.loads(machine.stdout)["data"]["effective_policy"]
    # Each section reports its own contracts and nothing else: a structured
    # contract belongs to the one section that configures it, and a
    # classification only to a contract that has the field.
    assert summary == {
        "policy_version": 2,
        "source": str(repository / ".researchctl-docs.yaml"),
        "root": "docs",
        "sections": [
            {
                "section": "design",
                "directory": "docs/design",
                "ordinary_contract": "simple-markdown-frontmatter",
                "structured_contract": "design-document",
                "classification": "design/architecture:document",
            },
            {
                "section": "profiling",
                "directory": "docs/profiling",
                "ordinary_contract": "simple-markdown-frontmatter",
                "structured_contract": "analysis-brief",
            },
            {
                "section": "runbooks",
                "directory": "docs/runbooks",
                "ordinary_contract": "simple-markdown-frontmatter",
            },
        ],
    }


def test_doc_contracts_keeps_route_facts_under_a_version_one_policy(
    tmp_path: Path,
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".researchctl-docs.yaml").write_text(
        dump_yaml({"routes": [dict(LEGACY_ROUTE)]}),
        encoding="utf-8",
    )
    runner = CliRunner()

    human = runner.invoke(app, ["doc", "contracts", "--project", str(tmp_path)])
    machine = runner.invoke(app, ["doc", "contracts", "-C", str(tmp_path), "--json"])

    assert human.exit_code == 0, human.stdout
    assert machine.exit_code == 0, machine.stdout
    assert "Effective policy: version 1" in human.stdout
    assert "Route: reference (docs/reference)" in human.stdout
    assert "Classification: reference/project:document" in human.stdout
    assert "Contract: markdown-frontmatter" in human.stdout
    assert "ordinary_contract" not in machine.stdout

    summary = json.loads(machine.stdout)["data"]["effective_policy"]
    assert summary["policy_version"] == 1
    assert summary["routes"] == [
        {
            "document_type": "reference",
            "classification": "reference/project:document",
            "contract": "markdown-frontmatter",
            "directory": "docs/reference",
        }
    ]
    assert "sections" not in summary


def test_doc_contracts_refuses_to_describe_a_project_with_no_policy(
    tmp_path: Path,
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    runner = CliRunner()

    human = runner.invoke(app, ["doc", "contracts", "--project", str(tmp_path)])
    machine = runner.invoke(app, ["doc", "contracts", "-C", str(tmp_path), "--json"])

    # Naming a project is a question about that project. Answering it from a
    # default layout would be a guess, so the command fails instead.
    assert human.exit_code == 2
    assert "document_policy_missing" in str(human.stderr)
    assert "Effective policy" not in human.stdout
    assert machine.exit_code == 2
    envelope = json.loads(machine.stdout)
    assert envelope["success"] is False
    assert envelope["errors"][0]["code"] == "document_policy_missing"
    assert envelope["data"] == {}


def test_the_version_two_policy_template_is_blocked_only_by_its_empty_sections(
    tmp_path: Path,
) -> None:
    candidate = tmp_path / ".researchctl-docs.yaml"
    runner = CliRunner()

    rendered = runner.invoke(
        app,
        ["doc", "policy-template", "--output-file", str(candidate)],
    )

    assert rendered.exit_code == 0, rendered.stdout + str(rendered.stderr)
    template = candidate.read_text(encoding="utf-8")
    # The one field the template refuses to fill is the one only this
    # repository knows. Everything else is ready to adopt as written.
    assert "sections: []\n" in template
    assert "version: 2\n" in template
    assert "root: docs\n" in template
    assert "max_depth: 3\n" in template
    assert "- README.md\n" in template
    assert "  source: codeowners\n" in template
    assert "  required: true\n" in template
    assert "  path: CLAUDE.md\n" in template
    assert "Inventory this repository before filling this in" in template
    assert "Directory names are\n# facts about this project" in template
    assert "#     - path: runbooks" in template
    assert "#       structured:\n#         contract: analysis-brief" in template
    assert "simple-markdown-frontmatter contract" in template

    blocked = runner.invoke(app, ["doc", "policy-lint", str(candidate), "--json"])

    assert blocked.exit_code == 2
    details = json.loads(blocked.stdout)["errors"][0]["context"]["details"]
    assert [detail["loc"] for detail in details] == [["sections"]]
    assert details[0]["type"] == "too_short"

    filled = template.replace(
        "sections: []\n",
        "sections:\n"
        "- path: runbooks\n"
        "- path: experiments\n"
        "  structured:\n"
        "    contract: analysis-brief\n",
    )
    assert filled != template
    candidate.write_text(filled, encoding="utf-8")

    adopted = runner.invoke(app, ["doc", "policy-lint", str(candidate), "--json"])

    # Listing the sections is the whole of the work: nothing else in the
    # template was left incomplete.
    assert adopted.exit_code == 0, adopted.stdout
    data = json.loads(adopted.stdout)["data"]
    assert data["policy_version"] == 2
    assert data["sections"] == ["runbooks", "experiments"]
    assert data["structured_sections"] == ["experiments"]
    assert data["ownership"] == {"source": "codeowners", "required": True}
    assert data["root_pages"] == ["README.md"]
    assert data["max_depth"] == 3
    assert data["agent_guides"] == 1


@pytest.mark.parametrize(
    ("guide_format", "guide_path"),
    [("claude", "CLAUDE.md"), ("agents", "AGENTS.md")],
)
def test_explicit_version_one_still_renders_the_original_template(
    tmp_path: Path,
    guide_format: str,
    guide_path: str,
) -> None:
    candidate = tmp_path / "candidate.yaml"

    rendered = CliRunner().invoke(
        app,
        [
            "doc",
            "policy-template",
            "--policy-version",
            "1",
            "--agent-format",
            guide_format,
            "--output-file",
            str(candidate),
        ],
    )

    assert rendered.exit_code == 0, rendered.stdout + str(rendered.stderr)
    assert candidate.read_bytes() == render_standalone_document_policy_template(
        guide_format  # type: ignore[arg-type]
    )
    assert guide_path in candidate.read_text(encoding="utf-8")
