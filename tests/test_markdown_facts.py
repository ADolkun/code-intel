"""Markdown facts (``markdown_facts``): exact lines, extraction, GitHub slugs and
failures reported as diagnostics."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from code_intel import code_paths
from code_intel import markdown_facts as mf


def facts(text: str) -> mf.DocFacts:
    return mf.parse_markdown(textwrap.dedent(text).lstrip("\n"))


def link_lines(doc: mf.DocFacts) -> dict[str, int]:
    return {link.href: link.line for link in doc.links}


# ── Lines ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_lines_count_every_newline_style(newline: str) -> None:
    text = newline.join(["# Title", "", "para [a](a.md)", "more [b](b.md)", "", "`c`", ""])
    doc = mf.parse_markdown(text)
    assert link_lines(doc) == {"a.md": 3, "b.md": 4}
    assert [(span.text, span.line) for span in doc.code_spans] == [("c", 6)]


def test_bom_is_dropped_and_lines_stay_exact() -> None:
    doc, problem = mf.doc_facts_from_bytes("d.md", "﻿# Title\n\n[a](a.md)\n".encode())
    assert problem is None
    assert doc is not None
    assert doc.headings[0].slug == "title"
    assert link_lines(doc) == {"a.md": 3}


def test_softbreaks_and_hardbreaks_advance_the_line() -> None:
    doc = facts(
        """
        first [a](a.md)
        second [b](b.md)\\
        third [c](c.md)
        fourth [d](d.md)
        """
    )
    assert link_lines(doc) == {"a.md": 1, "b.md": 2, "c.md": 3, "d.md": 4}


def test_reference_after_a_multi_line_code_span_keeps_its_line() -> None:
    # markdown-it turns the code span's newlines into spaces without a break
    # token; counting breaks alone would put every later link on line 2.
    doc = facts(
        """
        start `one
        two
        three` then [after](after.md)
        next `x` [last](last.md)
        """
    )
    assert [(s.text, s.line) for s in doc.code_spans] == [("one two three", 1), ("x", 4)]
    assert link_lines(doc) == {"after.md": 3, "last.md": 4}


def test_table_cells_take_their_row_line() -> None:
    doc = facts(
        """
        | Method | Path |
        | --- | --- |
        | `GET` | `/api/a` |
        | `POST` | [b](b.md) |
        """
    )
    assert [(s.text, s.line, s.context) for s in doc.code_spans] == [
        ("GET", 3, "table"),
        ("/api/a", 3, "table"),
        ("POST", 4, "table"),
    ]
    assert doc.links == (mf.Link("b.md", "link", 4, "table"),)


def test_fence_content_lines_follow_the_opening_fence() -> None:
    doc = facts(
        """
        intro

        ```python
        a = 1
        b = 2
        ```
        """
    )
    (fence,) = doc.fences
    assert (fence.info, fence.line, fence.content_line, fence.lines) == ("python", 3, 4, ("a = 1", "b = 2"))


def test_fence_nested_in_a_list_item() -> None:
    doc = facts(
        """
        - item

          ```sh
          uv run x
          ```
        """
    )
    (fence,) = doc.fences
    assert (fence.line, fence.content_line, fence.lines) == (3, 4, ("uv run x",))


def test_indented_code_block_is_a_fence_without_info() -> None:
    doc = facts("para\n\n    code line\n    second\n")
    (fence,) = doc.fences
    assert (fence.indented, fence.line, fence.content_line, fence.lines) == (True, 3, 3, ("code line", "second"))


# ── Extraction ───────────────────────────────────────────────────────────


def test_links_of_every_kind() -> None:
    doc = facts(
        """
        [inline](a.md#sec) and [ref][r] and ![img](pic.png) and <https://x.example/a>
        <a href="h.html">h</a> <img src="i.svg">

        <p><a href="block.md">b</a></p>

        [r]: ref.md
        """
    )
    assert [(link.href, link.kind, link.line) for link in doc.links] == [
        ("a.md#sec", "link", 1),
        ("ref.md", "link", 1),
        ("pic.png", "image", 1),
        ("https://x.example/a", "autolink", 1),
        ("h.html", "html_href", 2),
        ("i.svg", "html_src", 2),
        ("block.md", "html_href", 4),
    ]
    assert {link.context for link in doc.links if link.kind.startswith("html_")} == {"html"}


def test_links_in_comments_and_fences_are_not_links() -> None:
    doc = facts(
        """
        <!-- [hidden](hidden.md) <a href="x.md">x</a> -->

        ```md
        [fenced](fenced.md)
        ```

        text <!-- [inline](inline.md) --> end
        """
    )
    assert doc.links == ()
    assert doc.fences[0].lines == ("[fenced](fenced.md)",)


def test_escaped_backticks_are_not_code_spans() -> None:
    doc = facts("a \\`not code\\` and `real` [l](l.md)\n")
    assert [s.text for s in doc.code_spans] == ["real"]
    assert link_lines(doc) == {"l.md": 1}


def test_double_backtick_span_keeps_inner_backticks() -> None:
    doc = facts("x ``a `b` c``\n")
    assert [s.text for s in doc.code_spans] == ["a `b` c"]


def test_text_runs_carry_line_and_context() -> None:
    doc = facts(
        """
        Call **GET** /api/x here
        then `code` POST /api/y

        ## GET /api/z
        """
    )
    assert [(r.text, r.line, r.context) for r in doc.text_runs] == [
        ("Call GET /api/x here", 1, "prose"),
        ("then ", 2, "prose"),
        (" POST /api/y", 2, "prose"),
        ("GET /api/z", 4, "heading"),
    ]


# ── Slugs and anchors ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("heading", "slug"),
    [
        ("Hello, World!", "hello-world"),
        ("Audit & Analytics", "audit--analytics"),
        ("Café Ünïcode", "café-ünïcode"),
        ("Ship it 🚀 now", "ship-it--now"),
        ("A – B", "a--b"),
        ("snake_case and dash-ed", "snake_case-and-dash-ed"),
        ("`code_span()` heading", "code_span-heading"),
        ("[Link text](x.md) here", "link-text-here"),
        ("*Emphasis* **strong**", "emphasis-strong"),
    ],
)
def test_github_slug(heading: str, slug: str) -> None:
    doc = facts(f"# {heading}\n")
    assert doc.headings[0].slug == slug
    assert slug in doc.anchors


def test_duplicate_slugs_number_like_github_slugger() -> None:
    doc = facts("# Foo\n\n# Foo\n\n# Foo\n\n# Foo 1\n")
    assert [h.slug for h in doc.headings] == ["foo", "foo-1", "foo-2", "foo-1-1"]


def test_literal_numbered_heading_pushes_the_duplicate_on() -> None:
    doc = facts("# Foo\n\n# Foo 1\n\n# Foo\n")
    assert [h.slug for h in doc.headings] == ["foo", "foo-1", "foo-2"]


def test_setext_headings_have_slugs_and_lines() -> None:
    doc = facts("Title One\n=========\n\nSecond\n------\n")
    assert [(h.level, h.text, h.slug, h.line) for h in doc.headings] == [
        (1, "Title One", "title-one", 1),
        (2, "Second", "second", 4),
    ]


def test_html_id_and_name_are_anchors() -> None:
    doc = facts(
        """
        ## <a id="custom"></a>Heading

        <a name="legacy"></a>

        <div id="box">

        text
        </div>
        """
    )
    assert {"custom", "legacy", "box", "heading"} <= doc.anchors


def test_match_anchor_decodes_and_matches_case_exactly() -> None:
    anchors = {"audit--analytics", "café"}
    assert mf.match_anchor("audit--analytics", anchors)
    assert mf.match_anchor("caf%C3%A9", anchors)
    assert not mf.match_anchor("Audit--Analytics", anchors)
    assert not mf.match_anchor("missing", anchors)
    assert not mf.match_anchor("", anchors)
    assert mf.match_anchor("café", iter(["café"]))


# ── Failures ─────────────────────────────────────────────────────────────


def test_undecodable_bytes_are_a_decode_failed_diagnostic() -> None:
    doc, problem = mf.doc_facts_from_bytes("docs/bad.md", b"# ok\n\nbad \xff byte\n")
    assert doc is None
    assert problem is not None
    assert (problem.kind, problem.step, problem.file, problem.line) == (
        "decode_failed",
        "decode file",
        "docs/bad.md",
        3,
    )
    assert problem.stage == "doc refs"


def test_unreadable_file_is_a_read_failed_diagnostic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(path: Path) -> bytes:
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(code_paths, "read_file_bytes", refuse)
    doc, digest, problem = mf.read_doc_facts(tmp_path / "x.md", "x.md")
    assert doc is None
    # No digest: a caller caches only results that carry one.
    assert digest is None
    assert problem is not None
    assert (problem.kind, problem.step, problem.file) == ("read_failed", "read file", "x.md")


def test_read_returns_the_digest_of_the_bytes_parsed(tmp_path: Path) -> None:
    path = tmp_path / "d.md"
    path.write_bytes(b"# T\n")
    doc, digest, problem = mf.read_doc_facts(path, "d.md")
    assert problem is None
    assert doc is not None
    assert digest == code_paths.content_digest(path)


def test_parser_exception_is_a_parse_failed_diagnostic(monkeypatch: pytest.MonkeyPatch) -> None:
    class Exploding:
        def parse(self, text: str) -> list:
            raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(mf, "_parser", Exploding)
    doc, problem = mf.doc_facts_from_text("docs/deep.md", "> > > deep\n")
    assert doc is None
    assert problem is not None
    assert (problem.kind, problem.step, problem.exception) == ("parse_failed", "parse file", "RecursionError")


def test_each_thread_parses_with_its_own_parser() -> None:
    with ThreadPoolExecutor(max_workers=1) as pool:
        other = pool.submit(mf._parser).result()
    assert mf._parser() is mf._parser()
    assert other is not mf._parser()


def test_importing_fills_the_url_lookup_lists_before_any_thread_parses() -> None:
    code = (
        "import code_intel.markdown_facts; from mdurl import _decode, _encode; "
        "print(sorted(map(len, [*_encode.encode_cache.values(), *_decode.decode_cache.values()])))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[128, 128]"
