from __future__ import annotations

import subprocess

import pytest

from researchctl.adapters._subprocess import CommandResult
from researchctl.adapters.ssh import SSHHostProfile, SSHTransport, require_remote_absolute_path
from researchctl.errors import RCPError


class RecordingRunner:
    def __init__(self, result: CommandResult | BaseException) -> None:
        self.result = result
        self.calls: list[tuple[tuple[str, ...], float]] = []

    def run(self, argv, *, cwd, env, timeout_seconds):
        assert cwd is None
        assert env is None
        self.calls.append((argv, timeout_seconds))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def profile() -> SSHHostProfile:
    return SSHHostProfile(
        host="school-a",
        target="research@school-a",
        connect_timeout_seconds=7,
        command_timeout_seconds=11,
    )


def test_observe_uses_outbound_batch_ssh_and_shell_quotes_each_remote_argument() -> None:
    runner = RecordingRunner(CommandResult(returncode=0, stdout='{"state":"ready"}\n'))
    transport = SSHTransport(runner=runner)

    receipt = transport.observe(profile(), ("fleet", "status", "name with space", "$(id)"))

    assert receipt.host == "school-a"
    assert receipt.operation == "observe"
    assert receipt.returncode == 0
    argv, timeout = runner.calls[0]
    assert timeout == 11
    assert argv[:3] == ("ssh", "-T", "-o")
    assert "BatchMode=yes" in argv
    assert "ClearAllForwardings=yes" in argv
    assert "ForwardAgent=no" in argv
    assert argv[-2] == "research@school-a"
    assert argv[-1] == "researchctl-remote fleet status 'name with space' '$(id)'"


def test_mutation_is_bound_to_operation_id_and_timeout_is_uncertain() -> None:
    runner = RecordingRunner(subprocess.TimeoutExpired(("ssh",), timeout=2))
    transport = SSHTransport(runner=runner)

    with pytest.raises(RCPError) as caught:
        transport.mutate(
            profile(),
            ("run", "start", "run-1"),
            operation_id="op-1",
            timeout_seconds=2,
        )

    assert caught.value.code == "ssh_mutation_uncertain"
    assert caught.value.context == {"host": "school-a", "operation": "mutate"}
    assert runner.calls[0][0][-1] == (
        "researchctl-remote operation execute --operation-id op-1 -- run start run-1"
    )


def test_read_timeout_is_retryable_without_claiming_remote_mutation() -> None:
    runner = RecordingRunner(subprocess.TimeoutExpired(("ssh",), timeout=2))

    with pytest.raises(RCPError) as caught:
        SSHTransport(runner=runner).operation_status(profile(), "op-1")

    assert caught.value.code == "ssh_timeout"


@pytest.mark.parametrize(
    "change",
    [
        {"host": "School"},
        {"target": "-oProxyCommand=bad"},
        {"target": "host\ncommand"},
        {"remote_program": "bad program"},
        {"connect_timeout_seconds": 0},
        {"command_timeout_seconds": 0},
    ],
)
def test_host_profile_rejects_unsafe_connection_fields(change: dict[str, object]) -> None:
    values: dict[str, object] = {
        "host": "school-a",
        "target": "research@school-a",
        "remote_program": "researchctl-remote",
        "connect_timeout_seconds": 10,
        "command_timeout_seconds": 30,
    }
    values.update(change)

    with pytest.raises(ValueError):
        SSHHostProfile(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "argv",
    [(), ("",), ("ok\nnot-ok",), ["not", "a", "tuple"]],
)
def test_transport_rejects_invalid_remote_argv(argv: object) -> None:
    runner = RecordingRunner(CommandResult(returncode=0))

    with pytest.raises(ValueError):
        SSHTransport(runner=runner).observe(profile(), argv)  # type: ignore[arg-type]

    assert runner.calls == []


@pytest.mark.parametrize("path", ["relative/path", "/a/../b", "/a//b", "/a\n/b"])
def test_remote_absolute_path_is_canonical(path: str) -> None:
    with pytest.raises(ValueError):
        require_remote_absolute_path(path, "run_root")

    assert require_remote_absolute_path("/srv/research/runs", "run_root") == (
        "/srv/research/runs"
    )
