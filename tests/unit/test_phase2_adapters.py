from __future__ import annotations

import json
import shlex
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from researchctl.adapters import (
    ClaudeCliAdapter,
    CodexCliAdapter,
    CommandResult,
    GitWorktreeAdapter,
    SubprocessCommandRunner,
    TmuxAdapter,
    WorktreeObservationState,
    WorktreeSpec,
    deterministic_tmux_session_name,
)
from researchctl.errors import RCPError

BASE_COMMIT = "a" * 40
OTHER_COMMIT = "b" * 40
BRANCH = "research/task/MAR-17/session-a"
SESSION_ID = "session_20260803T120000Z_" + "c" * 24
TMUX_NAME = f"research-{SESSION_ID}"
NATIVE_ID = "11111111-1111-4111-8111-111111111111"
OTHER_NATIVE_ID = "22222222-2222-4222-8222-222222222222"


@dataclass(frozen=True)
class RunnerCall:
    argv: tuple[str, ...]
    cwd: Path | None
    env: dict[str, str] | None
    timeout_seconds: float


class ScriptedRunner:
    def __init__(
        self,
        handler: Callable[[RunnerCall], CommandResult],
    ) -> None:
        self.handler = handler
        self.calls: list[RunnerCall] = []

    def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path | None,
        env: Mapping[str, str] | None,
        timeout_seconds: float,
    ) -> CommandResult:
        call = RunnerCall(
            argv=argv,
            cwd=cwd,
            env=dict(env) if env is not None else None,
            timeout_seconds=timeout_seconds,
        )
        self.calls.append(call)
        return self.handler(call)


class GitStateRunner:
    def __init__(
        self,
        *,
        worktree: Path,
        base_commit: str = BASE_COMMIT,
        branch: str = BRANCH,
        branch_commit: str | None = None,
        registered: bool = False,
        valid_branch: bool = True,
    ) -> None:
        self.worktree = worktree
        self.base_commit = base_commit
        self.branch = branch
        self.branch_commit = branch_commit
        self.registered = registered
        self.valid_branch = valid_branch
        self.calls: list[RunnerCall] = []

    def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path | None,
        env: Mapping[str, str] | None,
        timeout_seconds: float,
    ) -> CommandResult:
        call = RunnerCall(
            argv=argv,
            cwd=cwd,
            env=dict(env) if env is not None else None,
            timeout_seconds=timeout_seconds,
        )
        self.calls.append(call)
        args = argv[5:]

        if args[:2] == ("check-ref-format", "--branch"):
            return CommandResult(0 if self.valid_branch else 1)
        if args[:2] == ("rev-parse", "--verify") and "--quiet" not in args:
            return CommandResult(0, stdout=f"{self.base_commit}\n")
        if args[:3] == ("rev-parse", "--verify", "--quiet"):
            if self.branch_commit is None:
                return CommandResult(1)
            return CommandResult(0, stdout=f"{self.branch_commit}\n")
        if args == ("worktree", "list", "--porcelain", "-z"):
            if not self.registered:
                return CommandResult(0)
            output = (
                f"worktree {self.worktree}\x00"
                f"HEAD {self.branch_commit}\x00"
                f"branch refs/heads/{self.branch}\x00\x00"
            )
            return CommandResult(0, stdout=output)
        if args[:3] == ("worktree", "add", "-b"):
            self.branch_commit = self.base_commit
            self.registered = True
            self.worktree.mkdir()
            return CommandResult(0)
        raise AssertionError(f"unexpected Git argv: {argv!r}")


class TmuxStateRunner:
    def __init__(self, *, present: bool = False) -> None:
        self.present = present
        self.calls: list[RunnerCall] = []

    def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path | None,
        env: Mapping[str, str] | None,
        timeout_seconds: float,
    ) -> CommandResult:
        call = RunnerCall(
            argv=argv,
            cwd=cwd,
            env=dict(env) if env is not None else None,
            timeout_seconds=timeout_seconds,
        )
        self.calls.append(call)
        if argv[1] == "has-session":
            return CommandResult(0 if self.present else 1)
        if argv[1] == "start-session":
            self.present = True
            return CommandResult(0)
        if argv[1] == "send-keys":
            return CommandResult(0 if self.present else 1)
        raise AssertionError(f"unexpected tmux argv: {argv!r}")


def _worktree_spec(tmp_path: Path) -> WorktreeSpec:
    root = tmp_path / "repository"
    root.mkdir(parents=True)
    return WorktreeSpec(
        root=root,
        base_commit=BASE_COMMIT,
        branch=BRANCH,
        worktree=tmp_path / "session-worktree",
    )


def _jsonl(*events: dict[str, Any]) -> str:
    return "\n".join(json.dumps(event, separators=(",", ":")) for event in events)


def test_subprocess_runner_uses_an_argv_list_and_never_a_shell(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    observed: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        observed["argv"] = argv
        observed.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = SubprocessCommandRunner().run(
        ("tool", "argument with spaces", "$(not-executed)"),
        cwd=tmp_path,
        env={"SAFE": "1"},
        timeout_seconds=2.5,
    )

    assert result == CommandResult(0, "ok", "")
    assert observed["argv"] == ["tool", "argument with spaces", "$(not-executed)"]
    assert observed["shell"] is False
    assert observed["cwd"] == str(tmp_path)
    assert observed["env"] == {"SAFE": "1"}


def test_git_worktree_create_is_observed_idempotent_and_never_pushes_or_deletes(
    tmp_path: Path,
) -> None:
    spec = _worktree_spec(tmp_path)
    runner = GitStateRunner(worktree=spec.worktree)
    adapter = GitWorktreeAdapter(runner=runner)

    first = adapter.create_or_observe(spec)
    second = adapter.create_or_observe(spec)

    assert first.state is WorktreeObservationState.EXACT
    assert second.state is WorktreeObservationState.EXACT
    git_args = [call.argv[5:] for call in runner.calls]
    assert git_args[0] == ("check-ref-format", "--branch", BRANCH)
    assert sum(args[:2] == ("worktree", "add") for args in git_args) == 1
    assert all("push" not in args and "remove" not in args for args in git_args)


def test_git_worktree_observe_distinguishes_absent_exact_and_conflict(
    tmp_path: Path,
) -> None:
    absent_spec = _worktree_spec(tmp_path / "absent")
    absent_runner = GitStateRunner(worktree=absent_spec.worktree)
    absent = GitWorktreeAdapter(runner=absent_runner).observe(absent_spec)
    assert absent.state is WorktreeObservationState.ABSENT

    exact_spec = _worktree_spec(tmp_path / "exact")
    exact_spec.worktree.mkdir()
    exact_runner = GitStateRunner(
        worktree=exact_spec.worktree,
        branch_commit=BASE_COMMIT,
        registered=True,
    )
    exact = GitWorktreeAdapter(runner=exact_runner).observe(exact_spec)
    assert exact.state is WorktreeObservationState.EXACT

    conflict_spec = _worktree_spec(tmp_path / "conflict")
    conflict_runner = GitStateRunner(
        worktree=conflict_spec.worktree,
        branch_commit=OTHER_COMMIT,
    )
    conflict = GitWorktreeAdapter(runner=conflict_runner).observe(conflict_spec)
    assert conflict.state is WorktreeObservationState.CONFLICT
    assert conflict.reason == "requested branch does not point to the base commit"


def test_git_worktree_conflict_fails_without_mutation(tmp_path: Path) -> None:
    spec = _worktree_spec(tmp_path)
    spec.worktree.mkdir()
    runner = GitStateRunner(worktree=spec.worktree)
    adapter = GitWorktreeAdapter(runner=runner)

    with pytest.raises(RCPError) as raised:
        adapter.create_or_observe(spec)

    assert raised.value.code == "git_worktree_conflict"
    assert all(call.argv[5:7] != ("worktree", "add") for call in runner.calls)


def test_git_validates_branch_with_git_before_other_git_operations(
    tmp_path: Path,
) -> None:
    spec = _worktree_spec(tmp_path)
    runner = GitStateRunner(worktree=spec.worktree, valid_branch=False)

    with pytest.raises(RCPError) as raised:
        GitWorktreeAdapter(runner=runner).observe(spec)

    assert raised.value.code == "git_worktree_branch_invalid"
    assert [call.argv[5:] for call in runner.calls] == [
        ("check-ref-format", "--branch", BRANCH)
    ]


def test_git_rejects_a_symlink_worktree_before_running_git(tmp_path: Path) -> None:
    spec = _worktree_spec(tmp_path)
    target = tmp_path / "target"
    target.mkdir()
    spec.worktree.symlink_to(target, target_is_directory=True)
    runner = GitStateRunner(worktree=spec.worktree)

    with pytest.raises(RCPError) as raised:
        GitWorktreeAdapter(runner=runner).observe(spec)

    assert raised.value.code == "git_worktree_path_symlink"
    assert runner.calls == []


def test_git_cleans_inherited_repository_and_config_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    spec = _worktree_spec(tmp_path)
    runner = GitStateRunner(worktree=spec.worktree)
    monkeypatch.setenv("GIT_DIR", "/secret/repository")
    monkeypatch.setenv("GIT_WORK_TREE", "/secret/worktree")
    monkeypatch.setenv("GIT_COMMON_DIR", "/secret/common")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "credential.helper")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "unsafe")
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "credential.helper=unsafe")
    monkeypatch.setenv("RCP_UNRELATED", "preserved")

    GitWorktreeAdapter(runner=runner).observe(spec)

    assert runner.calls
    for call in runner.calls:
        assert call.env is not None
        assert "GIT_DIR" not in call.env
        assert "GIT_WORK_TREE" not in call.env
        assert "GIT_COMMON_DIR" not in call.env
        assert "GIT_CONFIG_COUNT" not in call.env
        assert "GIT_CONFIG_KEY_0" not in call.env
        assert "GIT_CONFIG_VALUE_0" not in call.env
        assert "GIT_CONFIG_PARAMETERS" not in call.env
        assert call.env["RCP_UNRELATED"] == "preserved"


def test_git_timeout_is_a_typed_error(tmp_path: Path) -> None:
    spec = _worktree_spec(tmp_path)

    def timeout(call: RunnerCall) -> CommandResult:
        raise subprocess.TimeoutExpired(call.argv, call.timeout_seconds)

    runner = ScriptedRunner(timeout)
    with pytest.raises(RCPError) as raised:
        GitWorktreeAdapter(runner=runner, timeout_seconds=0.25).observe(spec)

    assert raised.value.code == "git_timeout"
    assert raised.value.context == {"root": str(spec.root)}


def test_git_resolve_commit_accepts_a_full_object_id(tmp_path: Path) -> None:
    def resolve(call: RunnerCall) -> CommandResult:
        assert call.argv[5:] == (
            "rev-parse",
            "--verify",
            "--quiet",
            f"{BASE_COMMIT}^{{commit}}",
        )
        return CommandResult(0, stdout=f"{BASE_COMMIT}\n")

    runner = ScriptedRunner(resolve)
    resolved = GitWorktreeAdapter(runner=runner).resolve_commit(tmp_path, BASE_COMMIT)

    assert resolved == BASE_COMMIT
    assert len(runner.calls) == 1
    assert runner.calls[0].argv[:5] == (
        "git",
        "-c",
        "core.fsmonitor=false",
        "-C",
        str(tmp_path),
    )


def test_git_resolve_commit_accepts_a_valid_heads_ref(tmp_path: Path) -> None:
    revision = f"refs/heads/{BRANCH}"

    def resolve(call: RunnerCall) -> CommandResult:
        args = call.argv[5:]
        if args == ("check-ref-format", revision):
            return CommandResult(0)
        assert args == (
            "rev-parse",
            "--verify",
            "--quiet",
            f"{revision}^{{commit}}",
        )
        return CommandResult(0, stdout=f"{BASE_COMMIT}\n")

    runner = ScriptedRunner(resolve)
    resolved = GitWorktreeAdapter(runner=runner).resolve_commit(tmp_path, revision)

    assert resolved == BASE_COMMIT
    assert [call.argv[5:] for call in runner.calls] == [
        ("check-ref-format", revision),
        ("rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}"),
    ]


@pytest.mark.parametrize(
    "revision",
    [
        "HEAD",
        "main",
        "refs/tags/v1",
        "--verify",
        "refs/heads/",
        "refs/heads/-option-like",
        f"{BASE_COMMIT}^",
        f"{BASE_COMMIT}\x00tail",
        "refs/heads/main\n--help",
    ],
)
def test_git_resolve_commit_rejects_untrusted_revision_without_running(
    tmp_path: Path,
    revision: str,
) -> None:
    runner = ScriptedRunner(lambda call: CommandResult(0))

    with pytest.raises(RCPError) as raised:
        GitWorktreeAdapter(runner=runner).resolve_commit(tmp_path, revision)

    assert raised.value.code == "git_revision_invalid"
    assert runner.calls == []


def test_git_resolve_commit_rejects_ref_git_declares_invalid(
    tmp_path: Path,
) -> None:
    revision = "refs/heads/bad..name"

    def invalid_ref(call: RunnerCall) -> CommandResult:
        assert call.argv[5:] == ("check-ref-format", revision)
        return CommandResult(1)

    runner = ScriptedRunner(invalid_ref)
    with pytest.raises(RCPError) as raised:
        GitWorktreeAdapter(runner=runner).resolve_commit(tmp_path, revision)

    assert raised.value.code == "git_revision_invalid"
    assert len(runner.calls) == 1


@pytest.mark.parametrize(
    "output",
    [
        f"{BASE_COMMIT}\n{OTHER_COMMIT}\n",
        f" {BASE_COMMIT}\n",
        f"{BASE_COMMIT.upper()}\n",
        "not-an-object-id\n",
    ],
)
def test_git_resolve_commit_rejects_malformed_output(
    tmp_path: Path,
    output: str,
) -> None:
    runner = ScriptedRunner(lambda call: CommandResult(0, stdout=output))

    with pytest.raises(RCPError) as raised:
        GitWorktreeAdapter(runner=runner).resolve_commit(tmp_path, BASE_COMMIT)

    assert raised.value.code == "git_output_invalid"


@pytest.mark.parametrize(
    ("returncode", "code"),
    [
        (1, "git_revision_not_found"),
        (2, "git_command_failed"),
    ],
)
def test_git_resolve_commit_maps_not_found_and_command_error(
    tmp_path: Path,
    returncode: int,
    code: str,
) -> None:
    runner = ScriptedRunner(lambda call: CommandResult(returncode))

    with pytest.raises(RCPError) as raised:
        GitWorktreeAdapter(runner=runner).resolve_commit(tmp_path, BASE_COMMIT)

    assert raised.value.code == code
    if returncode == 2:
        assert raised.value.context == {"returncode": 2}


def test_git_resolve_commit_maps_ref_validation_command_error(
    tmp_path: Path,
) -> None:
    runner = ScriptedRunner(lambda call: CommandResult(2))

    with pytest.raises(RCPError) as raised:
        GitWorktreeAdapter(runner=runner).resolve_commit(
            tmp_path,
            "refs/heads/valid-name",
        )

    assert raised.value.code == "git_command_failed"
    assert raised.value.context == {"returncode": 2}


def test_git_worktree_head_uses_a_fixed_revision_argv(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    def head(call: RunnerCall) -> CommandResult:
        assert call.argv[5:] == (
            "rev-parse",
            "--verify",
            "--quiet",
            "HEAD^{commit}",
        )
        return CommandResult(0, stdout=f"{OTHER_COMMIT}\n")

    runner = ScriptedRunner(head)
    resolved = GitWorktreeAdapter(runner=runner).worktree_head(worktree)

    assert resolved == OTHER_COMMIT
    assert len(runner.calls) == 1
    assert runner.calls[0].argv[4] == str(worktree)


@pytest.mark.parametrize(
    ("result", "code"),
    [
        (CommandResult(0, stdout=f"{BASE_COMMIT}\nextra\n"), "git_output_invalid"),
        (CommandResult(1), "git_worktree_head_not_found"),
        (CommandResult(128), "git_command_failed"),
    ],
)
def test_git_worktree_head_maps_malformed_and_failed_results(
    tmp_path: Path,
    result: CommandResult,
    code: str,
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    runner = ScriptedRunner(lambda call: result)

    with pytest.raises(RCPError) as raised:
        GitWorktreeAdapter(runner=runner).worktree_head(worktree)

    assert raised.value.code == code


@pytest.mark.parametrize("operation", ["resolve", "head"])
def test_git_read_only_methods_map_timeout(
    tmp_path: Path,
    operation: str,
) -> None:
    def timeout(call: RunnerCall) -> CommandResult:
        raise subprocess.TimeoutExpired(call.argv, call.timeout_seconds)

    runner = ScriptedRunner(timeout)
    adapter = GitWorktreeAdapter(runner=runner, timeout_seconds=0.25)

    with pytest.raises(RCPError) as raised:
        if operation == "resolve":
            adapter.resolve_commit(tmp_path, BASE_COMMIT)
        else:
            adapter.worktree_head(tmp_path)

    assert raised.value.code == "git_timeout"
    assert raised.value.context == {"root": str(tmp_path)}


def test_tmux_name_is_derived_only_from_a_canonical_session_id() -> None:
    assert deterministic_tmux_session_name(SESSION_ID) == TMUX_NAME

    for invalid in ("manual-name", "session_bad", f"{SESSION_ID};new-session"):
        with pytest.raises(RCPError) as raised:
            deterministic_tmux_session_name(invalid)
        assert raised.value.code == "tmux_session_id_invalid"


def test_tmux_start_quotes_one_shell_command_and_sets_validated_environment(
    tmp_path: Path,
) -> None:
    runner = TmuxStateRunner()
    adapter = TmuxAdapter(runner=runner)
    marker = tmp_path / "not-created"
    command = (
        "python",
        "runner script.py",
        "single'quote",
        f"$(touch {marker}); echo injected",
        "",
    )

    created = adapter.start_session(
        TMUX_NAME,
        cwd=tmp_path,
        argv=command,
        environment={"MODE": "safe value", "DATASET": "trial'one"},
    )
    observed = adapter.start_session(
        TMUX_NAME,
        cwd=tmp_path,
        argv=("ignored-on-observe",),
    )

    assert created is True
    assert observed is False
    starts = [call.argv for call in runner.calls if call.argv[1] == "start-session"]
    assert len(starts) == 1
    start = starts[0]
    assert start[:8] == (
        "tmux",
        "start-session",
        "-d",
        "-s",
        TMUX_NAME,
        "-c",
        str(tmp_path),
        "-e",
    )
    assert start[8:11] == ("DATASET=trial'one", "-e", "MODE=safe value")
    assert len(start) == 12
    shell_command = start[-1]
    assert shell_command.startswith("exec ")
    assert shlex.split(shell_command.removeprefix("exec ")) == list(command)
    assert command[-2] not in start[:-1]
    assert not marker.exists()


@pytest.mark.parametrize(
    ("argv", "environment", "code"),
    [
        (("python\nnext",), None, "tmux_command_invalid"),
        (("python", "bad\x00arg"), None, "tmux_command_invalid"),
        (["python"], None, "tmux_command_invalid"),
        (("python",), {"lowercase": "value"}, "tmux_environment_invalid"),
        (("python",), {"1INVALID": "value"}, "tmux_environment_invalid"),
        (("python",), {"SECRET": "value\x00tail"}, "tmux_environment_invalid"),
    ],
)
def test_tmux_rejects_invalid_commands_and_environment_without_running(
    tmp_path: Path,
    argv: Any,
    environment: Any,
    code: str,
) -> None:
    runner = TmuxStateRunner()

    with pytest.raises(RCPError) as raised:
        TmuxAdapter(runner=runner).start_session(
            TMUX_NAME,
            cwd=tmp_path,
            argv=argv,
            environment=environment,
        )

    assert raised.value.code == code
    assert "SECRET" not in str(raised.value.context)
    assert "value" not in str(raised.value.context)
    assert runner.calls == []


def test_tmux_interrupt_and_attach_use_fixed_argv(tmp_path: Path) -> None:
    runner = TmuxStateRunner(present=True)
    adapter = TmuxAdapter(runner=runner)

    adapter.send_interrupt(TMUX_NAME)
    attach = adapter.attach_argv(TMUX_NAME)

    assert adapter.interrupt_argv(TMUX_NAME) == (
        "tmux",
        "send-keys",
        "-t",
        TMUX_NAME,
        "C-c",
    )
    assert attach == ("tmux", "attach-session", "-t", TMUX_NAME)
    assert any(
        call.argv == ("tmux", "send-keys", "-t", TMUX_NAME, "C-c")
        for call in runner.calls
    )


def test_tmux_missing_session_and_timeout_are_typed_errors() -> None:
    absent = TmuxAdapter(runner=TmuxStateRunner())

    with pytest.raises(RCPError) as missing:
        absent.attach_argv(TMUX_NAME)
    assert missing.value.code == "tmux_session_not_found"

    def timeout(call: RunnerCall) -> CommandResult:
        raise subprocess.TimeoutExpired(call.argv, call.timeout_seconds)

    with pytest.raises(RCPError) as timed_out:
        TmuxAdapter(runner=ScriptedRunner(timeout)).has_session(TMUX_NAME)
    assert timed_out.value.code == "tmux_timeout"


def test_codex_start_and_resume_argv_match_the_local_cli_contract(
    tmp_path: Path,
) -> None:
    adapter = CodexCliAdapter()
    prompt = "Inspect 'results' and do not run $(anything)."

    started = adapter.start_command(worktree=tmp_path, prompt=prompt)
    resumed = adapter.resume_command(
        worktree=tmp_path,
        prompt=prompt,
        session_id=NATIVE_ID,
    )

    assert started.argv == (
        "codex",
        "exec",
        "--json",
        "--sandbox",
        "workspace-write",
        "--cd",
        str(tmp_path),
        prompt,
    )
    assert resumed.argv == (
        "codex",
        "exec",
        "resume",
        NATIVE_ID,
        "--json",
        prompt,
    )
    assert started.cwd == tmp_path
    assert resumed.expected_session_id == NATIVE_ID
    assert "--dangerously-bypass-approvals-and-sandbox" not in started.argv
    assert "--dangerously-bypass-approvals-and-sandbox" not in resumed.argv


def test_claude_start_and_resume_argv_use_accept_edits_without_bypass(
    tmp_path: Path,
) -> None:
    adapter = ClaudeCliAdapter()
    prompt = "Continue the declared task."

    started = adapter.start_command(
        worktree=tmp_path,
        prompt=prompt,
        session_id=NATIVE_ID,
    )
    resumed = adapter.resume_command(
        worktree=tmp_path,
        prompt=prompt,
        session_id=NATIVE_ID,
    )

    common = (
        "claude",
        "--print",
        "--output-format",
        "stream-json",
        "--permission-mode",
        "acceptEdits",
    )
    assert started.argv == (*common, "--session-id", NATIVE_ID, prompt)
    assert resumed.argv == (*common, "--resume", NATIVE_ID, prompt)
    assert started.cwd == tmp_path
    assert "--dangerously-skip-permissions" not in started.argv
    assert "--dangerously-skip-permissions" not in resumed.argv


def test_codex_parser_reads_thread_started_and_allows_same_id_replay() -> None:
    output = _jsonl(
        {"type": "thread.started", "thread_id": NATIVE_ID},
        {"type": "item.completed", "item": {"type": "reasoning"}},
        {"type": "thread.started", "thread_id": NATIVE_ID},
    )

    assert CodexCliAdapter().parse_session_id(output) == NATIVE_ID


def test_claude_parser_accepts_real_top_level_stream_event_shapes() -> None:
    output = _jsonl(
        {"type": "system", "subtype": "init", "session_id": NATIVE_ID},
        {"type": "stream_event", "event": {"type": "content_block_delta"}},
        {"type": "assistant", "session_id": NATIVE_ID, "message": {}},
        {"type": "result", "session_id": NATIVE_ID, "result": "done"},
    )

    assert ClaudeCliAdapter().parse_session_id(output) == NATIVE_ID


@pytest.mark.parametrize(
    ("adapter", "output"),
    [
        (
            CodexCliAdapter(),
            _jsonl(
                {"type": "thread.started", "thread_id": NATIVE_ID},
                {"type": "thread.started", "thread_id": OTHER_NATIVE_ID},
            ),
        ),
        (
            ClaudeCliAdapter(),
            _jsonl(
                {"type": "system", "subtype": "init", "session_id": NATIVE_ID},
                {"type": "result", "session_id": OTHER_NATIVE_ID},
            ),
        ),
    ],
)
def test_agent_parsers_reject_conflicting_native_session_ids(
    adapter: Any,
    output: str,
) -> None:
    with pytest.raises(RCPError) as raised:
        adapter.parse_session_id(output)

    assert raised.value.code.endswith("_session_id_conflict")


@pytest.mark.parametrize("adapter", [CodexCliAdapter(), ClaudeCliAdapter()])
@pytest.mark.parametrize("output", ["{broken", "[]\n", "42\n"])
def test_agent_parsers_reject_malformed_json_lines(
    adapter: Any,
    output: str,
) -> None:
    with pytest.raises(RCPError) as raised:
        adapter.parse_session_id(output)

    assert raised.value.code.endswith("_jsonl_malformed")


def test_agent_parsers_reject_missing_or_invalid_session_ids() -> None:
    with pytest.raises(RCPError) as codex_missing:
        CodexCliAdapter().parse_session_id(_jsonl({"type": "turn.started"}))
    assert codex_missing.value.code == "codex_session_id_missing"

    with pytest.raises(RCPError) as claude_invalid:
        ClaudeCliAdapter().parse_session_id(
            _jsonl(
                {
                    "type": "system",
                    "subtype": "init",
                    "session_id": "--resume",
                }
            )
        )
    assert claude_invalid.value.code == "claude_session_id_invalid"


def test_agent_command_validation_rejects_unsafe_identity_inputs(
    tmp_path: Path,
) -> None:
    with pytest.raises(RCPError) as codex_supplied:
        CodexCliAdapter().start_command(
            worktree=tmp_path,
            prompt="task",
            session_id=NATIVE_ID,
        )
    assert codex_supplied.value.code == "codex_start_session_id_unsupported"

    with pytest.raises(RCPError) as claude_missing:
        ClaudeCliAdapter().start_command(worktree=tmp_path, prompt="task")
    assert claude_missing.value.code == "claude_session_id_required"

    with pytest.raises(RCPError) as invalid_resume:
        CodexCliAdapter().resume_command(
            worktree=tmp_path,
            prompt="task",
            session_id="--last",
        )
    assert invalid_resume.value.code == "codex_session_id_invalid"
