from __future__ import annotations

import hashlib
import os
import re
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from researchctl.serialization import canonical_digest, load_yaml

_PROVENANCE = re.compile(
    rb"<!-- researchctl-generated:"
    rb"(?P<renderer>[A-Za-z0-9._-]+);"
    rb"source=(?P<source>sha256:[0-9a-f]{64});"
    rb"(?:source-format=(?P<source_format>[A-Za-z0-9._-]+);)?"
    rb"body=(?P<body>sha256:[0-9a-f]{64}) -->"
)


@dataclass(frozen=True, slots=True)
class GeneratedMarkdownProvenance:
    renderer_id: str
    source_digest: str
    source_format: str | None
    body_digest: str


@dataclass(frozen=True, slots=True)
class ProjectFrontmatterEnvelope:
    values: dict[str, object]
    prefix: bytes
    body: bytes


_FRONTMATTER = re.compile(
    rb"\A---(?P<newline>\r?\n)(?P<yaml>.*?)(?P=newline)---(?P=newline)",
    re.DOTALL,
)


def inspect_project_frontmatter(
    content: bytes,
) -> ProjectFrontmatterEnvelope | None:
    match = _FRONTMATTER.match(content)
    if match is None:
        return None
    payload = load_yaml(match.group("yaml").decode("utf-8", errors="strict"))
    newline = match.group("newline")
    prefix = content[: match.end()].rstrip(b"\r\n") + newline + newline
    body = content[match.end() :].lstrip(b"\r\n")
    return ProjectFrontmatterEnvelope(values=payload, prefix=prefix, body=body)


def _digest(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def render_generated_markdown(
    lines: list[str],
    *,
    renderer_id: str,
    source: BaseModel | dict[str, object],
    source_format: str | None = None,
) -> bytes:
    body = ("\n".join(lines).rstrip() + "\n").encode("utf-8")
    visible_marker = f"researchctl-renderer:{renderer_id}"
    marker_indexes = [index for index, line in enumerate(lines) if visible_marker in line]
    if len(marker_indexes) != 1:
        raise ValueError("generated Markdown requires exactly one visible renderer marker")
    format_marker = f"source-format={source_format};" if source_format else ""
    provenance = (
        f"<!-- researchctl-generated:{renderer_id};"
        f"source={canonical_digest(source)};{format_marker}body={_digest(body)} -->"
    )
    rendered = list(lines)
    rendered.insert(marker_indexes[0] + 1, provenance)
    return ("\n".join(rendered).rstrip() + "\n").encode("utf-8")


def inspect_generated_markdown(
    content: bytes,
) -> GeneratedMarkdownProvenance | None:
    matches: list[tuple[re.Match[bytes], int]] = []
    lines = content.splitlines(keepends=True)
    for index, line in enumerate(lines):
        match = _PROVENANCE.fullmatch(line.rstrip(b"\r\n"))
        if match is not None:
            matches.append((match, index))
    if len(matches) != 1:
        return None
    match, marker_index = matches[0]
    body = b"".join((*lines[:marker_index], *lines[marker_index + 1 :]))
    recorded_body = match.group("body").decode("ascii")
    if _digest(body) != recorded_body:
        return None
    return GeneratedMarkdownProvenance(
        renderer_id=match.group("renderer").decode("ascii"),
        source_digest=match.group("source").decode("ascii"),
        source_format=(
            match.group("source_format").decode("ascii")
            if match.group("source_format") is not None
            else None
        ),
        body_digest=recorded_body,
    )


def permits_generated_markdown_replacement(existing: bytes, replacement: bytes) -> bool:
    existing_provenance = inspect_generated_markdown(existing)
    replacement_provenance = inspect_generated_markdown(replacement)
    return (
        existing_provenance is not None
        and replacement_provenance is not None
        and existing_provenance.renderer_id == replacement_provenance.renderer_id
    )


def atomic_replace_bytes(destination: Path, content: bytes, mode: int) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except Exception:
        with suppress(OSError):
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise
