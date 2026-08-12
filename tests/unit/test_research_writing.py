from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from researchctl.cli import app
from researchctl.domain.models import AnalysisBrief, ResearchUpdate
from researchctl.errors import RCPError
from researchctl.serialization import dump_yaml
from researchctl.services.research_writing import (
    ANALYSIS_BRIEF_RENDERER_ID,
    RESEARCH_UPDATE_RENDERER_ID,
    lint_analysis_brief,
    lint_research_update,
    render_analysis_brief,
    render_research_update,
)

SESSION_ID = "session_20260804T120000Z_" + "1" * 24
SOURCE_COMMIT = "2" * 40


def _brief(**overrides: object) -> AnalysisBrief:
    payload: dict[str, object] = {
        "question": "Does BEST assignment drive selection accuracy?",
        "answer": (
            "Random reassignment matches production within noise. "
            "The judge policy mainly changes claim calibration."
        ),
        "protocol": "dapo100-4t-dual-3seed",
        "metrics": [
            {"key": "selection_acc", "label": "Selection accuracy"},
            {"key": "pass_at_4", "label": "pass@4"},
        ],
        "evidence": [
            {
                "setting": "Production",
                "values": {
                    "selection_acc": "20.3 +/- 1.5",
                    "pass_at_4": "57.7 +/- 1.5",
                },
                "source_keys": ["production-run"],
            },
            {
                "setting": "Random",
                "values": {
                    "selection_acc": "21.0 +/- 1.0",
                    "pass_at_4": "56.7 +/- 4.0",
                },
                "source_keys": ["random-run"],
            },
        ],
        "interpretation": [
            "Random versus production is not significant (p=0.57).",
        ],
        "limitations": ["The experiment uses the 1.7B ablation subset."],
        "sources": [
            {
                "key": "production-run",
                "location": "data/label_filter_sensitivity.md#production",
            },
            {
                "key": "random-run",
                "location": "data/label_filter_sensitivity.md#random",
            },
        ],
    }
    payload.update(overrides)
    return AnalysisBrief.model_validate(payload)


def _update(**overrides: object) -> ResearchUpdate:
    payload: dict[str, object] = {
        "event": "completed",
        "session_id": SESSION_ID,
        "session_label": "best-reassignment",
        "task_key": "MAR-18",
        "source_commit": SOURCE_COMMIT,
        "observed_at": "2026-08-04T12:00:00Z",
        "summary": "Random BEST reassignment matched production within noise.",
        "evidence": [
            {"kind": "production", "value": "20.3 +/- 1.5"},
            {"kind": "random", "value": "21.0 +/- 1.0"},
        ],
        "source": {
            "key": "label-filter",
            "location": "data/label_filter_sensitivity.md#best-assignment",
        },
        "next_action": "Use the result in the reviewer-facing brief.",
    }
    payload.update(overrides)
    return ResearchUpdate.model_validate(payload)


def test_analysis_brief_requires_setting_rows_with_rectangular_metrics() -> None:
    payload = _brief().model_dump(mode="json")
    evidence = payload["evidence"]
    assert isinstance(evidence, list)
    first = evidence[0]
    assert isinstance(first, dict)
    values = first["values"]
    assert isinstance(values, dict)
    values.pop("pass_at_4")

    with pytest.raises(ValidationError, match="every analysis brief setting"):
        AnalysisBrief.model_validate(payload)


def test_analysis_brief_rejects_unused_sources() -> None:
    payload = _brief().model_dump(mode="json")
    sources = payload["sources"]
    assert isinstance(sources, list)
    sources.append({"key": "unused-run", "location": "results/unused.json"})

    with pytest.raises(ValidationError, match="sources must all be used"):
        AnalysisBrief.model_validate(payload)


def test_analysis_brief_lint_enforces_answer_budget() -> None:
    brief = _brief(answer=" ".join(["word"] * 61) + ".")

    result = lint_analysis_brief(brief)

    assert result.terminal_result == "invalid"
    assert any(
        finding.code == "prose_english_words_exceeded"
        and finding.field_path == "answer"
        for finding in result.findings
    )
    with pytest.raises(RCPError, match="concise writing contract"):
        render_analysis_brief(brief)


def test_analysis_brief_accepts_legacy_conclusion_but_serializes_answer() -> None:
    payload = _brief().model_dump(mode="json")
    payload["conclusion"] = payload.pop("answer")

    brief = AnalysisBrief.model_validate(payload)

    assert brief.answer.startswith("Random reassignment")
    assert "answer:" in dump_yaml(brief)
    assert "conclusion:" not in dump_yaml(brief)

    payload["answer"] = "Conflicting canonical answer."
    with pytest.raises(ValidationError, match="both answer and conclusion"):
        AnalysisBrief.model_validate(payload)


def test_analysis_brief_renderer_is_fixed_and_uses_setting() -> None:
    content = render_analysis_brief(_brief()).decode("utf-8")

    assert content.startswith("# Does BEST assignment drive selection accuracy?\n")
    assert (
        f"> Renderer: `researchctl-renderer:{ANALYSIS_BRIEF_RENDERER_ID}`"
        in content
    )
    assert "<!-- researchctl-renderer:" not in content
    assert "| Setting | Selection accuracy | pass@4 | Sources |" in content
    assert "| Arm |" not in content
    assert content.index("## Answer") < content.index("## Evidence")
    assert content.index("## Evidence") < content.index("## Interpretation")
    assert content.index("## Interpretation") < content.index("## Limits")
    assert content.index("## Limits") < content.index("## Sources")
    assert "source-format=canonical-json-model" in content


def test_analysis_brief_renderer_preserves_arrows_and_escapes_html_tags() -> None:
    content = render_analysis_brief(
        _brief(answer="Tokenizer -> metric while <think> remains literal text.")
    ).decode("utf-8")

    assert "Tokenizer -> metric" in content
    assert "-&gt;" not in content
    assert "&lt;think&gt;" in content


def test_analysis_brief_requires_quoted_decimal_display_precision() -> None:
    ambiguous = _brief()
    payload = ambiguous.model_dump(mode="json")
    evidence = payload["evidence"]
    assert isinstance(evidence, list) and isinstance(evidence[0], dict)
    values = evidence[0]["values"]
    assert isinstance(values, dict)
    values["selection_acc"] = 0.20
    ambiguous = AnalysisBrief.model_validate(payload)

    result = lint_analysis_brief(ambiguous)

    assert any(
        finding.code == "brief_float_format_ambiguous"
        and finding.field_path == "evidence.0.values.selection_acc"
        for finding in result.findings
    )
    assert "'0.20'" in result.findings[-1].message
    with pytest.raises(RCPError, match="concise writing contract"):
        render_analysis_brief(ambiguous)

    precise = _brief()
    precise_payload = precise.model_dump(mode="json")
    precise_evidence = precise_payload["evidence"]
    assert isinstance(precise_evidence, list) and isinstance(precise_evidence[0], dict)
    precise_values = precise_evidence[0]["values"]
    assert isinstance(precise_values, dict)
    precise_values["selection_acc"] = "0.20"
    rendered = render_analysis_brief(
        AnalysisBrief.model_validate(precise_payload)
    ).decode("utf-8")
    assert "| Production | 0.20 |" in rendered


def test_research_update_lint_and_renderer_stay_linear_sized() -> None:
    update = _update()

    result = lint_research_update(update)
    content = render_research_update(update).decode("utf-8")

    assert result.terminal_result == "passed"
    expected = [
        "**Completed · MAR-18 · best-reassignment**",
        "",
        "Random BEST reassignment matched production within noise.",
        "",
        "Evidence:",
        "- **production:** 20.3 +/- 1.5",
        "- **random:** 21.0 +/- 1.0",
        "",
        (
            "Source: `label-filter` · "
            "`data/label_filter_sensitivity.md#best-assignment`"
        ),
        f"Session: `{SESSION_ID}` · Commit: `{SOURCE_COMMIT}` · Observed: `2026-08-04`",
        "",
        "Next: Use the result in the reviewer-facing brief.",
        "",
        f"> Renderer: `researchctl-renderer:{RESEARCH_UPDATE_RENDERER_ID}`",
    ]
    assert content.splitlines()[:-1] == expected
    assert content.splitlines()[-1].startswith(
        f"<!-- researchctl-generated:{RESEARCH_UPDATE_RENDERER_ID};source=sha256:"
    )


def test_research_update_lint_rejects_verbose_summary() -> None:
    update = _update(summary=" ".join(["word"] * 61) + ".")

    result = lint_research_update(update)

    assert result.terminal_result == "invalid"
    assert any(finding.field_path == "summary" for finding in result.findings)


def test_writing_cli_lints_and_renders_both_records(tmp_path: Path) -> None:
    brief_path = tmp_path / "brief.yaml"
    update_path = tmp_path / "update.yaml"
    rendered_path = tmp_path / "brief.md"
    brief_path.write_text(dump_yaml(_brief()), encoding="utf-8")
    update_path.write_text(dump_yaml(_update()), encoding="utf-8")
    runner = CliRunner()

    brief_lint = runner.invoke(app, ["brief", "lint", str(brief_path), "--json"])
    brief_render = runner.invoke(
        app,
        ["brief", "render", str(brief_path), "--output-file", str(rendered_path)],
    )
    update_lint = runner.invoke(app, ["update", "lint", str(update_path), "--json"])
    update_render = runner.invoke(app, ["update", "render", str(update_path)])

    assert brief_lint.exit_code == 0
    assert '"terminal_result": "passed"' in brief_lint.stdout
    assert brief_render.exit_code == 0
    assert rendered_path.read_bytes() == render_analysis_brief(_brief())
    assert update_lint.exit_code == 0
    assert update_render.exit_code == 0
    assert f"researchctl-renderer:{RESEARCH_UPDATE_RENDERER_ID}" in update_render.stdout


def test_renderer_refreshes_owned_output_but_rejects_manual_edits(tmp_path: Path) -> None:
    source = tmp_path / "brief.yaml"
    output = tmp_path / "brief.md"
    source.write_text(dump_yaml(_brief()), encoding="utf-8")
    runner = CliRunner()

    first = runner.invoke(
        app,
        ["brief", "render", str(source), "--output-file", str(output)],
    )
    source.write_text(
        dump_yaml(_brief(answer="Megatron's result remains within noise.")),
        encoding="utf-8",
    )
    refreshed = runner.invoke(
        app,
        ["brief", "render", str(source), "--output-file", str(output)],
    )

    assert first.exit_code == 0
    assert refreshed.exit_code == 0
    assert "Updated:" in refreshed.stdout
    rendered = output.read_text(encoding="utf-8")
    assert "Megatron's result" in rendered
    assert "Megatron&\\#x27;s" not in rendered

    output.write_text(rendered.replace("within noise", "manually edited"), encoding="utf-8")
    source.write_text(
        dump_yaml(_brief(answer="A third deterministic answer.")),
        encoding="utf-8",
    )
    rejected = runner.invoke(
        app,
        ["brief", "render", str(source), "--output-file", str(output)],
    )
    assert rejected.exit_code == 2
    assert "writing_output_conflict" in rejected.stderr


def test_writing_cli_returns_two_for_lint_failure(tmp_path: Path) -> None:
    brief_path = tmp_path / "brief.yaml"
    brief_path.write_text(
        dump_yaml(_brief(answer=" ".join(["word"] * 61) + ".")),
        encoding="utf-8",
    )

    result = CliRunner().invoke(app, ["brief", "lint", str(brief_path), "--json"])

    assert result.exit_code == 2
    assert '"success": false' in result.stdout
    assert "prose_english_words_exceeded" in result.stdout


def test_brief_lint_aggregates_schema_and_all_detectable_prose_errors(
    tmp_path: Path,
) -> None:
    brief_path = tmp_path / "brief.yaml"
    payload = _brief().model_dump(mode="json")
    payload["protocol"] = "x" * 513
    payload["interpretation"] = [
        " ".join(["first"] * 46) + ".",
        " ".join(["second"] * 47) + ".",
    ]
    brief_path.write_text(dump_yaml(payload), encoding="utf-8")

    result = CliRunner().invoke(app, ["brief", "lint", str(brief_path), "--json"])

    assert result.exit_code == 2
    assert '"loc": [\n              "protocol"' in result.stdout
    assert result.stdout.count('"type": "prose_english_words_exceeded"') == 2
    assert '"interpretation",\n              0' in result.stdout
    assert '"interpretation",\n              1' in result.stdout
    assert "has 46 English words; maximum is 45" in result.stdout
    assert "has 47 English words; maximum is 45" in result.stdout


def test_writing_cli_reports_yaml_scanner_location_and_specific_remediation(
    tmp_path: Path,
) -> None:
    brief_path = tmp_path / "brief.yaml"
    brief_path.write_text(
        "question: Broken brief\n"
        "answer: Results: evidence\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(app, ["brief", "lint", str(brief_path), "--json"])

    assert result.exit_code == 2
    payload = result.stdout
    assert "ScannerError" in payload
    assert '"line": 2' in payload
    assert '"column": 16' in payload
    assert "invalid YAML (ScannerError)" in payload
    assert "invalid protocol YAML" not in payload
    assert "Quote plain YAML text" in payload
    assert "duplicate keys or aliases" not in payload


def test_writing_cli_human_validation_error_names_field_and_yaml_location(
    tmp_path: Path,
) -> None:
    brief_path = tmp_path / "brief.yaml"
    payload = _brief().model_dump(mode="json")
    payload["answer"] = 11677
    brief_path.write_text(dump_yaml(payload), encoding="utf-8")

    result = CliRunner().invoke(app, ["brief", "lint", str(brief_path)])

    assert result.exit_code == 2
    assert "invalid: answer [validation_error]" in result.stderr
    assert "Input should be a valid string" in result.stderr
    assert "line " in result.stderr and "column " in result.stderr
