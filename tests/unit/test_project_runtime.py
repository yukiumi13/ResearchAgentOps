from __future__ import annotations

import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from researchctl.adapters._subprocess import CommandResult
from researchctl.config import ProjectConfig, dump_project_config
from researchctl.domain.models import (
    AgentPolicy,
    ProjectPolicy,
    ProjectRecord,
    RepositoryIdentity,
)
from researchctl.errors import RCPError
from researchctl.schema import generate_schema_files, schema_manifest_digest
from researchctl.serialization import dump_yaml
from researchctl.services.init_project import initialize_project
from researchctl.services.project_runtime import (
    ProjectRuntimePaths,
    ProjectRuntimeService,
    discover_managed_project,
    ensure_runtime_directories,
)

PROJECT_ID = "project_20260803T120000Z_" + "a" * 24
OTHER_PROJECT_ID = "project_20260803T120000Z_" + "b" * 24
DENIED_PATHS = (
    ".research/decisions/**",
    ".research/policies/**",
    ".research/project.yaml",
    ".research/impacts/**",
    ".research/reports/**",
    ".research/tasks/**",
)


@dataclass(frozen=True, slots=True)
class RunnerCall:
    argv: tuple[str, ...]
    cwd: Path | None
    env: dict[str, str] | None
    timeout_seconds: float


class DiscoveryRunner:
    def __init__(
        self,
        *,
        root: Path,
        common_dir: Path,
        common_output: str | None = None,
    ) -> None:
        self.root = root
        self.common_dir = common_dir
        self.common_output = common_output
        self.calls: list[RunnerCall] = []

    def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path | None,
        env: Mapping[str, str] | None,
        timeout_seconds: float,
    ) -> CommandResult:
        self.calls.append(
            RunnerCall(
                argv=argv,
                cwd=cwd,
                env=dict(env) if env is not None else None,
                timeout_seconds=timeout_seconds,
            )
        )
        args = argv[5:]
        if args == ("rev-parse", "--show-toplevel"):
            return CommandResult(0, stdout=f"{self.root}\n")
        if args == (
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        ):
            output = self.common_output
            if output is None:
                output = f"{self.common_dir}\n"
            return CommandResult(0, stdout=output)
        raise AssertionError(f"unexpected Git argv: {argv!r}")


class TimeoutRunner:
    def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path | None,
        env: Mapping[str, str] | None,
        timeout_seconds: float,
    ) -> CommandResult:
        raise subprocess.TimeoutExpired(argv, timeout_seconds)


def _write_managed_project(
    root: Path,
    *,
    config_project_id: str = PROJECT_ID,
    record_project_id: str | None = None,
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    policy_directory = root / ".research/policies"
    policy_directory.mkdir(parents=True)
    config = ProjectConfig(
        project_id=config_project_id,
        schema_manifest_digest=schema_manifest_digest(generate_schema_files()),
    )
    record = ProjectRecord(
        project_id=record_project_id or config_project_id,
        key="RCP",
        name="Research control plane",
        repository=RepositoryIdentity(default_branch="main"),
        created_at=datetime(2026, 8, 3, 12, 0, tzinfo=UTC),
    )
    policy = ProjectPolicy(agent=AgentPolicy(accepted_paths_denied=DENIED_PATHS))
    (root / ".researchctl.toml").write_bytes(dump_project_config(config))
    (root / ".research/project.yaml").write_text(
        dump_yaml(record),
        encoding="utf-8",
    )
    (policy_directory / "default.yaml").write_text(
        dump_yaml(policy),
        encoding="utf-8",
    )


def _fake_project(tmp_path: Path) -> tuple[Path, Path, DiscoveryRunner]:
    root = tmp_path / "repository"
    common_dir = root / ".git"
    common_dir.mkdir(parents=True)
    _write_managed_project(root)
    runner = DiscoveryRunner(root=root.resolve(), common_dir=common_dir.resolve())
    return root, common_dir, runner


def _run_git(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return result


def test_discovery_loads_identity_and_is_strictly_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, common_dir, runner = _fake_project(tmp_path)
    nested = root / "src/training"
    nested.mkdir(parents=True)
    monkeypatch.setenv("GIT_DIR", "/wrong/git-dir")
    monkeypatch.setenv("GIT_COMMON_DIR", "/wrong/common-dir")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "credential.helper")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "unsafe")
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "credential.helper=unsafe")
    monkeypatch.setenv("RCP_UNRELATED", "preserved")

    discovered = ProjectRuntimeService(
        runner=runner,
        timeout_seconds=2.5,
    ).discover(nested, expected_project_id=PROJECT_ID)

    assert discovered.repository_root == root.resolve()
    assert discovered.project_id == PROJECT_ID
    assert discovered.config.project_id == PROJECT_ID
    assert discovered.project.repository.default_branch == "main"
    assert discovered.policy.agent.dangerous_skip_permissions is False
    assert discovered.runtime == ProjectRuntimePaths(
        git_common_dir=common_dir.resolve(),
        state_directory=common_dir.resolve() / "researchctl",
        database_path=common_dir.resolve() / "researchctl/runtime-v1.sqlite3",
        worktrees_directory=common_dir.resolve() / "researchctl/worktrees",
    )
    assert not discovered.runtime.state_directory.exists()
    assert len(runner.calls) == 2
    assert runner.calls[0].argv == (
        "git",
        "-c",
        "core.fsmonitor=false",
        "-C",
        str(nested.resolve()),
        "rev-parse",
        "--show-toplevel",
    )
    assert runner.calls[1].argv[-3:] == (
        "rev-parse",
        "--path-format=absolute",
        "--git-common-dir",
    )
    for call in runner.calls:
        assert call.cwd is None
        assert call.timeout_seconds == 2.5
        assert call.env is not None
        assert "GIT_DIR" not in call.env
        assert "GIT_COMMON_DIR" not in call.env
        assert "GIT_CONFIG_COUNT" not in call.env
        assert "GIT_CONFIG_KEY_0" not in call.env
        assert "GIT_CONFIG_VALUE_0" not in call.env
        assert "GIT_CONFIG_PARAMETERS" not in call.env
        assert call.env["GIT_OPTIONAL_LOCKS"] == "0"
        assert call.env["RCP_UNRELATED"] == "preserved"


def test_convenience_discovery_uses_the_same_contract(tmp_path: Path) -> None:
    root, common_dir, runner = _fake_project(tmp_path)

    discovered = discover_managed_project(root, runner=runner)

    assert discovered.repository_root == root.resolve()
    assert discovered.runtime.git_common_dir == common_dir.resolve()


def test_discovery_rejects_mismatched_project_ids(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    common_dir = root / ".git"
    common_dir.mkdir(parents=True)
    _write_managed_project(root, record_project_id=OTHER_PROJECT_ID)
    runner = DiscoveryRunner(root=root.resolve(), common_dir=common_dir.resolve())

    with pytest.raises(RCPError) as raised:
        ProjectRuntimeService(runner=runner).discover(root)

    assert raised.value.code == "project_id_mismatch"
    assert raised.value.context == {
        "config_project_id": PROJECT_ID,
        "record_project_id": OTHER_PROJECT_ID,
    }
    assert not (common_dir / "researchctl").exists()


def test_discovery_rejects_an_unexpected_project_id(tmp_path: Path) -> None:
    root, common_dir, runner = _fake_project(tmp_path)

    with pytest.raises(RCPError) as raised:
        ProjectRuntimeService(runner=runner).discover(
            root,
            expected_project_id=OTHER_PROJECT_ID,
        )

    assert raised.value.code == "project_id_mismatch"
    assert raised.value.context["actual_project_id"] == PROJECT_ID
    assert not (common_dir / "researchctl").exists()


@pytest.mark.parametrize(
    ("relative", "extra", "code"),
    [
        (".researchctl.toml", "unknown_field = true\n", "project_config_invalid"),
        (".research/project.yaml", "unknown_field: true\n", "project_record_invalid"),
        (
            ".research/policies/default.yaml",
            "unknown_field: true\n",
            "project_policy_invalid",
        ),
    ],
)
def test_discovery_strictly_rejects_unknown_protocol_fields(
    tmp_path: Path,
    relative: str,
    extra: str,
    code: str,
) -> None:
    root, common_dir, runner = _fake_project(tmp_path)
    path = root / relative
    path.write_text(path.read_text(encoding="utf-8") + extra, encoding="utf-8")

    with pytest.raises(RCPError) as raised:
        ProjectRuntimeService(runner=runner).discover(root)

    assert raised.value.code == code
    assert not (common_dir / "researchctl").exists()


def test_ensure_creates_only_runtime_directories_and_is_idempotent(
    tmp_path: Path,
) -> None:
    root, common_dir, runner = _fake_project(tmp_path)
    service = ProjectRuntimeService(runner=runner)
    discovered = service.discover(root)

    first = service.ensure_runtime_directories(discovered)
    second = ensure_runtime_directories(discovered.runtime)

    assert first == second == discovered.runtime
    assert discovered.runtime.state_directory.is_dir()
    assert discovered.runtime.worktrees_directory.is_dir()
    assert not discovered.runtime.database_path.exists()
    assert set(common_dir.iterdir()) == {discovered.runtime.state_directory}
    assert set(discovered.runtime.state_directory.iterdir()) == {
        discovered.runtime.worktrees_directory
    }


@pytest.mark.parametrize("conflict", ["state-file", "state-symlink", "worktrees-symlink"])
def test_ensure_rejects_non_directories_and_symlinks(
    tmp_path: Path,
    conflict: str,
) -> None:
    root, common_dir, runner = _fake_project(tmp_path)
    discovered = ProjectRuntimeService(runner=runner).discover(root)
    state = discovered.runtime.state_directory
    target = tmp_path / "outside-target"
    if conflict == "state-file":
        state.write_text("not a directory\n", encoding="utf-8")
    elif conflict == "state-symlink":
        target.mkdir()
        state.symlink_to(target, target_is_directory=True)
    else:
        state.mkdir()
        target.mkdir()
        discovered.runtime.worktrees_directory.symlink_to(
            target,
            target_is_directory=True,
        )

    with pytest.raises(RCPError) as raised:
        ProjectRuntimeService().ensure_runtime_directories(discovered)

    assert raised.value.code == "runtime_directory_invalid"
    assert not discovered.runtime.database_path.exists()
    assert common_dir.is_dir()


def test_ensure_rejects_a_symlink_database_path(tmp_path: Path) -> None:
    root, _, runner = _fake_project(tmp_path)
    discovered = ProjectRuntimeService(runner=runner).discover(root)
    discovered.runtime.state_directory.mkdir()
    target = tmp_path / "database-target"
    target.write_bytes(b"not sqlite")
    discovered.runtime.database_path.symlink_to(target)

    with pytest.raises(RCPError) as raised:
        ProjectRuntimeService().ensure_runtime_directories(discovered)

    assert raised.value.code == "runtime_database_path_invalid"
    assert not discovered.runtime.worktrees_directory.exists()


def test_ensure_rejects_a_forged_runtime_layout_without_writes(tmp_path: Path) -> None:
    common_dir = tmp_path / "common"
    common_dir.mkdir()
    forged = ProjectRuntimePaths(
        git_common_dir=common_dir,
        state_directory=common_dir / "other",
        database_path=common_dir / "other/runtime-v1.sqlite3",
        worktrees_directory=common_dir / "other/worktrees",
    )

    with pytest.raises(RCPError) as raised:
        ensure_runtime_directories(forged)

    assert raised.value.code == "runtime_layout_invalid"
    assert list(common_dir.iterdir()) == []


def test_git_timeout_is_typed_and_does_not_create_runtime_state(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    root.mkdir()

    with pytest.raises(RCPError) as raised:
        ProjectRuntimeService(
            runner=TimeoutRunner(),
            timeout_seconds=0.25,
        ).discover(root)

    assert raised.value.code == "git_timeout"
    assert raised.value.context == {"path": str(root.resolve())}
    assert list(root.iterdir()) == []


def test_git_common_dir_output_must_be_absolute(tmp_path: Path) -> None:
    root, common_dir, _ = _fake_project(tmp_path)
    runner = DiscoveryRunner(
        root=root.resolve(),
        common_dir=common_dir.resolve(),
        common_output=".git\n",
    )

    with pytest.raises(RCPError) as raised:
        ProjectRuntimeService(runner=runner).discover(root)

    assert raised.value.code == "git_output_invalid"
    assert not (common_dir / "researchctl").exists()


def test_real_git_main_and_linked_worktrees_share_one_runtime_location(
    tmp_path: Path,
) -> None:
    root = tmp_path / "main-worktree"
    linked = tmp_path / "linked-worktree"
    root.mkdir()
    _run_git(root, "init", "-b", "main")
    (root / "README.md").write_text("# Project\n", encoding="utf-8")
    _run_git(root, "add", "README.md")
    _run_git(
        root,
        "-c",
        "user.name=ResearchCTL Tests",
        "-c",
        "user.email=researchctl@example.invalid",
        "commit",
        "-m",
        "initial",
    )
    initialize_project(root)
    _run_git(root, "add", ".researchctl.toml", ".research")
    _run_git(
        root,
        "-c",
        "user.name=ResearchCTL Tests",
        "-c",
        "user.email=researchctl@example.invalid",
        "commit",
        "-m",
        "initialize research control plane",
    )
    _run_git(root, "worktree", "add", "-b", "linked", str(linked), "HEAD")

    service = ProjectRuntimeService()
    main_project = service.discover(root)
    linked_project = service.discover(linked)
    expected_common = (root / ".git").resolve()

    assert main_project.repository_root == root.resolve()
    assert linked_project.repository_root == linked.resolve()
    assert main_project.project_id == linked_project.project_id
    assert main_project.runtime == linked_project.runtime
    assert main_project.runtime.git_common_dir == expected_common
    assert main_project.runtime.database_path == (
        expected_common / "researchctl/runtime-v1.sqlite3"
    )
    assert main_project.runtime.worktrees_directory == (
        expected_common / "researchctl/worktrees"
    )
    assert not main_project.runtime.state_directory.exists()
