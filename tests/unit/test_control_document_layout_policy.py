from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from researchctl.cli import app
from researchctl.constants import PROJECT_POLICY_PATH
from researchctl.domain.enums import ProjectState
from researchctl.domain.models import DocumentLayoutPolicy, ProjectPolicy, ProjectRecord
from researchctl.errors import RCPError
from researchctl.serialization import dump_yaml, load_model
from researchctl.services.actor import ActorContext, ActorRole, CredentialKind
from researchctl.services.control_document_layout_policy import (
    ControlDocumentLayoutPolicyRepository,
)
from researchctl.services.factory import open_application
from researchctl.services.requests import DocumentLayoutConfigureRequest


OPERATION_ID = "operation_20260804T140000Z_" + "d" * 24
OTHER_OPERATION_ID = "operation_20260804T140001Z_" + "e" * 24


def _git(repository: Path, *arguments: str) -> str:
    environment = os.environ.copy()
    for key in tuple(environment):
        if key.startswith("GIT_CONFIG_") or key in {
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            "GIT_COMMON_DIR",
            "GIT_DIR",
            "GIT_INDEX_FILE",
            "GIT_OBJECT_DIRECTORY",
            "GIT_WORK_TREE",
        }:
            environment.pop(key, None)
    environment.pop("GIT_CONFIG_PARAMETERS", None)
    return subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        env=environment,
        shell=False,
        text=True,
    ).stdout


def _commit_managed(repository: Path) -> str:
    project_path = repository / ".research" / "project.yaml"
    project = load_model(project_path, ProjectRecord)
    project_path.write_text(
        dump_yaml(project.model_copy(update={"state": ProjectState.MANAGED})),
        encoding="utf-8",
    )
    _git(repository, "add", ".researchctl.toml", ".research")
    _git(
        repository,
        "-c",
        "user.name=Tests",
        "-c",
        "user.email=tests@example.invalid",
        "commit",
        "--no-gpg-sign",
        "-m",
        "accept managed fixture",
    )
    return _git(repository, "rev-parse", "HEAD").strip()


def _layout() -> DocumentLayoutPolicy:
    payload = DocumentLayoutPolicy().model_dump(mode="json")
    payload["routes"].append(
        {
            "classification": "research/evidence:ledger",
            "document_type": "ledger",
            "directory": "docs/ledger",
            "contract": "markdown-frontmatter",
            "rationale": "Existing evidence ledgers require this route.",
            "required_relations": [],
        }
    )
    return DocumentLayoutPolicy.model_validate(payload)


def _control(
    repository: Path,
    *,
    operation_id: str,
    expected_head: str,
) -> ControlDocumentLayoutPolicyRepository:
    worktrees = repository / ".git" / "researchctl" / "worktrees"
    worktrees.mkdir(parents=True, exist_ok=True)
    return ControlDocumentLayoutPolicyRepository(
        repository_root=repository,
        worktrees_directory=worktrees,
        default_branch="main",
        operation_id=operation_id,
        expected_default_head=expected_head,
    )


def test_document_layout_policy_changes_only_project_policy_and_retry_is_stable(
    initialized_repository: Path,
) -> None:
    base = _commit_managed(initialized_repository)
    accepted = load_model(
        initialized_repository / PROJECT_POLICY_PATH,
        ProjectPolicy,
    )
    requested = _layout()

    first = _control(
        initialized_repository,
        operation_id=OPERATION_ID,
        expected_head=base,
    ).configure(requested)
    repeated = _control(
        initialized_repository,
        operation_id=OPERATION_ID,
        expected_head=base,
    ).configure(requested)

    expected = accepted.model_copy(update={"document_layout": requested})
    assert first.project_policy == expected
    assert first.proposal.effect_applied is True
    assert repeated.proposal.changed is False
    assert repeated.proposal.commit == first.proposal.commit
    assert _git(
        initialized_repository,
        "show",
        "-s",
        "--format=%B",
        first.proposal.commit,
    ).rstrip("\n") == f"researchctl: doc.configure-layout {OPERATION_ID}"
    assert _git(
        initialized_repository,
        "show",
        f"{first.proposal.commit}:{PROJECT_POLICY_PATH}",
    ) == dump_yaml(expected)


def test_application_journals_manager_layout_policy_and_denies_agent(
    initialized_repository: Path,
) -> None:
    base = _commit_managed(initialized_repository)
    request = DocumentLayoutConfigureRequest(
        operation_id=OPERATION_ID,
        idempotency_key="document-layout-configure",
        expected_default_head=base,
        document_layout=_layout(),
    )
    options = {
        "local_host": "host-a",
        "environment": {},
        "document_layout_operation_id": request.operation_id,
        "document_layout_expected_default_head": base,
    }

    with open_application(initialized_repository, **options) as handle:
        first = handle.service.document_layout_configure(request, handle.actor)
    with open_application(initialized_repository, **options) as handle:
        repeated = handle.service.document_layout_configure(request, handle.actor)

    assert repeated == first
    assert first.terminal_result == "proposal_prepared"
    assert first.data["path"] == PROJECT_POLICY_PATH

    denied_request = request.model_copy(
        update={
            "operation_id": OTHER_OPERATION_ID,
            "idempotency_key": "document-layout-agent-denied",
        }
    )
    agent = ActorContext(
        actor_id="agent-session-test",
        role=ActorRole.AGENT,
        credential_kind=CredentialKind.SESSION_CAPABILITY,
        bound_session_id="session_20260804T140000Z_" + "f" * 24,
    )
    with open_application(
        initialized_repository,
        local_host="host-a",
        environment={},
        document_layout_operation_id=OTHER_OPERATION_ID,
        document_layout_expected_default_head=base,
    ) as handle:
        with pytest.raises(RCPError) as denied:
            handle.service.document_layout_configure(denied_request, agent)
    assert denied.value.code == "authorization_denied"


def test_document_layout_cli_has_human_and_json_idempotent_outputs(
    initialized_repository: Path,
) -> None:
    base = _commit_managed(initialized_repository)
    policy_file = initialized_repository / "document-layout.yaml"
    policy_file.write_text(dump_yaml(_layout()), encoding="utf-8")
    arguments = [
        "doc",
        "configure-layout",
        "--policy-file",
        str(policy_file),
        "--expected-default-head",
        base,
        "--project",
        str(initialized_repository),
        "--operation-id",
        OPERATION_ID,
        "--idempotency-key",
        "document-layout-cli",
    ]

    human = CliRunner().invoke(app, arguments)
    machine = CliRunner().invoke(app, [*arguments, "--json"])

    assert human.exit_code == 0, human.stdout
    assert f"Operation: {OPERATION_ID}" in human.stdout
    assert "Outcome: proposal_prepared" in human.stdout
    assert machine.exit_code == 0, machine.stdout
    assert '"command": "doc.configure-layout"' in machine.stdout
    assert '"terminal_result": "proposal_prepared"' in machine.stdout
