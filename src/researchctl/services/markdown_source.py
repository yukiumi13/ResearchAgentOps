"""Read Markdown structure with a CommonMark parser instead of pattern matching.

Titles and links are structural facts about a Markdown document. Deriving them
with regular expressions silently mishandles fenced code, indented code, HTML
blocks, link references, and escapes, so the simple document contract reads them
through ``markdown-it-py`` and never through ad hoc patterns.
"""

from __future__ import annotations

from markdown_it import MarkdownIt
from markdown_it.token import Token

_PARSER = MarkdownIt("commonmark")


def parse_markdown(text: str) -> list[Token]:
    return _PARSER.parse(text)


def _inline_text(token: Token) -> str:
    if token.children is None:
        return token.content
    parts: list[str] = []
    for child in token.children:
        if child.type in {"text", "code_inline"}:
            parts.append(child.content)
        elif child.type in {"softbreak", "hardbreak"}:
            parts.append(" ")
    return "".join(parts).strip()


def first_heading_title(text: str) -> str | None:
    """Return the text of the first level-one ATX/Setext heading, if any."""

    tokens = parse_markdown(text)
    for index, token in enumerate(tokens):
        if token.type != "heading_open" or token.tag != "h1":
            continue
        if index + 1 < len(tokens) and tokens[index + 1].type == "inline":
            title = _inline_text(tokens[index + 1])
            if title:
                return title
    return None


def link_destinations(text: str) -> tuple[str, ...]:
    """Return every link and image destination in document order, deduplicated."""

    destinations: list[str] = []
    seen: set[str] = set()

    def record(value: str | None) -> None:
        if value is None:
            return
        candidate = value.strip()
        if not candidate or candidate in seen:
            return
        seen.add(candidate)
        destinations.append(candidate)

    def walk(tokens: list[Token]) -> None:
        for token in tokens:
            if token.type == "link_open":
                record(token.attrGet("href"))
            elif token.type == "image":
                record(token.attrGet("src"))
            if token.children:
                walk(token.children)

    walk(parse_markdown(text))
    return tuple(destinations)


def html_block_texts(text: str) -> tuple[str, ...]:
    """Return the source of every block-level raw HTML token, in order.

    Only real HTML blocks are reported. A comment quoted inside a fenced or
    indented code sample is code, and a comment written mid-paragraph is inline
    prose; neither is a block the renderers would ever emit.
    """

    return tuple(
        token.content
        for token in parse_markdown(text)
        if token.type == "html_block"
    )


def blockquote_texts(text: str) -> tuple[str, ...]:
    """Return the flattened text of every outermost block quote, in order."""

    quotes: list[str] = []
    parts: list[str] = []
    depth = 0
    for token in parse_markdown(text):
        if token.type == "blockquote_open":
            depth += 1
        elif token.type == "blockquote_close":
            depth -= 1
            if depth == 0:
                quotes.append(" ".join(part for part in parts if part))
                parts = []
        elif depth and token.type == "inline":
            parts.append(_inline_text(token))
    return tuple(quotes)
