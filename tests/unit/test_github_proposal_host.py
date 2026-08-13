from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from researchctl.adapters.github_app import GitHubAppInstallationCredential
from researchctl.domain.enums import ProjectState, SessionState
from researchctl.domain.models import (
    GitHubGovernancePolicy,
    ProjectPolicy,
    ProjectRecord,
    ReportProposal,
    ResearchSubmission,
)
from researchctl.errors import RCPError
from researchctl.github_proposal_host import PinnedGitCommandRunner, run_submission
from researchctl.runtime import RuntimeSession, RuntimeStore, hash_session_token
from researchctl.serialization import dump_yaml, load_model
from researchctl.services.project_runtime import discover_managed_project
from researchctl.services.requests import SubmissionCreateRequest

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
TOKEN = "github_pat_" + "t" * 48
SESSION_TOKEN = "session-capability-token"


class _Issuer:
    def __init__(self) -> None:
        self.calls: list[GitHubGovernancePolicy] = []

    def issue(self, governance: GitHubGovernancePolicy) -> GitHubAppInstallationCredential:
        self.calls.append(governance)
        return GitHubAppInstallationCredential(
            app_id=4577593,
            installation_id=153350892,
            app_slug="rcp-agent",
            bot_login="rcp-agent[bot]",
            repository="owner/project",
            permissions={
                "contents": "write",
                "metadata": "read",
                "pull_requests": "write",
            },
            expires_at=NOW + timedelta(hours=1),
            token=TOKEN,
        )


@dataclass
class _Result:
    data: dict[str, object]


class _Service:
    def __init__(self) -> None:
        self.calls: list[tuple[SubmissionCreateRequest, object]] = []

    def submission_create(self, request, actor):
        self.calls.append((request, actor))
        return _Result(data={"submission": {"pull_request": {"number": 17}}})


class _Handle:
    def __init__(self, service: _Service) -> None:
        self.service = service
        self.actor = SimpleNamespace(role="agent")

    def __enter__(self):
        return self

    def __exit__(self, *_: object) -> None:
        pass


class _ApplicationOpener:
    def __init__(self) -> None:
        self.service = _Service()
        self.calls: list[dict[str, object]] = []

    def __call__(self, path, **options):
        self.calls.append({"path": path, **options})
        return _Handle(self.service)


class _CommandRunner:
    def __init__(self) -> None:
        self.argv: tuple[str, ...] | None = None

    def run(self, argv, *, cwd, env, timeout_seconds):
        self.argv = argv
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def _id(kind: str, fill: str) -> str:
    return f"{kind}_20260813T120000Z_{fill * 24}"


def _managed_project(repository: Path) -> tuple[object, str, str]:
    project_path = repository / ".research" / "project.yaml"
    project = load_model(project_path, ProjectRecord)
    repository_record = project.repository.model_copy(
        update={"remote_url": "git@github.com:owner/project.git"}
    )
    project_path.write_text(
        dump_yaml(
            project.model_copy(
                update={
                    "state": ProjectState.MANAGED,
                    "repository": repository_record,
                }
            )
        ),
        encoding="utf-8",
    )
    policy_path = repository / ".research" / "policies" / "default.yaml"
    policy = load_model(policy_path, ProjectPolicy)
    governance = GitHubGovernancePolicy.model_validate(
        {
            "repository": "owner/project",
            "default_branch": "main",
            "agent_app": {
                "app_id": 4577593,
                "installation_id": 153350892,
                "login": "rcp-agent[bot]",
            },
            "managers": [{"kind": "user", "login": "manager"}],
        }
    )
    policy_path.write_text(
        dump_yaml(policy.model_copy(update={"github": governance})),
        encoding="utf-8",
    )
    managed = discover_managed_project(repository)
    managed.runtime.state_directory.mkdir(mode=0o700)
    managed.runtime.worktrees_directory.mkdir(mode=0o700)
    session_id = _id("session", "e")
    task_id = _id("task", "a")
    with RuntimeStore(managed.runtime.database_path) as runtime:
        runtime.save_session(
            RuntimeSession(
                session_id=session_id,
                project_id=managed.project_id,
                task_id=task_id,
                state=SessionState.ACTIVE,
                created_at=NOW,
                updated_at=NOW,
                actor_token_digest=hash_session_token(SESSION_TOKEN),
            )
        )
    return managed, session_id, task_id


def _request(session_id: str, task_id: str) -> SubmissionCreateRequest:
    submission_id = _id("submission", "f")
    submission = ResearchSubmission.model_validate(
        {
            "schema_version": "0.1",
            "submission_id": submission_id,
            "task_id": task_id,
            "session_id": session_id,
            "category": "candidate_result",
            "claim": "The candidate result is ready for review.",
            "run_result_ids": [_id("result", "1")],
            "created_at": NOW,
        }
    )
    proposal = ReportProposal(
        submission_id=submission_id,
        report_id=_id("report", "9"),
        expected_report_revision=0,
        title="Candidate result",
        evidence_tree="a" * 40,
    )
    return SubmissionCreateRequest(
        operation_id=_id("operation", "2"),
        idempotency_key="github-app-host-test",
        base_commit="b" * 40,
        submission=submission,
        report_proposal=proposal,
        run_ids=(_id("run", "c"),),
    )


def test_host_authenticates_session_before_issuing_and_returns_secret_free_receipt(
    initialized_repository: Path,
) -> None:
    managed, session_id, task_id = _managed_project(initialized_repository)
    request = _request(session_id, task_id)
    issuer = _Issuer()
    opener = _ApplicationOpener()
    git_executable = Path("/usr/bin/git")
    gh_executable = Path("/usr/bin/git")

    result = run_submission(
        project=initialized_repository,
        private_key=Path("/not/read/by/fake"),
        request=request,
        source_environment={
            "RESEARCHCTL_SESSION_ID": session_id,
            "RESEARCHCTL_SESSION_TOKEN": SESSION_TOKEN,
            "HOME": "/home/manager",
            "SSH_AUTH_SOCK": "/tmp/manager.sock",
            "GH_TOKEN": "human-token",
        },
        issuer=issuer,
        application_opener=opener,
        git_executable=git_executable,
        gh_executable=gh_executable,
    )

    assert len(issuer.calls) == 1
    assert result["submission"] == {"pull_request": {"number": 17}}
    assert result["github_app"]["bot_login"] == "rcp-agent[bot]"
    assert TOKEN not in repr(result)
    options = opener.calls[0]
    assert options["path"] == managed.repository_root
    assert options["environment"] == {
        "RESEARCHCTL_SESSION_ID": session_id,
        "RESEARCHCTL_SESSION_TOKEN": SESSION_TOKEN,
    }
    assert options["project_runtime_service"] is not None
    delivery = options["submission_delivery"]
    assert delivery._mutation_remote_url == "https://github.com/owner/project.git"
    assert delivery._git_executable == str(git_executable)
    assert delivery._gh_executable == str(gh_executable)
    assert "SSH_AUTH_SOCK" not in delivery._git_environment
    assert delivery._git_environment["RESEARCHCTL_GITHUB_APP_TOKEN"] == TOKEN
    assert delivery._git_environment["HOME"] != "/home/manager"
    assert delivery._gh_environment["GH_TOKEN"] == TOKEN
    assert TOKEN not in repr(opener.service.calls)


@pytest.mark.parametrize("identity", ["manager", "wrong_session", "wrong_token"])
def test_host_rejects_unscoped_callers_before_credential_issuance(
    initialized_repository: Path,
    identity: str,
) -> None:
    _, session_id, task_id = _managed_project(initialized_repository)
    request = _request(session_id, task_id)
    environment: dict[str, str] = {}
    if identity == "wrong_session":
        request = _request(_id("session", "d"), task_id)
        environment = {
            "RESEARCHCTL_SESSION_ID": session_id,
            "RESEARCHCTL_SESSION_TOKEN": SESSION_TOKEN,
        }
    elif identity == "wrong_token":
        environment = {
            "RESEARCHCTL_SESSION_ID": session_id,
            "RESEARCHCTL_SESSION_TOKEN": "wrong-token",
        }
    issuer = _Issuer()

    with pytest.raises(RCPError) as raised:
        run_submission(
            project=initialized_repository,
            private_key=Path("/not/read/by/fake"),
            request=request,
            source_environment=environment,
                issuer=issuer,
                application_opener=_ApplicationOpener(),
                git_executable=Path("/usr/bin/git"),
                gh_executable=Path("/usr/bin/git"),
        )

    expected = {
        "manager": "github_proposal_session_required",
        "wrong_session": "session_scope_denied",
        "wrong_token": "unauthorized_actor",
    }
    assert raised.value.code == expected[identity]
    assert issuer.calls == []


def test_host_rejects_untrusted_executables_before_credential_issuance(
    initialized_repository: Path,
) -> None:
    _, session_id, task_id = _managed_project(initialized_repository)
    issuer = _Issuer()

    with pytest.raises(RCPError) as raised:
        run_submission(
            project=initialized_repository,
            private_key=Path("/not/read/by/fake"),
            request=_request(session_id, task_id),
            source_environment={
                "RESEARCHCTL_SESSION_ID": session_id,
                "RESEARCHCTL_SESSION_TOKEN": SESSION_TOKEN,
            },
            issuer=issuer,
            application_opener=_ApplicationOpener(),
            git_executable=Path("relative-git"),
            gh_executable=Path("/unreached/gh"),
        )

    assert raised.value.code == "github_proposal_executable_invalid"
    assert issuer.calls == []


def test_pinned_git_runner_replaces_only_the_expected_binary() -> None:
    delegate = _CommandRunner()
    runner = PinnedGitCommandRunner(Path("/usr/bin/git"), delegate=delegate)

    runner.run(
        ("git", "status", "--short"),
        cwd=None,
        env={"PATH": "/untrusted"},
        timeout_seconds=3,
    )

    assert delegate.argv == ("/usr/bin/git", "status", "--short")
    with pytest.raises(RCPError) as raised:
        runner.run(
            ("sh", "-c", "false"),
            cwd=None,
            env={},
            timeout_seconds=3,
        )
    assert raised.value.code == "github_proposal_command_invalid"
