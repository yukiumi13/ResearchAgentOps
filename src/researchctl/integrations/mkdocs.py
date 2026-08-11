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

from researchctl.domain.models import DocumentSiteManifest, DocumentSitePage
from researchctl.errors import RCPError
from researchctl.repository import safe_repository_path
from researchctl.services.generated_markdown import inspect_project_frontmatter


class ResearchctlPlugin(BasePlugin):
    """Project a validated researchctl manifest into MkDocs navigation and pages."""

    config_scheme = (
        ("manifest", config_options.Type(str, required=True)),
        ("require_clean", config_options.Type(bool, default=True)),
    )

    def __init__(self) -> None:
        super().__init__()
        self._manifest: DocumentSiteManifest | None = None
        self._project_root: Path | None = None
        self._pages_by_uri: dict[str, DocumentSitePage] = {}
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
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest = DocumentSiteManifest.model_validate(payload)
        except (OSError, UnicodeError, json.JSONDecodeError, ValidationError) as error:
            raise ConfigurationError(
                f"researchctl manifest is invalid: {type(error).__name__}: {error}"
            ) from error
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

        pages_by_uri: dict[str, DocumentSitePage] = {}
        for page in manifest.pages:
            uri = self._document_uri(page.path, manifest.document_root)
            try:
                page_path = safe_repository_path(self._project_root, page.path)
            except RCPError as error:
                raise ConfigurationError(
                    f"researchctl site page path is unsafe: {page.path}"
                ) from error
            if page_path.is_symlink() or not page_path.is_file():
                raise ConfigurationError(
                    f"researchctl site page is missing or unsafe: {page.path}"
                )
            observed = "sha256:" + hashlib.sha256(page_path.read_bytes()).hexdigest()
            if observed != page.content_digest:
                raise ConfigurationError(
                    "researchctl site page changed after manifest validation: "
                    f"{page.path}"
                )
            if page.source_path is not None:
                assert page.source_digest is not None
                try:
                    source_path = safe_repository_path(
                        self._project_root,
                        page.source_path,
                    )
                except RCPError as error:
                    raise ConfigurationError(
                        f"researchctl site source path is unsafe: {page.source_path}"
                    ) from error
                if source_path.is_symlink() or not source_path.is_file():
                    raise ConfigurationError(
                        "researchctl structured source is missing or unsafe: "
                        f"{page.source_path}"
                    )
                source_digest = (
                    "sha256:" + hashlib.sha256(source_path.read_bytes()).hexdigest()
                )
                if source_digest != page.source_digest:
                    raise ConfigurationError(
                        "researchctl structured source changed after manifest validation: "
                        f"{page.source_path}"
                    )
            pages_by_uri[uri] = page

        self._manifest = manifest
        self._pages_by_uri = pages_by_uri
        self._excluded_uris = {
            self._document_uri(item.path, manifest.document_root)
            for item in manifest.excluded_paths
        }
        config["nav"] = self._navigation(manifest)
        return config

    def on_files(self, files, *, config):  # type: ignore[no-untyped-def]
        del config
        retained = []
        for file in files:
            uri = PurePosixPath(file.src_uri).as_posix()
            if uri in self._excluded_uris:
                continue
            if (
                PurePosixPath(uri).suffix.lower() in {".md", ".markdown"}
                and uri not in self._pages_by_uri
            ):
                raise ConfigurationError(
                    "MkDocs discovered Markdown absent from the validated "
                    f"researchctl manifest: {uri}"
                )
            retained.append(file)
        return Files(retained)

    def on_page_markdown(self, markdown, *, page, config, files):  # type: ignore[no-untyped-def]
        del config, files
        uri = PurePosixPath(page.file.src_uri).as_posix()
        document = self._pages_by_uri.get(uri)
        if document is None:
            return markdown
        if document.kind in {"manual", "structured"}:
            envelope = inspect_project_frontmatter(markdown.encode("utf-8"))
            if envelope is not None:
                markdown = envelope.body.decode("utf-8")
        metadata = self._metadata(document)
        return metadata + markdown

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
