"""The docs check's resolvers (``doc_resolvers``) called directly: the
per-report context, route-stage precedence, directory landing pages, path
classes and when the symbol resolver reads code words. ``test_doc_refs``
covers the same rules end to end through ``build_report``."""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from doc_config import doc_config
from fake_repo_files import FakeRepoFiles

from code_intel.code_words import CodeWords
from code_intel.doc_report import route_rows, symbol_rows
from code_intel.doc_resolvers import (
    Draft,
    LinkResolver,
    PathResolver,
    ResolutionContext,
    RouteResolver,
    SymbolResolver,
)
from code_intel.doc_rules import DocRefsConfig, NextAppPages, PageSet, RouteMention, SymbolMention
from code_intel.markdown_facts import DocFacts, Link

CONFIG = doc_config({"scope": ["docs/**/*.md"], "route_prefixes": []})


def _repo(root: Path, files: list[str], *, untracked: tuple[str, ...] = ()) -> FakeRepoFiles:
    dirs: set[str] = set()
    for rel in files:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n", encoding="utf-8")
        parent = os.path.dirname(rel)
        while parent:
            dirs.add(parent)
            parent = os.path.dirname(parent)
    listed = frozenset(files)
    return FakeRepoFiles(repo_root=root, files=listed, tracked=listed - set(untracked), dirs=frozenset(dirs))


def _route(method: str, path: str, line: int, file: str = "backend/app.py", handler: str = "h") -> dict[str, Any]:
    return {"method": method, "path": path, "kind": "http", "handler": handler, "file": file, "line": line}


def _context(
    tmp_path: Path,
    files: list[str],
    routes: Sequence[dict[str, Any]] = (),
    unmounted: Sequence[dict[str, Any]] = (),
    symbols: Sequence[dict[str, Any]] = (),
    untracked: tuple[str, ...] = (),
    package_bindings: Mapping[str, frozenset[str]] | None = None,
    config: DocRefsConfig = CONFIG,
    page_set: PageSet | None = None,
) -> ResolutionContext:
    repo = _repo(tmp_path, ["backend/app.py", *files], untracked=untracked)
    if page_set is None:
        page_set = NextAppPages("frontend/app").pages(repo.tracked)
    return ResolutionContext.build(
        repo, config, route_rows(routes), route_rows(unmounted), symbol_rows(symbols), package_bindings or {}, page_set
    )


def _facts(anchors: set[str]) -> DocFacts:
    return DocFacts(headings=(), anchors=frozenset(anchors), links=(), code_spans=(), fences=(), text_runs=())


# ── Context ──────────────────────────────────────────────────────────────


def test_the_context_drops_untracked_and_config_file_rows_and_is_immutable(tmp_path: Path) -> None:
    ctx = _context(
        tmp_path,
        ["backend/scratch.py", ".code-intel/config.toml", "frontend/app/articles/page.tsx"],
        routes=[_route("GET", "/items/{id}", 1), _route("GET", "/scratch", 1, file="backend/scratch.py")],
        symbols=[
            {"name": "Kept", "kind": "class", "container": None, "file": "backend/app.py", "line": 1},
            {"name": "Scratch", "kind": "class", "container": None, "file": "backend/scratch.py", "line": 1},
            {
                "name": "Fixture",
                "kind": "class",
                "container": None,
                "file": ".code-intel/config.toml",
                "line": 1,
            },
        ],
        untracked=("backend/scratch.py",),
    )
    assert [r.path for r in ctx.routes] == ["/items/{id}"]
    assert [s.name for s in ctx.symbols] == ["Kept"]
    assert ctx.python_classes == frozenset({"Kept"})
    assert ctx.backend_roots == frozenset({"items"})
    # FastAPI's own pages pass the gate whatever the app mounts.
    assert ctx.gate_roots == frozenset({"items", "articles", "docs", "redoc", "openapi.json"})
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.routes = ()  # type: ignore[misc]


def test_the_context_maps_are_read_only(tmp_path: Path) -> None:
    kept = {"name": "Kept", "kind": "class", "container": None, "file": "backend/app.py", "line": 1}
    ctx = _context(tmp_path, ["docs/README.md"], symbols=[kept])
    for mapping in (ctx.children, ctx.by_basename, ctx.markdown_in_dir, ctx.markdown_in_dir["docs"], ctx.by_name):
        with pytest.raises(TypeError):
            mapping["x"] = ()  # type: ignore[index]


def test_a_directory_lands_on_its_readme_before_its_index(tmp_path: Path) -> None:
    ctx = _context(tmp_path, ["docs/g/index.md", "docs/g/README.md", "docs/i/INDEX.md", "docs/n/x.md"])
    assert (ctx.dir_landing("docs/g"), ctx.dir_landing("docs/i"), ctx.dir_landing("docs/n")) == (
        "docs/g/README.md",
        "docs/i/INDEX.md",
        None,
    )


# ── Routes ───────────────────────────────────────────────────────────────


def _resolve_route(ctx: ResolutionContext, text: str, hint: str = "") -> Draft:
    method, _, path = text.rpartition(" ")
    mention = RouteMention(text, method or None, path)
    return RouteResolver(ctx, hint).route("docs/a.md", mention, 1, "code", bare=False)


@pytest.mark.parametrize(
    ("text", "status", "target"),
    [
        # An exact live route beats everything, then another method of it.
        ("GET /items/7", "ok", "GET /items/{id}"),
        ("PUT /items/7", "method_differs", "GET /items/{id}"),
        # Without a method, an exact page beats a word filling a parameter.
        ("/items/compare", "frontend", "/items/compare"),
        ("GET /items/compare", "weak_param", "GET /items/{id}"),
        # A FastAPI path beats a word filling a top-level parameter.
        ("GET /docs", "framework", None),
        ("GET /other", "weak_param", "GET /{slug}"),
        # An unmounted route counts only after the live ones.
        ("GET /legacy/thing", "unmounted", "GET /legacy/thing"),
        # A word filling a parameter beats the start of a longer route.
        ("/items", "weak_param", "GET /{slug}"),
        ("/items/*", "prefix", "GET /items/{id}"),
        ("GET /a/b", "missing", None),
    ],
)
def test_route_stages_run_in_precedence_order(tmp_path: Path, text: str, status: str, target: str | None) -> None:
    ctx = _context(
        tmp_path,
        ["frontend/app/items/compare/page.tsx"],
        routes=[_route("GET", "/items/{id}", 1), _route("GET", "/{slug}", 2)],
        unmounted=[_route("GET", "/legacy/thing", 1, file="backend/app.py", handler="legacy")],
    )
    draft = _resolve_route(ctx, text)
    assert (draft.status, draft.target) == (status, target)


def test_a_missing_route_ends_with_the_hint(tmp_path: Path) -> None:
    ctx = _context(tmp_path, [], routes=[_route("GET", "/items/{id}", 1)])
    assert _resolve_route(ctx, "GET /gone", "; see below").detail == "not in the analyzed backend routes; see below"
    assert _resolve_route(ctx, "GET /gone").detail == "not in the analyzed backend routes"


@pytest.mark.parametrize(
    ("text", "status", "target", "detail"),
    [
        ("POST .../approve", "suffix", "POST /appointments/{id}/approve", "the end of a live route"),
        ("POST …/approve", "suffix", "POST /appointments/{id}/approve", "the end of a live route"),
        ("POST .../{rid}/approve", "suffix", "POST /appointments/{id}/approve", "the end of a live route"),
        ("POST .../7/approve", "suffix", "POST /appointments/{id}/approve", "the end of a live route"),
        ("GET .../approve", "method_differs", "POST /appointments/{id}/approve", "served: POST"),
        ("POST .../jira/link", "missing", None, "no live route ends with /jira/link"),
        # The ellipsis may stand for nothing but the `/api` a doc leaves out.
        ("POST .../appointments/{id}/approve", "suffix", "POST /appointments/{id}/approve", "the end of a live route"),
        ("GET .../x/items/{id}", "missing", None, "no live route ends with /x/items/{id}"),
        # A tail of a route parameter filled with a word fails, for its method only.
        ("GET .../notes", "weak_param", "GET /items/{id}", "only /items/{id} ends that way, by a word in a parameter"),
        ("PUT .../notes", "missing", None, "no live route ends with /notes"),
        ("POST .../old", "unmounted", "POST /legacy/old", "legacy at backend/app.py:1 is not mounted on the app"),
        # Without a method a page may end that way too.
        (".../compare", "frontend", "/items/compare", "the end of a frontend page, route handler or public file"),
        (
            "GET .../compare",
            "weak_param",
            "GET /items/{id}",
            "only /items/{id} ends that way, by a word in a parameter",
        ),
        # Only a leading ellipsis names a tail; anywhere else it is a placeholder.
        ("POST /appointments/.../approve", "skipped", None, "placeholder"),
        ("POST .../x/.../approve", "skipped", None, "placeholder"),
        ("POST .../${id}/approve", "skipped", None, "placeholder"),
    ],
)
def test_a_route_tail_fits_the_end_of_a_route(
    tmp_path: Path, text: str, status: str, target: str | None, detail: str
) -> None:
    ctx = _context(
        tmp_path,
        ["frontend/app/items/compare/page.tsx"],
        routes=[_route("POST", "/appointments/{id}/approve", 1), _route("GET", "/items/{id}", 2)],
        unmounted=[_route("POST", "/legacy/old", 1, handler="legacy")],
    )
    draft = _resolve_route(ctx, text)
    assert (draft.status, draft.target, draft.detail) == (status, target, detail)


def test_a_route_tail_several_routes_end_with_fails_naming_them(tmp_path: Path) -> None:
    # The appointment approval is gone; the tail must not pass on the others.
    access = _route("POST", "/api/access-requests/{request_id}/approve", 1)
    scheduled = _route("POST", "/scheduled-grants/{grant_id}/approve", 2)
    both = _resolve_route(_context(tmp_path, [], routes=[access, scheduled]), "POST .../approve")
    assert (both.status, both.target, both.defined_in, both.detail) == (
        "ambiguous",
        None,
        ("backend/app.py:1", "backend/app.py:2"),
        "2 live routes end with /approve: POST /api/access-requests/{request_id}/approve, "
        "POST /scheduled-grants/{grant_id}/approve; write the full path",
    )
    # A tail is only as specific as the live routes make it: with one left it
    # passes on that one, whichever route the doc meant.
    one = _resolve_route(_context(tmp_path, [], routes=[scheduled]), "POST .../approve")
    assert (one.status, one.target) == ("suffix", "POST /scheduled-grants/{grant_id}/approve")
    full = _resolve_route(_context(tmp_path, [], routes=[access, scheduled]), "POST .../{grant_id}/approve")
    assert full.status == "ambiguous", "a parameter matches whatever its name"
    longer = _resolve_route(
        _context(tmp_path, [], routes=[access, scheduled]), "POST .../scheduled-grants/{id}/approve"
    )
    assert (longer.status, longer.target) == ("suffix", "POST /scheduled-grants/{grant_id}/approve")


def test_an_ambiguous_route_tail_counts_only_routes_of_its_method(tmp_path: Path) -> None:
    routes = [
        _route("POST", "/appointments/{id}/approve", 1),
        _route("GET", "/scheduled-grants/{id}/approve", 2),
        _route("PUT", "/absences/{id}/approve", 3),
    ]
    ctx = _context(tmp_path, [], routes=routes)
    post = _resolve_route(ctx, "POST .../approve")
    assert (post.status, post.target) == ("suffix", "POST /appointments/{id}/approve")
    # Another method of the same path is still one route to the mention.
    same_path = _context(tmp_path, [], routes=[_route("GET", "/items/{id}", 1), _route("HEAD", "/items/{id}", 2)])
    assert (_resolve_route(same_path, "HEAD .../{id}").status, _resolve_route(same_path, ".../{id}").status) == (
        "suffix",
        "suffix",
    )
    # A routed method that differs is not ambiguous: it fails as before.
    assert _resolve_route(ctx, "DELETE .../approve").status == "method_differs"


def test_an_ambiguous_route_tail_lists_five_routes_then_a_count(tmp_path: Path) -> None:
    routes = [_route("POST", f"/r{n}/{{id}}/approve", n) for n in range(1, 8)]
    draft = _resolve_route(_context(tmp_path, [], routes=routes), "POST .../approve")
    assert draft.status == "ambiguous"
    assert draft.detail == (
        "7 live routes end with /approve: POST /r1/{id}/approve, POST /r2/{id}/approve, POST /r3/{id}/approve, "
        "POST /r4/{id}/approve, POST /r5/{id}/approve and 2 more; write the full path"
    )
    assert len(draft.defined_in) == 7


def test_a_methodless_route_tail_several_pages_end_with_is_ambiguous(tmp_path: Path) -> None:
    pages = ["frontend/app/articles/compare/page.tsx", "frontend/app/items/compare/page.tsx"]
    draft = _resolve_route(_context(tmp_path, pages, routes=[_route("GET", "/other", 1)]), ".../compare")
    assert (draft.status, draft.target, draft.defined_in, draft.detail) == (
        "ambiguous",
        None,
        tuple(f"{page}:1" for page in pages),
        "2 frontend pages end with /compare: /articles/compare, /items/compare; write the full path",
    )
    one = _resolve_route(_context(tmp_path, pages[:1], routes=[_route("GET", "/other", 1)]), ".../compare")
    assert (one.status, one.target) == ("frontend", "/articles/compare")


def test_a_gated_route_tail_is_skipped(tmp_path: Path) -> None:
    ctx = _context(tmp_path, [], routes=[_route("POST", "/appointments/{id}/approve", 1)])
    mention = RouteMention("post .../approve", "POST", ".../approve", gated=True)
    draft = RouteResolver(ctx).route("docs/a.md", mention, 1, "code", bare=False)
    assert (draft.status, draft.detail) == ("skipped", "not under a backend or frontend route root")


def test_a_word_in_a_page_parameter_fails_naming_the_page(tmp_path: Path) -> None:
    # Still a failure: accepting it would hide a removed literal page such as
    # `/items/compare`; only the diagnosis names the page that nearly fits.
    ctx = _context(tmp_path, ["frontend/app/items/[id]/page.tsx"], routes=[_route("GET", "/other", 1)])
    word = _resolve_route(ctx, "/items/abc")
    assert (word.status, word.target, word.defined_in, word.detail) == (
        "weak_param",
        "/items/[id]",
        ("frontend/app/items/[id]/page.tsx:1",),
        "only the page /items/[id] fits, by a word in a parameter",
    )
    assert (_resolve_route(ctx, "/items/42").status, _resolve_route(ctx, "/items/42").target) == (
        "frontend",
        "/items/[id]",
    )
    assert _resolve_route(ctx, "GET /items/abc").status == "missing", "a method names a backend route"


# ── Links ────────────────────────────────────────────────────────────────


def test_links_read_facts_only_for_a_fragment_on_a_landing_page(tmp_path: Path) -> None:
    ctx = _context(tmp_path, ["docs/a.md", "docs/g/README.md", "docs/n/x.md"])
    read: list[str] = []

    def facts(rel: str) -> DocFacts | None:
        read.append(rel)
        return _facts({"real"})

    links = LinkResolver(ctx, facts)
    found = [
        (d.status, d.target)
        for d in (
            links.link("docs/a.md", Link("g/", "link", 1, "prose")),
            links.link("docs/a.md", Link("n/#x", "link", 1, "prose")),
            links.link("docs/a.md", Link("g/#real", "link", 1, "prose")),
            links.link("docs/a.md", Link("g/#nope", "link", 1, "prose")),
        )
    ]
    assert found == [("ok", "docs/g"), ("not_checked", "docs/n"), ("ok", "docs/g"), ("anchor_missing", "docs/g")]
    assert read == ["docs/g/README.md", "docs/g/README.md"], "no landing page, no fragment: nothing to read"
    assert links.markdown_target("docs/a.md", Link("g/#real", "link", 1, "prose")) == "docs/g/README.md"


def test_a_link_keeps_its_href_as_written_when_decoding_changes_it(tmp_path: Path) -> None:
    links = LinkResolver(_context(tmp_path, ["docs/a b.md"]), lambda rel: None)
    spaced = links.link("docs/x.md", Link("a%20b.md", "link", 1, "prose"))
    plain = links.link("docs/x.md", Link("x.md", "link", 1, "prose"))
    assert (spaced.text, spaced.raw_text, spaced.target) == ("a b.md", "a%20b.md", "docs/a b.md")
    assert plain.raw_text is None


# ── Paths ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("path", "relative", "form", "expected"),
    [
        ("backend/app.py", False, "span", ("root", [""])),
        ("app.py", False, "span", (None, [])),
        ("service_backend/x.py", False, "span", ("backend-rel", ["backend/", "docs"])),
        ("service_backend/x.py", False, "fence", (None, [])),
        ("../backend/app.py", False, "span", ("doc-rel", ["docs"])),
        ("tests/x.py", True, "span", ("doc-rel", ["docs"])),
        ("nowhere/x.py", False, "span", ("unknown", ["docs"])),
    ],
)
def test_path_classes_and_bases(tmp_path: Path, path: str, relative: bool, form: Any, expected: Any) -> None:
    ctx = _context(tmp_path, ["backend/service_backend/x.py", "docs/a.md"])
    assert PathResolver(ctx).classify("docs/a.md", path, relative, form) == expected


# ── Symbols ──────────────────────────────────────────────────────────────


def test_code_words_are_read_only_when_the_index_has_no_definition(tmp_path: Path) -> None:
    ctx = _context(
        tmp_path,
        [],
        symbols=[{"name": "Kept", "kind": "class", "container": None, "file": "backend/app.py", "line": 3}],
    )
    calls: list[int] = []

    def words() -> CodeWords:
        calls.append(1)
        return CodeWords(frozenset({"UsedName"}), frozenset())

    symbols = SymbolResolver(ctx, words)
    kept = symbols.symbol("docs/a.md", SymbolMention("Kept", "Kept", False, False), 1, "code")
    assert (kept.status, kept.defined_in, calls) == ("ok", ("backend/app.py:3",), [])
    used = symbols.symbol("docs/a.md", SymbolMention("UsedName", "UsedName", False, False), 1, "code")
    assert (used.status, used.detail, len(calls)) == ("ok", "used in tracked code", 1)
    gone = symbols.symbol("docs/a.md", SymbolMention("GoneName", "GoneName", False, False), 1, "code")
    assert (gone.status, gone.checked) == ("missing", True)


def _symbol_ctx(tmp_path: Path) -> ResolutionContext:
    def row(name: str, kind: str, container: str | None, file: str, line: int, scope: str = "module") -> dict[str, Any]:
        return {"name": name, "kind": kind, "container": container, "file": file, "line": line, "scope": scope}

    return _context(
        tmp_path,
        [
            "backend/pkg/mod.py",
            "backend/pkg/__init__.py",
            "backend/nest.py",
            "frontend/lib/api/index.ts",
            "frontend/components/article/Panel.tsx",
        ],
        symbols=[
            row("SomeClass", "class", None, "backend/app.py", 1),
            row("ItemOwner", "class", None, "backend/pkg/mod.py", 1),
            row("attr", "method", "ItemOwner", "backend/pkg/mod.py", 2),
            row("func", "function", None, "backend/pkg/mod.py", 5),
            row("setup", "function", None, "backend/pkg/__init__.py", 1),
            row("Outer", "class", None, "backend/nest.py", 1),
            row("Inner", "class", "Outer", "backend/nest.py", 2),
            row("attr", "method", "Outer.Inner", "backend/nest.py", 3),
            row("Wrong", "class", None, "backend/nest.py", 9),
            row("fetchItems", "function", None, "frontend/lib/api/index.ts", 1),
            row("helper", "function", None, "backend/pkg/mod.py", 7, "local"),
            row("view", "const", None, "frontend/components/article/Panel.tsx", 3),
        ],
        # `pkg/__init__.py` re-exports ItemOwner, not func.
        package_bindings={"backend/pkg": frozenset({"ItemOwner"})},
    )


@pytest.mark.parametrize(
    ("name", "status", "defined_in"),
    [
        ("ItemOwner.attr", "ok", ("backend/pkg/mod.py:2",)),
        # ItemOwner is not nested in SomeClass: the extra qualifier must not be dropped.
        ("SomeClass.ItemOwner.attr", "missing", ()),
        ("Outer.Inner.attr", "ok", ("backend/nest.py:3",)),
        ("Inner.attr", "ok", ("backend/nest.py:3",)),
        ("Wrong.Inner.attr", "missing", ()),
        ("pkg.mod.ItemOwner.attr", "ok", ("backend/pkg/mod.py:2",)),
        ("mod.ItemOwner.attr", "ok", ("backend/pkg/mod.py:2",)),
        ("mod.func", "ok", ("backend/pkg/mod.py:5",)),
        ("pkg.setup", "ok", ("backend/pkg/__init__.py:1",)),
        ("api.fetchItems", "ok", ("frontend/lib/api/index.ts:1",)),
        ("other.mod.ItemOwner.attr", "unresolved", ()),
        ("other.func", "unresolved", ()),
        ("pkg.mod.ItemOwner", "ok", ("backend/pkg/mod.py:1",)),
        ("other.mod.ItemOwner", "missing", ()),
        # A package containing the module re-exports it; a run that skips a
        # package or names one the module is not in does not.
        ("pkg.ItemOwner.attr", "ok", ("backend/pkg/mod.py:2",)),
        ("pkg.ItemOwner", "ok", ("backend/pkg/mod.py:1",)),
        ("pkg.func", "unresolved", ()),
        ("backend.mod.ItemOwner", "missing", ()),
        ("nest.ItemOwner", "missing", ()),
        # A function's local is no module attribute; a TypeScript directory
        # is no package (`article.view` is a permission, not Panel's `view`).
        ("mod.helper", "unresolved", ()),
        ("article.view", "unresolved", ()),
        ("Panel.view", "ok", ("frontend/components/article/Panel.tsx:3",)),
    ],
)
def test_dotted_symbols_match_their_whole_qualifier_chain(
    tmp_path: Path, name: str, status: str, defined_in: tuple[str, ...]
) -> None:
    symbols = SymbolResolver(_symbol_ctx(tmp_path), lambda: CodeWords(frozenset(), frozenset()))
    draft = symbols.symbol("docs/a.md", SymbolMention(name, name, False, False), 1, "code")
    assert (draft.status, draft.defined_in) == (status, defined_in)
