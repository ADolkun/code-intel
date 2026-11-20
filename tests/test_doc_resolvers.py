"""The docs check's resolvers (``doc_resolvers``) called directly: the
per-report context, route-stage precedence, directory landing pages, path
classes and when the symbol resolver reads code words. ``test_doc_refs``
covers the same rules end to end through ``build_report``."""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from doc_config import doc_config
from fake_repo_files import FakeRepoFiles

from stack_intel.code_words import NO_MODULES, CodeWords, TopLevel, module_bindings
from stack_intel.definition_files import Unreadable
from stack_intel.doc_commands import CommandMention, command_mentions
from stack_intel.doc_report import route_rows, symbol_rows
from stack_intel.doc_resolvers import (
    CommandResolver,
    Draft,
    LinkResolver,
    PathResolver,
    ResolutionContext,
    RouteResolver,
    SymbolResolver,
)
from stack_intel.doc_rules import DocRefsConfig, NextAppPages, PageSet, RouteMention, SymbolMention
from stack_intel.make_targets import MakeTargets
from stack_intel.markdown_facts import DocFacts, Link
from stack_intel.mise_tasks import MiseFile

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
        ["backend/scratch.py", ".stack-intel/config.toml", "frontend/app/articles/page.tsx"],
        routes=[_route("GET", "/items/{id}", 1), _route("GET", "/scratch", 1, file="backend/scratch.py")],
        symbols=[
            {"name": "Kept", "kind": "class", "container": None, "file": "backend/app.py", "line": 1},
            {"name": "Scratch", "kind": "class", "container": None, "file": "backend/scratch.py", "line": 1},
            {
                "name": "Fixture",
                "kind": "class",
                "container": None,
                "file": ".stack-intel/config.toml",
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
    frozen: Any = ctx
    with pytest.raises(dataclasses.FrozenInstanceError):
        frozen.routes = ()


def test_the_context_maps_are_read_only(tmp_path: Path) -> None:
    kept = {"name": "Kept", "kind": "class", "container": None, "file": "backend/app.py", "line": 1}
    ctx = _context(tmp_path, ["docs/README.md"], symbols=[kept])
    mappings: tuple[Any, ...] = (
        ctx.children,
        ctx.by_basename,
        ctx.markdown_in_dir,
        ctx.markdown_in_dir["docs"],
        ctx.by_name,
    )
    for mapping in mappings:
        with pytest.raises(TypeError):
            mapping["x"] = ()


def test_a_directory_lands_on_its_readme_before_its_index(tmp_path: Path) -> None:
    ctx = _context(tmp_path, ["docs/g/index.md", "docs/g/README.md", "docs/i/INDEX.md", "docs/n/x.md"])
    assert (ctx.dir_landing("docs/g"), ctx.dir_landing("docs/i"), ctx.dir_landing("docs/n")) == (
        "docs/g/README.md",
        "docs/i/INDEX.md",
        None,
    )


# ── Commands ─────────────────────────────────────────────────────────────


def test_the_context_lists_every_tracked_package_json_directory(tmp_path: Path) -> None:
    files = ["package.json", "web/package.json", "web/node_modules/dep/package.json", "scratch/package.json"]
    ctx = _context(tmp_path, files, untracked=("scratch/package.json",))
    assert ctx.manifest_dirs == frozenset({"", "web", "web/node_modules/dep"})


def test_the_context_indexes_makefiles_and_mise_files(tmp_path: Path) -> None:
    files = [
        "Makefile",
        "GNUmakefile",
        "tools/makefile",
        "tools/Makefile",
        "node_modules/x/Makefile",
        "mise.toml",
        "mise.local.toml",
        "backend/.config/mise/config.toml",
        "mise-tasks/build.sh",
        "backend/.mise/tasks/db/migrate",
        "docs/mise.md",
    ]
    ctx = _context(tmp_path, files)
    assert dict(ctx.makefiles) == {
        "": "GNUmakefile",
        "tools": "tools/makefile",
        "node_modules/x": "node_modules/x/Makefile",
    }
    assert dict(ctx.mise_configs) == {
        "": ("mise.local.toml", "mise.toml"),
        "backend": ("backend/.config/mise/config.toml",),
    }
    assert dict(ctx.mise_task_files) == {"": ("mise-tasks/build.sh",), "backend": ("backend/.mise/tasks/db/migrate",)}


def _resolver(
    ctx: ResolutionContext,
    scripts: Mapping[str, frozenset[str] | Unreadable] | None = None,
    make: Mapping[str, MakeTargets | Unreadable] | None = None,
    mise: Mapping[str, MiseFile | Unreadable] | None = None,
    read: list[str] | None = None,
    npmrc: Mapping[str, bool | Unreadable] | None = None,
) -> CommandResolver:
    def reader[T](table: Mapping[str, T] | None, default: T) -> Callable[[str], T]:
        def read_one(key: str) -> T:
            if read is not None:
                read.append(key)
            return (table or {}).get(key, default)

        return read_one

    return CommandResolver(
        ctx,
        reader(scripts, frozenset()),
        reader(make, MakeTargets(frozenset())),
        reader(mise, MiseFile(frozenset())),
        reader(npmrc, False),
    )


def _command(resolver: CommandResolver, mention: CommandMention, doc: str = "docs/a.md") -> Draft:
    draft = resolver.command(doc, mention, 1, "code")
    assert draft is not None
    return draft


def test_a_script_is_ok_near_the_doc_and_not_checked_when_only_another_package_json_defines_it(
    tmp_path: Path,
) -> None:
    ctx = _context(
        tmp_path, ["package.json", "frontend/package.json", "frontend/ui/package.json", "tools/package.json"]
    )
    read: list[str] = []
    scripts = {"frontend/ui": frozenset({"ui"}), "tools": frozenset({"tool"})}
    resolver = _resolver(ctx, scripts=scripts, read=read)
    draft = _command(resolver, CommandMention("npm", "ui", None, "npm run ui"))
    assert (draft.status, draft.target, draft.checked) == ("ok", "frontend/ui/package.json", True)
    draft = _command(resolver, CommandMention("npm", "tool", None, "npm run tool"))
    assert (draft.status, draft.target, draft.checked) == ("not_checked", "tools/package.json", False)
    assert draft.detail == "defined only in tools/package.json; the doc names no directory"
    read.clear()
    draft = _command(resolver, CommandMention("npm", "ui", "tools", "npm run ui --prefix tools"))
    assert (draft.status, draft.detail) == ("missing", "tools/package.json defines no script 'ui'")
    assert read == ["tools"]


def test_a_package_json_that_cannot_be_read_leaves_the_script_unchecked(tmp_path: Path) -> None:
    ctx = _context(tmp_path, ["frontend/package.json"])
    broken = Unreadable("frontend/package.json", "Expecting value: line 1 column 1 (char 0)")
    resolver = _resolver(ctx, scripts={"frontend": broken})
    draft = _command(resolver, CommandMention("npm", "build", None, "npm run build"))
    assert (draft.status, draft.detail, draft.checked) == (
        "not_checked",
        "frontend/package.json could not be read: Expecting value: line 1 column 1 (char 0)",
        False,
    )


def test_an_unproven_form_is_ok_when_defined_and_not_checked_otherwise(tmp_path: Path) -> None:
    ctx = _context(tmp_path, ["package.json"])
    resolver = _resolver(ctx, scripts={"": frozenset({"build"})})
    ok = _command(resolver, CommandMention("yarn", "build", None, "yarn build", "unproven", "why"))
    missing = _command(resolver, CommandMention("yarn", "deploy", None, "yarn deploy", "unproven", "why"))
    assert (ok.status, missing.status, missing.detail, missing.checked) == ("ok", "not_checked", "why", False)


def test_unchecked_and_skipped_forms_are_not_looked_up(tmp_path: Path) -> None:
    ctx = _context(tmp_path, ["package.json", "Makefile"])
    read: list[str] = []
    resolver = _resolver(ctx, read=read)
    unchecked = _command(resolver, CommandMention("make", "lint", None, "make -f x lint", "unchecked", "-f"))
    skipped = _command(resolver, CommandMention("npm", "<x>", None, "npm run <x>", "skipped", "a placeholder"))
    assert [(d.status, d.detail, d.checked) for d in (unchecked, skipped)] == [
        ("not_checked", "-f", False),
        ("skipped", "a placeholder", False),
    ]
    assert read == []


def test_a_runner_without_definition_files_gives_no_reference(tmp_path: Path) -> None:
    resolver = _resolver(_context(tmp_path, ["package.json"]))
    assert resolver.command("docs/a.md", CommandMention("make", "lint", None, "make lint"), 1, "code") is None
    assert resolver.command("docs/a.md", CommandMention("mise", "lint", None, "mise run lint"), 1, "code") is None


def test_a_make_target_absent_from_every_provable_makefile_is_missing(tmp_path: Path) -> None:
    ctx = _context(tmp_path, ["Makefile", "tools/Makefile"])
    make = {"Makefile": MakeTargets(frozenset({"lint"})), "tools/Makefile": MakeTargets(frozenset({"build"}))}
    resolver = _resolver(ctx, make=make)
    found = {
        name: _command(resolver, CommandMention("make", name, None, f"make {name}"))
        for name in ("lint", "build", "nope")
    }
    assert [(d.status, d.target, d.detail) for d in found.values()] == [
        ("ok", "Makefile", "defined in Makefile"),
        ("not_checked", "tools/Makefile", "defined only in tools/Makefile; the doc names no directory"),
        ("missing", None, "Makefile or tools/Makefile defines no target 'nope'"),
    ]


@pytest.mark.parametrize(
    ("targets", "mention", "detail"),
    [
        (
            MakeTargets(frozenset({"lint"}), opaque="has include (line 1)"),
            CommandMention("make", "nope", None, "make nope"),
            "Makefile has include (line 1), so its targets are not all known",
        ),
        (
            MakeTargets(frozenset({"web/out"}), variables=frozenset({"OUT"})),
            CommandMention("make", "nope", None, "make nope OUT=x", overrides=frozenset({"OUT"})),
            "the command sets OUT, and Makefile names targets with variables",
        ),
    ],
)
def test_an_unprovable_makefile_leaves_a_target_unchecked(
    tmp_path: Path, targets: MakeTargets, mention: CommandMention, detail: str
) -> None:
    draft = _command(_resolver(_context(tmp_path, ["Makefile"]), make={"Makefile": targets}), mention)
    assert (draft.status, draft.detail, draft.checked) == ("not_checked", detail, False)


@pytest.mark.parametrize(
    ("files", "name", "detail"),
    [
        (["dist/app.js"], "dist", "dist exists, so make has nothing to do for it"),
        (["README"], "README", "README exists, so make has nothing to do for it"),
        (["prog.c"], "prog", "prog.c exists, and a built-in make rule may build prog from it"),
        (["prog.c"], "prog.o", "prog.c exists, and a built-in make rule may build prog.o from it"),
        (["RCS/notes,v"], "notes", "RCS/notes,v exists, and a built-in make rule may build notes from it"),
        (["SCCS/s.notes"], "notes", "SCCS/s.notes exists, and a built-in make rule may build notes from it"),
    ],
)
def test_a_file_make_can_find_or_build_leaves_a_target_unchecked(
    tmp_path: Path, files: list[str], name: str, detail: str
) -> None:
    ctx = _context(tmp_path, ["Makefile", *files])
    draft = _command(
        _resolver(ctx, make={"Makefile": MakeTargets(frozenset({"lint"}))}),
        CommandMention("make", name, None, f"make {name}"),
    )
    assert (draft.status, draft.detail) == ("not_checked", detail)


def test_mise_reads_only_the_directories_from_the_root_down_to_the_named_one(tmp_path: Path) -> None:
    ctx = _context(tmp_path, ["mise.toml", "backend/mise.toml", "frontend/mise.toml"])
    read: list[str] = []
    mise = {
        "mise.toml": MiseFile(frozenset({"release"})),
        "backend/mise.toml": MiseFile(frozenset({"test"})),
        "frontend/mise.toml": MiseFile(frozenset({"build"})),
    }
    resolver = _resolver(ctx, mise=mise, read=read)
    draft = _command(resolver, CommandMention("mise", "release", "backend", "mise -C backend run release"))
    assert (draft.status, draft.target) == ("ok", "mise.toml")
    draft = _command(resolver, CommandMention("mise", "test", "backend", "mise -C backend run test"))
    assert (draft.status, draft.target) == ("ok", "backend/mise.toml")
    assert set(read) == {"mise.toml", "backend/mise.toml"}


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
        ("../backend/app.py", False, "command", ("shell-rel", ["", "docs"])),
        ("../backend/app.py", False, "fence", (None, [])),
        ("tests/x.py", True, "span", ("doc-rel", ["docs"])),
        ("nowhere/x.py", False, "span", ("unknown", ["docs"])),
    ],
)
def test_path_classes_and_bases(tmp_path: Path, path: str, relative: bool, form: Any, expected: Any) -> None:
    ctx = _context(tmp_path, ["backend/service_backend/x.py", "docs/a.md"])
    assert PathResolver(ctx).classify("docs/a.md", path, relative, form) == expected


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("**/b.py", "b.py"),
        ("a/**/c.py", "a/c.py"),
        ("a/**", "a/c.py"),
        ("a/*/d.py", "a/x/d.py"),
        ("a/?/d.py", "a/x/d.py"),
        ("**/x/*.py", "a/x/d.py"),
        ("app/[id]/*.tsx", "app/[id]/page.tsx"),
        ("a/**/*.ts", None),
    ],
)
def test_the_first_glob_match_skips_paths_that_cannot_match(tmp_path: Path, pattern: str, expected: str | None) -> None:
    paths = PathResolver(_context(tmp_path, ["b.py", "a/c.py", "a/x/d.py", "app/[id]/page.tsx"]))
    assert paths._first_match(pattern) == expected


@pytest.mark.parametrize(
    ("glob", "resolved", "ended", "status"),
    [
        # The first alternative that matches ends the search.
        ("pkg/**/*.{py,pyi,mts}", ["pkg/**/*.py"], [], "ok"),
        ("pkg/**/*.{mts,py,pyi}", ["pkg/**/*.mts", "pkg/**/*.py"], [], "ok"),
        # The suffix match runs only once every alternative misses at every base.
        ("pkg/**/*.{mts,cts}", ["pkg/**/*.mts", "pkg/**/*.cts"], ["pkg/**/*.mts", "pkg/**/*.cts"], "missing"),
    ],
)
def test_glob_alternatives_are_tried_lazily(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, glob: str, resolved: list[str], ended: list[str], status: str
) -> None:
    paths = PathResolver(_context(tmp_path, ["pkg/a.py", "pkg/sub/b.py"]))
    calls: dict[str, list[str]] = {"resolve_in": [], "_ending_with": []}
    for name in calls:
        real = getattr(paths, name)

        def spy(*args: Any, _real: Callable[..., Any] = real, _name: str = name) -> Any:
            calls[_name].append(args[-1])
            return _real(*args)

        monkeypatch.setattr(paths, name, spy)
    draft = paths.path("docs/a.md", glob, "span", 1, "code")
    assert draft is not None
    assert (calls["resolve_in"], calls["_ending_with"], draft.status) == (resolved, ended, status)


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

    symbols = SymbolResolver(ctx, words, lambda: NO_MODULES)
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
    symbols = SymbolResolver(_symbol_ctx(tmp_path), lambda: CodeWords(frozenset(), frozenset()), lambda: NO_MODULES)
    draft = symbols.symbol("docs/a.md", SymbolMention(name, name, False, False), 1, "code")
    assert (draft.status, draft.defined_in) == (status, defined_in)


@pytest.mark.parametrize(
    ("name", "status", "detail"),
    [
        ("pkg.ProgressEvent", "ok", "bound at the top level of pkg/__init__.py"),
        ("pkg.errors.AuthState", "ok", "bound at the top level of pkg/errors.py"),
        ("errors.AuthState", "ok", "bound at the top level of pkg/errors.py"),
        ("test_widgets.TestWidgetLayout", "ok", "bound at the top level of tests/test_widgets.py"),
        # The qualifier names a module that does not bind the name, or no module.
        ("pkg.AuthState", "missing", "no definition in the symbol index and no use in tracked code"),
        ("pkg.state.ProgressEvent", "missing", "no definition in the symbol index and no use in tracked code"),
        ("other.errors.AuthState", "missing", "no definition in the symbol index and no use in tracked code"),
        # A binding proves the class, not its attributes.
        ("pkg.errors.AuthState.gone", "unresolved", "no definition in the symbol index and no use in tracked code"),
    ],
)
def test_a_module_qualifies_the_names_it_binds_at_top_level(
    tmp_path: Path, name: str, status: str, detail: str
) -> None:
    modules = module_bindings(
        {
            "pkg/__init__.py": TopLevel(names=frozenset({"ProgressEvent"})),
            "pkg/errors.py": TopLevel(names=frozenset({"AuthState"})),
            "pkg/state.py": TopLevel(names=frozenset({"AuthState"})),
            "tests/test_widgets.py": TopLevel(names=frozenset({"TestWidgetLayout"})),
        }
    )
    symbols = SymbolResolver(_context(tmp_path, []), lambda: CodeWords(frozenset(), frozenset()), lambda: modules)
    draft = symbols.symbol("docs/a.md", SymbolMention(name, name, False, False), 1, "code")
    assert (draft.status, draft.detail) == (status, detail)


def _first(text: str) -> CommandMention:
    return command_mentions(text)[0]


@pytest.mark.parametrize(
    ("text", "files"),
    [
        ("make -C ../other build", ["Makefile"]),
        ("cd ~/proj && make build", ["Makefile"]),
        ("cd $APP && make build", ["Makefile"]),
        ("(cd ../infra && make deploy)", ["Makefile"]),
        ("cd .. && mise run deploy", ["mise.toml"]),
        ("mise -C /opt/x run deploy", ["mise.toml"]),
    ],
)
def test_an_unproven_make_or_mise_mention_is_never_missing(tmp_path: Path, text: str, files: list[str]) -> None:
    ctx = _context(tmp_path, files)
    resolver = _resolver(
        ctx, make={"Makefile": MakeTargets(frozenset({"lint"}))}, mise={"mise.toml": MiseFile(frozenset({"lint"}))}
    )
    draft = _command(resolver, _first(text))
    assert (draft.status, draft.checked) == ("not_checked", False)
    assert draft.detail == "runs in a directory outside the repository or not written literally"


@pytest.mark.parametrize(
    ("files", "doc"),
    [
        (["configure.ac", "Makefile.am", "tools/Makefile"], "docs/a.md"),
        (["Makefile.PL", "t/Makefile"], "docs/a.md"),
    ],
)
def test_make_with_no_makefile_where_the_doc_runs_it_is_not_checked(tmp_path: Path, files: list[str], doc: str) -> None:
    ctx = _context(tmp_path, files)
    make = {rel: MakeTargets(frozenset({"build"})) for rel in files if rel.endswith("/Makefile")}
    draft = _command(_resolver(ctx, make=make), _first("make install"), doc)
    assert (draft.status, draft.detail) == ("not_checked", "no makefile where the doc runs it")


def test_mise_with_no_config_where_the_doc_runs_it_is_not_checked(tmp_path: Path) -> None:
    ctx = _context(tmp_path, ["tools/mise.toml"])
    draft = _command(_resolver(ctx, mise={"tools/mise.toml": MiseFile(frozenset({"x"}))}), _first("mise run deploy"))
    assert (draft.status, draft.detail) == ("not_checked", "no mise config where the doc runs it")


@pytest.mark.parametrize(
    ("text", "targets"),
    [
        ("PREFIX=x make x-build", MakeTargets(frozenset({"app-build"}), variables=frozenset({"PREFIX"}))),
        ("env PREFIX=x make x-build", MakeTargets(frozenset({"app-build"}), variables=frozenset({"PREFIX"}))),
        ("make ENV=prod prod-svc-up", MakeTargets(frozenset({"dev-svc-up"}), variables=frozenset({"NAME"}))),
        ("PREFIX=x make -e x-build", MakeTargets(frozenset({"app-build"}), variables=frozenset({"PREFIX"}))),
    ],
)
def test_a_command_that_sets_variables_leaves_variable_target_names_unproven(
    tmp_path: Path, text: str, targets: MakeTargets
) -> None:
    draft = _command(_resolver(_context(tmp_path, ["Makefile"]), make={"Makefile": targets}), _first(text))
    assert (draft.status, draft.checked) == ("not_checked", False)


def test_a_leading_dot_slash_is_not_part_of_a_goal(tmp_path: Path) -> None:
    resolver = _resolver(_context(tmp_path, ["Makefile"]), make={"Makefile": MakeTargets(frozenset({"out/app"}))})
    assert _command(resolver, _first("make ./out/app")).status == "ok"


@pytest.mark.parametrize("text", ["make TARGET", "npm run SCRIPT", "mise run TASK"])
def test_an_undefined_all_caps_name_is_a_placeholder(tmp_path: Path, text: str) -> None:
    ctx = _context(tmp_path, ["Makefile", "package.json", "mise.toml"])
    resolver = _resolver(
        ctx,
        scripts={"": frozenset({"build"})},
        make={"Makefile": MakeTargets(frozenset({"build"}))},
        mise={"mise.toml": MiseFile(frozenset({"build"}))},
    )
    assert (_command(resolver, _first(text)).status) == "skipped"


@pytest.mark.parametrize(("sets", "status"), [(True, "not_checked"), (False, "missing")])
def test_an_npmrc_that_sets_if_present_leaves_a_missing_script_unproven(
    tmp_path: Path, sets: bool, status: str
) -> None:
    ctx = _context(tmp_path, ["package.json", ".npmrc"])
    resolver = _resolver(ctx, scripts={"": frozenset({"lint"})}, npmrc={"": sets})
    assert _command(resolver, _first("npm run deploy")).status == status
    assert _command(resolver, _first("yarn deploy")).status == "not_checked"
