"""Frontend pages for the docs check: react-router paths from ``<Route>`` facts
(joined, parameterized, deduplicated), the provider's failure modes, and the
provider each ``[frontend.pages] kind`` gets."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from mcp_fixtures import LAYOUT, toolchain, write

from code_intel import ts_bridge
from code_intel.config import parse_config
from code_intel.doc_rules import CATCH_OPT, PARAM, FrontendPage
from code_intel.pages import ReactRouterPages, page_provider, route_pages
from code_intel.ts_facts import FactsCache, JsxElement

pytestmark = pytest.mark.usefixtures("ctime_follows_mtime")

NOW = 1_000_000_000_000_000_000
MODULE = "frontend/src/App.tsx"


def route(line: int, path: str | None = None, *, ancestor: int | None = None, column: int = 1) -> JsxElement:
    return JsxElement(
        tag="Route",
        line=line,
        column=column,
        kind="open",
        parent=None,
        snippet="<Route>",
        attributes={} if path is None else {"path": path},
        ancestor=None if ancestor is None else (ancestor, 1),
    )


def pages_of(*routes: JsxElement) -> list[tuple[tuple[str, ...], str, int]]:
    return [(p.segments, p.display, p.line) for p in route_pages(MODULE, routes)]


# ── Joining and segments ─────────────────────────────────────────────────


def test_a_nested_relative_path_joins_onto_its_parent() -> None:
    assert pages_of(route(1, "/items"), route(2, ":id", ancestor=1), route(3, ":id/edit", ancestor=1)) == [
        (("items",), "/items", 1),
        (("items", PARAM), "/items/:id", 2),
        (("items", PARAM, "edit"), "/items/:id/edit", 3),
    ]


def test_an_absolute_child_path_stands_alone() -> None:
    found = pages_of(route(1, "/items"), route(2, "/other", ancestor=1))
    assert [display for _, display, _ in found] == ["/items", "/other"]


def test_a_route_without_a_path_passes_its_parents_path_to_its_children() -> None:
    found = pages_of(
        route(1, "/shop"), route(2, ancestor=1), route(3, "cart", ancestor=2), route(4, "/", ancestor=None)
    )
    assert [(display, line) for _, display, line in found] == [("/shop", 1), ("/shop/cart", 3), ("/", 4)]


def test_a_pathless_layout_at_the_top_adds_nothing_to_its_children() -> None:
    found = pages_of(route(1), route(2, "/", ancestor=1), route(3, "/reports", ancestor=1))
    assert [display for _, display, _ in found] == ["/", "/reports"]
    assert found[0][0] == ()


def test_parameters_optional_segments_and_a_trailing_star() -> None:
    found = pages_of(route(1, "/files/:folder?"), route(2, "/docs/*"))
    assert [segments for segments, _, _ in found] == [("files",), ("files", PARAM), ("docs", CATCH_OPT)]


def test_the_not_found_route_is_not_a_page() -> None:
    assert pages_of(route(1, "*"), route(2, "/*"), route(3, "/real")) == [(("real",), "/real", 3)]


def test_one_url_declared_twice_keeps_the_first_declaration() -> None:
    assert pages_of(route(5, "/a"), route(9, "/a")) == [(("a",), "/a", 5)]


def test_an_enclosing_route_that_is_not_in_the_list_is_the_top() -> None:
    assert pages_of(route(2, "x", ancestor=1)) == [(("x",), "/x", 2)]


# ── The provider over a facts cache ──────────────────────────────────────


class FakeHelper:
    def __init__(self, routes: list[dict[str, Any]], *, syntax_errors: int = 0) -> None:
        self.routes = routes
        self.syntax_errors = syntax_errors
        self.runs = 0
        self.error: Exception | None = None

    def __call__(self, tc: Any, mode: str, request: dict, *, timeout_s: float) -> dict:
        self.runs += 1
        if self.error is not None:
            raise self.error
        entries = [
            {
                "path": path,
                "status": "ok",
                "error": None,
                "syntax_errors": self.syntax_errors,
                "first_syntax_error": {"line": 4, "column": 2, "message": "'>' expected."}
                if self.syntax_errors
                else None,
                "elements": [
                    {"tag": "Route", "kind": "open", "parent": None, "snippet": "<Route>", "column": 1, **r}
                    for r in self.routes
                ],
                "decls": [],
            }
            for path in request["files"]
        ]
        return {"protocol": 1, "mode": mode, "facts_schema": 1, "files": entries}


def provider(
    repo: Path, helper: FakeHelper, *, available: bool = True, public_dir: str | None = "frontend/public"
) -> ReactRouterPages:
    cache = FactsCache(
        repo,
        lambda: toolchain(repo, available=available),
        clock=lambda: NOW,
        run_helper=helper,
        attribute_tags=("Route",),
    )
    return ReactRouterPages(repo, (MODULE,), public_dir, cache)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    write(tmp_path / MODULE, "export {};\n")
    return tmp_path


TRACKED = frozenset({MODULE, "frontend/public/robots.txt"})
ROUTES = [
    {"line": 3, "attributes": {"path": "/"}, "ancestor": None},
    {"line": 4, "attributes": {"path": "items"}, "ancestor": {"line": 3, "column": 1}},
]


def test_the_pages_are_the_routes_then_the_public_files(repo: Path) -> None:
    found = provider(repo, FakeHelper(ROUTES)).pages(TRACKED)
    assert [(p.display, p.file, p.line) for p in found.pages] == [
        ("/", MODULE, 3),
        ("/items", MODULE, 4),
        ("/robots.txt", "frontend/public/robots.txt", 1),
    ]
    assert found.available and found.problems == ()


def test_a_second_call_reads_the_helper_cache(repo: Path) -> None:
    helper = FakeHelper(ROUTES)
    pages = provider(repo, helper)
    first = pages.pages(TRACKED)
    assert pages.pages(TRACKED) == first
    assert helper.runs == 1


def test_a_missing_toolchain_makes_the_pages_unavailable_with_a_limitation(repo: Path) -> None:
    helper = FakeHelper(ROUTES)
    found = provider(repo, helper, available=False).pages(TRACKED)
    assert not found.available
    assert [p.display for p in found.pages] == ["/robots.txt"]
    (problem,) = found.problems
    assert (problem.kind, problem.is_diagnostic) == ("frontend_pages_unavailable", False)
    assert "react-router pages were not read" in problem.message
    assert helper.runs == 0


def test_a_crashed_helper_makes_the_pages_unavailable_with_a_limitation(repo: Path) -> None:
    helper = FakeHelper(ROUTES)
    helper.error = ts_bridge.HelperError("facts", "exit", "boom")
    found = provider(repo, helper).pages(TRACKED)
    assert not found.available
    assert [p.kind for p in found.problems] == ["frontend_pages_unavailable"]
    assert "boom" in found.problems[0].message


def test_a_route_module_that_is_not_tracked_is_a_diagnostic(repo: Path) -> None:
    helper = FakeHelper(ROUTES)
    found = provider(repo, helper).pages(frozenset({"frontend/public/robots.txt"}))
    assert found.available
    assert [p.display for p in found.pages] == ["/robots.txt"]
    (problem,) = found.problems
    assert (problem.kind, problem.file, problem.is_diagnostic) == ("read_failed", MODULE, True)
    assert "not a tracked file" in problem.message
    assert helper.runs == 0


def test_a_syntax_error_in_the_route_module_is_a_diagnostic_and_the_pages_stay(repo: Path) -> None:
    found = provider(repo, FakeHelper(ROUTES, syntax_errors=1)).pages(TRACKED)
    assert [p.kind for p in found.problems] == ["typescript_syntax_error"]
    assert [p.display for p in found.pages][:2] == ["/", "/items"]


def test_without_a_public_dir_only_the_routes_are_pages(repo: Path) -> None:
    found = provider(repo, FakeHelper(ROUTES), public_dir=None).pages(TRACKED)
    assert [p.display for p in found.pages] == ["/", "/items"]


# ── Against the real helper ──────────────────────────────────────────────

APP = """\
export const App = () => (
  <Routes>
    <Route path="/login" element={<Login />} />
    <Route element={<Layout />}>
      <Route path="/" element={<Home />} />
      <Route path="/groups" element={<Groups />} />
      <Route path="/groups/:id" element={<Group />} />
    </Route>
    <Route path="/admin">
      <Route index element={<Admin />} />
      <Route path="users/:id?" element={<Users />} />
    </Route>
    <Route path="*" element={<NotFound />} />
  </Routes>
);
"""


def test_the_real_helper_gives_the_joined_pages(tmp_path: Path, typescript_toolchain: ts_bridge.Toolchain) -> None:
    write(tmp_path / MODULE, APP)
    cache = FactsCache(tmp_path, lambda: typescript_toolchain, attribute_tags=("Route",))
    found = ReactRouterPages(tmp_path, (MODULE,), None, cache).pages(frozenset({MODULE}))
    assert found.problems == ()
    assert [(p.segments, p.display, p.line) for p in found.pages] == [
        (("login",), "/login", 3),
        ((), "/", 5),
        (("groups",), "/groups", 6),
        (("groups", PARAM), "/groups/:id", 7),
        (("admin",), "/admin", 9),
        (("admin", "users"), "/admin/users/:id?", 11),
        (("admin", "users", PARAM), "/admin/users/:id?", 11),
    ]


# ── The provider for each kind ───────────────────────────────────────────


def config_for(pages: str):
    return parse_config(LAYOUT + f"[frontend.pages]\n{pages}\n", ".code-intel/config.toml", Path("/x/config.toml"))


@pytest.mark.parametrize(
    ("pages", "name"),
    [
        ('kind = "none"', "NoPages"),
        ('kind = "next-app"', "NextAppPages"),
        (f'kind = "react-router"\nmodules = ["{MODULE}"]', "ReactRouterPages"),
    ],
)
def test_the_provider_follows_the_pages_kind(pages: str, name: str) -> None:
    clock: Callable[[], int] = time.time_ns
    made = page_provider(Path("/x"), config_for(pages), lambda: toolchain(Path("/x")), clock)
    assert type(made).__name__ == name


def test_next_app_pages_come_from_the_frontends_app_directory() -> None:
    made = page_provider(Path("/x"), config_for('kind = "next-app"'), lambda: toolchain(Path("/x")), time.time_ns)
    found = made.pages(frozenset({"frontend/app/items/page.tsx"}))
    assert found.pages == (FrontendPage(("items",), "/items", "frontend/app/items/page.tsx"),)
