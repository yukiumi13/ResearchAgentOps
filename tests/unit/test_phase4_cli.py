from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from researchctl.cli import app
from researchctl.domain.enums import (
    ClaimScope,
    CodeDisposition,
    ReviewDisposition,
)
from researchctl.domain.models import ReportProposal, ResearchSubmission
from researchctl.serialization import dump_yaml
from researchctl.services.application import ServiceResult
from researchctl.services.requests import (
    ReviewAcceptRequest,
    SubmissionCreateRequest,
)


def _id(kind: str, fill: str) -> str:
    return f"{kind}_20260803T120000Z_{fill * 24}"


@dataclass
class _Call:
    method: str
    request: object
    actor: object


class _SpyService:
    def __init__(self) -> None:
        self.calls: list[_Call] = []

    def submission_create(
        self,
        request: SubmissionCreateRequest,
        actor: object,
    ) -> ServiceResult:
        self.calls.append(_Call("submission_create", request, actor))
        return ServiceResult(
            command="submission.create",
            operation_id=request.operation_id,
            terminal_result="proposal_open",
            data={
                "submission": {
                    "bundle": {
                        "submission_id": request.submission.submission_id,
                        "manifest_digest": "sha256:" + "4" * 64,
                    },
                    "proposal": {
                        "branch": (
                            "research/submission/"
                            f"{request.submission.submission_id}"
                        ),
                        "commit": "6" * 40,
                    },
                }
            },
        )

    def review_accept(
        self,
        request: ReviewAcceptRequest,
        actor: object,
    ) -> ServiceResult:
        self.calls.append(_Call("review_accept", request, actor))
        return ServiceResult(
            command="review.accept",
            operation_id=request.operation_id,
            terminal_result="acceptance_prepared",
            data={
                "review": {
                    "bundle": {
                        "submission_id": request.submission_id,
                        "report_id": _id("report", "9"),
                        "report_revision": request.expected_report_revision + 1,
                    },
                    "proposal": {"commit": "7" * 40},
                }
            },
        )


class _Handle:
    def __init__(self, service: _SpyService, actor: object) -> None:
        self.service = service
        self.actor = actor

    def __enter__(self) -> _Handle:
        return self

    def __exit__(self, *_: object) -> None:
        return None


@pytest.fixture
def phase4_cli_spy(monkeypatch: pytest.MonkeyPatch):
    from researchctl.services import factory

    service = _SpyService()
    actor = object()
    opens: list[tuple[Path, dict[str, Any]]] = []

    def open_application(path: Path, **options: Any) -> _Handle:
        opens.append((path, options))
        return _Handle(service, actor)

    monkeypatch.setattr(factory, "open_application", open_application)
    return service, actor, opens


def _payload(request: object) -> str:
    assert isinstance(request, (SubmissionCreateRequest, ReviewAcceptRequest))
    return json.dumps(request.model_dump(mode="json", exclude_none=True))


def _cases(
    tmp_path: Path,
    submission_payload,
) -> list[tuple[list[str], list[str], object, str]]:
    submission = ResearchSubmission.model_validate(
        submission_payload(state="open")
    )
    proposal = ReportProposal(
        submission_id=submission.submission_id,
        report_id=_id("report", "9"),
        expected_report_revision=0,
        title="Stopping policy result",
        evidence_tree="a" * 40,
    )
    submission_path = tmp_path / "submission.yaml"
    proposal_path = tmp_path / "report-proposal.yaml"
    submission_path.write_text(dump_yaml(submission), encoding="utf-8")
    proposal_path.write_text(dump_yaml(proposal), encoding="utf-8")
    create = SubmissionCreateRequest(
        operation_id=_id("operation", "1"),
        idempotency_key="submit-stopping-policy",
        base_commit="b" * 40,
        submission=submission,
        report_proposal=proposal,
        run_ids=(_id("run", "c"),),
    )
    accept = ReviewAcceptRequest(
        operation_id=_id("operation", "2"),
        idempotency_key="accept-stopping-policy",
        submission_id=submission.submission_id,
        task_id=submission.task_id,
        expected_head="6" * 40,
        decision_id=_id("decision", "7"),
        expected_report_revision=0,
        disposition=ReviewDisposition.ACCEPTED_WITH_CONDITIONS,
        conditions=("Re-run after the next dataset revision.",),
        claim_scope=ClaimScope.BASELINE,
        code_disposition=CodeDisposition.MERGE,
    )
    return [
        (
            [
                "submit",
                create.run_ids[0],
                "--submission-file",
                str(submission_path),
                "--report-proposal-file",
                str(proposal_path),
                "--base-commit",
                create.base_commit,
                "--operation-id",
                create.operation_id,
                "--idempotency-key",
                create.idempotency_key,
            ],
            ["submit"],
            create,
            "submission_create",
        ),
        (
            [
                "review",
                "accept",
                accept.submission_id,
                "--task-id",
                accept.task_id,
                "--expected-head",
                accept.expected_head,
                "--expected-report-revision",
                str(accept.expected_report_revision),
                "--decision-id",
                accept.decision_id,
                "--disposition",
                accept.disposition.value,
                "--condition",
                accept.conditions[0],
                "--claim-scope",
                accept.claim_scope.value,
                "--code-disposition",
                accept.code_disposition.value,
                "--operation-id",
                accept.operation_id,
                "--idempotency-key",
                accept.idempotency_key,
            ],
            ["review", "accept"],
            accept,
            "review_accept",
        ),
    ]


def test_human_flags_and_agent_json_are_phase4_request_parity(
    tmp_path: Path,
    submission_payload,
    phase4_cli_spy,
) -> None:
    service, actor, opens = phase4_cli_spy
    runner = CliRunner()

    for human_args, machine_args, expected, method in _cases(
        tmp_path,
        submission_payload,
    ):
        call_count = len(service.calls)
        human = runner.invoke(
            app,
            [*human_args, "--project", str(tmp_path)],
        )
        assert human.exit_code == 0, human.output
        human_call = service.calls[call_count]
        assert human_call == _Call(method, expected, actor)
        assert f"Operation: {expected.operation_id}" in human.output
        if method == "submission_create":
            assert "Next: push this branch, open a PR" in human.output
        else:
            assert "Prepared only: exact-head CI" in human.output

        machine = runner.invoke(
            app,
            [*machine_args, "--json", "--project", str(tmp_path)],
            input=_payload(expected),
        )
        assert machine.exit_code == 0, machine.output
        machine_call = service.calls[call_count + 1]
        assert machine_call.method == human_call.method
        assert machine_call.request == human_call.request
        assert machine_call.actor is actor
        output = json.loads(machine.output)
        assert output["success"] is True
        assert output["errors"] == []
        assert "Operation:" not in machine.output

    assert opens == [(tmp_path, {}), (tmp_path, {})] * 2


def test_phase4_agent_json_rejects_non_protocol_control_fields(
    tmp_path: Path,
    submission_payload,
    phase4_cli_spy,
) -> None:
    service, _actor, opens = phase4_cli_spy
    runner = CliRunner()
    cases = _cases(tmp_path, submission_payload)

    create = cases[0][2]
    assert isinstance(create, SubmissionCreateRequest)
    create_payload = create.model_dump(mode="json", exclude_none=True)
    create_payload["accepted"] = True
    invalid_create = runner.invoke(
        app,
        ["submit", "--json", "--project", str(tmp_path)],
        input=json.dumps(create_payload),
    )

    accept = cases[1][2]
    assert isinstance(accept, ReviewAcceptRequest)
    accept_payload = accept.model_dump(mode="json", exclude_none=True)
    accept_payload["linear_issue_id"] = "44444444-4444-4444-8444-444444444444"
    invalid_accept = runner.invoke(
        app,
        ["review", "accept", "--json", "--project", str(tmp_path)],
        input=json.dumps(accept_payload),
    )

    for result, command in (
        (invalid_create, "submission.create"),
        (invalid_accept, "review.accept"),
    ):
        assert result.exit_code == 2
        output = json.loads(result.output)
        assert output["command"] == command
        assert output["success"] is False
        assert output["errors"][0]["code"] == "validation_error"
    assert service.calls == []
    assert opens == []
