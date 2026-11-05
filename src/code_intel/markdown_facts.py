"""Markdown → facts with exact 1-based source lines, for the docs check.

Everything structural comes from markdown-it-py tokens (CommonMark plus GFM
tables, the configuration the drift measurement used); no regex reads Markdown
structure. markdown-it gives block tokens a line ``map`` but none to inline
children, so a child's line is found by a forward search with a cursor in its
block's source (see ``_child_lines``). HTML attributes come from the stdlib
``html.parser``.
"""

from __future__ import annotations

import re
import threading
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Literal
from urllib.parse import unquote

from markdown_it import MarkdownIt
from markdown_it.token import Token

from code_intel.code_paths import read_bytes_with_digest
from code_intel.doc_rules import STAGE
from code_intel.problems import Problem, diagnostic

Context = Literal["prose", "heading", "table", "code", "fence", "html"]
LinkKind = Literal["link", "image", "autolink", "html_href", "html_src"]

# `doc_refs` parses docs on threads, and two lazy setups in markdown-it-py race
# on first use. A parser compiles its rule chains on first parse, so each
# thread gets its own. mdurl, its URL helper, publishes each lookup list before
# filling it (an IndexError in CI), so one autolink fills both lists here.
_LOCAL = threading.local()


def _parser() -> MarkdownIt:
    parser = getattr(_LOCAL, "parser", None)
    if parser is None:
        parser = _LOCAL.parser = MarkdownIt("commonmark").enable("table")
    return parser


_parser().parse("<https://example.com>")


_HTML_LINK_ATTRS: dict[str, LinkKind] = {"href": "html_href", "src": "html_src"}
_HTML_ANCHOR_ATTRS = frozenset({"id", "name"})
_BREAKS = frozenset({"softbreak", "hardbreak"})


# ── Facts ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Heading:
    level: int
    text: str
    slug: str
    line: int


@dataclass(frozen=True)
class Link:
    """``href`` is the destination as markdown-it normalized it (percent-encoded,
    reference-style links already resolved); decode it before using it as a path."""

    href: str
    kind: LinkKind
    line: int
    context: Context


@dataclass(frozen=True)
class CodeSpan:
    text: str
    line: int
    context: Context


@dataclass(frozen=True)
class Fence:
    """A fenced (``indented=False``) or indented code block. ``line`` is the
    opening fence line, ``content_line`` the line of ``lines[0]``; for an
    indented block both are its first line."""

    info: str
    line: int
    content_line: int
    lines: tuple[str, ...]
    indented: bool = False


@dataclass(frozen=True)
class TextRun:
    """Adjacent plain-text children of one block on one line. Code spans, HTML,
    images and line breaks end a run; emphasis and link boundaries do not, so
    ``**GET** /api/x`` reads as one run."""

    text: str
    line: int
    context: Context


@dataclass(frozen=True)
class DocFacts:
    """``anchors`` holds every heading slug plus every HTML ``id``/``name``."""

    headings: tuple[Heading, ...]
    anchors: frozenset[str]
    links: tuple[Link, ...]
    code_spans: tuple[CodeSpan, ...]
    fences: tuple[Fence, ...]
    text_runs: tuple[TextRun, ...]


# ── GitHub slugs ─────────────────────────────────────────────────────────


def github_slug(text: str) -> str:
    """GitHub's heading id (github-slugger): lowercase, keep letters, marks,
    numbers, connector punctuation, ``-`` and spaces, then each space becomes
    ``-`` without collapsing, so ``Audit & Analytics`` → ``audit--analytics``."""
    kept = [
        ch
        for ch in text.lower()
        if ch in (" ", "-") or unicodedata.category(ch)[0] in "LMN" or unicodedata.category(ch) == "Pc"
    ]
    return "".join(kept).replace(" ", "-")


class Slugger:
    """Duplicate slugs as github-slugger numbers them: a taken slug tries
    ``slug-1``, ``slug-2``, … per original slug until one is free, so a literal
    ``Foo 1`` heading before a second ``Foo`` pushes it to ``foo-2``."""

    def __init__(self) -> None:
        self._occurrences: dict[str, int] = {}

    def slug(self, text: str) -> str:
        original = result = github_slug(text)
        while result in self._occurrences:
            self._occurrences[original] += 1
            result = f"{original}-{self._occurrences[original]}"
        self._occurrences[result] = 0
        return result


def match_anchor(fragment: str, anchors: Iterable[str]) -> bool:
    """Whether a link fragment (without ``#``), URL-decoded, is exactly one of
    ``anchors``. Case counts: how GitHub treats a fragment in another case is
    unverified, and a lenient reading could only hide a broken anchor. An
    empty fragment never matches."""
    wanted = unquote(fragment)
    return bool(wanted) and wanted in (anchors if isinstance(anchors, (set, frozenset)) else set(anchors))


# ── HTML attributes ──────────────────────────────────────────────────────


class _HtmlScan(HTMLParser):
    """Collects ``href``/``src`` and ``id``/``name`` attribute values with their
    1-based line within the fed snippet. Comments yield nothing."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, LinkKind, int]] = []
        self.anchors: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        line = self.getpos()[0]
        for name, value in attrs:
            if value is None:
                continue
            kind = _HTML_LINK_ATTRS.get(name)
            if kind is not None:
                self.links.append((value, kind, line))
            if name in _HTML_ANCHOR_ATTRS and value:
                self.anchors.append(value)


def _scan_html(snippet: str) -> _HtmlScan:
    scan = _HtmlScan()
    scan.feed(snippet)
    scan.close()
    return scan


# ── Inline line positions ────────────────────────────────────────────────


def _code_span_pattern(child: Token) -> re.Pattern[str]:
    """Locates an already-parsed code span in its block's source. markdown-it
    turns a newline inside a code span into a space and strips one padding
    space, so the content's words are matched with any whitespace between them."""
    fence = re.escape(child.markup or "`")
    words = r"\s+".join(re.escape(word) for word in child.content.split())
    return re.compile(rf"{fence}\s*{words}\s*{fence}")


def _child_lines(block: Token, children: Sequence[Token]) -> list[int]:
    """1-based line of each inline child.

    Line breaks would be enough if every newline in the source became a
    ``softbreak``/``hardbreak``, but a multi-line code span's newline turns into
    a space, which shifts every later child. So each child is located by a
    forward search from a cursor in the block's source: breaks find the next
    newline, text, HTML and code spans find themselves, and anything else
    (link and emphasis markers) sits at the cursor. A child whose source form
    differs from its content (an escape, an entity) is not found and leaves
    the cursor where it was; the next found child corrects it.
    """
    base = block.map[0] + 1 if block.map else 1
    source = block.content
    if "\n" not in source:
        return [base] * len(children)
    lines: list[int] = []
    cursor = 0
    last_pos = 0
    line = base
    for child in children:
        pos = cursor
        if child.type in _BREAKS:
            found = source.find("\n", cursor)
            if found >= 0:
                pos, cursor = found, found + 1
        elif child.type == "code_inline":
            match = _code_span_pattern(child).search(source, cursor)
            if match is not None:
                pos, cursor = match.start(), match.end()
        elif child.type in ("text", "html_inline") and child.content:
            found = source.find(child.content, cursor)
            if found >= 0:
                pos, cursor = found, found + len(child.content)
        line += source.count("\n", last_pos, pos)
        last_pos = pos
        lines.append(line)
    return lines


# ── Extraction ───────────────────────────────────────────────────────────


class _Collector:
    def __init__(self) -> None:
        self.headings: list[Heading] = []
        self.anchors: set[str] = set()
        self.links: list[Link] = []
        self.code_spans: list[CodeSpan] = []
        self.fences: list[Fence] = []
        self.text_runs: list[TextRun] = []
        self.slugger = Slugger()

    def html(self, snippet: str, first_line: int) -> None:
        scan = _scan_html(snippet)
        for href, kind, line in scan.links:
            self.links.append(Link(href, kind, first_line + line - 1, "html"))
        self.anchors.update(scan.anchors)

    def inline(self, block: Token, context: Context) -> None:
        children = block.children or []
        lines = _child_lines(block, children)
        run: list[str] = []
        run_line = 0

        def flush() -> None:
            if run and "".join(run).strip():
                self.text_runs.append(TextRun("".join(run), run_line, context))
            run.clear()

        for child, line in zip(children, lines, strict=True):
            kind = child.type
            if kind == "text":
                if run and line != run_line:
                    flush()
                if not run:
                    run_line = line
                run.append(child.content)
                continue
            if kind in ("link_open", "link_close", "strong_open", "strong_close", "em_open", "em_close"):
                if kind == "link_open":
                    link_kind: LinkKind = "autolink" if child.markup == "autolink" else "link"
                    self.links.append(Link(str(child.attrGet("href") or ""), link_kind, line, context))
                continue
            flush()
            if kind == "code_inline":
                self.code_spans.append(CodeSpan(child.content, line, context))
            elif kind == "image":
                self.links.append(Link(str(child.attrGet("src") or ""), "image", line, context))
            elif kind == "html_inline":
                self.html(child.content, line)
        flush()

    def heading(self, block: Token, level: int) -> None:
        line = block.map[0] + 1 if block.map else 1
        # GitHub slugs the heading's rendered text: text and code spans count,
        # emphasis markers and HTML tags drop out, and image alt text is not
        # part of it (scope docs have no image in a heading; unverified beyond).
        parts: list[str] = []
        for child in block.children or []:
            if child.type in ("text", "code_inline"):
                parts.append(child.content)
            elif child.type == "softbreak":
                parts.append("\n")
        text = "".join(parts)
        slug = self.slugger.slug(text)
        self.headings.append(Heading(level, text.strip(), slug, line))
        self.anchors.add(slug)

    def facts(self) -> DocFacts:
        return DocFacts(
            headings=tuple(self.headings),
            anchors=frozenset(self.anchors),
            links=tuple(self.links),
            code_spans=tuple(self.code_spans),
            fences=tuple(self.fences),
            text_runs=tuple(self.text_runs),
        )


def _content_lines(content: str) -> tuple[str, ...]:
    # markdown-it has already turned \r\n and \r into \n; splitlines() would
    # also split on form feeds and U+2028 and shift every later line.
    lines = content.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return tuple(lines)


def parse_markdown(text: str) -> DocFacts:
    """Facts of one Markdown document. Raises whatever the parser raises."""
    collector = _Collector()
    heading_level: int | None = None
    in_cell = False
    for token in _parser().parse(text):
        kind = token.type
        if kind == "heading_open":
            heading_level = int(token.tag[1:])
        elif kind == "heading_close":
            heading_level = None
        elif kind in ("th_open", "td_open"):
            in_cell = True
        elif kind in ("th_close", "td_close"):
            in_cell = False
        elif kind == "inline":
            if heading_level is not None:
                collector.heading(token, heading_level)
                collector.inline(token, "heading")
            elif in_cell:
                collector.inline(token, "table")
            else:
                collector.inline(token, "prose")
        elif kind in ("fence", "code_block") and token.map:
            fenced = kind == "fence"
            start = token.map[0] + 1
            collector.fences.append(
                Fence(
                    info=token.info.strip() if fenced else "",
                    line=start,
                    content_line=start + 1 if fenced else start,
                    lines=_content_lines(token.content),
                    indented=not fenced,
                )
            )
        elif kind == "html_block" and token.map:
            collector.html(token.content, token.map[0] + 1)
    return collector.facts()


def decode_markdown(rel: str, data: bytes, *, stage: str = STAGE) -> tuple[str | None, Problem | None]:
    """UTF-8 with a leading BOM dropped, or a ``decode_failed`` diagnostic that
    names the line of the first bad byte."""
    try:
        return data.decode("utf-8-sig"), None
    except UnicodeDecodeError as exc:
        line = data.count(b"\n", 0, exc.start) + 1
        return None, diagnostic("decode_failed", stage=stage, step="decode file", file=rel, line=line, exc=exc)


def doc_facts_from_text(rel: str, text: str, *, stage: str = STAGE) -> tuple[DocFacts | None, Problem | None]:
    """``parse_markdown`` with a parser failure reported as ``parse_failed``:
    RecursionError on deep nesting, html.parser's AssertionError on a malformed
    marked section, or a parser bug is reported for this document instead of
    stopping the whole check."""
    try:
        return parse_markdown(text), None
    except Exception as exc:  # noqa: BLE001 - one document must not stop the check
        return None, diagnostic("parse_failed", stage=stage, step="parse file", file=rel, exc=exc)


def doc_facts_from_bytes(rel: str, data: bytes, *, stage: str = STAGE) -> tuple[DocFacts | None, Problem | None]:
    text, problem = decode_markdown(rel, data, stage=stage)
    if text is None:
        return None, problem
    return doc_facts_from_text(rel, text, stage=stage)


def read_doc_facts(path: Path, rel: str, *, stage: str = STAGE) -> tuple[DocFacts | None, str | None, Problem | None]:
    """``(facts, SHA-256 of the bytes read, None)`` or ``(None, digest-or-None,
    problem)``. The digest comes from the same read as the facts, so a cache
    can store them together; it is None only when the read failed
    (``read_failed``), which a caller must not cache."""
    data, digest, problem = read_bytes_with_digest(path, rel, stage=stage)
    if data is None:
        return None, None, problem
    facts, problem = doc_facts_from_bytes(rel, data, stage=stage)
    return facts, digest, problem
