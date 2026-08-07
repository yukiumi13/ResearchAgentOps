from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from re import sub
from typing import Annotated, Literal, NoReturn

import typer
from pydantic import TypeAdapter, ValidationError

from researchctl.constants import PROJECT_POLICY_PATH
from researchctl.domain.ids import new_id
from researchctl.domain.models import (
    AgentGuideFormat,
    AnalysisBrief,
    DesignDocument,
    DocumentLayoutPolicy,
    DocumentRoute,
    DocumentSchema,
    MarkdownFrontmatter,
    ProjectPolicy,
    ProjectStatusSummary,
)
from researchctl.domain.types import RepositoryPath
from researchctl.errors import RCPError
from researchctl.output import (
    dump_envelope,
    envelope,
    error_payload,
    human_error_detail_lines,
)
from researchctl.repository import current_head, discover_repository, safe_repository_path
from researchctl.schema import generate_schema_files
from researchctl.serialization import (
    SerializationError,
    dump_yaml,
    load_model,
    load_yaml,
    validation_error_details,
)
from researchctl.services.generated_markdown import (
    inspect_project_frontmatter,
    permits_generated_markdown_replacement,
)
from researchctl.services.project_documents import (
    DocumentLintResult,
    DocumentTreeLintResult,
    agent_guide_markers,
    inspect_document_relation_target,
    lint_document_tree,
    lint_project_document,
    load_markdown_frontmatter,
    load_project_document,
    render_document_index,
    render_project_agent_guide,
    render_project_document,
    render_standalone_document_policy_template,
    require_adopted_document_policy,
)
from researchctl.services.requests import DocumentLayoutConfigureRequest
from researchctl.services.research_writing import (
    lint_analysis_brief,
    lint_analysis_brief_payload,
    render_analysis_brief,
    writing_findings_as_validation_details,
)

doc_app = typer.Typer(
    help="Draft policy, lint, render, and classify governed project documents.",
    no_args_is_help=True,
)

_STANDALONE_POLICY = ".researchctl-docs.yaml"
_DOCUMENT_CONTRACTS: tuple[DocumentSchema, ...] = (
    "markdown-frontmatter",
    "analysis-brief",
    "design-document",
    "project-status-summary",
)
_REPOSITORY_PATH = TypeAdapter(RepositoryPath)


def _stream_output(output_file: Path | None) -> bool:
    return output_file is None or str(output_file) in {
        "-",
        "/dev/stdout",
        "/proc/self/fd/1",
    }


def _error(
    exc: Exception,
    *,
    source_path: Path | None = None,
    additional_details: list[dict[str, object]] | None = None,
) -> RCPError:
    if isinstance(exc, RCPError):
        return exc
    if isinstance(exc, ValidationError):
        details = validation_error_details(exc, source_path=source_path)
        if additional_details:
            details.extend(additional_details)
        return RCPError(
            code="validation_error",
            message="Document contract schema validation failed.",
            remediation=(
                "Fix the listed fields and rerun `researchctl doc check PATH`."
            ),
            context={"details": details},
        )
    if isinstance(exc, SerializationError):
        return RCPError(
            code="serialization_error",
            message=str(exc),
            remediation=exc.remediation or "Fix the reported canonical YAML error.",
            context=exc.context(),
        )
    if isinstance(exc, (OSError, UnicodeError, ValueError)):
        return RCPError(
            code="invalid_local_state",
            message=str(exc),
            remediation="Check document paths, policy, and file contents.",
        )
    raise exc


def _analysis_brief_schema_error(exc: ValidationError, source: Path) -> RCPError:
    additional: list[dict[str, object]] = []
    try:
        payload = load_yaml(source.read_text(encoding="utf-8"))
        raw_lint = lint_analysis_brief_payload(payload)
        additional = writing_findings_as_validation_details(raw_lint.findings)
    except (OSError, SerializationError):
        pass
    return _error(
        exc,
        source_path=source,
        additional_details=additional,
    )


def _abort(error: RCPError, *, command: str, json_output: bool) -> NoReturn:
    if json_output:
        typer.echo(
            dump_envelope(
                envelope(
                    command=command,
                    success=False,
                    errors=[error_payload(error)],
                )
            )
        )
    else:
        typer.echo(f"Error [{error.code}]: {error.message}", err=True)
        for line in human_error_detail_lines(error):
            typer.echo(line, err=True)
        if error.remediation:
            typer.echo(f"Next: {error.remediation}", err=True)
    raise typer.Exit(code=2)


def _emit_lint(
    result: DocumentLintResult | DocumentTreeLintResult,
    *,
    command: str,
    json_output: bool,
) -> None:
    data = result.as_dict()
    if json_output:
        typer.echo(dump_envelope(envelope(command=command, success=result.passed, data=data)))
    else:
        typer.echo(f"Outcome: {result.terminal_result}")
        if isinstance(result, DocumentLintResult):
            typer.echo(f"Document: {result.document_id} ({result.document_kind})")
            typer.echo(f"Classification: {result.classification}")
        else:
            typer.echo(f"Root: {result.root}")
            typer.echo(f"Checked: {result.checked_files} files")
            typer.echo(f"Structured documents: {result.structured_documents}")
        for finding in result.findings:
            typer.echo(
                f"  {finding.kind}: {finding.path} [{finding.code}] {finding.message}"
            )
    if not result.passed:
        raise typer.Exit(code=2)


def _write_or_echo(
    content: bytes,
    output_file: Path | None,
    *,
    preserved_frontmatter_fields: tuple[str, ...] = (),
) -> None:
    if _stream_output(output_file):
        typer.echo(content.decode("utf-8"), nl=False)
        return
    destination = Path(os.path.abspath(os.fspath(output_file)))
    if destination.parent.is_symlink() or not destination.parent.is_dir():
        raise RCPError(
            code="document_output_path_invalid",
            message="Document output parent must be an existing non-symlink directory.",
            context={"path": str(output_file)},
        )
    if destination.exists() or destination.is_symlink():
        regular_file = destination.is_file() and not destination.is_symlink()
        if regular_file and preserved_frontmatter_fields:
            observed = destination.read_bytes()
            envelope = inspect_project_frontmatter(observed)
            if envelope is None:
                raise RCPError(
                    code="document_generated_frontmatter_missing",
                    message="Generated output has no configured project frontmatter envelope.",
                    remediation="Add the project frontmatter block, then rerun doc render.",
                    context={"path": str(output_file)},
                )
            missing = tuple(
                field
                for field in preserved_frontmatter_fields
                if field not in envelope.values
            )
            if missing:
                raise RCPError(
                    code="document_generated_frontmatter_invalid",
                    message="Generated output is missing required project frontmatter fields.",
                    remediation="Complete the project-owned frontmatter and rerun doc render.",
                    context={"path": str(output_file), "missing_fields": list(missing)},
                )
            replacement = envelope.prefix + content
            if observed == replacement:
                typer.echo(f"Unchanged: {destination}")
                return
            if not envelope.body or permits_generated_markdown_replacement(
                envelope.body,
                content,
            ):
                _atomic_replace(
                    destination,
                    replacement,
                    stat.S_IMODE(destination.stat().st_mode),
                )
                typer.echo(f"Updated: {destination}")
                return
            raise RCPError(
                code="document_output_conflict",
                message="Generated Markdown body differs from its recorded body digest.",
                remediation=(
                    "Restore the renderer-owned body while preserving project frontmatter, "
                    "then rerun doc render."
                ),
                context={"path": str(output_file)},
            )
        if (
            regular_file
            and destination.read_bytes() == content
        ):
            typer.echo(f"Unchanged: {destination}")
            return
        if regular_file:
            observed = destination.read_bytes()
            if permits_generated_markdown_replacement(observed, content):
                _atomic_replace(
                    destination,
                    content,
                    stat.S_IMODE(destination.stat().st_mode),
                )
                typer.echo(f"Updated: {destination}")
                return
        raise RCPError(
            code="document_output_conflict",
            message="Document output path already contains different content.",
            remediation=(
                "Choose a new path, or restore an unedited renderer-owned output before "
                "refreshing it."
            ),
            context={"path": str(output_file)},
        )
    if preserved_frontmatter_fields:
        raise RCPError(
            code="document_generated_frontmatter_missing",
            message="Configured project frontmatter must exist before first routed render.",
            remediation=(
                "Create the Markdown target with its project frontmatter block, then "
                "rerun doc render; researchctl will fill only the generated body."
            ),
            context={
                "path": str(output_file),
                "required_fields": list(preserved_frontmatter_fields),
            },
        )
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    typer.echo(f"Rendered: {destination}")


def _agent_guide_destination(
    repository: Path,
    output_file: Path,
    policy: DocumentLayoutPolicy,
    requested_format: AgentGuideFormat | None,
) -> tuple[Path, AgentGuideFormat]:
    lexical = (
        Path(os.path.abspath(os.fspath(output_file)))
        if output_file.is_absolute()
        else Path(os.path.abspath(os.fspath(repository / output_file)))
    )
    try:
        relative = lexical.relative_to(repository).as_posix()
    except ValueError as error:
        raise RCPError(
            code="agent_guide_output_outside_repository",
            message="Agent guide output must stay inside the selected repository.",
            context={"path": str(output_file)},
        ) from error
    target = next((item for item in policy.agent_guides if item.path == relative), None)
    if target is None:
        raise RCPError(
            code="agent_guide_target_unconfigured",
            message="Agent guide output is not declared in policy.agent_guides.",
            remediation="Declare the path and format in the protected document policy.",
            context={"path": relative},
        )
    if requested_format is not None and requested_format != target.format:
        raise RCPError(
            code="agent_guide_format_mismatch",
            message="Requested Agent guide format differs from the configured target.",
            context={
                "path": relative,
                "requested_format": requested_format,
                "configured_format": target.format,
            },
        )
    return safe_repository_path(repository, target.path), target.format


def _prepare_agent_guide_parent(repository: Path, destination: Path) -> None:
    try:
        relative_parent = destination.parent.relative_to(repository)
    except ValueError as error:
        raise RCPError(
            code="agent_guide_output_outside_repository",
            message="Agent guide output parent escapes the selected repository.",
        ) from error
    current = repository
    for part in relative_parent.parts:
        current = current / part
        if current.exists() or current.is_symlink():
            if current.is_symlink() or not current.is_dir():
                raise RCPError(
                    code="agent_guide_output_path_invalid",
                    message="Agent guide output parent must use non-symlink directories.",
                    context={"path": str(current)},
                )
            continue
        current.mkdir(mode=0o755)


def _atomic_replace(destination: Path, content: bytes, mode: int) -> None:
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
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise


def _upsert_agent_guide(
    repository: Path,
    destination: Path,
    content: bytes,
    guide_format: AgentGuideFormat,
) -> None:
    _prepare_agent_guide_parent(repository, destination)
    observed: str | None = None
    existing_mode = 0o644
    if destination.exists() or destination.is_symlink():
        if destination.is_symlink() or not destination.is_file():
            raise RCPError(
                code="agent_guide_output_path_invalid",
                message="Agent guide output must be a regular non-symlink file.",
                context={"path": str(destination)},
            )
        observed = destination.read_text(encoding="utf-8")
        existing_mode = stat.S_IMODE(destination.stat().st_mode)

    rendered = content.decode("utf-8")
    if observed is None:
        updated = rendered
        outcome = "Rendered"
    else:
        begin, end = agent_guide_markers(guide_format)
        identity = begin.removeprefix("<!-- researchctl-agent-guide:").removesuffix(
            ":begin -->"
        )
        current_pattern = re.compile(
            re.escape(begin) + r".*?" + re.escape(end) + r"(?:\r?\n)?",
            re.DOTALL,
        )
        legacy_pattern = re.compile(
            rf"<!-- researchctl-agent-guide:{re.escape(identity)}\.v"
            r"(?P<version>[0-9]+):begin -->.*?"
            rf"<!-- researchctl-agent-guide:{re.escape(identity)}\.v"
            r"(?P=version):end -->(?:\r?\n)?",
            re.DOTALL,
        )
        blocks = sorted(
            (*current_pattern.finditer(observed), *legacy_pattern.finditer(observed)),
            key=lambda match: match.start(),
        )
        marker_pattern = re.compile(
            rf"<!-- researchctl-agent-guide:{re.escape(identity)}"
            r"(?:\.v[0-9]+)?:(?:begin|end) -->"
        )
        overlaps = any(
            previous.end() > current.start()
            for previous, current in zip(blocks, blocks[1:], strict=False)
        )
        if len(marker_pattern.findall(observed)) != 2 * len(blocks) or overlaps:
            raise RCPError(
                code="agent_guide_marker_invalid",
                message="Agent guide contains incomplete or overlapping managed markers.",
            )
        if not blocks:
            separator = "" if not observed else ("\n" if observed.endswith("\n") else "\n\n")
            updated = observed + separator + rendered
        else:
            pieces = [observed[: blocks[0].start()], rendered]
            cursor = blocks[0].end()
            for block in blocks[1:]:
                between = observed[cursor : block.start()]
                if between.strip():
                    pieces.append(between)
                cursor = block.end()
            suffix = observed[cursor:]
            if suffix.strip():
                pieces.append(suffix)
            updated = "".join(pieces)
        outcome = "Updated"

    encoded = updated.encode("utf-8")
    if observed is not None and encoded == observed.encode("utf-8"):
        typer.echo(f"Unchanged: {destination}")
        return
    _atomic_replace(destination, encoded, existing_mode)
    typer.echo(f"{outcome}: {destination}")


def _repository_and_policy(
    project: Path,
    policy_file: Path | None,
) -> tuple[Path, DocumentLayoutPolicy]:
    def policy_validation_error(error: ValidationError, path: Path) -> RCPError:
        return RCPError(
            code="validation_error",
            message=f"Document policy schema validation failed in {path}.",
            remediation=(
                "Fix the listed policy fields and rerun "
                "`researchctl doc tree --project PROJECT`."
            ),
            context={
                "path": str(path),
                "details": validation_error_details(error, source_path=path),
            },
        )

    repository = discover_repository(project).root
    if policy_file is not None:
        try:
            policy = load_model(policy_file, DocumentLayoutPolicy)
        except ValidationError as error:
            raise policy_validation_error(error, policy_file) from error
        require_adopted_document_policy(policy)
        return repository, policy
    managed_policy = repository / PROJECT_POLICY_PATH
    standalone_policy = repository / _STANDALONE_POLICY
    if managed_policy.is_file() and not managed_policy.is_symlink():
        if standalone_policy.exists() or standalone_policy.is_symlink():
            raise RCPError(
                code="document_policy_shadowed",
                message="Managed projects cannot also define a standalone document policy.",
            )
        try:
            policy = load_model(managed_policy, ProjectPolicy).document_layout
        except ValidationError as error:
            raise policy_validation_error(error, managed_policy) from error
        require_adopted_document_policy(policy)
        return repository, policy
    if standalone_policy.is_file() and not standalone_policy.is_symlink():
        try:
            policy = load_model(standalone_policy, DocumentLayoutPolicy)
        except ValidationError as error:
            raise policy_validation_error(error, standalone_policy) from error
        require_adopted_document_policy(policy)
        return repository, policy
    if standalone_policy.is_symlink():
        raise RCPError(
            code="document_policy_invalid",
            message="Standalone document policy cannot be a symbolic link.",
        )
    raise RCPError(
        code="document_policy_missing",
        message="Repository has no managed or standalone document policy.",
        remediation=(
            "Create .researchctl-docs.yaml, pass --policy-file, or run "
            "researchctl init."
        ),
    )


def _baseline_repository_and_document_root(
    project: Path,
    *,
    fallback_root: str,
) -> tuple[Path, str, bool]:
    """Read only the baseline policy field needed to protect frozen documents."""

    repository = discover_repository(project).root
    try:
        managed_policy = safe_repository_path(repository, PROJECT_POLICY_PATH)
        standalone_policy = safe_repository_path(repository, _STANDALONE_POLICY)
    except RCPError as error:
        raise RCPError(
            code="document_baseline_policy_invalid",
            message="Baseline document policy path cannot contain symbolic links.",
        ) from error
    managed_exists = managed_policy.exists() or managed_policy.is_symlink()
    standalone_exists = standalone_policy.exists() or standalone_policy.is_symlink()
    if managed_exists and standalone_exists:
        raise RCPError(
            code="document_policy_shadowed",
            message="Managed projects cannot also define a standalone document policy.",
        )
    if not managed_exists and not standalone_exists:
        return repository, fallback_root, True

    policy_path = managed_policy if managed_exists else standalone_policy
    if not policy_path.is_file():
        raise RCPError(
            code="document_baseline_policy_invalid",
            message="Baseline document policy must be a regular non-symlink file.",
            context={"path": policy_path.relative_to(repository).as_posix()},
        )
    try:
        payload = load_yaml(policy_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, SerializationError) as error:
        raise RCPError(
            code="document_baseline_policy_invalid",
            message=f"Baseline document policy cannot be read safely: {error}",
            remediation="Repair the baseline policy syntax before enforcing frozen documents.",
            context={"path": policy_path.relative_to(repository).as_posix()},
        ) from error

    if policy_path == managed_policy:
        document_layout = payload.get("document_layout", {})
        if not isinstance(document_layout, dict):
            raise RCPError(
                code="document_baseline_policy_invalid",
                message="Baseline managed policy document_layout must be a mapping.",
                context={"path": PROJECT_POLICY_PATH},
            )
        raw_root = document_layout.get("root", "docs")
    else:
        raw_root = payload.get("root", "docs")
    try:
        document_root = _REPOSITORY_PATH.validate_python(raw_root)
    except ValidationError as error:
        raise RCPError(
            code="document_baseline_policy_invalid",
            message="Baseline document root is not a safe repository-relative path.",
            context={
                "path": policy_path.relative_to(repository).as_posix(),
                "details": validation_error_details(error),
            },
        ) from error
    return repository, document_root, False


def _document_relative_path(repository: Path, document_file: Path) -> tuple[Path, str]:
    candidate = Path(
        os.path.abspath(
            os.fspath(document_file if document_file.is_absolute() else repository / document_file)
        )
    )
    try:
        relative = candidate.relative_to(repository).as_posix()
    except ValueError as error:
        raise RCPError(
            code="document_path_outside_repository",
            message="Routed document paths must stay inside the selected repository.",
            context={"path": str(document_file)},
        ) from error
    safe = safe_repository_path(repository, relative)
    if safe.is_symlink() or not safe.is_file():
        raise RCPError(
            code="document_source_invalid",
            message="Document source must be an existing non-symlink regular file.",
            context={"path": relative},
        )
    return safe, relative


def _route_for_relative(policy: DocumentLayoutPolicy, relative: str) -> DocumentRoute:
    matches = [
        route
        for route in policy.routes
        if relative.startswith(f"{route.directory}/")
    ]
    if len(matches) != 1:
        raise RCPError(
            code="document_path_unclassified",
            message="Document path is not covered by exactly one accepted route.",
            context={"path": relative},
        )
    return matches[0]


def _slugify(title: str) -> str:
    slug = sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    if not slug or not slug[0].isalpha():
        slug = f"document-{slug}" if slug else "document"
    return slug[:80].rstrip("-")


def _contract_data(contract: DocumentSchema) -> dict[str, object]:
    schema = json.loads(generate_schema_files()[f"{contract}.schema.json"])
    properties = schema.get("properties", {})
    prose_limits: dict[str, object] = {}
    if isinstance(schema.get("x-researchctl-prose"), dict):
        prose_limits["$"] = schema["x-researchctl-prose"]
    if isinstance(properties, dict):
        for name, value in properties.items():
            if isinstance(value, dict) and isinstance(
                value.get("x-researchctl-prose"), dict
            ):
                prose_limits[str(name)] = value["x-researchctl-prose"]
    source_format = (
        "Markdown with YAML frontmatter"
        if contract == "markdown-frontmatter"
        else "YAML"
    )
    standalone_check = (
        "researchctl brief lint PATH"
        if contract == "analysis-brief"
        else None
    )
    standalone_render = (
        "researchctl brief render PATH --output-file PATH.md"
        if contract == "analysis-brief"
        else None
    )
    return {
        "contract": contract,
        "source_format": source_format,
        "required_fields": schema.get("required", []),
        "check_command": standalone_check or "researchctl doc check PATH",
        "standalone_check_command": standalone_check,
        "routed_check_command": "researchctl doc check PATH --project .",
        "render_command": standalone_render or (
            None
            if contract == "markdown-frontmatter"
            else "researchctl doc render PATH --output-file PATH.md"
        ),
        "standalone_render_command": standalone_render,
        "routed_render_command": (
            None
            if contract == "markdown-frontmatter"
            else "researchctl doc render PATH --project . --output-file PATH.md"
        ),
        "schema_command": f"researchctl doc schema --contract {contract}",
        "prose_limits": prose_limits,
        "provenance": (
            "Use keyed sources + provenance for measured, estimated, derived, or "
            "external Markdown claims."
            if contract == "markdown-frontmatter"
            else "Use the contract's keyed sources/evidence fields."
        ),
        "source_storage": (
            "Standalone lint/render may use any YAML path. Governed route checking "
            "tracks SOURCE.yaml beside generated SOURCE.md; the marker source digest "
            "hashes canonical model JSON, not the YAML file bytes."
            if contract == "analysis-brief"
            else None
        ),
    }


def _scaffold_for_route(
    *,
    route: DocumentRoute,
    title: str,
    owner: str,
    basis_commit: str,
    now: datetime,
    relations: dict[str, tuple[str, ...]],
) -> bytes:
    if route.contract == "markdown-frontmatter":
        missing = [
            relation
            for relation in route.required_relations
            if not relations[relation]
        ]
        if missing:
            raise RCPError(
                code="document_scaffold_relation_missing",
                message="The selected route requires relation targets.",
                remediation=(
                    "Pass --supersedes, --derived-from, or --see-also for every "
                    "required relation."
                ),
                context={"required_relations": missing},
            )
        frontmatter = MarkdownFrontmatter.model_validate(
            {
                "type": route.document_type,
                "title": title,
                "owner": owner,
                "last_updated": now.date().isoformat(),
                "validity": "valid",
                "tags": [],
                "references": [],
                "relations": relations,
            }
        )
        contract_example = (
            "# Provenance example (uncomment and replace as one complete block):\n"
            "# sources:\n"
            "# - key: result-log\n"
            "#   kind: repository_path\n"
            "#   location: data/results.json\n"
            "# provenance:\n"
            "# - key: measured-count\n"
            "#   value: \"11677\"  # Quote numeric-looking display text.\n"
            "#   basis: derived\n"
            "#   source_keys: [result-log]\n"
            "#   method: Sum the per-shard counts.\n"
            "# The value must occur verbatim in the Markdown body.\n"
            "# Relation paths are repository-root relative, for example:\n"
            "# relations:\n"
            "#   see_also: [docs/runbooks/evaluation.md]\n"
        )
        return (
            "---\n"
            + dump_yaml(frontmatter)
            + contract_example
            + "---\n\n"
            + f"# {title}\n\n"
            + "Replace this paragraph with the governed document body.\n"
        ).encode("utf-8")
    if route.contract == "analysis-brief":
        document = AnalysisBrief.model_validate(
            {
                "question": title,
                "answer": "Replace with the evidence-supported answer.",
                "protocol": "replace-with-protocol",
                "metrics": [{"key": "result", "label": "Result"}],
                "evidence": [
                    {
                        "setting": "Baseline",
                        "values": {"result": "pending"},
                        "source_keys": ["source"],
                    }
                ],
                "interpretation": [],
                "limitations": ["Replace with the material limitation."],
                "sources": [
                    {"key": "source", "location": "data/replace-with-source.json"}
                ],
            }
        )
        return dump_yaml(document).encode("utf-8")

    common: dict[str, object] = {
        "document_id": new_id("document", now=now),
        "classification": route.classification,
        "slug": _slugify(title),
        "title": title,
        "status": "draft",
        "basis_commit": basis_commit,
        "revision": 1,
        "authored_by": {"role": "external_agent", "actor_id": owner},
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "sources": [],
    }
    if route.contract == "design-document":
        document = DesignDocument.model_validate(
            {
                **common,
                "document_kind": "design_document",
                "problem": "Replace with the concrete problem.",
                "context": "Replace with the relevant current state.",
                "goals": ["Replace with one measurable goal."],
                "non_goals": ["Replace with one explicit non-goal."],
                "constraints": ["Replace with one binding constraint."],
                "options": [
                    {
                        "key": "selected",
                        "summary": "Selected approach",
                        "benefits": ["Replace with a benefit."],
                        "drawbacks": ["Replace with a drawback."],
                        "disposition": "selected",
                        "rationale": "Replace with the selection rationale.",
                    },
                    {
                        "key": "alternative",
                        "summary": "Rejected alternative",
                        "benefits": ["Replace with a benefit."],
                        "drawbacks": ["Replace with a drawback."],
                        "disposition": "rejected",
                        "rationale": "Replace with the rejection rationale.",
                    },
                ],
                "components": [
                    {
                        "key": "component",
                        "responsibility": "Replace with the component responsibility.",
                        "interfaces": [],
                    }
                ],
                "workflows": [
                    {
                        "name": "Primary workflow",
                        "steps": ["Replace with step one.", "Replace with step two."],
                    }
                ],
                "security_considerations": ["Replace with a security consideration."],
                "failure_modes": [
                    {
                        "condition": "Replace with a failure condition.",
                        "behavior": "Replace with fail-closed behavior.",
                        "recovery": "Replace with the recovery action.",
                    }
                ],
                "migration_steps": ["Replace with a migration step."],
                "validation": [
                    {
                        "case": "Replace with a validation case.",
                        "expected": "Replace with the expected result.",
                        "evidence": "Replace with the evidence location.",
                    }
                ],
            }
        )
        return dump_yaml(document).encode("utf-8")
    if route.contract == "project-status-summary":
        common["sources"] = [
            {
                "key": "source",
                "kind": "repository_path",
                "location": "docs/README.md",
            }
        ]
        document = ProjectStatusSummary.model_validate(
            {
                **common,
                "document_kind": "project_status_summary",
                "as_of": now.isoformat(),
                "executive_summary": "Replace with the current project state.",
                "capabilities": [
                    {
                        "key": "capability",
                        "title": "Replace with a capability",
                        "status": "designed",
                        "summary": "Replace with current behavior.",
                        "evidence_keys": ["source"],
                        "missing": ["Replace with remaining work."],
                    }
                ],
                "next_steps": ["Replace with the next concrete action."],
            }
        )
        return dump_yaml(document).encode("utf-8")
    raise AssertionError(f"unsupported document contract: {route.contract}")


@doc_app.command("contracts")
def doc_contracts_command(
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit a stable JSON envelope."),
    ] = False,
) -> None:
    """List built-in document contracts and the commands that handle them."""

    contracts = [_contract_data(contract) for contract in _DOCUMENT_CONTRACTS]
    if json_output:
        typer.echo(
            dump_envelope(
                envelope(
                    command="doc.contracts",
                    success=True,
                    data={"contracts": contracts},
                )
            )
        )
        return
    for item in contracts:
        typer.echo(f"Contract: {item['contract']}")
        typer.echo(f"  Source: {item['source_format']}")
        required = item["required_fields"]
        if not isinstance(required, list):
            raise AssertionError("JSON Schema required fields must be a list")
        typer.echo(f"  Required: {', '.join(required)}")
        typer.echo(f"  Check: {item['check_command']}")
        standalone_check = item["standalone_check_command"]
        if standalone_check is not None:
            typer.echo(f"  Standalone check (no policy): {standalone_check}")
            typer.echo(f"  Routed check (policy required): {item['routed_check_command']}")
        typer.echo(f"  Schema: {item['schema_command']}")
        prose_limits = item["prose_limits"]
        if isinstance(prose_limits, dict) and prose_limits:
            typer.echo(
                "  Prose limits: "
                + json.dumps(prose_limits, ensure_ascii=False, sort_keys=True)
            )
        typer.echo(f"  Provenance: {item['provenance']}")
        renderer = item["render_command"] or "none (manual Markdown is canonical)"
        typer.echo(f"  Render: {renderer}")
        source_storage = item["source_storage"]
        if source_storage is not None:
            typer.echo(f"  Source storage: {source_storage}")


@doc_app.command("schema")
def doc_schema_command(
    contract: Annotated[
        DocumentSchema,
        typer.Option("--contract", help="Built-in document contract name."),
    ],
    output_file: Annotated[
        Path | None,
        typer.Option("--output-file", help="Write the JSON Schema to this path."),
    ] = None,
) -> None:
    """Print the complete JSON Schema for one document contract."""

    try:
        _write_or_echo(generate_schema_files()[f"{contract}.schema.json"], output_file)
    except Exception as exc:
        _abort(_error(exc), command="doc.schema", json_output=False)


@doc_app.command("scaffold")
def doc_scaffold_command(
    document_type: Annotated[
        str,
        typer.Option("--type", help="Document type from the effective policy."),
    ],
    title: Annotated[str, typer.Option("--title", help="Initial document title.")],
    project: Annotated[Path, typer.Option("--project", "-C")] = Path("."),
    policy_file: Annotated[
        Path | None,
        typer.Option("--policy-file", help="Standalone DocumentLayoutPolicy YAML."),
    ] = None,
    owner: Annotated[
        str,
        typer.Option(
            "--owner",
            help="Frontmatter owner or structured external-agent actor ID.",
        ),
    ] = "person:TODO",
    supersedes: Annotated[list[str] | None, typer.Option("--supersedes")] = None,
    derived_from: Annotated[list[str] | None, typer.Option("--derived-from")] = None,
    see_also: Annotated[list[str] | None, typer.Option("--see-also")] = None,
    output_file: Annotated[
        Path | None,
        typer.Option("--output-file", help="Write the scaffold to this path."),
    ] = None,
) -> None:
    """Generate a schema-valid source skeleton for one accepted route type."""

    try:
        repository, policy = _repository_and_policy(project, policy_file)
        route = next(
            (candidate for candidate in policy.routes if candidate.document_type == document_type),
            None,
        )
        if route is None:
            raise RCPError(
                code="document_type_unaccepted",
                message="Document type is not declared by the effective policy.",
                remediation="Run researchctl doc contracts and inspect the accepted routes.",
                context={"document_type": document_type},
            )
        repository_record = discover_repository(repository)
        now = datetime.now(UTC).replace(microsecond=0)
        content = _scaffold_for_route(
            route=route,
            title=title,
            owner=owner,
            basis_commit=current_head(repository_record) or "0" * 40,
            now=now,
            relations={
                "supersedes": tuple(supersedes or ()),
                "derived_from": tuple(derived_from or ()),
                "see_also": tuple(see_also or ()),
            },
        )
        _write_or_echo(content, output_file)
    except Exception as exc:
        _abort(_error(exc), command="doc.scaffold", json_output=False)


@doc_app.command("check")
def doc_check_command(
    document_file: Annotated[Path, typer.Argument(help="Routed Markdown or YAML source.")],
    project: Annotated[Path, typer.Option("--project", "-C")] = Path("."),
    policy_file: Annotated[
        Path | None,
        typer.Option("--policy-file", help="Standalone DocumentLayoutPolicy YAML."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit a stable JSON envelope."),
    ] = False,
) -> None:
    """Validate one source by dispatching through its accepted route contract."""

    command = "doc.check"
    validation_source: Path | None = None
    try:
        repository, policy = _repository_and_policy(project, policy_file)
        source, relative = _document_relative_path(repository, document_file)
        validation_source = source
        route = _route_for_relative(policy, relative)
        nested = PurePosixPath(relative).relative_to(route.directory)
        findings: list[dict[str, str]] = []
        if len(nested.parts) - 1 > policy.max_depth:
            findings.append(
                {
                    "kind": "invalid",
                    "code": "document_path_too_deep",
                    "path": relative,
                    "message": "Document path exceeds the configured maximum depth.",
                }
            )
        prose: dict[str, object] | None = None
        if route.contract == "markdown-frontmatter":
            if source.suffix.lower() != ".md":
                raise RCPError(
                    code="document_extension_invalid",
                    message="Markdown-frontmatter routes accept only .md files.",
                )
            frontmatter, body = load_markdown_frontmatter(
                source.read_text(encoding="utf-8"),
                path=relative,
            )
            if frontmatter.type != route.document_type:
                findings.append(
                    {
                        "kind": "invalid",
                        "code": "document_type_path_mismatch",
                        "path": relative,
                        "message": (
                            f"Frontmatter type {frontmatter.type} does not match route "
                            f"type {route.document_type}."
                        ),
                    }
                )
            for relation in route.required_relations:
                if not getattr(frontmatter.relations, relation):
                    findings.append(
                        {
                            "kind": "invalid",
                            "code": "document_required_relation_missing",
                            "path": f"{relative}:relations.{relation}",
                            "message": f"Route requires a non-empty {relation} relation.",
                        }
                    )
            for item in frontmatter.provenance:
                if item.value not in body:
                    findings.append(
                        {
                            "kind": "invalid",
                            "code": "document_provenance_value_missing",
                            "path": f"{relative}:provenance.{item.key}",
                            "message": (
                                f"Provenance value {item.value!r} does not occur in "
                                "the Markdown body."
                            ),
                        }
                    )
            for relation in ("supersedes", "derived_from", "see_also"):
                for target in getattr(frontmatter.relations, relation):
                    status, resolved = inspect_document_relation_target(
                        repository,
                        document_root=policy.root,
                        target=target,
                    )
                    if status == "legacy":
                        findings.append(
                            {
                                "kind": "invalid",
                                "code": "document_relation_path_legacy",
                                "path": f"{relative}:relations.{relation}",
                                "message": (
                                    "Relation paths are repository-root relative; "
                                    f"replace {target!r} with {resolved!r}."
                                ),
                            }
                        )
                    elif status == "missing":
                        findings.append(
                            {
                                "kind": "invalid",
                                "code": "document_relation_target_missing",
                                "path": f"{relative}:relations.{relation}",
                                "message": f"Relation target does not exist: {resolved}.",
                            }
                        )
        elif route.contract == "analysis-brief":
            if source.suffix.lower() != ".yaml":
                raise RCPError(
                    code="document_extension_invalid",
                    message="Structured document routes accept canonical .yaml sources.",
                )
            if len(nested.parts) != 1:
                findings.append(
                    {
                        "kind": "invalid",
                        "code": "structured_document_path_noncanonical",
                        "path": relative,
                        "message": "Structured document sources must be direct route children.",
                    }
                )
            try:
                brief = load_model(source, AnalysisBrief)
            except ValidationError as error:
                raise _analysis_brief_schema_error(error, source) from error
            result = lint_analysis_brief(brief)
            findings.extend(
                {
                    "kind": "invalid",
                    "code": finding.code,
                    "path": finding.field_path,
                    "message": finding.message,
                }
                for finding in result.findings
            )
            prose = {
                **result.prose.as_dict(),
                "max_english_words": result.max_english_words,
                "max_cjk_characters": result.max_cjk_characters,
            }
        else:
            if source.suffix.lower() != ".yaml":
                raise RCPError(
                    code="document_extension_invalid",
                    message="Structured document routes accept canonical .yaml sources.",
                )
            document = load_project_document(source)
            result = lint_project_document(document, policy=policy)
            findings.extend(finding.as_dict() for finding in result.findings)
            if document.classification != route.classification:
                findings.append(
                    {
                        "kind": "invalid",
                        "code": "document_classification_path_mismatch",
                        "path": relative,
                        "message": (
                            f"Document classification {document.classification} does not match "
                            f"path route {route.classification}."
                        ),
                    }
                )
            expected = f"{route.directory}/{document.slug}.yaml"
            if relative != expected:
                findings.append(
                    {
                        "kind": "invalid",
                        "code": "structured_document_path_noncanonical",
                        "path": relative,
                        "message": f"Document classification and slug require path {expected}.",
                    }
                )
        passed = not any(finding["kind"] == "invalid" for finding in findings)
        data: dict[str, object] = {
            "path": relative,
            "document_type": route.document_type,
            "classification": route.classification,
            "contract": route.contract,
            "terminal_result": "passed" if passed else "invalid",
            "findings": findings,
        }
        if prose is not None:
            data["prose"] = prose
    except Exception as exc:
        _abort(
            _error(exc, source_path=validation_source),
            command=command,
            json_output=json_output,
        )
    if json_output:
        typer.echo(dump_envelope(envelope(command=command, success=passed, data=data)))
    else:
        typer.echo(f"Outcome: {data['terminal_result']}")
        typer.echo(f"Path: {data['path']}")
        typer.echo(f"Contract: {data['contract']}")
        if prose is not None:
            typer.echo(
                "Prose: "
                f"{prose['english_words']}/{prose['max_english_words']} English words, "
                f"{prose['cjk_characters']}/{prose['max_cjk_characters']} CJK characters"
            )
        for finding in findings:
            typer.echo(
                f"  {finding['kind']}: {finding['path']} "
                f"[{finding['code']}] {finding['message']}"
            )
    if not passed:
        raise typer.Exit(code=2)


@doc_app.command("lint")
def doc_lint_command(
    document_file: Annotated[Path, typer.Argument(help="Structured document YAML.")],
    project: Annotated[Path, typer.Option("--project", "-C")] = Path("."),
    policy_file: Annotated[
        Path | None,
        typer.Option("--policy-file", help="Standalone DocumentLayoutPolicy YAML."),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Validate document schema, semantics, and accepted classification."""

    try:
        _repository, policy = _repository_and_policy(project, policy_file)
        document = load_project_document(document_file)
        result = lint_project_document(document, policy=policy)
    except Exception as exc:
        _abort(
            _error(exc, source_path=document_file),
            command="doc.lint",
            json_output=json_output,
        )
    _emit_lint(result, command="doc.lint", json_output=json_output)


@doc_app.command("render")
def doc_render_command(
    document_file: Annotated[Path, typer.Argument(help="Linted structured document YAML.")],
    output_file: Annotated[
        Path | None,
        typer.Option("--output-file", help="Write deterministic Markdown here."),
    ] = None,
    project: Annotated[Path, typer.Option("--project", "-C")] = Path("."),
    policy_file: Annotated[
        Path | None,
        typer.Option("--policy-file", help="Standalone DocumentLayoutPolicy YAML."),
    ] = None,
) -> None:
    """Render a passing routed YAML source as deterministic Markdown."""

    try:
        repository, policy = _repository_and_policy(project, policy_file)
        source, relative = _document_relative_path(repository, document_file)
        route = _route_for_relative(policy, relative)
        if route.contract == "markdown-frontmatter":
            raise RCPError(
                code="document_render_not_applicable",
                message="Manual Markdown is canonical and has no generated pair.",
                remediation="Validate it with researchctl doc check PATH.",
            )
        if route.contract == "analysis-brief":
            nested = PurePosixPath(relative).relative_to(route.directory)
            if len(nested.parts) != 1 or source.suffix.lower() != ".yaml":
                raise RCPError(
                    code="structured_document_path_noncanonical",
                    message=(
                        "Structured YAML sources must be direct children of their "
                        "accepted route directory."
                    ),
                )
            try:
                document = load_model(source, AnalysisBrief)
            except ValidationError as error:
                raise _analysis_brief_schema_error(error, source) from error
            content = render_analysis_brief(document)
        else:
            document = load_project_document(source)
            result = lint_project_document(document, policy=policy)
            expected = f"{route.directory}/{document.slug}.yaml"
            if (
                not result.passed
                or document.classification != route.classification
                or relative != expected
            ):
                raise RCPError(
                    code="document_lint_invalid",
                    message="Document does not satisfy its accepted classification route.",
                    context=result.as_dict(),
                )
            content = render_project_document(document)
        frontmatter_fields = (
            route.generated_markdown_frontmatter.required_fields
            if route.generated_markdown_frontmatter is not None
            else ()
        )
        _write_or_echo(
            content,
            output_file,
            preserved_frontmatter_fields=frontmatter_fields,
        )
    except Exception as exc:
        _abort(
            _error(exc, source_path=document_file),
            command="doc.render",
            json_output=False,
        )


@doc_app.command("tree")
def doc_tree_command(
    project: Annotated[Path, typer.Option("--project", "-C")] = Path("."),
    policy_file: Annotated[
        Path | None,
        typer.Option("--policy-file", help="Standalone DocumentLayoutPolicy YAML."),
    ] = None,
    baseline_project: Annotated[
        Path | None,
        typer.Option(
            "--baseline-project",
            help="Optional baseline checkout used to enforce frozen documents.",
        ),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Validate configured documents, Agent guides, and generated pairs."""

    try:
        repository, policy = _repository_and_policy(project, policy_file)
        baseline_repository: Path | None = None
        baseline_document_root: str | None = None
        baseline_policy_missing = False
        if baseline_project is not None:
            (
                baseline_repository,
                baseline_document_root,
                baseline_policy_missing,
            ) = _baseline_repository_and_document_root(
                baseline_project,
                fallback_root=policy.root,
            )
        result = lint_document_tree(
            repository,
            policy,
            baseline_root=baseline_repository,
            baseline_document_root=baseline_document_root,
            baseline_policy_missing=baseline_policy_missing,
        )
    except Exception as exc:
        _abort(_error(exc), command="doc.tree", json_output=json_output)
    _emit_lint(result, command="doc.tree", json_output=json_output)


@doc_app.command("index")
def doc_index_command(
    project: Annotated[Path, typer.Option("--project", "-C")] = Path("."),
    policy_file: Annotated[
        Path | None,
        typer.Option("--policy-file", help="Standalone DocumentLayoutPolicy YAML."),
    ] = None,
    output_file: Annotated[
        Path | None,
        typer.Option("--output-file", help="Write the deterministic Markdown index here."),
    ] = None,
) -> None:
    """Render the configured type/classification/directory index."""

    try:
        _repository, policy = _repository_and_policy(project, policy_file)
        _write_or_echo(render_document_index(policy), output_file)
    except Exception as exc:
        _abort(_error(exc), command="doc.index", json_output=False)


@doc_app.command("policy-template")
def doc_policy_template_command(
    agent_format: Annotated[
        Literal["claude", "agents"],
        typer.Option(
            "--agent-format",
            help="Project instruction target included in the example policy.",
        ),
    ] = "claude",
    output_file: Annotated[
        Path | None,
        typer.Option("--output-file", help="Write the standalone policy candidate here."),
    ] = None,
) -> None:
    """Render a structural policy candidate with required rationale placeholders."""

    try:
        _write_or_echo(
            render_standalone_document_policy_template(agent_format),
            output_file,
        )
    except Exception as exc:
        _abort(_error(exc), command="doc.policy-template", json_output=False)


@doc_app.command("policy-lint")
def doc_policy_lint_command(
    policy_file: Annotated[
        Path,
        typer.Argument(help="Standalone DocumentLayoutPolicy YAML candidate."),
    ],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Validate a document policy without requiring a repository or research init."""

    command = "doc.policy-lint"
    try:
        if policy_file.is_symlink() or not policy_file.is_file():
            raise RCPError(
                code="document_policy_invalid",
                message="Document policy must be an existing non-symlink regular file.",
                context={"path": str(policy_file)},
            )
        policy = load_model(policy_file, DocumentLayoutPolicy)
        require_adopted_document_policy(policy)
        data = {
            "path": str(policy_file),
            "terminal_result": "passed",
            "routes": len(policy.routes),
            "agent_guides": len(policy.agent_guides),
            "classification_depth": {
                "minimum": policy.classification_depth.minimum,
                "maximum": policy.classification_depth.maximum,
            },
            "max_depth": policy.max_depth,
        }
    except Exception as exc:
        _abort(
            _error(exc, source_path=policy_file),
            command=command,
            json_output=json_output,
        )
    if json_output:
        typer.echo(dump_envelope(envelope(command=command, success=True, data=data)))
    else:
        typer.echo("Outcome: passed")
        typer.echo(f"Policy: {data['path']}")
        typer.echo(f"Routes: {data['routes']}")
        typer.echo(f"Agent guides: {data['agent_guides']}")
        depth = data["classification_depth"]
        if not isinstance(depth, dict):
            raise AssertionError("classification depth output must be a mapping")
        typer.echo(f"Classification depth: {depth['minimum']}..{depth['maximum']}")
        typer.echo(f"Filesystem max depth: {data['max_depth']}")


@doc_app.command("agent-guide")
def doc_agent_guide_command(
    project: Annotated[Path, typer.Option("--project", "-C")] = Path("."),
    policy_file: Annotated[
        Path | None,
        typer.Option("--policy-file", help="Standalone DocumentLayoutPolicy YAML."),
    ] = None,
    guide_format: Annotated[
        Literal["claude", "agents"] | None,
        typer.Option("--format", help="Guide target format; inferred when writing."),
    ] = None,
    output_file: Annotated[
        Path | None,
        typer.Option(
            "--output-file",
            help="Insert or update the managed block in a configured guide target.",
        ),
    ] = None,
) -> None:
    """Render project-local instructions that teach Agents the document workflow."""

    try:
        repository, policy = _repository_and_policy(project, policy_file)
        if output_file is None:
            selected_format: AgentGuideFormat
            if guide_format is not None:
                selected_format = guide_format
            else:
                configured_formats = {target.format for target in policy.agent_guides}
                if len(configured_formats) > 1:
                    raise RCPError(
                        code="agent_guide_format_required",
                        message="Policy declares multiple Agent guide formats.",
                        remediation="Select one with --format.",
                    )
                selected_format = next(iter(configured_formats), "claude")
            typer.echo(
                render_project_agent_guide(policy, selected_format).decode("utf-8"),
                nl=False,
            )
            return
        destination, selected_format = _agent_guide_destination(
            repository,
            output_file,
            policy,
            guide_format,
        )
        _upsert_agent_guide(
            repository,
            destination,
            render_project_agent_guide(policy, selected_format),
            selected_format,
        )
    except Exception as exc:
        _abort(_error(exc), command="doc.agent-guide", json_output=False)


@doc_app.command("configure-layout")
def doc_configure_layout_command(
    policy_file: Annotated[
        Path,
        typer.Option("--policy-file", help="Complete DocumentLayoutPolicy YAML."),
    ],
    expected_default_head: Annotated[
        str,
        typer.Option("--expected-default-head", help="Exact protected-base commit."),
    ],
    project: Annotated[Path, typer.Option("--project", "-C")] = Path("."),
    operation_id: Annotated[str | None, typer.Option("--operation-id")] = None,
    idempotency_key: Annotated[str | None, typer.Option("--idempotency-key")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Prepare a manager-owned document classification/layout policy proposal."""

    command = "doc.configure-layout"
    selected_operation = operation_id or new_id("operation")
    try:
        request = DocumentLayoutConfigureRequest(
            operation_id=selected_operation,
            idempotency_key=idempotency_key or f"human:{selected_operation}",
            expected_default_head=expected_default_head,
            document_layout=load_model(policy_file, DocumentLayoutPolicy),
        )
        from researchctl.services.factory import open_application

        with open_application(
            project,
            document_layout_operation_id=request.operation_id,
            document_layout_expected_default_head=request.expected_default_head,
        ) as handle:
            result = handle.service.document_layout_configure(request, handle.actor)
        data = result.as_dict()
    except Exception as exc:
        _abort(_error(exc), command=command, json_output=json_output)
    if json_output:
        typer.echo(dump_envelope(envelope(command=command, success=True, data=data)))
    else:
        typer.echo(f"Operation: {data['operation_id']}")
        typer.echo(f"Outcome: {data['terminal_result']}")
        proposal = data.get("proposal")
        if isinstance(proposal, dict):
            typer.echo(f"Branch: {proposal.get('branch')}")
            typer.echo(f"Commit: {proposal.get('commit')}")
