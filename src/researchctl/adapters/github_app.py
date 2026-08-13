from __future__ import annotations

import json
import os
import re
import ssl
import stat
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

import jwt

from researchctl.domain.models import GitHubGovernancePolicy
from researchctl.errors import RCPError

_API_ROOT = "https://api.github.com"
_API_VERSION = "2022-11-28"
_MAX_KEY_BYTES = 64 * 1024
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_TOKEN_BYTES = 4096
_EXPECTED_PERMISSIONS = {
    "contents": "write",
    "metadata": "read",
    "pull_requests": "write",
}
_APP_INSTALLATION_PATH = re.compile(r"^/app/installations/[1-9][0-9]*$")
_APP_TOKEN_PATH = re.compile(
    r"^/app/installations/[1-9][0-9]*/access_tokens$"
)
class GitHubAppApiClient(Protocol):
    def request(
        self,
        method: str,
        path: str,
        *,
        bearer_token: str,
        payload: Mapping[str, object] | None = None,
    ) -> object: ...


class GitHubAppJwtSigner(Protocol):
    def sign(self, private_key: bytes, claims: Mapping[str, object]) -> str: ...


class PyJwtGitHubAppSigner:
    def sign(self, private_key: bytes, claims: Mapping[str, object]) -> str:
        try:
            encoded = jwt.encode(dict(claims), private_key, algorithm="RS256")
        except Exception as error:
            raise RCPError(
                code="github_app_private_key_invalid",
                message="GitHub App private key could not sign an RS256 JWT.",
                context={"error_type": type(error).__name__},
            ) from error
        if not isinstance(encoded, str) or not encoded:
            raise RCPError(
                code="github_app_jwt_invalid",
                message="GitHub App JWT signer returned an invalid token.",
            )
        return encoded


class UrllibGitHubAppApiClient:
    def __init__(
        self,
        *,
        api_root: str = _API_ROOT,
        timeout_seconds: float = 20.0,
        max_response_bytes: int = _MAX_RESPONSE_BYTES,
    ) -> None:
        if api_root != _API_ROOT:
            raise ValueError("the initial GitHub App broker supports api.github.com only")
        if timeout_seconds <= 0 or max_response_bytes < 1:
            raise ValueError("GitHub App API bounds must be positive")
        self._api_root = api_root
        self._timeout_seconds = timeout_seconds
        self._max_response_bytes = max_response_bytes
        verify_paths = ssl.get_default_verify_paths()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_verify_locations(
            cafile=verify_paths.openssl_cafile,
            capath=verify_paths.openssl_capath,
        )
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=context),
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        bearer_token: str,
        payload: Mapping[str, object] | None = None,
    ) -> object:
        if method not in {"GET", "POST"} or not _safe_api_path(path):
            raise RCPError(
                code="github_app_api_request_invalid",
                message="GitHub App API request is outside the fixed broker surface.",
            )
        body = None
        if payload is not None:
            body = json.dumps(
                dict(payload),
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        request = urllib.request.Request(
            self._api_root + path,
            data=body,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {bearer_token}",
                "Content-Type": "application/json",
                "User-Agent": "researchctl-github-app-broker",
                "X-GitHub-Api-Version": _API_VERSION,
            },
        )
        try:
            with self._opener.open(request, timeout=self._timeout_seconds) as response:
                content = response.read(self._max_response_bytes + 1)
        except urllib.error.HTTPError as error:
            raise RCPError(
                code="github_app_api_failed",
                message="GitHub App API rejected a broker request.",
                context={"status": error.code},
            ) from error
        except (OSError, TimeoutError) as error:
            raise RCPError(
                code="github_app_api_unavailable",
                message="GitHub App API request did not complete.",
                context={"error_type": type(error).__name__},
            ) from error
        if len(content) > self._max_response_bytes:
            raise RCPError(
                code="github_app_response_invalid",
                message="GitHub App API response exceeds the configured bound.",
            )
        try:
            return json.loads(content, object_pairs_hook=_unique_object)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as error:
            raise RCPError(
                code="github_app_response_invalid",
                message="GitHub App API returned invalid bounded JSON.",
            ) from error


@dataclass(frozen=True, slots=True)
class GitHubAppInstallationCredential:
    app_id: int
    installation_id: int
    app_slug: str
    bot_login: str
    repository: str
    permissions: Mapping[str, str]
    expires_at: datetime
    token: str = field(repr=False)

    def public_receipt(self) -> dict[str, object]:
        return {
            "app_id": self.app_id,
            "installation_id": self.installation_id,
            "app_slug": self.app_slug,
            "bot_login": self.bot_login,
            "repository": self.repository,
            "permissions": dict(sorted(self.permissions.items())),
            "expires_at": self.expires_at.isoformat().replace("+00:00", "Z"),
        }


@dataclass(frozen=True, slots=True)
class GitHubAppDeliveryEnvironment:
    credential: GitHubAppInstallationCredential = field(repr=False)
    home: Path
    askpass: Path
    git_directory: Path
    object_directory: Path
    network_environment: Mapping[str, str]

    def git(self) -> dict[str, str]:
        environment = dict(self.network_environment)
        environment.update(
            {
                "GIT_ALLOW_PROTOCOL": "https",
                "GIT_ASKPASS": str(self.askpass),
                "GIT_ASKPASS_REQUIRE": "force",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CURL_VERBOSE": "0",
                "GIT_DIR": str(self.git_directory),
                "GIT_OBJECT_DIRECTORY": str(self.object_directory),
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_PROTOCOL_FROM_USER": "0",
                "GIT_TERMINAL_PROMPT": "0",
                "HOME": str(self.home),
                "RESEARCHCTL_GITHUB_APP_HOST": "github.com",
                "RESEARCHCTL_GITHUB_APP_TOKEN": self.credential.token,
            }
        )
        return environment

    def gh(self) -> dict[str, str]:
        environment = dict(self.network_environment)
        environment.update(
            {
                "GH_CONFIG_DIR": str(self.home / "gh"),
                "GH_PROMPT_DISABLED": "1",
                "GH_TOKEN": self.credential.token,
                "HOME": str(self.home),
            }
        )
        return environment


class GitHubAppTokenIssuer:
    def __init__(
        self,
        *,
        private_key_path: Path,
        forbidden_roots: tuple[Path, ...],
        api: GitHubAppApiClient | None = None,
        signer: GitHubAppJwtSigner | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._private_key_path = private_key_path
        self._forbidden_roots = forbidden_roots
        self._api = api or UrllibGitHubAppApiClient()
        self._signer = signer or PyJwtGitHubAppSigner()
        self._clock = clock or (lambda: datetime.now(UTC))

    def issue(self, governance: GitHubGovernancePolicy) -> GitHubAppInstallationCredential:
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ValueError("GitHub App issuer clock must return an aware datetime")
        now = now.astimezone(UTC)
        private_key = _read_private_key(
            self._private_key_path,
            forbidden_roots=self._forbidden_roots,
        )
        try:
            app_jwt = self._signer.sign(
                private_key,
                {
                    "exp": int((now + timedelta(minutes=9)).timestamp()),
                    "iat": int((now - timedelta(seconds=60)).timestamp()),
                    "iss": str(governance.agent_app.app_id),
                },
            )
        finally:
            private_key = b""

        app = _mapping(
            self._api.request("GET", "/app", bearer_token=app_jwt),
            "GitHub App identity",
        )
        app_slug = _nonempty_string(app.get("slug"), "GitHub App slug")
        if (
            app.get("id") != governance.agent_app.app_id
            or f"{app_slug}[bot]".lower() != governance.agent_app.login.lower()
        ):
            _identity_mismatch("GitHub App identity differs from accepted policy.")

        installation_id = governance.agent_app.installation_id
        installation = _mapping(
            self._api.request(
                "GET",
                f"/app/installations/{installation_id}",
                bearer_token=app_jwt,
            ),
            "GitHub App installation",
        )
        owner, repository_name = governance.repository.split("/", maxsplit=1)
        account = _mapping(installation.get("account"), "GitHub installation account")
        if (
            installation.get("id") != installation_id
            or installation.get("app_id") != governance.agent_app.app_id
            or str(installation.get("app_slug", "")).lower() != app_slug.lower()
            or str(account.get("login", "")).lower() != owner.lower()
            or installation.get("target_type") not in {"User", "Organization"}
            or installation.get("repository_selection") != "selected"
            or installation.get("suspended_at") is not None
            or _permissions(installation.get("permissions")) != _EXPECTED_PERMISSIONS
        ):
            _identity_mismatch("GitHub App installation differs from accepted policy.")

        token_response = _mapping(
            self._api.request(
                "POST",
                f"/app/installations/{installation_id}/access_tokens",
                bearer_token=app_jwt,
                payload={
                    "permissions": _EXPECTED_PERMISSIONS,
                    "repositories": [repository_name],
                },
            ),
            "GitHub installation token",
        )
        token = _installation_token(token_response.get("token"))
        expires_at = _expiration(token_response.get("expires_at"), now=now)
        if (
            token_response.get("repository_selection") != "selected"
            or _permissions(token_response.get("permissions")) != _EXPECTED_PERMISSIONS
        ):
            _identity_mismatch("GitHub installation token scope differs from accepted policy.")

        repositories = _mapping(
            self._api.request(
                "GET",
                "/installation/repositories?per_page=100",
                bearer_token=token,
            ),
            "GitHub installation repositories",
        )
        values = repositories.get("repositories")
        if (
            repositories.get("total_count") != 1
            or not isinstance(values, list)
            or len(values) != 1
            or not isinstance(values[0], dict)
            or str(values[0].get("full_name", "")).lower()
            != governance.repository.lower()
        ):
            _identity_mismatch("GitHub installation token is not bound to one accepted repository.")
        return GitHubAppInstallationCredential(
            app_id=governance.agent_app.app_id,
            installation_id=installation_id,
            app_slug=app_slug,
            bot_login=governance.agent_app.login,
            repository=governance.repository,
            permissions=dict(_EXPECTED_PERMISSIONS),
            expires_at=expires_at,
            token=token,
        )


@contextmanager
def isolated_github_app_environment(
    credential: GitHubAppInstallationCredential,
    *,
    object_directory: Path,
) -> Iterator[GitHubAppDeliveryEnvironment]:
    network = {"PATH": os.defpath}
    executable = Path(sys.executable)
    if not executable.is_absolute() or any(character.isspace() for character in str(executable)):
        raise RCPError(
            code="github_app_runtime_invalid",
            message="Trusted Python executable path is not suitable for Git askpass.",
        )
    with tempfile.TemporaryDirectory(prefix="researchctl-github-app-") as raw:
        home = Path(raw)
        home.chmod(0o700)
        requested_objects = object_directory.absolute()
        objects = requested_objects.resolve(strict=True)
        if objects != requested_objects or requested_objects.is_symlink() or not objects.is_dir():
            raise RCPError(
                code="github_app_git_objects_invalid",
                message="Accepted Git object database is not a directory.",
            )
        git_directory = home / "repository.git"
        git_directory.mkdir(mode=0o700)
        (git_directory / "HEAD").write_text("ref: refs/heads/main\n", encoding="ascii")
        (git_directory / "config").write_text(
            "[core]\n\trepositoryformatversion = 0\n\tbare = true\n",
            encoding="ascii",
        )
        askpass = home / "askpass.py"
        askpass.write_text(
            f"#!{executable}\n"
            "import os, sys\n"
            "prompt = sys.argv[1].lower() if len(sys.argv) == 2 else ''\n"
            "host = os.environ['RESEARCHCTL_GITHUB_APP_HOST']\n"
            "if f'https://{host}' not in prompt:\n"
            "    raise SystemExit(1)\n"
            "if 'username' in prompt:\n"
            "    print('x-access-token')\n"
            "elif 'password' in prompt:\n"
            "    print(os.environ['RESEARCHCTL_GITHUB_APP_TOKEN'])\n"
            "else:\n"
            "    raise SystemExit(1)\n",
            encoding="utf-8",
        )
        askpass.chmod(0o700)
        yield GitHubAppDeliveryEnvironment(
            credential=credential,
            home=home,
            askpass=askpass,
            git_directory=git_directory,
            object_directory=objects,
            network_environment=network,
        )


def _read_private_key(path: Path, *, forbidden_roots: tuple[Path, ...]) -> bytes:
    if not path.is_absolute():
        _key_error("GitHub App private-key path must be absolute.")
    candidate = path.absolute()
    try:
        resolved = candidate.resolve(strict=True)
        info = candidate.lstat()
        parent_info = candidate.parent.lstat()
    except OSError as error:
        raise RCPError(
            code="github_app_private_key_invalid",
            message="GitHub App private key or its parent directory is unavailable.",
            context={"error_type": type(error).__name__},
        ) from error
    if resolved != candidate or candidate.is_symlink():
        _key_error("GitHub App private-key path must not contain symlinks.")
    current = candidate.parent
    while True:
        if current.is_symlink():
            _key_error("GitHub App private-key path must not contain symlinks.")
        parent = current.parent
        if parent == current:
            break
        current = parent
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        _key_error("GitHub App private key must be an owner-controlled regular file.")
    if stat.S_IMODE(info.st_mode) != 0o400:
        _key_error("GitHub App private key must have mode 0400.")
    if (
        not stat.S_ISDIR(parent_info.st_mode)
        or parent_info.st_uid != os.getuid()
        or stat.S_IMODE(parent_info.st_mode) != 0o700
    ):
        _key_error("GitHub App private-key directory must be owner-controlled mode 0700.")
    for root in forbidden_roots:
        forbidden = root.resolve()
        if resolved == forbidden or forbidden in resolved.parents:
            _key_error("GitHub App private key must remain outside repository state.")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(candidate, flags)
        try:
            opened = os.fstat(descriptor)
            if (
                opened.st_dev != info.st_dev
                or opened.st_ino != info.st_ino
                or not stat.S_ISREG(opened.st_mode)
                or stat.S_IMODE(opened.st_mode) != 0o400
                or opened.st_uid != os.getuid()
                or opened.st_size < 1
                or opened.st_size > _MAX_KEY_BYTES
            ):
                _key_error("GitHub App private key changed during secure open.")
            content = os.read(descriptor, _MAX_KEY_BYTES + 1)
        finally:
            os.close(descriptor)
    except RCPError:
        raise
    except OSError as error:
        raise RCPError(
            code="github_app_private_key_invalid",
            message="GitHub App private key could not be read securely.",
            context={"error_type": type(error).__name__},
        ) from error
    if not content or len(content) > _MAX_KEY_BYTES:
        _key_error("GitHub App private key has an invalid bounded size.")
    return content


def _safe_api_path(path: str) -> bool:
    return (
        path in {"/app", "/installation/repositories?per_page=100"}
        or _APP_INSTALLATION_PATH.fullmatch(path) is not None
        or _APP_TOKEN_PATH.fullmatch(path) is not None
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise RCPError(
            code="github_app_response_invalid",
            message=f"{label} response is not an object.",
        )
    return value


def _nonempty_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 255:
        raise RCPError(
            code="github_app_response_invalid",
            message=f"{label} response field is invalid.",
        )
    return value


def _permissions(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or any(
        not isinstance(key, str) or not isinstance(access, str)
        for key, access in value.items()
    ):
        raise RCPError(
            code="github_app_response_invalid",
            message="GitHub App permission response is invalid.",
        )
    return dict(value)


def _installation_token(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 20 <= len(value.encode("utf-8")) <= _MAX_TOKEN_BYTES
        or any(ord(character) < 0x21 or ord(character) > 0x7E for character in value)
    ):
        raise RCPError(
            code="github_app_response_invalid",
            message="GitHub installation token response is invalid.",
        )
    return value


def _expiration(value: object, *, now: datetime) -> datetime:
    if not isinstance(value, str):
        _identity_mismatch("GitHub installation token expiry is invalid.")
    try:
        observed = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError as error:
        raise RCPError(
            code="github_app_response_invalid",
            message="GitHub installation token expiry is invalid.",
        ) from error
    if observed <= now + timedelta(seconds=30) or observed > now + timedelta(minutes=61):
        _identity_mismatch("GitHub installation token lifetime is outside broker bounds.")
    return observed


def _identity_mismatch(message: str) -> None:
    raise RCPError(code="github_app_identity_mismatch", message=message)


def _key_error(message: str) -> None:
    raise RCPError(code="github_app_private_key_invalid", message=message)
