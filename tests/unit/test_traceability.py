from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCENARIO_CATALOG = REPOSITORY_ROOT / "docs" / "USER_SCENARIOS.md"
TRACEABILITY_MATRIX = REPOSITORY_ROOT / "docs" / "TRACEABILITY_MATRIX.md"
REQUIREMENT_LEDGER = REPOSITORY_ROOT / "docs" / "REQUIREMENT_LEDGER.md"
WORKFLOW_COVERAGE = REPOSITORY_ROOT / "docs" / "WORKFLOW_COVERAGE.md"
PROMPT_MANIFEST = REPOSITORY_ROOT / "docs" / "HISTORICAL_PROMPT_MANIFEST.json"

EXPECTED_SCENARIO_IDS = tuple(f"US-{number:03d}" for number in range(1, 34))
EXPECTED_TEST_IDS = tuple(f"AT-US-{number:03d}" for number in range(1, 34))
EXPECTED_POST_EXPORT_REQUIREMENTS = tuple(
    f"REQ-20260803-{number:03d}" for number in range(1, 13)
)

_SCENARIO_HEADING = re.compile(r"^### (US-\d{3}) - .+$", re.MULTILINE)
_MATRIX_SCENARIO = re.compile(r"`(US-\d{3})`")
_MATRIX_TEST = re.compile(r"`(AT-US-\d{3})`")
_PHASE = re.compile(r"(\d+)( Integrations)?")
_WORKFLOW_HEADING = re.compile(r"^### (WF-\d{2}) - .+$", re.MULTILINE)

EXPECTED_WORKFLOW_STATUSES = {
    "verified_local": {
        "US-002",
        "US-003",
        "US-008",
        "US-014",
        "US-015",
        "US-016",
        "US-023",
        "US-025",
    },
    "partial": {
        "US-001",
        "US-011",
        "US-012",
        "US-013",
        "US-017",
        "US-018",
        "US-019",
        "US-020",
        "US-021",
        "US-022",
        "US-026",
        "US-027",
        "US-028",
        "US-031",
    },
    "deployment_pending": {
        "US-006",
        "US-009",
        "US-010",
        "US-030",
        "US-033",
    },
    "designed": {
        "US-004",
        "US-005",
        "US-007",
        "US-024",
        "US-029",
        "US-032",
    },
}


def _scenario_sections(catalog: str) -> list[tuple[str, str]]:
    matches = list(_SCENARIO_HEADING.finditer(catalog))
    sections: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(catalog)
        sections.append((match.group(1), catalog[match.start() : end]))
    return sections


def _continued_bullet(section: str, label: str, next_label: str) -> str:
    pattern = re.compile(
        rf"^- {re.escape(label)}: (?P<value>.*?)(?=^- {re.escape(next_label)}:)",
        re.MULTILINE | re.DOTALL,
    )
    matches = list(pattern.finditer(section))
    assert len(matches) == 1, f"expected one {label!r} bullet"
    return re.sub(r"\s+", " ", matches[0].group("value")).strip()


def _matrix_rows(matrix: str) -> list[tuple[str, str, str, str, str, str]]:
    main_table = matrix.partition("## Cross-cutting gates")[0]
    rows: list[tuple[str, str, str, str, str, str]] = []
    for line in main_table.splitlines():
        if not line.startswith("| `US-"):
            continue
        cells = tuple(cell.strip() for cell in line.strip().strip("|").split("|"))
        assert len(cells) == 6, f"malformed traceability row: {line}"
        rows.append(cells)
    return rows


def test_at_us_025_traceability_is_complete() -> None:
    catalog = SCENARIO_CATALOG.read_text(encoding="utf-8")
    matrix = TRACEABILITY_MATRIX.read_text(encoding="utf-8")

    catalog_claim = re.search(
        r"This catalog turns all (\d+) historical user prompts into (\d+)\s+"
        r"independently\s+testable scenarios\.",
        catalog,
        re.DOTALL,
    )
    assert catalog_claim is not None
    assert tuple(map(int, catalog_claim.groups())) == (77, 33)

    sections = _scenario_sections(catalog)
    catalog_scenario_ids = tuple(scenario_id for scenario_id, _ in sections)
    assert catalog_scenario_ids == EXPECTED_SCENARIO_IDS
    assert Counter(catalog_scenario_ids) == Counter(EXPECTED_SCENARIO_IDS)

    catalog_test_ids: list[str] = []
    catalog_phases: dict[str, str] = {}
    prompt_ids_by_scenario: dict[str, tuple[str, ...]] = {}
    for scenario_id, section in sections:
        sources = _continued_bullet(section, "Sources", "Real expectation")
        assert re.fullmatch(r"historical P-\d{3}(?:, P-\d{3})*\.", sources), (
            f"{scenario_id} has a malformed Sources bullet: {sources}"
        )
        prompt_ids = tuple(re.findall(r"\bP-\d{3}\b", sources))
        assert len(prompt_ids) == len(set(prompt_ids)), (
            f"{scenario_id} maps the same prompt more than once"
        )
        prompt_ids_by_scenario[scenario_id] = prompt_ids

        scope = _continued_bullet(section, "Scope / earliest phase / test", "Acceptance")
        scope_match = re.fullmatch(
            r"(?:Core|Extension) / Phase (\d+(?: Integrations)?) / "
            r"`(AT-US-\d{3})`\.",
            scope,
        )
        assert scope_match is not None, f"{scenario_id} has a malformed scope: {scope}"
        phase, test_id = scope_match.groups()
        assert test_id == f"AT-{scenario_id}"
        catalog_phases[scenario_id] = phase
        catalog_test_ids.append(test_id)

        acceptance_matches = re.findall(r"^- Acceptance: \S", section, re.MULTILINE)
        assert len(acceptance_matches) == 1, f"{scenario_id} needs exactly one acceptance"

    assert tuple(catalog_test_ids) == EXPECTED_TEST_IDS
    assert Counter(catalog_test_ids) == Counter(EXPECTED_TEST_IDS)

    rows = _matrix_rows(matrix)
    matrix_scenario_ids: list[str] = []
    matrix_test_ids: list[str] = []
    for scenario_cell, requirement, contract, phase, test_cell, verification in rows:
        scenario_match = _MATRIX_SCENARIO.fullmatch(scenario_cell)
        test_match = _MATRIX_TEST.fullmatch(test_cell)
        assert scenario_match is not None, f"invalid matrix scenario: {scenario_cell}"
        assert test_match is not None, f"invalid matrix test ID: {test_cell}"
        scenario_id = scenario_match.group(1)
        test_id = test_match.group(1)
        assert test_id == f"AT-{scenario_id}"
        assert _PHASE.fullmatch(phase), f"invalid phase for {scenario_id}: {phase}"
        assert phase == catalog_phases[scenario_id], (
            f"phase mismatch for {scenario_id}: catalog={catalog_phases[scenario_id]}, "
            f"matrix={phase}"
        )
        assert requirement and contract and verification, f"incomplete matrix row for {scenario_id}"
        matrix_scenario_ids.append(scenario_id)
        matrix_test_ids.append(test_id)

    assert tuple(matrix_scenario_ids) == EXPECTED_SCENARIO_IDS
    assert Counter(matrix_scenario_ids) == Counter(EXPECTED_SCENARIO_IDS)
    assert tuple(matrix_test_ids) == EXPECTED_TEST_IDS
    assert Counter(matrix_test_ids) == Counter(EXPECTED_TEST_IDS)

    manifest = json.loads(PROMPT_MANIFEST.read_text(encoding="utf-8"))
    assert set(manifest) == {
        "prompt_count",
        "prompt_ids",
        "response_count",
        "scenario_count",
        "schema_version",
        "source",
    }
    assert manifest["schema_version"] == 2
    assert manifest["source"] == "private historical design research"
    assert manifest["prompt_count"] == 77
    assert manifest["response_count"] == 77
    assert manifest["scenario_count"] == 33
    prompt_ids = tuple(manifest["prompt_ids"])
    assert prompt_ids == tuple(f"P-{number:03d}" for number in range(1, 78))

    referenced_prompt_ids = {
        prompt_id
        for scenario_prompt_ids in prompt_ids_by_scenario.values()
        for prompt_id in scenario_prompt_ids
    }
    assert referenced_prompt_ids == set(prompt_ids), (
        f"missing prompt anchors: {sorted(set(prompt_ids) - referenced_prompt_ids)}; "
        f"unknown prompt anchors: {sorted(referenced_prompt_ids - set(prompt_ids))}"
    )


def test_post_export_requirement_ledger_is_complete() -> None:
    ledger = REQUIREMENT_LEDGER.read_text(encoding="utf-8")
    matrix = TRACEABILITY_MATRIX.read_text(encoding="utf-8")
    headings = tuple(
        re.findall(r"^## (REQ-\d{8}-\d{3}) - .+$", ledger, re.MULTILINE)
    )

    assert headings == EXPECTED_POST_EXPORT_REQUIREMENTS
    assert len(headings) == len(set(headings))
    for requirement_id in headings:
        section_start = ledger.index(f"## {requirement_id} - ")
        next_start = ledger.find("\n## REQ-", section_start + 1)
        section = ledger[section_start : next_start if next_start >= 0 else None]
        assert re.search(r"^- Source: \S", section, re.MULTILINE)
        assert re.search(r"^- Maps to: .*`US-\d{3}`", section, re.MULTILINE)
        assert re.search(r"^- Acceptance: \S", section, re.MULTILINE)
        assert matrix.count(f"`{requirement_id}`") == 1


def test_every_scenario_has_one_valid_workflow_and_status_checklist_row() -> None:
    content = WORKFLOW_COVERAGE.read_text(encoding="utf-8")
    workflow_ids = tuple(_WORKFLOW_HEADING.findall(content))
    assert workflow_ids == tuple(f"WF-{number:02d}" for number in range(15))
    assert len(workflow_ids) == len(set(workflow_ids))

    rows: list[tuple[str, ...]] = []
    for line in content.splitlines():
        if not line.startswith("| [x] | `US-"):
            continue
        cells = tuple(cell.strip() for cell in line.strip().strip("|").split("|"))
        assert len(cells) == 7, f"malformed workflow checklist row: {line}"
        rows.append(cells)

    scenario_ids = tuple(cell[1].strip("`") for cell in rows)
    assert scenario_ids == EXPECTED_SCENARIO_IDS
    assert len(scenario_ids) == len(set(scenario_ids))

    observed_statuses = {status: set() for status in EXPECTED_WORKFLOW_STATUSES}
    referenced_workflows: set[str] = set()
    for check, scenario_cell, status_cell, primary_cell, supporting, proof, gap in rows:
        scenario_id = scenario_cell.strip("`")
        status = status_cell.strip("`")
        primary = primary_cell.strip("`")
        assert check == "[x]"
        assert status in observed_statuses, f"unknown status for {scenario_id}: {status}"
        assert primary in workflow_ids, f"unknown primary workflow for {scenario_id}"
        assert proof and gap
        observed_statuses[status].add(scenario_id)
        referenced_workflows.add(primary)
        for supporting_id in re.findall(r"`(WF-\d{2})`", supporting):
            assert supporting_id in workflow_ids
            assert supporting_id != primary
            referenced_workflows.add(supporting_id)

    assert observed_statuses == EXPECTED_WORKFLOW_STATUSES
    assert referenced_workflows == set(workflow_ids)
