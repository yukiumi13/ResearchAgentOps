from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from researchctl.adapters.github_app import (
    GitHubAppTokenIssuer,
    isolated_github_app_environment,
)
from researchctl.domain.models import GitHubGovernancePolicy
from researchctl.errors import RCPError

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
TOKEN = "github_pat_" + "s" * 48
KEY = b"test-private-key-material"


class _Signer:
    def __init__(self) -> None:
        self.private_key: bytes | None = None
        self.claims: dict[str, object] | None = None

    def sign(self, private_key: bytes, claims) -> str:
        self.private_key = private_key
        self.claims = dict(claims)
        return "signed-app-jwt"


class _Api:
    def __init__(self, responses: dict[tuple[str, str], object]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, str, dict[str, object] | None]] = []

    def request(self, method, path, *, bearer_token, payload=None):
        self.calls.append(
            (
                method,
                path,
                bearer_token,
                dict(payload) if payload is not None else None,
            )
        )
        return self.responses[(method, path)]


def _governance() -> GitHubGovernancePolicy:
    return GitHubGovernancePolicy.model_validate(
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


def _responses() -> dict[tuple[str, str], object]:
    permissions = {
        "contents": "write",
        "metadata": "read",
        "pull_requests": "write",
    }
    return {
        ("GET", "/app"): {"id": 4577593, "slug": "rcp-agent"},
        ("GET", "/app/installations/153350892"): {
            "id": 153350892,
            "app_id": 4577593,
            "app_slug": "rcp-agent",
            "account": {"login": "owner"},
            "target_type": "User",
            "repository_selection": "selected",
            "permissions": permissions,
            "suspended_at": None,
        },
        ("POST", "/app/installations/153350892/access_tokens"): {
            "token": TOKEN,
            "expires_at": (NOW + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
            "permissions": permissions,
            "repository_selection": "selected",
        },
        ("GET", "/installation/repositories?per_page=100"): {
            "total_count": 1,
            "repositories": [{"full_name": "owner/project"}],
        },
    }


def _key(tmp_path: Path) -> Path:
    directory = tmp_path / "secret"
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    path = directory / "app.pem"
    path.write_bytes(KEY)
    path.chmod(0o400)
    return path


def _issuer(
    key: Path,
    responses: dict[tuple[str, str], object],
    *,
    signer: _Signer | None = None,
    forbidden_roots: tuple[Path, ...] = (),
) -> tuple[GitHubAppTokenIssuer, _Api, _Signer]:
    api = _Api(responses)
    selected_signer = signer or _Signer()
    return (
        GitHubAppTokenIssuer(
            private_key_path=key,
            forbidden_roots=forbidden_roots,
            api=api,
            signer=selected_signer,
            clock=lambda: NOW,
        ),
        api,
        selected_signer,
    )


def test_issuer_binds_exact_app_installation_repository_permissions_and_lifetime(
    tmp_path: Path,
) -> None:
    issuer, api, signer = _issuer(_key(tmp_path), _responses())

    credential = issuer.issue(_governance())

    assert credential.token == TOKEN
    assert credential.bot_login == "rcp-agent[bot]"
    assert signer.private_key == KEY
    assert signer.claims == {
        "exp": int((NOW + timedelta(minutes=9)).timestamp()),
        "iat": int((NOW - timedelta(seconds=60)).timestamp()),
        "iss": "4577593",
    }
    token_call = next(call for call in api.calls if call[0] == "POST")
    assert token_call == (
        "POST",
        "/app/installations/153350892/access_tokens",
        "signed-app-jwt",
        {
            "permissions": {
                "contents": "write",
                "metadata": "read",
                "pull_requests": "write",
            },
            "repositories": ["project"],
        },
    )
    receipt = credential.public_receipt()
    assert receipt["repository"] == "owner/project"
    assert TOKEN not in repr(credential)
    assert TOKEN not in repr(receipt)


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda data: data[("GET", "/app")].update(id=999), "github_app_identity_mismatch"),
        (
            lambda data: data[("GET", "/app/installations/153350892")].update(
                app_slug="other"
            ),
            "github_app_identity_mismatch",
        ),
        (
            lambda data: data[("GET", "/app/installations/153350892")][
                "permissions"
            ].update(checks="write"),
            "github_app_identity_mismatch",
        ),
        (
            lambda data: data[("POST", "/app/installations/153350892/access_tokens")]
            .get("permissions")
            .update(contents="read"),
            "github_app_identity_mismatch",
        ),
        (
            lambda data: data[("GET", "/installation/repositories?per_page=100")].update(
                repositories=[{"full_name": "owner/other"}]
            ),
            "github_app_identity_mismatch",
        ),
        (
            lambda data: data[("POST", "/app/installations/153350892/access_tokens")].update(
                expires_at=(NOW - timedelta(seconds=1)).isoformat()
            ),
            "github_app_identity_mismatch",
        ),
        (
            lambda data: data[("POST", "/app/installations/153350892/access_tokens")].update(
                token="x" * 4097
            ),
            "github_app_response_invalid",
        ),
    ],
)
def test_issuer_fails_closed_on_identity_scope_and_token_response_conflicts(
    tmp_path: Path,
    mutate,
    expected: str,
) -> None:
    responses = _responses()
    mutate(responses)
    issuer, _, _ = _issuer(_key(tmp_path), responses)

    with pytest.raises(RCPError) as raised:
        issuer.issue(_governance())

    assert raised.value.code == expected
    assert TOKEN not in str(raised.value)
    assert TOKEN not in repr(raised.value.context)


@pytest.mark.parametrize("failure", ["mode", "parent_mode", "symlink", "forbidden"])
def test_private_key_must_be_owner_only_nonsymlink_and_outside_forbidden_roots(
    tmp_path: Path,
    failure: str,
) -> None:
    path = _key(tmp_path)
    forbidden: tuple[Path, ...] = ()
    if failure == "mode":
        path.chmod(0o600)
    elif failure == "parent_mode":
        path.parent.chmod(0o750)
    elif failure == "symlink":
        link = tmp_path / "linked.pem"
        link.symlink_to(path)
        path = link
    else:
        forbidden = (path.parent,)
    issuer, api, _ = _issuer(path, _responses(), forbidden_roots=forbidden)

    with pytest.raises(RCPError) as raised:
        issuer.issue(_governance())

    assert raised.value.code == "github_app_private_key_invalid"
    assert api.calls == []


def test_isolated_environment_fences_home_ssh_helpers_and_secret_outputs(
    tmp_path: Path,
) -> None:
    issuer, _, _ = _issuer(_key(tmp_path), _responses())
    credential = issuer.issue(_governance())
    source = {
        "PATH": os.defpath,
        "HOME": "/home/manager",
        "SSH_AUTH_SOCK": "/tmp/manager.sock",
        "GH_TOKEN": "human-token",
        "UNRELATED_SECRET": "secret",
    }
    objects = tmp_path / "objects"
    objects.mkdir()

    with isolated_github_app_environment(
        credential,
        object_directory=objects,
    ) as isolated:
        git = isolated.git()
        gh = isolated.gh()
        assert git["HOME"] != source["HOME"]
        assert gh["HOME"] == git["HOME"]
        assert git["GIT_CONFIG_GLOBAL"] == os.devnull
        assert git["GIT_ALLOW_PROTOCOL"] == "https"
        assert git["GIT_PROTOCOL_FROM_USER"] == "0"
        assert git["RESEARCHCTL_GITHUB_APP_HOST"] == "github.com"
        assert Path(git["GIT_DIR"]).parent == isolated.home
        assert Path(git["GIT_OBJECT_DIRECTORY"]) == objects.resolve()
        assert "SSH_AUTH_SOCK" not in git
        assert "UNRELATED_SECRET" not in git
        assert "UNRELATED_SECRET" not in gh
        assert gh["GH_TOKEN"] == TOKEN
        username = subprocess.run(
            (str(isolated.askpass), "Username for https://github.com:"),
            check=True,
            env=git,
            capture_output=True,
            text=True,
        )
        password = subprocess.run(
            (str(isolated.askpass), "Password for https://github.com:"),
            check=True,
            env=git,
            capture_output=True,
            text=True,
        )
        assert username.stdout.strip() == "x-access-token"
        assert password.stdout.strip() == TOKEN
        wrong_host = subprocess.run(
            (str(isolated.askpass), "Password for https://attacker.invalid:"),
            check=False,
            env=git,
            capture_output=True,
            text=True,
        )
        assert wrong_host.returncode != 0
        assert wrong_host.stdout == ""
        temporary_home = isolated.home
    assert not temporary_home.exists()
