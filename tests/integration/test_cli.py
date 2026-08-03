from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from researchctl.constants import OUTPUT_SCHEMA_VERSION, PROTOCOL_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _invoke_cli(*args: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    source_path = str(PROJECT_ROOT / "src")
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        f"{source_path}{os.pathsep}{existing_pythonpath}"
        if existing_pythonpath
        else source_path
    )
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "researchctl", *args],
        cwd=PROJECT_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


def _load_envelope(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    value = json.loads(result.stdout)
    assert isinstance(value, dict)
    assert set(value) == {
        "schema_version",
        "command",
        "success",
        "data",
        "warnings",
        "errors",
        "observed_at",
    }
    assert value["schema_version"] == OUTPUT_SCHEMA_VERSION
    observed_at = datetime.fromisoformat(value["observed_at"].replace("Z", "+00:00"))
    assert observed_at.tzinfo == UTC
    return value


def test_cli_init_dry_run_emits_success_json_and_zero_exit(
    git_repository: Path,
) -> None:
    result = _invoke_cli("init", str(git_repository), "--dry-run", "--json")

    assert result.returncode == 0
    assert result.stderr == ""
    envelope = _load_envelope(result)
    assert envelope["command"] == "init"
    assert envelope["success"] is True
    assert envelope["errors"] == []
    assert envelope["data"]["repository"] == str(git_repository.resolve())
    assert envelope["data"]["dry_run"] is True
    assert envelope["data"]["created"]
    assert not (git_repository / ".researchctl.toml").exists()
    assert not (git_repository / ".research").exists()


def test_cli_non_git_error_emits_failure_json_and_stable_exit_code(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "not-git"
    directory.mkdir()

    result = _invoke_cli("init", str(directory), "--json")

    assert result.returncode == 2
    assert result.stderr == ""
    envelope = _load_envelope(result)
    assert envelope["command"] == "init"
    assert envelope["success"] is False
    assert envelope["data"] == {}
    assert envelope["warnings"] == []
    assert len(envelope["errors"]) == 1
    assert envelope["errors"][0] == {
        "code": "repository_not_found",
        "message": f"Not inside a Git repository: {directory.resolve()}",
        "remediation": "Run the command inside a Git repository.",
        "context": {"path": str(directory.resolve())},
    }


def test_cli_doctor_tampered_schema_returns_unhealthy_envelope_and_exit_two(
    initialized_repository: Path,
) -> None:
    relative = ".research/schemas/task.schema.json"
    (initialized_repository / relative).write_text("{}\n", encoding="utf-8")

    result = _invoke_cli("doctor", str(initialized_repository), "--json")

    assert result.returncode == 2
    assert result.stderr == ""
    envelope = _load_envelope(result)
    assert envelope["command"] == "doctor"
    assert envelope["success"] is False
    assert envelope["data"]["healthy"] is False
    assert any(
        error["code"] == f"schema:{relative}" for error in envelope["errors"]
    )


def test_cli_upgrade_current_and_future_protocol_exit_codes(
    initialized_repository: Path,
) -> None:
    current_result = _invoke_cli(
        "upgrade",
        str(initialized_repository),
        "--check",
        "--json",
    )

    assert current_result.returncode == 0
    assert current_result.stderr == ""
    current_envelope = _load_envelope(current_result)
    assert current_envelope["command"] == "upgrade"
    assert current_envelope["success"] is True
    assert current_envelope["data"]["current"] == PROTOCOL_VERSION
    assert current_envelope["data"]["target"] == PROTOCOL_VERSION
    assert current_envelope["data"]["migration_required"] is False

    config_path = initialized_repository / ".researchctl.toml"
    current = config_path.read_text(encoding="utf-8")
    config_path.write_text(
        current.replace(
            f'protocol_version = "{PROTOCOL_VERSION}"',
            'protocol_version = "9.0"',
        ),
        encoding="utf-8",
    )
    future_result = _invoke_cli("upgrade", str(initialized_repository), "--json")

    assert future_result.returncode == 2
    assert future_result.stderr == ""
    future_envelope = _load_envelope(future_result)
    assert future_envelope["command"] == "upgrade"
    assert future_envelope["success"] is False
    assert future_envelope["data"] == {}
    assert future_envelope["errors"][0]["code"] == "unsupported_protocol"
    assert future_envelope["errors"][0]["context"] == {
        "found": "9.0",
        "supported": PROTOCOL_VERSION,
    }
