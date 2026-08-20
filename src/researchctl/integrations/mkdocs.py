from __future__ import annotations

import hashlib
import html
import json
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlsplit

from mkdocs.config import config_options
from mkdocs.exceptions import ConfigurationError
from mkdocs.plugins import BasePlugin
from mkdocs.structure.files import Files
from pydantic import ValidationError

from researchctl.domain.models import (
    DocumentSiteManifest,
    DocumentSitePage,
    SimpleDocumentSiteAsset,
    SimpleDocumentSiteManifest,
    SimpleDocumentSitePage,
)
from researchctl.errors import RCPError
from researchctl.repository import safe_repository_path
from researchctl.serialization import SerializationError
from researchctl.services.generated_markdown import inspect_project_frontmatter

#: A manifest says which kind it is. Nothing here guesses from the fields it
#: carries, because two kinds can overlap in shape and the wrong model would
#: then silently accept the wrong document tree.
_MANIFEST_MODELS: dict[str, type[DocumentSiteManifest] | type[SimpleDocumentSiteManifest]] = {
    "document_site_manifest": DocumentSiteManifest,
    "simple_document_site_manifest": SimpleDocumentSiteManifest,
}
_MARKDOWN_SUFFIXES = {".md", ".markdown"}

SiteManifest = DocumentSiteManifest | SimpleDocumentSiteManifest
SitePage = DocumentSitePage | SimpleDocumentSitePage


class ResearchctlPlugin(BasePlugin):
    """Project a validated researchctl manifest into MkDocs navigation and pages."""

    config_scheme = (
        ("manifest", config_options.Type(str, required=True)),
        ("require_clean", config_options.Type(bool, default=True)),
    )

    def __init__(self) -> None:
        super().__init__()
        self._manifest: SiteManifest | None = None
        self._project_root: Path | None = None
        self._docs_dir: Path | None = None
        self._pages_by_uri: dict[str, SitePage] = {}
        self._assets_by_uri: dict[str, SimpleDocumentSiteAsset] = {}
        self._excluded_uris: set[str] = set()

    def on_config(self, config):  # type: ignore[no-untyped-def]
        config_file = config.get("config_file_path")
        if not config_file:
            raise ConfigurationError(
                "researchctl requires MkDocs to load a project configuration file"
            )
        self._project_root = Path(config_file).resolve().parent
        manifest_path = Path(self.config["manifest"])
        if not manifest_path.is_absolute():
            manifest_path = self._project_root / manifest_path
        manifest_path = manifest_path.absolute()
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise ConfigurationError(
                "researchctl manifest must be an existing non-symlink regular file"
            )
        manifest = self._load_manifest(manifest_path)
        if self.config["require_clean"] and manifest.repository_state != "clean":
            raise ConfigurationError(
                "researchctl manifest records a dirty repository; publication requires clean"
            )

        docs_dir = Path(config["docs_dir"]).resolve()
        expected_docs_dir = (self._project_root / manifest.document_root).resolve()
        if docs_dir != expected_docs_dir:
            raise ConfigurationError(
                "MkDocs docs_dir differs from the researchctl manifest document_root: "
                f"expected {expected_docs_dir}, observed {docs_dir}"
            )

        pages_by_uri: dict[str, SitePage] = {}
        for page in manifest.pages:
            uri = self._document_uri(page.path, manifest.document_root)
            self._verify_published(
                page.path,
                page.content_digest,
                path_label="site page path",
                subject="site page",
            )
            if page.source_path is not None:
                assert page.source_digest is not None
                self._verify_published(
                    page.source_path,
                    page.source_digest,
                    path_label="site source path",
                    subject="structured source",
                )
            pages_by_uri[uri] = page

        assets_by_uri: dict[str, SimpleDocumentSiteAsset] = {}
        if isinstance(manifest, SimpleDocumentSiteManifest):
            # A static asset is published byte for byte, so it is trusted the
            # same way a page is: only the bytes the manifest digested ship.
            for asset in manifest.assets:
                self._verify_published(
                    asset.path,
                    asset.content_digest,
                    path_label="static asset path",
                    subject="static asset",
                )
                assets_by_uri[self._document_uri(asset.path, manifest.document_root)] = asset

        excluded_uris = {
            self._document_uri(item.path, manifest.document_root)
            for item in manifest.excluded_paths
        }
        self._reject_uri_role_collisions(pages_by_uri, assets_by_uri, excluded_uris)

        self._manifest = manifest
        self._docs_dir = docs_dir
        self._pages_by_uri = pages_by_uri
        self._assets_by_uri = assets_by_uri
        self._excluded_uris = excluded_uris
        config["nav"] = (
            self._simple_navigation(manifest)
            if isinstance(manifest, SimpleDocumentSiteManifest)
            else self._navigation(manifest)
        )
        return config

    def on_files(self, files, *, config):  # type: ignore[no-untyped-def]
        del config
        retained = []
        for file in files:
            uri = PurePosixPath(file.src_uri).as_posix()
            if not self._from_documentation_tree(file):
                # Theme files share this collection. They are the theme's
                # business, not the manifest's, and the manifest never lists
                # them, so the closed-world rule must not see them at all.
                retained.append(file)
                continue
            if uri in self._excluded_uris:
                continue
            if uri in self._pages_by_uri or uri in self._assets_by_uri:
                retained.append(file)
                continue
            is_markdown = PurePosixPath(uri).suffix.lower() in _MARKDOWN_SUFFIXES
            if not is_markdown and not self._enumerates_assets:
                # A classification-route manifest has no asset list, so the
                # absence of a static file from it is not a verdict about that
                # file. Publishing it is the behaviour v1 sites already rely on.
                retained.append(file)
                continue
            # Closed world: this manifest enumerates everything publishable, so
            # a file it never saw is not published quietly.
            subject = "Markdown" if is_markdown else "a file"
            raise ConfigurationError(
                f"MkDocs discovered {subject} absent from the validated "
                f"researchctl manifest: {uri}"
            )
        return Files(retained)

    def on_page_markdown(self, markdown, *, page, config, files):  # type: ignore[no-untyped-def]
        del config, files
        uri = PurePosixPath(page.file.src_uri).as_posix()
        document = self._pages_by_uri.get(uri)
        if document is None:
            return markdown
        if isinstance(document, SimpleDocumentSitePage):
            # Frontmatter is how a version 2 document records its own facts. The
            # manifest already carries them, so the envelope is source metadata
            # and never prose to publish.
            return self._simple_metadata(document) + self._without_frontmatter(
                markdown,
                uri=uri,
            )
        if document.kind in {"manual", "structured"}:
            markdown = self._without_frontmatter(markdown, uri=uri)
        metadata = self._metadata(document)
        return metadata + markdown

    def _load_manifest(self, manifest_path: Path) -> SiteManifest:
        """Validate the manifest with the model its own declaration names."""

        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ConfigurationError(
                f"researchctl manifest is invalid: {type(error).__name__}: {error}"
            ) from error
        kind = payload.get("manifest_kind") if isinstance(payload, dict) else None
        model = _MANIFEST_MODELS.get(kind) if isinstance(kind, str) else None
        if model is None:
            raise ConfigurationError(
                f"researchctl manifest declares no supported manifest_kind: {kind!r}"
            )
        try:
            return model.model_validate(payload)
        except ValidationError as error:
            raise ConfigurationError(
                f"researchctl manifest is invalid: {type(error).__name__}: {error}"
            ) from error

    def _verify_published(
        self,
        relative: str,
        expected_digest: str,
        *,
        path_label: str,
        subject: str,
    ) -> None:
        """Re-read one published file and refuse bytes the manifest never saw."""

        assert self._project_root is not None
        try:
            location = safe_repository_path(self._project_root, relative)
        except RCPError as error:
            raise ConfigurationError(
                f"researchctl {path_label} is unsafe: {relative}"
            ) from error
        if location.is_symlink() or not location.is_file():
            raise ConfigurationError(
                f"researchctl {subject} is missing or unsafe: {relative}"
            )
        try:
            content = location.read_bytes()
        except OSError as error:
            # The path passed every check and still would not open. MkDocs must
            # hear that as a configuration failure naming the file, not as a
            # bare OSError from somewhere inside the plugin.
            raise ConfigurationError(
                f"researchctl {subject} could not be read: {relative} "
                f"({type(error).__name__})"
            ) from error
        observed = "sha256:" + hashlib.sha256(content).hexdigest()
        if observed != expected_digest:
            raise ConfigurationError(
                f"researchctl {subject} changed after manifest validation: {relative}"
            )

    @staticmethod
    def _reject_uri_role_collisions(
        pages: dict[str, SitePage],
        assets: dict[str, SimpleDocumentSiteAsset],
        excluded: set[str],
    ) -> None:
        """One URI, one role.

        The manifest models already keep these three sets disjoint. This is a
        defence against a manifest validated by some other version of them, not
        a second opinion about which role a path should hold.
        """

        for left, right, roles in (
            (set(pages), set(assets), "a page and an asset"),
            (set(pages), excluded, "a page and an excluded path"),
            (set(assets), excluded, "an asset and an excluded path"),
        ):
            shared = sorted(left & right)
            if shared:
                raise ConfigurationError(
                    f"researchctl manifest lists {shared[0]} as both {roles}"
                )

    @property
    def _enumerates_assets(self) -> bool:
        """True when the manifest lists static assets as well as pages.

        Only a directory-first manifest does. That is what makes its silence
        about a file mean "not publishable" rather than "not described".
        """

        return isinstance(self._manifest, SimpleDocumentSiteManifest)

    def _from_documentation_tree(self, file) -> bool:  # type: ignore[no-untyped-def]
        """True when MkDocs discovered this file inside the documented tree."""

        src_dir = getattr(file, "src_dir", None)
        if src_dir is None or self._docs_dir is None:
            return True
        return Path(src_dir).resolve() == self._docs_dir

    @staticmethod
    def _document_uri(path: str, document_root: str) -> str:
        try:
            return PurePosixPath(path).relative_to(PurePosixPath(document_root)).as_posix()
        except ValueError as error:
            raise ConfigurationError(
                f"researchctl manifest path is outside document_root: {path}"
            ) from error

    def _navigation(self, manifest: DocumentSiteManifest) -> list[object]:
        overview: list[dict[str, str]] = []
        current: dict[tuple[int, str], list[dict[str, str]]] = {}
        archive: list[dict[str, str]] = []
        legacy: list[dict[str, str]] = []
        for page in manifest.pages:
            item = {page.title: self._document_uri(page.path, manifest.document_root)}
            if page.kind == "root":
                overview.append(item)
            elif page.history_kind == "archive":
                archive.append(item)
            elif page.history_kind == "legacy":
                legacy.append(item)
            else:
                assert page.route_order is not None
                assert page.document_type is not None
                current.setdefault((page.route_order, page.document_type), []).append(item)

        navigation: list[object] = []
        if overview:
            navigation.append({"Overview": overview})
        for (_order, document_type), items in sorted(current.items()):
            label = document_type.replace("-", " ").title()
            navigation.append({label: items})
        history: list[dict[str, list[dict[str, str]]]] = []
        if archive:
            history.append({"Archive": archive})
        if legacy:
            history.append({"Legacy": legacy})
        if history:
            navigation.append({"History": history})
        return navigation

    def _simple_navigation(self, manifest: SimpleDocumentSiteManifest) -> list[object]:
        """Project a version 2 manifest into navigation.

        Everything here is read from the section and the section-relative path;
        the manifest carries no display labels and no second tree to consult.
        """

        overview: list[dict[str, str]] = []
        retired_roots: list[dict[str, str]] = []
        current: dict[str, list[SimpleDocumentSitePage]] = {}
        retired: dict[str, list[SimpleDocumentSitePage]] = {}
        for page in manifest.pages:
            if page.kind == "root":
                item = {page.title: self._document_uri(page.path, manifest.document_root)}
                (retired_roots if page.in_history else overview).append(item)
                continue
            assert page.section is not None
            bucket = retired if page.in_history else current
            bucket.setdefault(page.section, []).append(page)

        navigation: list[object] = []
        if overview:
            navigation.append({"Overview": overview})
        navigation.extend(self._section_groups(manifest, current))
        # A retired page keeps the section and directory it was filed under; it
        # only moves beneath one History heading.
        history: list[object] = [*retired_roots, *self._section_groups(manifest, retired)]
        if history:
            navigation.append({"History": history})
        return navigation

    def _section_groups(
        self,
        manifest: SimpleDocumentSiteManifest,
        pages_by_section: dict[str, list[SimpleDocumentSitePage]],
    ) -> list[dict[str, list[object]]]:
        groups: list[dict[str, list[object]]] = []
        for section in manifest.sections:
            pages = pages_by_section.get(section.path)
            if not pages:
                # A section nobody has written in yet is not a heading.
                continue
            groups.append({self._humanize(section.path): self._nested_items(manifest, pages)})
        return groups

    def _nested_items(
        self,
        manifest: SimpleDocumentSiteManifest,
        pages: list[SimpleDocumentSitePage],
        depth: int = 0,
    ) -> list[object]:
        """Rebuild the directory tree the section-relative paths already describe."""

        direct: list[object] = []
        nested: dict[str, list[SimpleDocumentSitePage]] = {}
        for page in pages:
            assert page.section_relative_path is not None
            remainder = PurePosixPath(page.section_relative_path).parts[depth:]
            if len(remainder) == 1:
                direct.append(
                    {page.title: self._document_uri(page.path, manifest.document_root)}
                )
            else:
                nested.setdefault(remainder[0], []).append(page)
        # Pages at this level first, in manifest order, then the directories
        # below it in the order the manifest first mentions each one.
        items: list[object] = list(direct)
        for directory, children in nested.items():
            items.append(
                {self._humanize(directory): self._nested_items(manifest, children, depth + 1)}
            )
        return items

    @staticmethod
    def _humanize(segment: str) -> str:
        """Turn one path segment into a heading.

        Display labels are the adapter's business. The manifest stores paths so
        that a different site engine can label them differently.
        """

        return segment.replace("-", " ").title()

    @staticmethod
    def _without_frontmatter(markdown: str, *, uri: str) -> str:
        try:
            envelope = inspect_project_frontmatter(markdown.encode("utf-8"))
        except (UnicodeError, SerializationError) as error:
            raise ConfigurationError(
                f"researchctl page frontmatter is invalid: {uri} "
                f"({type(error).__name__}: {error})"
            ) from error
        if envelope is None:
            return markdown
        return envelope.body.decode("utf-8")

    def _simple_metadata(self, page: SimpleDocumentSitePage) -> str:
        """State what the manifest knows about one version 2 page, and no more.

        Every line here is a fact some source of truth already settled:
        CODEOWNERS decided the owners, the document decided its own review date
        and status, and Git decided when it was last edited. Where a fact does
        not exist, the block says so rather than leaving a reader to guess
        whether it is absent or merely unrendered.
        """

        values: list[str] = [
            "Owned by " + (self._code_spans(page.owners) or "Unassigned"),
            (
                f"Reviewed `{page.reviewed_on.isoformat()}`"
                if page.reviewed_on is not None
                else "Reviewed Not recorded"
            ),
            (
                f"Edited `{page.last_edited_at.date().isoformat()}`"
                if page.last_edited_at is not None
                else "Edited Not recorded in Git"
            ),
            "Tags " + (self._code_spans(page.tags) or "None"),
        ]
        # An ordinary page states a status and a structured one states a
        # lifecycle, but only if its contract has one. A contract that states
        # neither gets neither invented for it.
        if page.status is not None:
            values.append(f"Status `{html.escape(page.status)}`")
        elif page.lifecycle is not None:
            values.append(f"Lifecycle `{html.escape(page.lifecycle)}`")
        values.append("Locked Yes" if page.locked else "Locked No")
        if page.source_path is not None:
            label = html.escape(page.source_path)
            source_url = self._source_url(page.source_path)
            values.append(
                f"Source [`{label}`]({source_url})" if source_url else f"Source `{label}`"
            )
        return (
            "<!-- researchctl-site-metadata:simple-document-site-manifest.v1 -->\n"
            + "> Document metadata: "
            + " | ".join(values)
            + "\n\n"
        )

    @staticmethod
    def _code_spans(values: tuple[str, ...]) -> str:
        return ", ".join(f"`{html.escape(value)}`" for value in values)

    def _metadata(self, page: DocumentSitePage) -> str:
        values: list[str] = []
        if page.document_type is not None:
            values.append(f"Type `{html.escape(page.document_type)}`")
        if page.classification is not None:
            values.append(f"Classification `{html.escape(page.classification)}`")
        if page.validity is not None:
            values.append(f"Validity `{html.escape(page.validity)}`")
        if page.lifecycle is not None:
            values.append(f"Lifecycle `{html.escape(page.lifecycle)}`")
        if page.source_path is not None:
            label = html.escape(page.source_path)
            source_url = self._source_url(page.source_path)
            values.append(
                f"Source [`{label}`]({source_url})" if source_url else f"Source `{label}`"
            )
        if not values:
            values.append(f"Source `{html.escape(page.path)}`")
        return (
            "<!-- researchctl-site-metadata:document-site-manifest.v1 -->\n"
            + "> Document metadata: "
            + " | ".join(values)
            + "\n\n"
        )

    def _source_url(self, source_path: str) -> str | None:
        if self._manifest is None or self._manifest.repository_head is None:
            return None
        remote = self._manifest.repository_remote
        if remote is None:
            return None
        host: str | None = None
        repository_path: str | None = None
        if "://" in remote:
            parsed = urlsplit(remote)
            host = parsed.hostname
            repository_path = parsed.path
        elif ":" in remote:
            host, repository_path = remote.split(":", maxsplit=1)
        if host not in {"github.com", "gitlab.com"} or repository_path is None:
            return None
        repository_path = repository_path.strip("/").removesuffix(".git")
        if not repository_path:
            return None
        head = quote(self._manifest.repository_head, safe="")
        path = quote(source_path, safe="/")
        return f"https://{host}/{repository_path}/blob/{head}/{path}"
