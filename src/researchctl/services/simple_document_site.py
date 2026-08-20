"""Project a passing version 2 document tree into an engine-neutral manifest.

The manifest restates nothing it decides for itself. The policy already fixed
the taxonomy, CODEOWNERS already fixed review, Git already fixed edit time, and
:func:`lint_simple_document_tree` already decided which files are publishable.
This module reads those answers, digests the bytes it saw, and records the
result; it never resolves a second opinion.

Because it is a projection of a tree that must be valid, it is built only from
a passing lint and re-checked against the tree and the exact bytes before it is
returned. A tree that changes underneath generation fails closed rather than
publishing a manifest describing files that no longer exist.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

from researchctl.domain.models import (
    SimpleDocumentLayoutPolicy,
    SimpleDocumentSiteAsset,
    SimpleDocumentSiteExcludedPath,
    SimpleDocumentSiteManifest,
    SimpleDocumentSitePage,
    SimpleDocumentSiteSection,
)
from researchctl.errors import RCPError, UnsafeRepositoryPathError
from researchctl.repository import (
    GitRepository,
    current_head,
    discover_repository,
    last_commit_timestamp,
    safe_repository_path,
    status_porcelain,
)
from researchctl.serialization import canonical_digest
from researchctl.services.document_ownership import resolve_ownership
from researchctl.services.markdown_source import link_destinations
from researchctl.services.project_documents_v2 import (
    SimpleDocumentTreeLintResult,
    lint_simple_document_tree,
)
from researchctl.services.simple_markdown_v2 import resolve_repository_link

#: Ordinary statuses and structured lifecycles that retire a page to History.
_RETIRED_STATUSES = frozenset({"deprecated", "archived"})
_RETIRED_LIFECYCLES = frozenset({"superseded", "deprecated"})

#: The projection fields a second lint must reproduce exactly.
_PROJECTION_FIELDS = (
    "root",
    "policy_version",
    "checked_files",
    "documents",
    "structured_documents",
    "assets",
    "codeowners_path",
    "findings",
    "document_facts",
    "structured_facts",
    "asset_paths",
)


def _digest(content: bytes) -> str:
    """Digest exactly the bytes this manifest observed."""

    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _tree_invalid(lint: SimpleDocumentTreeLintResult) -> RCPError:
    return RCPError(
        code="document_site_tree_invalid",
        message="Document site manifest requires a valid version 2 document tree.",
        remediation=(
            "Run `researchctl doc tree --project PROJECT --json` and fix every "
            "invalid finding."
        ),
        context=lint.as_dict(),
    )


def _tree_changed(message: str, context: dict[str, object]) -> RCPError:
    return RCPError(
        code="document_site_tree_changed",
        message=message,
        remediation="Rerun document tree lint and site manifest generation.",
        context=context,
    )


def _projection_facts(lint: SimpleDocumentTreeLintResult) -> dict[str, object]:
    """Summarise one lint compactly enough to put two of them in an error."""

    return {
        "terminal_result": lint.terminal_result,
        "checked_files": lint.checked_files,
        "documents": lint.documents,
        "structured_documents": lint.structured_documents,
        "assets": lint.assets,
        "codeowners_path": lint.codeowners_path,
        "invalid_findings": sum(
            1 for finding in lint.findings if finding.kind == "invalid"
        ),
    }


def _projection_changed(
    first: SimpleDocumentTreeLintResult,
    second: SimpleDocumentTreeLintResult,
) -> tuple[str, ...]:
    return tuple(
        name
        for name in _PROJECTION_FIELDS
        if getattr(first, name) != getattr(second, name)
    )


def _read(repository: Path, relative: str) -> bytes:
    """Read one file the lint already accepted, or declare the tree changed.

    Every byte in the manifest passes through here, so this is the one place
    that has to refuse a path that became a symlink, a directory, or an escape
    after the lint approved it. A read that cannot be trusted is a race, not a
    new kind of failure, and is reported as one.
    """

    try:
        location = safe_repository_path(repository, relative)
        if location.is_symlink() or not location.is_file():
            raise _tree_changed(
                "A publishable path is no longer a regular file.",
                {"path": relative},
            )
        return location.read_bytes()
    except RCPError as error:
        if error.code == "document_site_tree_changed":
            raise
        raise _tree_changed(
            "A publishable path can no longer be resolved safely.",
            {"path": relative, "error": error.code},
        ) from error
    except OSError as error:
        raise _tree_changed(
            "A publishable path could not be read.",
            {"path": relative, "error": type(error).__name__},
        ) from error


def _history(
    git_repository: GitRepository,
    relative: str,
) -> tuple[bool, datetime | None]:
    try:
        observed = last_commit_timestamp(git_repository, relative)
    except UnsafeRepositoryPathError as error:
        # The lint approved this path, so a path that no longer resolves safely
        # means the tree moved under the build. Every other Git failure is a
        # real observation error and keeps its own code: disguising it as a
        # tree race would hide a broken Git environment.
        raise _tree_changed(
            "A publishable path can no longer be resolved safely.",
            {"path": relative, "error": error.code},
        ) from error
    return observed.present, observed.last_edited_at


def _rendered_links(*, relative: str, content: bytes) -> tuple[str, ...]:
    """Resolve the repository links a generated page actually renders.

    A generated page is Markdown that readers navigate, so its links belong in
    the manifest exactly as an ordinary page's do. They are parsed from the
    token stream, never from patterns.

    Every safely resolved repository-relative target is kept whether or not it
    exists right now. Dropping a missing target would hide a broken link from
    whatever consumes the manifest; recording it lets the site build or a later
    tree rule diagnose it.
    """

    try:
        text = content.decode("utf-8")
    except UnicodeError as error:
        raise _tree_changed(
            "Generated Markdown is no longer valid UTF-8.",
            {"path": relative},
        ) from error
    resolved: list[str] = []
    for destination in link_destinations(text):
        target = resolve_repository_link(
            document_relative=relative,
            destination=destination,
        )
        if target is None or target.startswith("../") or target == "..":
            continue
        if target not in resolved:
            resolved.append(target)
    return tuple(resolved)


def build_simple_document_site_manifest(
    repository_root: Path,
    policy: SimpleDocumentLayoutPolicy,
) -> SimpleDocumentSiteManifest:
    """Project a fully valid version 2 document tree into a site manifest."""

    repository = Path(os.path.abspath(os.fspath(repository_root)))
    lint = lint_simple_document_tree(repository, policy)
    if not lint.passed:
        raise _tree_invalid(lint)

    git_repository = discover_repository(repository)
    # Every per-path history query is answered against one commit. Snapshotting
    # HEAD first, and requiring it back at the end, keeps the timestamps in the
    # manifest from mixing two different states of the repository.
    head = current_head(git_repository)
    ownership = resolve_ownership(repository, policy)
    root_prefix = f"{policy.root}/"
    section_order = {section.path: index for index, section in enumerate(policy.sections)}
    root_order = {path: index for index, path in enumerate(policy.root_page_paths())}

    pages: list[SimpleDocumentSitePage] = []
    order_keys: dict[str, tuple[int, int, int, str]] = {}

    for facts in lint.document_facts:
        content = _read(repository, facts.path)
        present, last_edited_at = _history(git_repository, facts.path)
        assert facts.title is not None, "a passing tree guarantees every page a title"
        in_history = facts.status in _RETIRED_STATUSES
        if facts.section is None:
            page = SimpleDocumentSitePage(
                path=facts.path,
                kind="root",
                title=facts.title,
                status=facts.status,
                tags=facts.tags,
                owners=facts.owners,
                reviewed_on=facts.reviewed_on,
                locked=facts.locked,
                depends_on=facts.depends_on,
                links=facts.links,
                git_history_present=present,
                last_edited_at=last_edited_at,
                content_digest=_digest(content),
                in_history=in_history,
            )
            rank = (0, root_order.get(facts.path, 0))
        else:
            page = SimpleDocumentSitePage(
                path=facts.path,
                kind="ordinary",
                section=facts.section,
                section_relative_path=facts.path.removeprefix(
                    f"{root_prefix}{facts.section}/"
                ),
                title=facts.title,
                status=facts.status,
                tags=facts.tags,
                owners=facts.owners,
                reviewed_on=facts.reviewed_on,
                locked=facts.locked,
                depends_on=facts.depends_on,
                links=facts.links,
                git_history_present=present,
                last_edited_at=last_edited_at,
                content_digest=_digest(content),
                in_history=in_history,
            )
            rank = (1, section_order[facts.section])
        pages.append(page)
        order_keys[page.path] = (1 if in_history else 0, *rank, page.path)

    excluded: list[SimpleDocumentSiteExcludedPath] = []
    for facts in lint.structured_facts:
        content = _read(repository, facts.render_path)
        source_content = _read(repository, facts.source_path)
        # The canonical source is what anyone edits, so its history is the
        # generated page's history. The render only ever changes with it.
        present, last_edited_at = _history(git_repository, facts.source_path)
        in_history = facts.lifecycle in _RETIRED_LIFECYCLES
        page = SimpleDocumentSitePage(
            path=facts.render_path,
            kind="structured",
            section=facts.section,
            section_relative_path=facts.render_path.removeprefix(
                f"{root_prefix}{facts.section}/"
            ),
            source_path=facts.source_path,
            contract=facts.contract,
            classification=facts.classification,
            title=facts.title,
            lifecycle=facts.lifecycle,
            tags=facts.tags,
            owners=facts.owners,
            links=_rendered_links(
                relative=facts.render_path,
                content=content,
            ),
            git_history_present=present,
            last_edited_at=last_edited_at,
            content_digest=_digest(content),
            source_digest=_digest(source_content),
            in_history=in_history,
        )
        pages.append(page)
        order_keys[page.path] = (
            1 if in_history else 0,
            1,
            section_order[facts.section],
            page.path,
        )
        excluded.append(
            SimpleDocumentSiteExcludedPath(
                path=facts.source_path,
                reason="structured_source",
                page_path=facts.render_path,
            )
        )

    excluded.extend(
        SimpleDocumentSiteExcludedPath(path=path, reason="codeowners")
        for path in sorted(ownership.excluded_paths)
    )

    assets: list[SimpleDocumentSiteAsset] = []
    for path in lint.asset_paths:
        section = policy.section_for_path(path)
        if section is None:
            raise AssertionError("a validated asset has no section")
        assets.append(
            SimpleDocumentSiteAsset(
                path=path,
                section=section.path,
                section_relative_path=path.removeprefix(f"{root_prefix}{section.path}/"),
                content_digest=_digest(_read(repository, path)),
            )
        )

    ordered_pages = tuple(sorted(pages, key=lambda page: order_keys[page.path]))
    ordered_assets = tuple(sorted(assets, key=lambda asset: asset.path))
    ordered_excluded = tuple(sorted(excluded, key=lambda item: item.path))

    final_lint = lint_simple_document_tree(repository, policy)
    differing = _projection_changed(lint, final_lint)
    if differing:
        # Comparing the whole projection, not just whether it still passes,
        # is what catches a change that stays valid: editing CODEOWNERS
        # reassigns owners without breaking a single rule.
        raise _tree_changed(
            "Document tree changed while the site manifest was being generated.",
            {
                "changed_fields": list(differing),
                "initial": _projection_facts(lint),
                "final": _projection_facts(final_lint),
            },
        )
    for page in ordered_pages:
        if _digest(_read(repository, page.path)) != page.content_digest:
            raise _tree_changed(
                "A publishable page changed during site manifest generation.",
                {"path": page.path},
            )
        if page.source_path is not None:
            observed = _digest(_read(repository, page.source_path))
            if observed != page.source_digest:
                raise _tree_changed(
                    "A structured source changed during site manifest generation.",
                    {"path": page.source_path},
                )
    for asset in ordered_assets:
        if _digest(_read(repository, asset.path)) != asset.content_digest:
            raise _tree_changed(
                "A published asset changed during site manifest generation.",
                {"path": asset.path},
            )

    final_head = current_head(git_repository)
    if final_head != head:
        # Every timestamp above was read against `head`. A commit landing mid
        # generation would leave the manifest describing two repositories.
        raise _tree_changed(
            "The repository moved while the site manifest was being generated.",
            {"initial_head": head, "final_head": final_head},
        )

    payload: dict[str, object] = {
        "schema_version": "0.1",
        "manifest_kind": "simple_document_site_manifest",
        "policy_version": policy.version,
        "document_root": policy.root,
        "repository_head": head,
        "repository_state": "dirty" if status_porcelain(git_repository) else "clean",
        "repository_remote": git_repository.remote_url,
        "policy_digest": canonical_digest(policy),
        "sections": [
            SimpleDocumentSiteSection(path=section.path).model_dump(mode="json")
            for section in policy.sections
        ],
        "pages": [page.model_dump(mode="json") for page in ordered_pages],
        "assets": [asset.model_dump(mode="json") for asset in ordered_assets],
        "excluded_paths": [item.model_dump(mode="json") for item in ordered_excluded],
    }
    payload["manifest_digest"] = canonical_digest(payload)
    return SimpleDocumentSiteManifest.model_validate(payload)


def render_simple_document_site_manifest(manifest: SimpleDocumentSiteManifest) -> bytes:
    """Render one manifest as deterministic, sorted-key JSON."""

    return (
        json.dumps(
            manifest.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
