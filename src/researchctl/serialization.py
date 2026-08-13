from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from researchctl.domain.types import Sha256Digest

_MAX_YAML_ALIASES = 50

_MAX_YAML_BYTES = 2 * 1024 * 1024
_MAX_YAML_DEPTH = 64
_MAX_YAML_NODES = 100_000


class SerializationError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        remediation: str | None = None,
        error_type: str | None = None,
        line: int | None = None,
        column: int | None = None,
    ) -> None:
        super().__init__(message)
        self.remediation = remediation
        self.error_type = error_type
        self.line = line
        self.column = column

    def context(self) -> dict[str, object]:
        return {
            key: value
            for key, value in {
                "error_type": self.error_type,
                "line": self.line,
                "column": self.column,
            }.items()
            if value is not None
        }


class UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: UniqueKeyLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            line = key_node.start_mark.line + 1
            column = key_node.start_mark.column + 1
            raise SerializationError(
                f"duplicate YAML key {key!r} at line {line}, column {column}",
                remediation="Remove the duplicate mapping key at the reported location.",
                error_type="DuplicateKeyError",
                line=line,
                column=column,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _assert_finite(
    value: Any,
    *,
    path: str = "$",
    ancestors: set[int] | None = None,
    budget: list[int] | None = None,
    depth: int = 0,
) -> None:
    ancestors = ancestors if ancestors is not None else set()
    budget = budget if budget is not None else [_MAX_YAML_NODES]
    budget[0] -= 1
    if budget[0] < 0:
        raise SerializationError("protocol data exceeds traversal budget")
    if depth > _MAX_YAML_DEPTH:
        raise SerializationError("protocol data exceeds maximum nesting depth")
    if isinstance(value, float) and not math.isfinite(value):
        raise SerializationError(f"non-finite number at {path}")

    if not isinstance(value, (dict, list, tuple)):
        return
    identity = id(value)
    if identity in ancestors:
        raise SerializationError("protocol data contains a recursive alias")
    ancestors.add(identity)
    try:
        if isinstance(value, dict):
            for key, nested in value.items():
                if not isinstance(key, str):
                    raise SerializationError("protocol mapping keys must be strings")
                _assert_finite(
                    nested,
                    path=f"{path}.{key}",
                    ancestors=ancestors,
                    budget=budget,
                    depth=depth + 1,
                )
        else:
            for index, nested in enumerate(value):
                _assert_finite(
                    nested,
                    path=f"{path}[{index}]",
                    ancestors=ancestors,
                    budget=budget,
                    depth=depth + 1,
                )
    finally:
        ancestors.remove(identity)


def to_jsonable(value: BaseModel | dict[str, Any]) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        data = value.model_dump(mode="json", exclude_none=True)
    else:
        data = value
    _assert_finite(data)
    return data


def canonical_json_bytes(value: BaseModel | dict[str, Any]) -> bytes:
    data = to_jsonable(value)
    return json.dumps(
        data,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_digest(value: BaseModel | dict[str, Any]) -> Sha256Digest:
    digest = hashlib.sha256(canonical_json_bytes(value)).hexdigest()
    return f"sha256:{digest}"  # type: ignore[return-value]


def dump_yaml(value: BaseModel | dict[str, Any]) -> str:
    data = to_jsonable(value)
    return yaml.safe_dump(
        data,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=True,
    )


def _load_yaml_unchecked(text: str) -> dict[str, Any]:
    if len(text.encode("utf-8")) > _MAX_YAML_BYTES:
        raise SerializationError(
            f"YAML input exceeds {_MAX_YAML_BYTES} byte limit"
        )
    alias_count = sum(
        isinstance(token, yaml.tokens.AliasToken) for token in yaml.scan(text)
    )
    if alias_count > _MAX_YAML_ALIASES:
        raise SerializationError(
            f"YAML alias count {alias_count} exceeds limit {_MAX_YAML_ALIASES}"
        )
    loaded = yaml.load(text, Loader=UniqueKeyLoader)
    if not isinstance(loaded, dict):
        raise SerializationError("YAML root must be a mapping")
    _assert_finite(loaded)
    return loaded


def load_yaml(text: str) -> dict[str, Any]:
    try:
        return _load_yaml_unchecked(text)
    except SerializationError:
        raise
    except yaml.MarkedYAMLError as exc:
        error_type = type(exc).__name__
        mark = exc.problem_mark
        line = mark.line + 1 if mark is not None else None
        column = mark.column + 1 if mark is not None else None
        location = (
            f" at line {line}, column {column}"
            if line is not None and column is not None
            else ""
        )
        problem = " ".join((exc.problem or "invalid YAML syntax").split())
        remediation = (
            "Quote plain YAML text containing ': ' and fix the syntax at the "
            "reported line and column."
            if isinstance(exc, (yaml.scanner.ScannerError, yaml.parser.ParserError))
            else "Fix the YAML construct at the reported line and column."
        )
        raise SerializationError(
            f"invalid YAML ({error_type}){location}: {problem}",
            remediation=remediation,
            error_type=error_type,
            line=line,
            column=column,
        ) from exc
    except (yaml.YAMLError, RecursionError, TypeError, ValueError) as exc:
        error_type = type(exc).__name__
        raise SerializationError(
            f"invalid YAML ({error_type})",
            remediation="Fix the malformed canonical YAML and rerun validation.",
            error_type=error_type,
        ) from exc


def _yaml_node_at_path(
    text: str,
    path: tuple[str | int, ...],
) -> yaml.Node | None:
    try:
        node = yaml.compose(text, Loader=yaml.SafeLoader)
    except (yaml.YAMLError, RecursionError):
        return None
    if node is None:
        return None
    for component in path:
        if isinstance(node, yaml.MappingNode) and isinstance(component, str):
            match = next(
                (
                    value_node
                    for key_node, value_node in node.value
                    if isinstance(key_node, yaml.ScalarNode)
                    and key_node.value == component
                ),
                None,
            )
            if match is None:
                break
            node = match
            continue
        if isinstance(node, yaml.SequenceNode) and isinstance(component, int):
            if component < 0 or component >= len(node.value):
                break
            node = node.value[component]
            continue
        break
    return node


def validation_error_details(
    error: Any,
    *,
    source_path: Path | None = None,
) -> list[dict[str, Any]]:
    details = error.errors(
        include_url=False,
        include_context=False,
        include_input=False,
    )
    yaml_text: str | None = None
    line_offset = 0
    if source_path is not None:
        try:
            normalized = source_path.read_text(encoding="utf-8").replace(
                "\r\n", "\n"
            ).replace("\r", "\n")
        except (OSError, UnicodeError):
            normalized = ""
        if source_path.suffix.lower() == ".md" and normalized.startswith("---\n"):
            marker = normalized.find("\n---\n", 4)
            if marker >= 0:
                yaml_text = normalized[4:marker]
                line_offset = 1
        elif normalized:
            yaml_text = normalized
    rendered: list[dict[str, Any]] = []
    for detail in details:
        item = dict(detail)
        location = tuple(
            component
            for component in item.get("loc", ())
            if isinstance(component, (str, int)) and not isinstance(component, bool)
        )
        item["loc"] = list(location)
        if yaml_text is not None:
            node = _yaml_node_at_path(yaml_text, location)
            if node is not None:
                item["line"] = node.start_mark.line + 1 + line_offset
                item["column"] = node.start_mark.column + 1
        rendered.append(item)
    return rendered


def load_model[ModelT: BaseModel](path: Path, model_type: type[ModelT]) -> ModelT:
    if path.stat().st_size > _MAX_YAML_BYTES:
        raise SerializationError(
            f"YAML input exceeds {_MAX_YAML_BYTES} byte limit"
        )
    data = load_yaml(path.read_text(encoding="utf-8"))
    return model_type.model_validate(data)


def write_model(path: Path, value: BaseModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_yaml(value), encoding="utf-8")
