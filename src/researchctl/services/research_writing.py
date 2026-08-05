from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from typing import Literal

from researchctl.domain.models import AnalysisBrief, ResearchUpdate
from researchctl.errors import RCPError


ANALYSIS_BRIEF_RENDERER_ID = "research-analysis-brief.v2"
ANALYSIS_BRIEF_RENDERER_VERSION = 2
RESEARCH_UPDATE_RENDERER_ID = "linear.research-update.v2"
RESEARCH_UPDATE_RENDERER_VERSION = 2

_ENGLISH_WORD = re.compile(r"[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)*")
_CJK_CHARACTER = re.compile(
    "[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]"
)
_SENTENCE_TERMINATORS = frozenset(".!?。！？")


@dataclass(frozen=True, slots=True)
class ProseMeasure:
    english_words: int
    cjk_characters: int
    sentences: int

    def as_dict(self) -> dict[str, int]:
        return {
            "english_words": self.english_words,
            "cjk_characters": self.cjk_characters,
            "sentences": self.sentences,
        }


@dataclass(frozen=True, slots=True)
class WritingFinding:
    code: str
    field_path: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "field_path": self.field_path,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class WritingLintResult:
    kind: Literal["analysis_brief", "research_update"]
    findings: tuple[WritingFinding, ...]
    prose: ProseMeasure

    @property
    def passed(self) -> bool:
        return not self.findings

    @property
    def terminal_result(self) -> Literal["passed", "invalid"]:
        return "passed" if self.passed else "invalid"

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "terminal_result": self.terminal_result,
            "prose": self.prose.as_dict(),
            "findings": [finding.as_dict() for finding in self.findings],
        }


def measure_prose(value: str) -> ProseMeasure:
    sentences = 0
    for index, character in enumerate(value):
        if character not in _SENTENCE_TERMINATORS:
            continue
        previous = value[index - 1] if index else ""
        following = value[index + 1] if index + 1 < len(value) else ""
        if character == "." and previous.isdigit() and following.isdigit():
            continue
        if following and not following.isspace() and following not in _SENTENCE_TERMINATORS:
            if character == ".":
                continue
        if following in _SENTENCE_TERMINATORS:
            continue
        sentences += 1
    if value.strip() and sentences == 0:
        sentences = 1
    return ProseMeasure(
        english_words=len(_ENGLISH_WORD.findall(value)),
        cjk_characters=len(_CJK_CHARACTER.findall(value)),
        sentences=sentences,
    )


def _add_text_findings(
    findings: list[WritingFinding],
    *,
    field_path: str,
    value: str,
    max_english_words: int,
    max_cjk_characters: int,
    max_sentences: int,
) -> ProseMeasure:
    measure = measure_prose(value)
    for observed, limit, suffix, label in (
        (measure.english_words, max_english_words, "english_words", "English words"),
        (measure.cjk_characters, max_cjk_characters, "cjk_characters", "CJK characters"),
        (measure.sentences, max_sentences, "sentences", "sentences"),
    ):
        if observed > limit:
            findings.append(
                WritingFinding(
                    code=f"prose_{suffix}_exceeded",
                    field_path=field_path,
                    message=f"{field_path} has {observed} {label}; maximum is {limit}.",
                )
            )
    return measure


def _sum_measures(values: list[ProseMeasure]) -> ProseMeasure:
    return ProseMeasure(
        english_words=sum(value.english_words for value in values),
        cjk_characters=sum(value.cjk_characters for value in values),
        sentences=sum(value.sentences for value in values),
    )


def lint_analysis_brief(brief: AnalysisBrief) -> WritingLintResult:
    findings: list[WritingFinding] = []
    measures = [
        _add_text_findings(
            findings,
            field_path="question",
            value=brief.question,
            max_english_words=40,
            max_cjk_characters=100,
            max_sentences=2,
        ),
        _add_text_findings(
            findings,
            field_path="conclusion",
            value=brief.conclusion,
            max_english_words=60,
            max_cjk_characters=140,
            max_sentences=2,
        ),
    ]
    for label, values in (
        ("interpretation", brief.interpretation),
        ("limitations", brief.limitations),
    ):
        for index, value in enumerate(values):
            measures.append(
                _add_text_findings(
                    findings,
                    field_path=f"{label}.{index}",
                    value=value,
                    max_english_words=45,
                    max_cjk_characters=120,
                    max_sentences=2,
                )
            )
    prose = _sum_measures(measures)
    if prose.english_words > 350:
        findings.append(
            WritingFinding(
                code="brief_english_words_exceeded",
                field_path="$",
                message=(
                    f"Analysis brief has {prose.english_words} English prose words; "
                    "maximum is 350."
                ),
            )
        )
    if prose.cjk_characters > 700:
        findings.append(
            WritingFinding(
                code="brief_cjk_characters_exceeded",
                field_path="$",
                message=(
                    f"Analysis brief has {prose.cjk_characters} CJK prose characters; "
                    "maximum is 700."
                ),
            )
        )
    return WritingLintResult(
        kind="analysis_brief",
        findings=tuple(findings),
        prose=prose,
    )


def lint_research_update(update: ResearchUpdate) -> WritingLintResult:
    findings: list[WritingFinding] = []
    measures = [
        _add_text_findings(
            findings,
            field_path="summary",
            value=update.summary,
            max_english_words=60,
            max_cjk_characters=140,
            max_sentences=2,
        )
    ]
    for index, evidence in enumerate(update.evidence):
        measures.append(
            _add_text_findings(
                findings,
                field_path=f"evidence.{index}.value",
                value=evidence.value,
                max_english_words=30,
                max_cjk_characters=80,
                max_sentences=1,
            )
        )
    if update.next_action is not None:
        measures.append(
            _add_text_findings(
                findings,
                field_path="next_action",
                value=update.next_action,
                max_english_words=40,
                max_cjk_characters=100,
                max_sentences=1,
            )
        )
    prose = _sum_measures(measures)
    if prose.english_words > 100:
        findings.append(
            WritingFinding(
                code="update_english_words_exceeded",
                field_path="$",
                message=(
                    f"Research update has {prose.english_words} English prose words; "
                    "maximum is 100."
                ),
            )
        )
    if prose.cjk_characters > 220:
        findings.append(
            WritingFinding(
                code="update_cjk_characters_exceeded",
                field_path="$",
                message=(
                    f"Research update has {prose.cjk_characters} CJK prose characters; "
                    "maximum is 220."
                ),
            )
        )
    return WritingLintResult(
        kind="research_update",
        findings=tuple(findings),
        prose=prose,
    )


def _require_passing(result: WritingLintResult) -> None:
    if result.passed:
        return
    raise RCPError(
        code=f"{result.kind}_lint_invalid",
        message=f"{result.kind} does not satisfy the concise writing contract.",
        remediation="Shorten the named fields and rerun the writing lint.",
        context=result.as_dict(),
    )


def _text(value: object) -> str:
    escaped = html.escape(str(value), quote=True).replace("\\", "\\\\")
    for character in ("`", "*", "_", "[", "]", "#", "|"):
        escaped = escaped.replace(character, f"\\{character}")
    return "<br>".join(escaped.replace("\r\n", "\n").replace("\r", "\n").split("\n"))


def _code(value: object) -> str:
    rendered = str(value).replace("\r", " ").replace("\n", " ")
    delimiter = "`" if "`" not in rendered else "``"
    return f"{delimiter}{rendered}{delimiter}"


def _visible_marker(renderer_id: str) -> str:
    return f"> Renderer: {_code(f'researchctl-renderer:{renderer_id}')}"


def _brief_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return json.dumps(value, allow_nan=False)
    return _text(value)


def render_analysis_brief(brief: AnalysisBrief) -> bytes:
    _require_passing(lint_analysis_brief(brief))
    metric_keys = tuple(metric.key for metric in brief.metrics)
    header = ["Setting", *(metric.label for metric in brief.metrics), "Sources"]
    lines = [
        f"# {_text(brief.question)}",
        "",
        _visible_marker(ANALYSIS_BRIEF_RENDERER_ID),
        "",
        "## Answer",
        "",
        _text(brief.conclusion),
        "",
        "## Evidence",
        "",
        f"Protocol: {_code(brief.protocol)}",
        "",
        "| " + " | ".join(_text(value) for value in header) + " |",
        "| " + " | ".join("---" if index == 0 else "---:" for index in range(len(header))) + " |",
    ]
    for row in brief.evidence:
        cells = [
            _text(row.setting),
            *(_brief_value(row.values[key]) for key in metric_keys),
            ", ".join(_code(key) for key in row.source_keys),
        ]
        lines.append("| " + " | ".join(cells) + " |")
    if brief.interpretation:
        lines.extend(["", "## Interpretation", ""])
        lines.extend(f"- {_text(value)}" for value in brief.interpretation)
    if brief.limitations:
        lines.extend(["", "## Limits", ""])
        lines.extend(f"- {_text(value)}" for value in brief.limitations)
    lines.extend(["", "## Sources", ""])
    lines.extend(f"- {_code(source.key)}: {_code(source.location)}" for source in brief.sources)
    return ("\n".join(lines) + "\n").encode("utf-8")


def render_research_update(update: ResearchUpdate) -> bytes:
    _require_passing(lint_research_update(update))
    event_labels = {
        "started": "Started",
        "completed": "Completed",
        "failed": "Failed",
        "conclusion_changed": "Conclusion changed",
    }
    context = tuple(
        value for value in (update.task_key, update.session_label) if value is not None
    )
    heading = event_labels[update.event]
    if context:
        heading += " · " + " · ".join(_text(value) for value in context)
    lines = [f"**{heading}**", "", _text(update.summary)]
    if update.evidence:
        lines.extend(["", "Evidence:"])
        lines.extend(
            f"- **{_text(item.kind)}:** {_text(item.value)}"
            for item in update.evidence
        )
    lines.extend(
        [
            "",
            f"Source: {_code(update.source.key)} · {_code(update.source.location)}",
            (
                f"Session: {_code(update.session_id)} · "
                f"Commit: {_code(update.source_commit)} · "
                f"Observed: {_code(update.observed_at.date().isoformat())}"
            ),
        ]
    )
    if update.next_action is not None:
        lines.extend(["", f"Next: {_text(update.next_action)}"])
    lines.extend(["", _visible_marker(RESEARCH_UPDATE_RENDERER_ID)])
    return ("\n".join(lines) + "\n").encode("utf-8")
