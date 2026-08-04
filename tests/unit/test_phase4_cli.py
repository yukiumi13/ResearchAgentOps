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
    ImpactDisposition,
    ReviewDisposition,
)
from researchctl.domain.models import ReportProposal, ResearchSubmission
from researchctl.serialization import dump_yaml
from researchctl.services.application import ServiceResult
from researchctl.services.requests import (
    ImpactBatchCreateRequest,
    ImpactCreateRequest,
    ImpactDecisionCreateRequest,
    ReportStatusRequest,
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
                    "delivery": {
                        "branch": {
                            "branch": (
                                "research/submission/"
                                f"{request.submission.submission_id}"
                            ),
                            "commit": "6" * 40,
                            "pushed": True,
                        },
                        "pull_request": {
                            "repository": "owner/project",
                            "number": 17,
                            "url": (
                                "https://github.example.invalid/"
                                "owner/project/pull/17"
                            ),
                            "state": "open",
                            "head_commit": "6" * 40,
                            "created": True,
                        },
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

    def impact_create(
        self,
        request: ImpactCreateRequest,
        actor: object,
    ) -> ServiceResult:
        self.calls.append(_Call("impact_create", request, actor))
        return ServiceResult(
            command="impact.create",
            operation_id=request.operation_id,
            terminal_result="proposal_open",
            data={
                "impact": {
                    "bundle": {
                        "impact_id": request.impact_id,
                        "report_id": request.report_id,
                        "proposed_report_revision": (
                            request.expected_report_revision + 1
                        ),
                        "outcome": "overlap",
                    },
                    "proposal": {
                        "branch": f"research/impact/{request.impact_id}",
                        "commit": "8" * 40,
                    },
                    "delivery": {
                        "pull_request": {
                            "number": 18,
                            "url": "https://github.example.invalid/owner/project/pull/18",
                        }
                    },
                }
            },
        )

    def impact_batch_create(
        self,
        request: ImpactBatchCreateRequest,
        actor: object,
    ) -> ServiceResult:
        self.calls.append(_Call("impact_batch_create", request, actor))
        return ServiceResult(
            command="impact.batch",
            operation_id=request.operation_id,
            terminal_result="no_change",
            data={
                "impact": {
                    "terminal_result": "no_change",
                    "analysis": {
                        "report_ids": [],
                        "before_commit": request.before_commit,
                        "target_commit": request.target_commit,
                    },
                    "bundle": None,
                    "proposal": None,
                    "delivery": None,
                }
            },
        )

    def impact_decide(
        self,
        request: ImpactDecisionCreateRequest,
        actor: object,
    ) -> ServiceResult:
        self.calls.append(_Call("impact_decide", request, actor))
        return ServiceResult(
            command="impact.decide",
            operation_id=request.operation_id,
            terminal_result="proposal_open",
            data={
                "impact_decision": {
                    "bundle": {
                        "decision_id": request.decision_id,
                        "report_id": request.report_id,
                        "report_revision": request.expected_report_revision + 1,
                        "disposition": request.disposition.value,
                    },
                    "proposal": {
                        "branch": f"research/impact-decision/{request.decision_id}",
                        "commit": "9" * 40,
                    },
                    "delivery": {
                        "pull_request": {
                            "number": 19,
                            "url": "https://github.example.invalid/owner/project/pull/19",
                        }
                    },
                }
            },
        )

    def report_status(
        self,
        request: ReportStatusRequest,
        actor: object,
    ) -> dict[str, object]:
        self.calls.append(_Call("report_status", request, actor))
        return {
            "report_id": request.report_id,
            "report_revision": 2,
            "target_commit": request.target_commit or "a" * 40,
            "target_tree": "b" * 40,
            "evidence_status": "verified",
            "stored_applicability": "current",
            "effective_applicability": "impact_pending",
            "reason": "governed_code_changed_since_validation",
            "changed_paths": ["src/evaluator.py"],
        }


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
    monkeypatch.setattr(
        factory,
        "open_impact_automation_application",
        open_application,
    )
    return service, actor, opens


def _payload(request: object) -> str:
    assert isinstance(
        request,
        (
            SubmissionCreateRequest,
            ReviewAcceptRequest,
            ImpactCreateRequest,
            ImpactDecisionCreateRequest,
            ReportStatusRequest,
        ),
    )
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
    impact = ImpactCreateRequest(
        operation_id=_id("operation", "3"),
        idempotency_key="impact-stopping-policy",
        impact_id=_id("impact", "8"),
        report_id=proposal.report_id,
        expected_report_revision=1,
        target_commit="8" * 40,
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
        (
            [
                "impact",
                impact.report_id,
                "--expected-report-revision",
                str(impact.expected_report_revision),
                "--target-commit",
                impact.target_commit,
                "--impact-id",
                impact.impact_id,
                "--operation-id",
                impact.operation_id,
                "--idempotency-key",
                impact.idempotency_key,
            ],
            ["impact"],
            impact,
            "impact_create",
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
            assert "Pull request: #17" in human.output
            assert "Proposal opened: human review" in human.output
            assert "Next: push this branch" not in human.output
        elif method == "review_accept":
            assert "Prepared only: exact-head CI" in human.output
        else:
            assert "Pull request: #18" in human.output
            assert "no Report validity changed" in human.output

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

    assert opens == [(tmp_path, {}), (tmp_path, {})] * 3


def test_report_status_human_and_json_paths_share_the_read_service(
    tmp_path: Path,
    phase4_cli_spy,
) -> None:
    service, actor, opens = phase4_cli_spy
    runner = CliRunner()
    request = ReportStatusRequest(
        report_id=_id("report", "3"),
        target_commit="c" * 40,
    )

    human = runner.invoke(
        app,
        [
            "report",
            "status",
            request.report_id,
            "--target-commit",
            request.target_commit,
            "--project",
            str(tmp_path),
        ],
    )
    assert human.exit_code == 0, human.output
    assert "Stored applicability: current" in human.output
    assert "Effective applicability: impact_pending" in human.output
    assert "changed src/evaluator.py" in human.output

    machine = runner.invoke(
        app,
        ["report", "status", "--json", "--project", str(tmp_path)],
        input=_payload(request),
    )
    assert machine.exit_code == 0, machine.output
    output = json.loads(machine.output)
    assert output["data"]["effective_applicability"] == "impact_pending"
    assert service.calls == [
        _Call("report_status", request, actor),
        _Call("report_status", request, actor),
    ]
    assert opens == [(tmp_path, {}), (tmp_path, {})]


def test_manager_impact_decision_human_and_json_requests_are_identical(
    tmp_path: Path,
    phase4_cli_spy,
) -> None:
    service, actor, opens = phase4_cli_spy
    runner = CliRunner()
    request = ImpactDecisionCreateRequest(
        operation_id=_id("operation", "4"),
        idempotency_key="keep-report-stale",
        decision_id=_id("decision", "4"),
        impact_id=_id("impact", "4"),
        report_id=_id("report", "4"),
        expected_report_revision=2,
        expected_impact_digest="sha256:" + "4" * 64,
        target_commit="4" * 40,
        disposition=ImpactDisposition.KEEP_STALE,
        reason="Wait for the rerun plan review.",
    )
    args = [
        "review",
        "impact",
        request.impact_id,
        request.report_id,
        "--expected-report-revision",
        str(request.expected_report_revision),
        "--expected-impact-digest",
        request.expected_impact_digest,
        "--target-commit",
        request.target_commit,
        "--disposition",
        request.disposition.value,
        "--reason",
        request.reason,
        "--decision-id",
        request.decision_id,
        "--operation-id",
        request.operation_id,
        "--idempotency-key",
        request.idempotency_key,
        "--project",
        str(tmp_path),
    ]

    human = runner.invoke(app, args)
    assert human.exit_code == 0, human.output
    assert "Disposition: keep_stale" in human.output
    assert "no experiment was started" in human.output

    machine = runner.invoke(
        app,
        ["review", "impact", "--json", "--project", str(tmp_path)],
        input=_payload(request),
    )
    assert machine.exit_code == 0, machine.output
    assert service.calls == [
        _Call("impact_decide", request, actor),
        _Call("impact_decide", request, actor),
    ]
    assert opens == [(tmp_path, {}), (tmp_path, {})]


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


def test_ci_impact_derives_stable_batch_identity_and_uses_application_service(
    tmp_path: Path,
    phase4_cli_spy,
) -> None:
    from researchctl.ci_cli import _impact_automation_id

    service, actor, opens = phase4_cli_spy
    before = "a" * 40
    target = "b" * 40
    generated_at = "2026-08-03T12:00:00Z"
    runner = CliRunner()
    arguments = [
        "ci",
        "impact",
        "--project",
        str(tmp_path),
        "--before",
        before,
        "--after",
        target,
        "--generated-at",
        generated_at,
    ]

    first = runner.invoke(app, arguments)
    second = runner.invoke(app, arguments)

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert "No Impact proposal was needed" in first.output
    assert len(service.calls) == 2
    first_request = service.calls[0].request
    assert isinstance(first_request, ImpactBatchCreateRequest)
    assert service.calls[0] == _Call("impact_batch_create", first_request, actor)
    assert service.calls[1].request == first_request
    assert first_request.impact_id == _impact_automation_id(
        "impact",
        generated_at=generated_at,
        before_commit=before,
        target_commit=target,
    )
    assert first_request.operation_id == _impact_automation_id(
        "operation",
        generated_at=generated_at,
        before_commit=before,
        target_commit=target,
    )
    assert opens == [(tmp_path, {}), (tmp_path, {})]


@pytest.mark.parametrize(
    "field",
    ["repository", "head_branch", "base_branch", "pull_request_body"],
)
def test_submit_json_cannot_select_pull_request_delivery_fields(
    tmp_path: Path,
    submission_payload,
    phase4_cli_spy,
    field: str,
) -> None:
    service, _actor, opens = phase4_cli_spy
    create = _cases(tmp_path, submission_payload)[0][2]
    assert isinstance(create, SubmissionCreateRequest)
    payload = create.model_dump(mode="json", exclude_none=True)
    payload[field] = "attacker-selected"

    result = CliRunner().invoke(
        app,
        ["submit", "--json", "--project", str(tmp_path)],
        input=json.dumps(payload),
    )

    assert result.exit_code == 2
    output = json.loads(result.output)
    assert output["errors"][0]["code"] == "validation_error"
    assert service.calls == []
    assert opens == []
