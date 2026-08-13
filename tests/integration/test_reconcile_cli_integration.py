from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _invoke_reconcile(
    repository: Path,
    tmux_directory: Path,
    *,
    json_output: bool,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    source_path = str(PROJECT_ROOT / "src")
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        f"{source_path}{os.pathsep}{existing_pythonpath}"
        if existing_pythonpath
        else source_path
    )
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment.pop("TMUX", None)
    environment["TMUX_TMPDIR"] = str(tmux_directory)
    environment["PATH"] = f"{tmux_directory}{os.pathsep}{environment['PATH']}"
    arguments = [sys.executable, "-m", "researchctl", "reconcile", str(repository)]
    if json_output:
        arguments.append("--json")
    return subprocess.run(
        arguments,
        cwd=PROJECT_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


def test_real_reconcile_does_not_create_a_missing_runtime_database(
    initialized_repository: Path,
    tmp_path: Path,
) -> None:
    tmux_directory = tmp_path / "isolated-tmux"
    tmux_directory.mkdir(mode=0o700)
    tmux_executable = tmux_directory / "tmux"
    tmux_executable.write_text(
        "#!/bin/sh\nprintf '%s\\n' 'no server running on test socket' >&2\nexit 1\n",
        encoding="utf-8",
    )
    tmux_executable.chmod(0o700)
    runtime_directory = initialized_repository / ".git" / "researchctl"
    database_path = runtime_directory / "runtime-v1.sqlite3"
    assert not runtime_directory.exists()

    machine = _invoke_reconcile(
        initialized_repository,
        tmux_directory,
        json_output=True,
    )
    human = _invoke_reconcile(
        initialized_repository,
        tmux_directory,
        json_output=False,
    )

    assert machine.returncode == 0, machine.stderr
    assert human.returncode == 0, human.stderr
    assert machine.stderr == ""
    assert human.stderr == ""
    assert not database_path.exists()
    assert not runtime_directory.exists()

    payload = json.loads(machine.stdout)
    assert payload["command"] == "reconcile"
    assert payload["success"] is True
    assert payload["data"]["outcome"] == "plan_ready", payload
    assert payload["data"]["runtime_observation"] == "missing"
    assert payload["data"]["items"] == []
    assert payload["data"]["runs"] == []
    assert payload["data"]["takeover_token_created"] is False
    assert len(payload["data"]["runtime_recovery_limits"]) == 4
    assert "Reconcile: plan_ready; 0 session(s)" in human.stdout
    assert "Runs: 0" in human.stdout
    assert "Runtime DB: missing" in human.stdout
