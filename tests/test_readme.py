"""The README is PyPI's long description, and PyPI renders a relative link as a 404."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from markdown_it import MarkdownIt
from markdown_it.token import Token

README = Path(__file__).resolve().parent.parent / "README.md"


def _parse() -> list[Token]:
    return MarkdownIt("commonmark").enable("table").parse(README.read_text(encoding="utf-8"))


def _targets(tokens: list[Token]) -> Iterator[str]:
    for token in tokens:
        if token.type == "link_open":
            yield str(token.attrs["href"])
        elif token.type == "image":
            yield str(token.attrs["src"])
        if token.children:
            yield from _targets(token.children)


def test_readme_links_are_absolute_or_same_page_anchors() -> None:
    targets = list(_targets(_parse()))
    assert targets, "no links found: the parser no longer sees the README's links"
    assert [t for t in targets if not t.startswith(("https://", "http://", "#"))] == []


def test_readme_has_no_raw_html() -> None:
    """Links inside raw HTML are not link tokens, so the test above would not see them."""
    tokens = _parse()
    html = [t.content for t in tokens if t.type == "html_block"]
    html += [c.content for t in tokens if t.children for c in t.children if c.type == "html_inline"]
    assert html == []
