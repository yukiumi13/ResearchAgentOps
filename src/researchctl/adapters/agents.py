from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from researchctl.errors import RCPError


_DANGEROUS_FLAGS = {
    "--dangerously-bypass-approvals-and-sandbox",
    "--dangerously-skip-permissions",
}


@dataclass(frozen=True, slots=True)
class AgentCommand:
    argv: tuple[str, ...]
    cwd: Path
    expected_session_id: str | None = None

    def __post_init__(self) -> None:
        if not self.argv or any(flag in self.argv for flag in _DANGEROUS_FLAGS):
            raise ValueError("agent command is empty or contains a dangerous bypass flag")


class AgentCliContract(Protocol):
    def start_command(
        self,
        *,
        worktree: Path,
        prompt: str,
        session_id: str | None = None,
    ) -> AgentCommand: ...

    def resume_command(
        self,
        *,
        worktree: Path,
        prompt: str,
        session_id: str,
    ) -> AgentCommand: ...

    def parse_session_id(self, jsonl: str) -> str: ...


class CodexCliAdapter:
    def start_command(
        self,
        *,
        worktree: Path,
        prompt: str,
        session_id: str | None = None,
    ) -> AgentCommand:
        if session_id is not None:
            raise RCPError(
                code="codex_start_session_id_unsupported",
                message="Codex assigns its native thread ID when execution starts.",
            )
        cwd = _validate_worktree(worktree)
        value = _validate_prompt(prompt)
        return AgentCommand(
            argv=(
                "codex",
                "exec",
                "--json",
                "--sandbox",
                "workspace-write",
                "--cd",
                str(cwd),
                value,
            ),
            cwd=cwd,
        )

    def resume_command(
        self,
        *,
        worktree: Path,
        prompt: str,
        session_id: str,
    ) -> AgentCommand:
        cwd = _validate_worktree(worktree)
        value = _validate_prompt(prompt)
        native_id = _validate_uuid(session_id, provider="codex")
        return AgentCommand(
            argv=(
                "codex",
                "exec",
                "resume",
                native_id,
                "--json",
                value,
            ),
            cwd=cwd,
            expected_session_id=native_id,
        )

    def parse_session_id(self, jsonl: str) -> str:
        candidates: list[str] = []
        for line_number, event in _jsonl_events(jsonl, provider="codex"):
            if event.get("type") != "thread.started":
                continue
            candidate = event.get("thread_id")
            if not isinstance(candidate, str):
                raise RCPError(
                    code="codex_session_id_invalid",
                    message="Codex thread.started event did not contain a thread_id.",
                    context={"line": line_number},
                )
            candidates.append(_validate_uuid(candidate, provider="codex"))
        return _single_session_id(candidates, provider="codex")


class ClaudeCliAdapter:
    _SESSION_EVENT_TYPES = {"assistant", "result", "system", "user"}

    def start_command(
        self,
        *,
        worktree: Path,
        prompt: str,
        session_id: str | None = None,
    ) -> AgentCommand:
        cwd = _validate_worktree(worktree)
        value = _validate_prompt(prompt)
        if session_id is None:
            raise RCPError(
                code="claude_session_id_required",
                message="Claude start requires a caller-allocated UUID.",
            )
        native_id = _validate_uuid(session_id, provider="claude")
        return AgentCommand(
            argv=(
                "claude",
                "--print",
                "--output-format",
                "stream-json",
                "--permission-mode",
                "acceptEdits",
                "--session-id",
                native_id,
                value,
            ),
            cwd=cwd,
            expected_session_id=native_id,
        )

    def resume_command(
        self,
        *,
        worktree: Path,
        prompt: str,
        session_id: str,
    ) -> AgentCommand:
        cwd = _validate_worktree(worktree)
        value = _validate_prompt(prompt)
        native_id = _validate_uuid(session_id, provider="claude")
        return AgentCommand(
            argv=(
                "claude",
                "--print",
                "--output-format",
                "stream-json",
                "--permission-mode",
                "acceptEdits",
                "--resume",
                native_id,
                value,
            ),
            cwd=cwd,
            expected_session_id=native_id,
        )

    def parse_session_id(self, jsonl: str) -> str:
        candidates: list[str] = []
        for line_number, event in _jsonl_events(jsonl, provider="claude"):
            event_type = event.get("type")
            if event_type not in self._SESSION_EVENT_TYPES:
                continue
            if event_type == "system" and event.get("subtype") != "init":
                continue
            candidate = event.get("session_id")
            if candidate is None:
                continue
            if not isinstance(candidate, str):
                raise RCPError(
                    code="claude_session_id_invalid",
                    message="Claude stream event contained a non-string session_id.",
                    context={"line": line_number},
                )
            candidates.append(_validate_uuid(candidate, provider="claude"))
        return _single_session_id(candidates, provider="claude")


def _validate_worktree(worktree: Path) -> Path:
    path = Path(os.path.abspath(os.fspath(worktree)))
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise RCPError(
            code="agent_worktree_invalid",
            message="Agent worktree must be an existing non-symlink directory.",
            context={"worktree": str(path)},
        )
    return path


def _validate_prompt(prompt: str) -> str:
    if (
        not isinstance(prompt, str)
        or not prompt.strip()
        or "\x00" in prompt
    ):
        raise RCPError(
            code="agent_prompt_invalid",
            message="Agent prompt must be a non-empty string without NUL bytes.",
        )
    return prompt


def _validate_uuid(value: str, *, provider: str) -> str:
    if not isinstance(value, str) or value.startswith("-"):
        raise RCPError(
            code=f"{provider}_session_id_invalid",
            message=f"{provider.capitalize()} session ID must be a canonical UUID.",
        )
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise RCPError(
            code=f"{provider}_session_id_invalid",
            message=f"{provider.capitalize()} session ID must be a canonical UUID.",
        ) from exc
    canonical = str(parsed)
    if value != canonical:
        raise RCPError(
            code=f"{provider}_session_id_invalid",
            message=f"{provider.capitalize()} session ID must be a canonical UUID.",
        )
    return canonical


def _jsonl_events(
    jsonl: str,
    *,
    provider: str,
) -> tuple[tuple[int, dict[str, Any]], ...]:
    if not isinstance(jsonl, str):
        raise RCPError(
            code=f"{provider}_jsonl_malformed",
            message=f"{provider.capitalize()} output must be JSON Lines text.",
        )

    events: list[tuple[int, dict[str, Any]]] = []
    for line_number, raw_line in enumerate(jsonl.splitlines(), start=1):
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except (json.JSONDecodeError, RecursionError) as exc:
            raise RCPError(
                code=f"{provider}_jsonl_malformed",
                message=f"{provider.capitalize()} output contains malformed JSON Lines.",
                context={"line": line_number},
            ) from exc
        if not isinstance(event, dict):
            raise RCPError(
                code=f"{provider}_jsonl_malformed",
                message=f"{provider.capitalize()} JSON Lines events must be objects.",
                context={"line": line_number},
            )
        events.append((line_number, event))
    return tuple(events)


def _single_session_id(candidates: list[str], *, provider: str) -> str:
    if not candidates:
        raise RCPError(
            code=f"{provider}_session_id_missing",
            message=f"{provider.capitalize()} output did not declare a session ID.",
        )
    unique = set(candidates)
    if len(unique) != 1:
        raise RCPError(
            code=f"{provider}_session_id_conflict",
            message=f"{provider.capitalize()} output declared conflicting session IDs.",
        )
    return candidates[0]
