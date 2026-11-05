"""Tests for router_graph: nodes, include edges, Starlette order, snapshot
semantics, factories, mounts, escapes, limitations, the prefilter and the
per-module cache. Scenarios FastAPI can import also run as oracle cases
(test_fastapi_routes_oracle.py); these pin the analyzer's own rules."""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import pytest

from code_intel import include_model, router_bases, router_graph
from code_intel.backend_model import Located, Unknown
from code_intel.oracles.fastapi_cases import CASES
from code_intel.router_graph import BackendBuilder, build_backend_model

MAIN = "pkg.main:app"


def write(root: Path, files: dict[str, str]) -> None:
    for rel, source in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")


# The include model of the FastAPI version the backend locks (0.135.2), unless a test sets another.
COPY = include_model.for_version("0.135.2", source=None)
LIVE = include_model.for_version("0.143.0", source=None)
UNKNOWN = include_model.for_version(None, source=None, reason="no lock in this test")


def build(tmp_path: Path, files: dict[str, str], app: str = MAIN, **kwargs):
    write(tmp_path, {"pkg/__init__.py": "", **files})
    kwargs.setdefault("include_model", COPY)
    return build_backend_model(tmp_path, "pkg", app, tmp_path, **kwargs)


def guards(route) -> list[str]:
    """Route-level dependency targets as written (``Depends(x)`` -> ``x``), in order."""
    out: list[str] = []
    for layer in route.layers:
        assert not isinstance(layer.dependencies, Unknown), layer
        for loc in layer.dependencies:
            node = loc.node
            out.append(ast.unparse(node.args[0]) if isinstance(node, ast.Call) and node.args else ast.unparse(node))
    return out


def rows(model) -> list[tuple[str, str, str, list[str]]]:
    return [(r.method, r.path, r.handler, guards(r)) for r in model.routes]


def limits(model) -> list[tuple[str, str, int]]:
    return [(p.kind, p.file, p.line) for p in model.limitations]


DEPS = """
def verify_key(): ...
def audit(): ...
"""


# ── Each route keeps its own router's mount ───────────────────────────────


def test_two_routers_in_one_file_keep_their_own_mounts(tmp_path: Path) -> None:
    """With ``router`` and ``open_router`` in one file, ``/public`` stays under
    ``/open`` without the ``verify_key`` guard: each route follows its own
    router's include, never a mount inferred per file."""
    model = build(
        tmp_path,
        {
            "pkg/deps.py": DEPS,
            "pkg/routes/__init__.py": "",
            "pkg/routes/a.py": """
                from fastapi import APIRouter
                router = APIRouter()
                open_router = APIRouter()
                @router.get("/guarded")
                def guarded(): ...
                @open_router.get("/public")
                def public(): ...
                """,
            "pkg/main.py": """
                from fastapi import FastAPI, Depends
                from pkg.routes import a as a_mod
                from pkg.routes.a import router as a_router
                from pkg.deps import verify_key
                app = FastAPI()
                app.include_router(a_router, prefix="/admin", dependencies=[Depends(verify_key)])
                app.include_router(a_mod.open_router, prefix="/open")
                """,
        },
    )
    assert rows(model) == [
        ("GET", "/admin/guarded", "guarded", ["verify_key"]),
        ("GET", "/open/public", "public", []),
    ]
    assert model.limitations == () and model.diagnostics == ()
    assert [r.chain for r in model.routes] == [
        ("pkg.main:app", "pkg.routes.a:router"),
        ("pkg.main:app", "pkg.routes.a:open_router"),
    ]


def test_a_router_nested_in_the_same_file_gets_both_includes(tmp_path: Path) -> None:
    """``sub`` included into ``router`` in the same file gets the
    app's include guard, then the inner include's guard, in FastAPI's order."""
    model = build(
        tmp_path,
        {
            "pkg/deps.py": DEPS,
            "pkg/a.py": """
                from fastapi import APIRouter, Depends
                from pkg.deps import audit
                router = APIRouter()
                sub = APIRouter()
                @sub.get("/s")
                def s(): ...
                router.include_router(sub, prefix="/sub", dependencies=[Depends(audit)])
                """,
            "pkg/main.py": """
                from fastapi import FastAPI, Depends
                from pkg.a import router as a_router
                from pkg.deps import verify_key
                app = FastAPI()
                app.include_router(a_router, prefix="/a", dependencies=[Depends(verify_key)])
                """,
        },
    )
    assert rows(model) == [("GET", "/a/sub/s", "s", ["verify_key", "audit"])]
    [route] = model.routes
    assert [layer.via for layer in route.layers] == ["app", "router", "router", "router", "router", "route decorator"]
    assert [e.parent for e in model.edges] == ["pkg.a:router", "pkg.main:app"]


def test_no_mount_is_inferred_per_file(tmp_path: Path) -> None:
    """A router nobody includes is not live, even when a sibling router in the
    same file is: its routes are listed as unmounted with a limitation."""
    model = build(
        tmp_path,
        {
            "pkg/a.py": """
                from fastapi import APIRouter
                router = APIRouter(prefix="/live")
                orphan = APIRouter(prefix="/orphan")
                @router.get("/x")
                def x(): ...
                @orphan.get("/y")
                def y(): ...
                """,
            "pkg/main.py": """
                from fastapi import FastAPI
                from pkg.a import router
                app = FastAPI()
                app.include_router(router)
                """,
        },
    )
    assert rows(model) == [("GET", "/live/x", "x", [])]
    assert [(r.method, r.path) for r in model.unmounted_routes] == [("GET", "/orphan/y")]
    assert limits(model) == [("unmounted_router", "pkg/a.py", 3)]
    assert (
        "router pkg.a:orphan has 1 route(s), but no include_router or mount the analysis can follow reaches it "
        "from pkg.main:app" in model.limitations[0].message
    )


# ── Order and snapshots ──────────────────────────────────────────────────


def test_same_module_include_copies_only_earlier_registrations(tmp_path: Path) -> None:
    """FastAPI copies a child's routes when include_router runs: in the child's
    own module, registrations after the include are not part of it."""
    model = build(
        tmp_path,
        {
            "pkg/main.py": """
                from fastapi import FastAPI, APIRouter
                app = FastAPI()
                sub = APIRouter(prefix="/sub")
                @sub.get("/early")
                def early(): ...
                app.include_router(sub)
                @sub.get("/late")
                def late(): ...
                """,
        },
    )
    assert [r.path for r in model.routes] == ["/sub/early"]


def test_routes_follow_starlette_registration_order(tmp_path: Path) -> None:
    model = build(
        tmp_path,
        {
            "pkg/r.py": """
                from fastapi import APIRouter
                router = APIRouter()
                @router.get("/{item_id}")
                def by_id(): ...
                @router.get("/static")
                def static(): ...
                """,
            "pkg/main.py": """
                from fastapi import FastAPI
                from pkg.r import router
                app = FastAPI()
                @app.get("/first")
                def first(): ...
                app.include_router(router, prefix="/items")
                @app.get("/last")
                def last(): ...
                """,
        },
    )
    assert [(r.order, r.path) for r in model.routes] == [
        (0, "/first"),
        (1, "/items/{item_id}"),
        (2, "/items/static"),
        (3, "/last"),
    ]


def test_a_router_included_twice_gives_two_route_sets(tmp_path: Path) -> None:
    model = build(
        tmp_path,
        {
            "pkg/deps.py": DEPS,
            "pkg/v.py": """
                from fastapi import APIRouter
                router = APIRouter()
                @router.get("/items")
                def items(): ...
                """,
            "pkg/main.py": """
                from fastapi import FastAPI, Depends
                from pkg.v import router as v1
                from pkg.v import router as v2
                from pkg.deps import verify_key
                app = FastAPI()
                app.include_router(v1, prefix="/v1")
                app.include_router(v2, prefix="/v2", dependencies=[Depends(verify_key)])
                """,
        },
    )
    assert rows(model) == [("GET", "/v1/items", "items", []), ("GET", "/v2/items", "items", ["verify_key"])]
    assert model.limitations == ()


def test_stacked_decorators_register_bottom_up(tmp_path: Path) -> None:
    model = build(
        tmp_path,
        {
            "pkg/main.py": """
                from fastapi import FastAPI
                app = FastAPI()
                @app.get("/top")
                @app.post("/bottom")
                def both(): ...
                """,
        },
    )
    assert [(r.method, r.path) for r in model.routes] == [("POST", "/bottom"), ("GET", "/top")]
    assert {r.line for r in model.routes} == {5}


# ── Factories, aliases, programmatic registration ────────────────────────


def test_a_router_factory_makes_one_router_per_call_site(tmp_path: Path) -> None:
    model = build(
        tmp_path,
        {
            "pkg/f.py": """
                from fastapi import APIRouter
                def make(prefix, **kw):
                    r = APIRouter(prefix=prefix, **kw)
                    @r.get("/ping")
                    def ping(): ...
                    return r
                one = make("/one")
                two = make(prefix="/two", tags=["t"])
                """,
            "pkg/main.py": """
                from fastapi import FastAPI
                from pkg.f import one, two
                app = FastAPI()
                app.include_router(one)
                app.include_router(two)
                """,
        },
    )
    assert [r.path for r in model.routes] == ["/one/ping", "/two/ping"]
    assert sorted(n.id for n in model.routers) == ["pkg.f:one", "pkg.f:two", "pkg.main:app"]
    assert {r.handler_id for r in model.routes} == {"pkg.f:make.<locals>.ping"}


def test_an_app_factory_runs_its_includes(tmp_path: Path) -> None:
    model = build(
        tmp_path,
        {
            "pkg/items.py": """
                from fastapi import APIRouter
                router = APIRouter()
                @router.get("/i")
                def i(): ...
                """,
            "pkg/main.py": """
                from fastapi import FastAPI
                from pkg.items import router as items_router
                def create_app() -> FastAPI:
                    application = FastAPI(redirect_slashes=False)
                    application.include_router(items_router, prefix="/items")
                    return application
                app = create_app()
                """,
        },
    )
    assert [r.path for r in model.routes] == ["/items/i"]
    assert model.app_id == "pkg.main:app"
    assert model.redirect_slashes is False
    assert model.limitations == ()


def test_programmatic_registration_and_app_router_alias(tmp_path: Path) -> None:
    model = build(
        tmp_path,
        {
            "pkg/main.py": """
                from fastapi import FastAPI, APIRouter, WebSocket
                VERBS = ["PUT", "DELETE"]
                app = FastAPI()
                r = APIRouter(prefix="/p")
                def handler(): ...
                async def ws(websocket: WebSocket): ...
                r.add_api_route("/h", handler, methods=VERBS)
                r.add_api_websocket_route("/ws", ws)
                r.get("/manual")(handler)
                app.router.include_router(r)
                """,
        },
    )
    assert [(r.method, r.path, r.kind, r.handler) for r in model.routes] == [
        ("DELETE", "/p/h", "http", "handler"),
        ("PUT", "/p/h", "http", "handler"),
        ("WS", "/p/ws", "websocket", "ws"),
        ("GET", "/p/manual", "http", "handler"),
    ]
    assert {r.line for r in model.routes if r.handler == "handler"} == {5}
    assert model.routes[0].registered_line == 7


def test_a_lambda_endpoint_is_located_at_its_own_line(tmp_path: Path) -> None:
    """The id's ``@L`` number and the route's line are both the lambda's line,
    even when the registration call starts on an earlier line."""
    model = build(
        tmp_path,
        {
            "pkg/main.py": """
                from fastapi import FastAPI
                app = FastAPI()
                app.add_api_route(
                    "/ping",
                    lambda: {"ok": True},
                    methods=["GET"],
                )
                """,
        },
    )
    [route] = model.routes
    assert (route.handler, route.handler_id, route.line, route.registered_line) == (
        "<lambda>",
        "pkg.main:<lambda>@L5",
        5,
        3,
    )


def test_mounted_fastapi_app_starts_its_own_layers_and_sees_later_routes(tmp_path: Path) -> None:
    """``mount`` keeps a reference to the sub-app (unlike include_router's copy),
    so routes added to it later are live too."""
    model = build(
        tmp_path,
        {
            "pkg/deps.py": DEPS,
            "pkg/main.py": """
                from fastapi import FastAPI, Depends
                from fastapi.staticfiles import StaticFiles
                from pkg.deps import verify_key, audit
                app = FastAPI(dependencies=[Depends(verify_key)])
                sub = FastAPI(dependencies=[Depends(audit)])
                app.mount("/sub", sub)
                @sub.get("/ping")
                def ping(): ...
                app.mount("/static", StaticFiles(directory="/tmp"))
                """,
        },
    )
    assert rows(model) == [("GET", "/sub/ping", "ping", ["audit"])]
    assert limits(model) == [("asgi_mount", "pkg/main.py", 9)]
    assert model.routes[0].chain == ("pkg.main:app", "pkg.main:sub")


# ── Values ────────────────────────────────────────────────────────────────


def test_paths_and_prefixes_evaluate_constants_and_kwargs(tmp_path: Path) -> None:
    model = build(
        tmp_path,
        {
            "pkg/paths.py": 'API = "/api"\nITEMS = "/items"\n',
            "pkg/k.py": """
                from fastapi import APIRouter
                from pkg.paths import ITEMS
                _PREFIX = "/k"
                router = APIRouter(prefix=_PREFIX)
                @router.get(ITEMS)
                def a(): ...
                @router.get(path=f"{ITEMS}/{{item_id}}")
                def b(): ...
                """,
            "pkg/main.py": """
                from fastapi import FastAPI
                from pkg import paths
                from pkg.k import router
                VERSION = "v9"
                MOUNT = dict(prefix=paths.API + f"/{VERSION}")
                app = FastAPI()
                app.include_router(router, **MOUNT)
                """,
        },
    )
    assert [r.path for r in model.routes] == ["/api/v9/k/items", "/api/v9/k/items/{item_id}"]
    assert model.limitations == ()


def test_values_that_cannot_be_evaluated_are_reported_not_guessed(tmp_path: Path) -> None:
    model = build(
        tmp_path,
        {
            "pkg/main.py": """
                import os
                from fastapi import FastAPI, APIRouter
                from pkg.extra import COMMON
                app = FastAPI()
                r = APIRouter(prefix=os.environ["P"], dependencies=COMMON)
                @r.get(os.environ["PATH_X"])
                def x(): ...
                app.include_router(r, prefix=compute())
                """,
            "pkg/extra.py": "COMMON = build()\n",
        },
    )
    assert [r.path for r in model.routes] == [
        '<unresolved: compute()><unresolved: os.environ["P"]><unresolved: os.environ["PATH_X"]>'
    ]
    assert sorted(p.kind for p in model.limitations) == [
        "unresolved_dependencies",
        "unresolved_include_prefix",
        "unresolved_route_path",
        "unresolved_route_prefix",
    ]
    [route] = model.routes
    router_layer = route.layers[2]
    assert isinstance(router_layer.dependencies, Unknown)


# ── Includes that may not run ────────────────────────────────────────────


def test_conditional_and_deferred_includes_are_marked(tmp_path: Path) -> None:
    model = build(
        tmp_path,
        {
            "pkg/r.py": """
                from fastapi import APIRouter
                cond = APIRouter(prefix="/cond")
                later = APIRouter(prefix="/later")
                lazy = APIRouter(prefix="/lazy")
                for r in (cond, later, lazy):
                    pass
                @cond.get("/a")
                def a(): ...
                @later.get("/b")
                def b(): ...
                @lazy.get("/c")
                def c(): ...
                """,
            "pkg/main.py": """
                import os
                from fastapi import FastAPI
                from pkg.r import cond, later, lazy
                app = FastAPI()
                if os.environ.get("X"):
                    app.include_router(cond)
                def setup():
                    app.include_router(later)
                hook = lambda: app.include_router(lazy)
                """,
        },
    )
    # Code that may never run is ordered after everything that runs at import.
    assert [(r.path, r.mount) for r in model.routes] == [
        ("/cond/a", "conditional"),
        ("/lazy/c", "deferred"),
        ("/later/b", "deferred"),
    ]
    assert limits(model) == [
        ("conditional_router_mount", "pkg/main.py", 6),
        ("deferred_router_mount", "pkg/main.py", 8),
        ("deferred_router_mount", "pkg/main.py", 9),
    ]
    assert "inside setup()" in model.limitations[1].message
    assert "lambda" in model.limitations[2].message


def test_a_local_flag_name_guards_nothing_in_its_function(tmp_path: Path) -> None:
    """A parameter named ``TYPE_CHECKING`` is that function's own value: its
    block may run. The module-level block still never does."""
    model = build(
        tmp_path,
        {
            "pkg/main.py": """
                from typing import TYPE_CHECKING
                from fastapi import APIRouter, FastAPI
                router = APIRouter()
                def add(TYPE_CHECKING):
                    if TYPE_CHECKING:
                        @router.get("/f")
                        def f(): ...
                add(True)
                if TYPE_CHECKING:
                    @router.get("/t")
                    def t(): ...
                app = FastAPI()
                app.include_router(router)
                """,
        },
    )
    assert [r.path for r in model.routes] == ["/f"]


def test_routes_registered_in_code_that_may_never_run_are_reported(tmp_path: Path) -> None:
    model = build(
        tmp_path,
        {
            "pkg/main.py": """
                from fastapi import FastAPI
                app = FastAPI()
                @app.get("/now")
                def now(): ...
                def register():
                    @app.get("/later")
                    def later(): ...
                hook = lambda: app.add_api_route("/lazy", now)
                """,
        },
    )
    assert [(r.path, r.mount) for r in model.routes] == [
        ("/now", "static"),
        ("/lazy", "deferred"),
        ("/later", "deferred"),
    ]
    assert limits(model) == [
        ("deferred_route_registration", "pkg/main.py", 6),
        ("deferred_route_registration", "pkg/main.py", 8),
    ]
    assert "inside register()" in model.limitations[0].message
    assert "lambda" in model.limitations[1].message


def test_branch_routers_merge_only_when_neither_was_used_on_its_branch(tmp_path: Path) -> None:
    """Two branches building equally configured routers were merged into one of
    them, so routes registered on the other on its own branch vanished from the
    include (listed as an unmounted router). The merge now stands only while the
    dropped router has no use of its own; otherwise the name is unresolved and
    the include and later routes are reported."""
    model = build(
        tmp_path,
        {
            "pkg/main.py": """
                import os
                from fastapi import APIRouter, FastAPI
                app = FastAPI()
                if os.environ.get("X"):
                    router = APIRouter(prefix="/p")
                    @router.get("/a")
                    def a(): ...
                else:
                    router = APIRouter(prefix="/p")
                    @router.get("/b")
                    def b(): ...
                @router.get("/c")
                def c(): ...
                app.include_router(router)
                """,
        },
    )
    assert model.routes == ()
    assert [(k, line) for k, _, line in limits(model) if k != "unmounted_router"] == [
        ("unresolved_route_router", 12),
        ("unresolved_include", 14),
    ]

    same = build(
        tmp_path / "same",
        {
            "pkg/main.py": """
                import os
                from fastapi import APIRouter, FastAPI
                app = FastAPI()
                if os.environ.get("X"):
                    router = APIRouter(prefix="/p")
                else:
                    router = APIRouter(prefix="/p")
                @router.get("/c")
                def c(): ...
                app.include_router(router)
                """,
        },
    )
    assert [(r.path, r.mount) for r in same.routes] == [("/p/c", "static")]
    assert limits(same) == []


def test_an_unresolvable_include_is_reported(tmp_path: Path) -> None:
    model = build(
        tmp_path,
        {
            "pkg/main.py": """
                from fastapi import FastAPI
                from pkg.plugins import load
                app = FastAPI()
                app.include_router(load())
                app.include_router(missing_name)
                """,
            "pkg/plugins.py": "import importlib\ndef load():\n    return importlib.import_module('x').router\n",
        },
    )
    assert model.routes == ()
    assert [k for k, _, _ in limits(model)] == ["unresolved_include", "unresolved_include"]
    assert "load()" in model.limitations[0].message


def test_include_cycles_are_cut(tmp_path: Path) -> None:
    """Includes across modules copy every registration, so two routers that
    include each other would recurse; the closing edge is cut and reported.
    (In one module the copy-at-include snapshot already ends the recursion.)"""
    model = build(
        tmp_path,
        {
            "pkg/a.py": """
                from fastapi import APIRouter
                from pkg.b import b
                a = APIRouter(prefix="/a")
                a.include_router(b)
                """,
            "pkg/b.py": """
                from fastapi import APIRouter
                from pkg.a import a
                b = APIRouter(prefix="/b")
                @b.get("/x")
                def x(): ...
                b.include_router(a)
                """,
            "pkg/main.py": """
                from fastapi import FastAPI
                from pkg.a import a
                app = FastAPI()
                app.include_router(a)
                """,
        },
    )
    assert [r.path for r in model.routes] == ["/a/b/x"]
    assert limits(model) == [("router_include_cycle", "pkg/b.py", 6)]
    assert "pkg.b:b.include_router(pkg.a:a) closes an include cycle" in model.limitations[0].message


def test_a_same_module_mutual_include_is_no_cycle(tmp_path: Path) -> None:
    model = build(
        tmp_path,
        {
            "pkg/main.py": """
                from fastapi import FastAPI, APIRouter
                app = FastAPI()
                a = APIRouter(prefix="/a")
                b = APIRouter(prefix="/b")
                @b.get("/x")
                def x(): ...
                a.include_router(b)
                b.include_router(a)
                app.include_router(a)
                """,
        },
    )
    assert [r.path for r in model.routes] == ["/a/b/x"]
    assert model.limitations == ()


# ── Escapes ───────────────────────────────────────────────────────────────


ESCAPES = {
    "pkg/helpers.py": """
        import external_lib
        def read_state(app, origins):
            return [*origins, *app.state.allowed]
        def register(app):
            app.include_router(object())
        def forward(app):
            external_lib.configure(app)
        def chain(app):
            read_state(app, [])
        def strip(app):
            app.router.routes.clear()
        def setup(app):
            external_lib.configure(app)
            app.router.routes.clear()
        def ident(app):
            return app
        def alias(app):
            routes = app.router.routes
            routes.clear()
        def count(app):
            return len(app.router.routes)
        """,
}


@pytest.mark.parametrize(
    "call,expected",
    [
        ("read_state(app, ORIGINS)", []),
        ("chain(app)", []),
        ("isinstance(app, FastAPI)", []),
        ("register(app)", ["unresolved_include"]),
        ("forward(app)", ["opaque_router_call"]),
        ("external_lib.wire(app)", ["opaque_router_call"]),
        ("strip(app)", ["opaque_router_change"]),
        ("setup(app)", ["opaque_router_change"]),
        ("ident(app)", ["opaque_router_change"]),
        ("alias(app)", ["opaque_router_change"]),
        ("count(app)", []),
    ],
)
def test_a_router_passed_to_other_code_is_followed(tmp_path: Path, call: str, expected: list[str]) -> None:
    model = build(
        tmp_path,
        {
            **ESCAPES,
            "pkg/main.py": f"""
                import external_lib
                from fastapi import FastAPI
                from pkg.helpers import alias, chain, count, forward, ident, read_state, register, setup, strip
                ORIGINS = ["x"]
                app = FastAPI()
                {call}
                """,
        },
    )
    assert [k for k, _, _ in limits(model)] == expected
    if expected == ["opaque_router_call"]:
        assert model.limitations[0].line == 6
        assert "pkg.main:app is passed to" in model.limitations[0].message
    if expected == ["unresolved_include"]:
        # register() runs at import with `app`, so its include is read, and its child is not a router.
        assert "pkg.main:app.include_router(object())" in model.limitations[0].message


def test_the_instrumentation_allowlist_is_not_an_escape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files = {
        "pkg/main.py": """
            from fastapi import FastAPI
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
            app = FastAPI()
            FastAPIInstrumentor.instrument_app(app)
            """,
    }
    assert build(tmp_path, files).limitations == ()
    monkeypatch.setattr(router_graph, "ROUTER_SAFE_EXTERNAL_CALLS", frozenset())
    assert [k for k, _, _ in limits(build(tmp_path, files))] == ["opaque_router_call"]


def test_mutations_and_starlette_registrations_are_reported(tmp_path: Path) -> None:
    model = build(
        tmp_path,
        {
            "pkg/main.py": """
                from fastapi import FastAPI, Depends
                app = FastAPI()
                app.dependencies = []
                app.router.routes.append(object())
                app.add_route("/raw", lambda request: None)
                app.state.flag = True
                app.openapi = lambda: {}
                """,
        },
    )
    assert limits(model) == [
        ("opaque_router_change", "pkg/main.py", 3),
        ("opaque_router_change", "pkg/main.py", 4),
        ("unsupported_route_registration", "pkg/main.py", 5),
    ]


# ── Diagnostics ──────────────────────────────────────────────────────────


def test_a_broken_module_is_a_diagnostic_and_the_rest_is_analyzed(tmp_path: Path) -> None:
    model = build(
        tmp_path,
        {
            "pkg/broken.py": "def (:\n",
            "pkg/main.py": """
                from fastapi import FastAPI
                app = FastAPI()
                @app.get("/ok")
                def ok(): ...
                """,
        },
    )
    assert [r.path for r in model.routes] == ["/ok"]
    assert [(d.kind, d.step, d.file) for d in model.diagnostics] == [("parse_failed", "parse file", "pkg/broken.py")]


def test_a_router_replay_failure_is_named_per_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files = {
        "pkg/r.py": "from fastapi import APIRouter\nrouter = APIRouter()\n@router.get('/a')\ndef a(): ...\n",
        "pkg/main.py": "from fastapi import FastAPI\napp = FastAPI()\n@app.get('/ok')\ndef ok(): ...\n",
    }
    real = router_bases.RouterBases.__init__

    def fail(self, module):
        if module.name == "pkg.r":
            raise RecursionError("maximum recursion depth exceeded")
        real(self, module)

    monkeypatch.setattr(router_bases.RouterBases, "__init__", fail)
    model = build(tmp_path, files)
    assert [r.path for r in model.routes] == ["/ok"]
    assert [(d.kind, d.step, d.file) for d in model.diagnostics] == [("analysis_failed", "analyze module", "pkg/r.py")]
    assert "pkg/r.py: could not analyze module (RecursionError" in model.diagnostics[0].message


def test_an_app_that_cannot_be_resolved_is_a_diagnostic(tmp_path: Path) -> None:
    model = build(tmp_path, {"pkg/main.py": "app = make_app()\n"})
    assert model.routes == ()
    assert [(d.kind, d.step) for d in model.diagnostics] == [("unresolved_app", "resolve app")]
    assert model.diagnostics[0].message.startswith("pkg.main:app: could not resolve the app")


# ── Prefilter and cache ──────────────────────────────────────────────────


def _signature(model) -> tuple:
    def route(r):
        layers = tuple(
            (layer.via, layer.owner, "?" if isinstance(layer.dependencies, Unknown) else len(layer.dependencies))
            for layer in r.layers
        )
        return (r.method, r.path, r.kind, r.handler_id, r.file, r.line, r.chain, r.mount, r.order, layers)

    return (
        model.app_id,
        model.redirect_slashes,
        tuple(route(r) for r in model.routes),
        tuple(route(r) for r in model.unmounted_routes),
        tuple((n.id, n.file, n.line, n.kind, str(n.prefix), n.redirect_slashes) for n in model.routers),
        tuple((e.parent, e.child, str(e.prefix), e.file, e.line, e.mount) for e in model.edges),
        tuple(p.as_dict().items() for p in model.diagnostics),
        tuple(tuple(p.as_dict().items()) for p in model.limitations),
        model.files_scanned,
    )


@pytest.mark.parametrize("case_id", sorted(CASES))
def test_the_prefilter_never_changes_the_model(tmp_path: Path, case_id: str) -> None:
    case = CASES[case_id]
    write(tmp_path, case.files)
    package = case.app.split(".", 1)[0]
    filtered = build_backend_model(tmp_path, package, case.app, tmp_path)
    everything = build_backend_model(tmp_path, package, case.app, tmp_path, prefilter=False)
    assert _signature(filtered) == _signature(everything)


def test_the_prefilter_reaches_modules_that_only_import_a_router(tmp_path: Path) -> None:
    files = {
        "pkg/__init__.py": "",
        "pkg/r.py": "from fastapi import APIRouter\nrouter = APIRouter()\n",
        "pkg/uses.py": "from pkg.r import router\nfrom pkg.hooks import attach\nattach(router)\n",
        "pkg/plain.py": "VALUE = 1\n",
    }
    write(tmp_path, files)
    sources = router_graph.PySources(tmp_path, "pkg", tmp_path).load()
    uses_module, plain_module = sources.module("pkg.uses"), sources.module("pkg.plain")
    assert uses_module is not None and plain_module is not None
    uses = router_bases.prefilter_facts(uses_module)
    assert not uses.direct
    assert uses.touches(frozenset({("pkg.r", "router")}))
    assert not router_bases.prefilter_facts(plain_module).touches(frozenset({("pkg.r", "router")}))


def test_a_one_file_edit_rebuilds_to_the_same_model_as_a_cold_build(tmp_path: Path) -> None:
    case = CASES["c07"]
    write(tmp_path, case.files)
    package = case.app.split(".", 1)[0]
    builder = BackendBuilder(tmp_path, package, case.app, tmp_path, clock=lambda: 10**20)
    builder.build()
    leaf = tmp_path / package / "api" / "routes" / "pkg" / "leaf.py"
    leaf.write_text(leaf.read_text(encoding="utf-8") + "@router.get('/y')\ndef leaf_y(): ...\n", encoding="utf-8")
    warm = builder.build()
    assert builder.sources.changed == {f"{package}.api.routes.pkg.leaf"}
    cold = build_backend_model(tmp_path, package, case.app, tmp_path)
    assert _signature(warm) == _signature(cold)
    assert [r.path for r in warm.routes][:2] == ["/api/v1/c/items/x", "/api/v1/c/items/y"]


def test_the_model_carries_its_sources_and_resolver(tmp_path: Path) -> None:
    model = build(tmp_path, {"pkg/main.py": "from fastapi import FastAPI\nNAME = '/n'\napp = FastAPI()\n"})
    assert model.files_scanned == 2
    assert model.resolver.string(Located("pkg.main", ast.Name("NAME"), None)) == "/n"
    assert model.sources.module("pkg.main") is not None


# ── Include sites: which binding an include sees, and when it runs ──────

_TWO_ROUTERS = "".join(
    f"from fastapi import APIRouter\n{n} = APIRouter(prefix='/{n}')\n@{n}.get('/x')\ndef h_{n}(): ...\n"
    for n in ("orig", "other")
)
_HEAD = "from fastapi import FastAPI\nfrom pkg.r import orig as child, other\napp = FastAPI()\n"


def _include_site(tmp_path: Path, main: str) -> tuple[list[tuple[str, str]], list[str]]:
    model = build(tmp_path, {"pkg/r.py": _TWO_ROUTERS, "pkg/main.py": main})
    assert model.diagnostics == ()
    kinds = [p.kind for p in model.limitations if p.kind != "unmounted_router"]
    return [(r.path, r.mount) for r in model.routes], kinds


# Scenarios FastAPI cannot import (or whose branch it cannot take both ways)
# are pinned here; the rest run under the runtime as cases g02-g05.
@pytest.mark.parametrize(
    ("main", "routes", "kinds"),
    [
        # Two reaching bindings, or one the analysis cannot read: reported, never guessed.
        (_HEAD + "if FLAG:\n    child = other\napp.include_router(child)\n", [], ["unresolved_include"]),
        (
            _HEAD + "match other:\n    case child:\n        pass\napp.include_router(child)\n",
            [],
            ["unresolved_include"],
        ),
        (
            _HEAD + "def rebind():\n    global child\n    child = other\nrebind()\napp.include_router(child)\n",
            [],
            ["unresolved_include"],
        ),
        (_HEAD + "type child = other\napp.include_router(child)\n", [], ["unresolved_include"]),
        (
            "from fastapi import FastAPI\nfrom .......r import orig as child\napp = FastAPI()\napp.include_router(child)\n",
            [],
            ["unresolved_include"],
        ),
        # A later import does not reach an include that already ran.
        (
            "from fastapi import FastAPI\napp = FastAPI()\napp.include_router(child)\nfrom pkg.r import orig as child\n",
            [],
            ["unresolved_include"],
        ),
        # If the only binding is skipped, the include fails with NameError, so an
        # app that imports at all took it; the module it imports is still one
        # imported under a branch, so what that module registers is conditional.
        (
            "from fastapi import FastAPI\nimport os\nif os.environ.get('X') is None:\n    from pkg.r import orig as child\n"
            "app = FastAPI()\napp.include_router(child)\n",
            [("/orig/x", "conditional")],
            ["conditional_import"],
        ),
        # The last unconditional import wins.
        (
            "from fastapi import FastAPI\nfrom pkg.r import orig as child\nfrom pkg.r import other as child\n"
            "app = FastAPI()\napp.include_router(child)\n",
            [("/other/x", "static")],
            [],
        ),
        # Local names hide the module's: parameters, assignments, lambda
        # parameters, comprehension targets and `del`.
        (_HEAD + "def install(child):\n    app.include_router(child)\n", [], ["unresolved_include"]),
        (_HEAD + "def install():\n    child = make()\n    app.include_router(child)\n", [], ["unresolved_include"]),
        (_HEAD + "hook = lambda child: app.include_router(child)\n", [], ["unresolved_include"]),
        (_HEAD + "def install[child]():\n    app.include_router(child)\n", [], ["unresolved_include"]),
        (_HEAD + "class C[child]:\n    app.include_router(child)\n", [], ["unresolved_include"]),
        (_HEAD + "_ = [app.include_router(child) for child in [other]]\n", [], ["unresolved_include"]),
        (_HEAD + "def install():\n    app.include_router(child)\n    del child\n", [], ["unresolved_include"]),
        # A function called at import runs with the caller's router.
        (_HEAD + "def mount(r):\n    app.include_router(r)\nmount(other)\n", [("/other/x", "static")], []),
        # A function body sees the module binding, even one made below the def.
        (
            _HEAD + "def install():\n    app.include_router(child)\n",
            [("/orig/x", "deferred")],
            ["deferred_router_mount"],
        ),
        (
            "from fastapi import FastAPI\napp = FastAPI()\ndef install():\n    app.include_router(child)\n"
            "from pkg.r import orig as child\n",
            [("/orig/x", "deferred")],
            ["deferred_router_mount"],
        ),
        (
            _HEAD + "def outer():\n    def inner():\n        global child\n        app.include_router(child)\n",
            [("/orig/x", "deferred")],
            ["deferred_router_mount"],
        ),
        # A function's own import is what its include sees; other functions'
        # imports and deeper scopes do not touch it.
        (
            "from fastapi import FastAPI\nfrom pkg.r import other as child\napp = FastAPI()\n"
            "def a():\n    from pkg.r import orig as child\n    app.include_router(child)\n"
            "def b():\n    from pkg.r import other as child\n",
            [("/orig/x", "deferred")],
            ["deferred_router_mount"],
        ),
        (
            _HEAD + "def other_fn():\n    from pkg.r import other as child\n    return child\n"
            "def install():\n    app.include_router(child)\n",
            [("/orig/x", "deferred")],
            ["deferred_router_mount"],
        ),
        (
            _HEAD + "def install():\n    def inner():\n        child = other\n    app.include_router(child)\n",
            [("/orig/x", "deferred")],
            ["deferred_router_mount"],
        ),
        # A function body may run after a later module binding.
        (
            _HEAD + "def assemble():\n    app.include_router(child)\nchild = other\nassemble()\n",
            [],
            ["unresolved_include"],
        ),
        # Bindings in other scopes, or after the include, leave the module name alone.
        (
            _HEAD
            + "def _seed(x=(z := 1)):\n    child = other\nclass _C:\n    child = 'attr'\napp.include_router(child)\n",
            [("/orig/x", "static")],
            [],
        ),
        (_HEAD + "app.include_router(child)\ntype child = other\n", [("/orig/x", "static")], []),
        (
            _HEAD + "def f(x):\n    match x:\n        case child:\n            pass\napp.include_router(child)\n",
            [("/orig/x", "static")],
            [],
        ),
        # Any function or method that rebinds the name through `global` may have run.
        (
            _HEAD + "def deco(fn):\n    global child\n    child = other\n    return fn\n@deco\ndef g(): ...\n"
            "app.include_router(child)\n",
            [],
            ["unresolved_include"],
        ),
        (
            _HEAD
            + "class C:\n    def m(self):\n        global child\n        child = other\napp.include_router(child)\n",
            [],
            ["unresolved_include"],
        ),
    ],
    ids=[
        "conditional-rebind",
        "match-capture",
        "global-helper",
        "type-alias",
        "relative-import-too-deep",
        "include-before-import",
        "conditional-only-import",
        "two-imports",
        "parameter",
        "local-assignment",
        "lambda-parameter",
        "function-type-parameter",
        "class-type-parameter",
        "comprehension-target",
        "del",
        "parameter-from-caller",
        "function-body",
        "function-above-import",
        "nested-global",
        "own-local-import",
        "sibling-function-import",
        "deeper-scope-binding",
        "later-module-binding",
        "inert-default-and-class-bindings",
        "type-alias-after-include",
        "match-in-function",
        "decorator-global",
        "method-global",
    ],
)
def test_an_include_sees_only_the_binding_live_when_it_runs(
    tmp_path: Path, main: str, routes: list, kinds: list[str]
) -> None:
    assert _include_site(tmp_path, main) == (routes, kinds)


@pytest.mark.parametrize(
    ("statement", "mount"),
    [
        ("try:\n    pass\nexcept ValueError:\n    app.include_router(child)\n", "conditional"),
        ("match 1:\n    case 1:\n        app.include_router(child)\n", "conditional"),
        ("try:\n    pass\nexcept ValueError:\n    pass\nelse:\n    app.include_router(child)\n", "conditional"),
        ("x = app.include_router(child) if FLAG else None\n", "conditional"),
        ("x = FLAG and app.include_router(child)\n", "conditional"),
        ("x = [app.include_router(child) for _ in range(1)]\n", "conditional"),
        ("x = 1 < 2 < app.include_router(child)\n", "conditional"),
        ("x = (app.include_router(child) for _ in range(1))\n", "deferred"),
        ("def f(x: app.include_router(child)): ...\n", "deferred"),
        ("x: app.include_router(child)\n", "deferred"),
        ("def f[T: app.include_router(child)](): ...\n", "deferred"),
        ("type X = app.include_router(child)\n", "deferred"),
        ("async def install():\n    app.include_router(child)\n", "deferred"),
        ("def install():\n    if FLAG:\n        app.include_router(child)\n", "deferred"),
        ("if FLAG:\n    hook = lambda: app.include_router(child)\n", "deferred"),
        ("hook = lambda x=app.include_router(child): x\n", "static"),
        ("try:\n    app.include_router(child)\nexcept ValueError:\n    pass\n", "static"),
        ("with ctx():\n    app.include_router(child)\n", "static"),
    ],
    ids=[
        "except",
        "match-case",
        "try-else",
        "ternary",
        "short-circuit",
        "comprehension-element",
        "chained-comparison",
        "generator",
        "annotation",
        "variable-annotation",
        "type-parameter-bound",
        "type-alias-value",
        "async-def",
        "branch-in-def",
        "lambda-under-if",
        "lambda-default",
        "try-body",
        "with-body",
    ],
)
def test_an_include_is_marked_by_when_it_may_run(tmp_path: Path, statement: str, mount: str) -> None:
    """Code that may be skipped is conditional; code that runs later or never
    (a function or lambda body, a generator, an annotation) is deferred; an
    include that may not run is listed and reported, never dropped."""
    routes, kinds = _include_site(tmp_path, _HEAD + statement)
    assert routes == [("/orig/x", mount)]
    expected = {"static": [], "conditional": ["conditional_router_mount"], "deferred": ["deferred_router_mount"]}
    assert kinds == expected[mount]


# ── Execution order and functions run at import ─────────────────────────


def test_a_registration_that_can_only_run_after_an_include_is_left_out_of_it(tmp_path: Path) -> None:
    """A function nothing calls at import registers on a router the app included
    at import: if it ever runs, FastAPI has already copied the router's routes,
    so the route is not the app's. It is left out and reported, naming the include."""
    model = build(
        tmp_path,
        {
            "pkg/main.py": """
                from fastapi import APIRouter, FastAPI
                router = APIRouter()
                app = FastAPI()
                app.include_router(router)
                def later():
                    @router.get("/f")
                    def f(): ...
                """,
        },
    )
    assert model.routes == ()
    assert [(k, line) for k, _, line in limits(model)] == [("deferred_route_registration", 6)]
    message = model.limitations[0].message
    assert "runs inside later(), which nothing calls at import" in message
    assert "pkg.main:app.include_router(pkg.main:router) ran at import (pkg/main.py:4)" in message
    assert "not served through that include" in message


def test_a_registration_whose_order_is_unknown_is_listed_as_deferred(tmp_path: Path) -> None:
    """A lambda may run at import before or after the include, and a function
    nothing calls registers on a router included in another function nothing
    calls: the analysis cannot order them, so the routes are listed as deferred
    and reported."""
    model = build(
        tmp_path,
        {
            "pkg/main.py": """
                from fastapi import APIRouter, FastAPI
                router = APIRouter()
                other = APIRouter()
                def handler(): ...
                hook = lambda: router.add_api_route("/hooked", handler)
                app = FastAPI()
                app.include_router(router)
                def install():
                    app.include_router(other)
                def later():
                    other.add_api_route("/late", handler)
                """,
        },
    )
    assert sorted((r.path, r.mount) for r in model.routes) == [("/hooked", "deferred"), ("/late", "deferred")]
    assert sorted(k for k, _, _ in limits(model)) == [
        "deferred_route_registration",
        "deferred_route_registration",
        "deferred_router_mount",
    ]


def test_a_recursive_factory_is_reported_not_followed(tmp_path: Path) -> None:
    model = build(
        tmp_path,
        {
            "pkg/main.py": """
                from fastapi import APIRouter, FastAPI
                def make(n):
                    r = APIRouter()
                    r.include_router(make(n - 1))
                    return r
                app = FastAPI()
                app.include_router(make(3))
                """,
        },
    )
    assert model.routes == ()
    assert [(p.kind, p.line) for p in model.limitations if p.kind == "recursive_router_call"] == [
        ("recursive_router_call", 4)
    ]
    messages = [p.message for p in model.limitations if p.kind == "unresolved_include"]
    assert any("recursive call: make() is already running in this call chain" in m for m in messages)


def test_mutually_recursive_functions_run_once_per_call_chain(tmp_path: Path) -> None:
    """FastAPI registers /done once; following ping -> pong -> ping again would
    register it once per round."""
    model = build(
        tmp_path,
        {
            "pkg/main.py": """
                from fastapi import APIRouter, FastAPI
                router = APIRouter()
                def ping(n):
                    if n <= 0:
                        @router.get("/done")
                        def done(): ...
                        return
                    pong(n - 1)
                def pong(n):
                    ping(n)
                ping(3)
                app = FastAPI()
                app.include_router(router)
                """,
        },
    )
    assert [r.path for r in model.routes] == ["/done"]
    [problem] = [p for p in model.limitations if p.kind == "recursive_router_call"]
    assert (problem.line, "ping() is already running in this call chain" in problem.message) == (10, True)


def test_a_call_chain_past_the_depth_limit_is_reported(tmp_path: Path) -> None:
    depth = router_graph.FRAME_MAX_DEPTH + 1
    chain = "".join(f"def f{i}():\n    f{i + 1}()\n" for i in range(depth))
    main = (
        "from fastapi import APIRouter, FastAPI\nrouter = APIRouter()\n"
        + chain
        + f"def f{depth}():\n    router.add_api_route('/deep', lambda: 1)\n"
        + "f0()\napp = FastAPI()\napp.include_router(router)\n"
    )
    model = build(tmp_path, {"pkg/main.py": main})
    # The innermost body still runs as code nothing is known to call.
    assert [(r.path, r.mount) for r in model.routes] == [("/deep", "deferred")]
    [problem] = [p for p in model.limitations if p.kind == "router_call_limit"]
    assert f"more than {router_graph.FRAME_MAX_DEPTH} function calls deep" in problem.message


def _fan_out(root: Path, calls: int, levels: int = 5) -> Path:
    """Functions f0..f{levels} where each calls the next ``calls`` times: the
    call chains grow as ``calls ** levels``."""
    body = "".join(f"def f{i}():\n" + "".join(f"    f{i + 1}()\n" for _ in range(calls)) for i in range(levels))
    write(
        root,
        {
            "pkg/__init__.py": "",
            "pkg/main.py": "from fastapi import APIRouter, FastAPI\nrouter = APIRouter()\n"
            + body
            + f"def f{levels}():\n    router.add_api_route('/x', lambda: 1)\n"
            + "f0()\napp = FastAPI()\napp.include_router(router)\n",
        },
    )
    return root


def _recursive(root: Path, calls: int) -> Path:
    """One function calling itself ``calls`` times."""
    body = "".join(f"    build(n - 1, path + '/{k}')\n" for k in range(calls))
    write(
        root,
        {
            "pkg/__init__.py": "",
            "pkg/main.py": "from fastapi import APIRouter, FastAPI\nrouter = APIRouter()\n"
            "def build(n, path):\n    if n <= 0:\n        router.add_api_route(path, lambda: 1)\n        return\n"
            + body
            + "build(1, '')\napp = FastAPI()\napp.include_router(router)\n",
        },
    )
    return root


def test_fan_out_past_the_call_chain_budget_is_reported(tmp_path: Path) -> None:
    model = build_backend_model(_fan_out(tmp_path, 6), "pkg", "pkg.main:app", tmp_path)
    assert any(
        p.kind == "router_call_limit" and f"past the {router_graph.FRAME_BUDGET} call chains" in p.message
        for p in model.limitations
    )


@pytest.mark.parametrize("make", [_fan_out, _recursive], ids=["fan-out", "recursion"])
def test_the_work_of_running_functions_at_import_is_bounded(tmp_path: Path, make) -> None:
    """Each call chain runs its function's body again: without the recursion
    check and the call chain budget, doubling the calls per body multiplies the
    work by 2 ** depth."""
    import scaling_check

    def made(n: int) -> Path:
        return make(tmp_path / f"n{n}", n)

    def run(root: Path) -> None:
        build_backend_model(root, "pkg", "pkg.main:app", root)

    scaling_check.assert_scales_linearly(made, run, 8)


def _many_calls(root: Path, calls: int) -> Path:
    """``calls`` top-level calls in one module to a function that registers a route."""
    write(
        root,
        {
            "pkg/__init__.py": "",
            "pkg/main.py": "from fastapi import APIRouter, FastAPI\nrouter = APIRouter()\n"
            "def add(path):\n    router.add_api_route(path, lambda: 1)\n"
            + "".join(f"add('/r{k}')\n" for k in range(calls))
            + "app = FastAPI()\napp.include_router(router)\n",
        },
    )
    return root


def test_calls_past_the_budget_are_one_limitation(tmp_path: Path) -> None:
    calls = router_graph.FRAME_BUDGET + 50
    model = build_backend_model(_many_calls(tmp_path, calls), "pkg", MAIN, tmp_path)
    [limit] = [p for p in model.limitations if p.kind == "router_call_limit"]
    assert limit.message.startswith("50 call(s) are past the")


def test_calls_past_the_budget_cost_no_more_than_the_ones_within_it(tmp_path: Path) -> None:
    """Three times the calls, most of them past the budget, take at most about
    three times as long: a skipped call is not re-read or reported on its own."""
    import scaling_check

    small, large = _many_calls(tmp_path / "small", 4000), _many_calls(tmp_path / "large", 12000)

    def run(root: Path) -> None:
        build_backend_model(root, "pkg", MAIN, root)

    # best_time pauses the collector around each measurement.
    ratio = scaling_check.best_time(run, large, repeats=2) / scaling_check.best_time(run, small, repeats=2)
    assert ratio < 6, f"three times the calls took {ratio:.2f}x as long"


def test_a_deferred_route_reached_by_several_chains_is_listed_once(tmp_path: Path) -> None:
    from code_intel.oracles.fastapi_cases import _chain

    model = build(tmp_path, {"pkg/main.py": _chain(20)})
    assert [(r.path, r.mount) for r in model.routes] == [("/leaf", "deferred")]
    assert [p.kind for p in model.limitations] == ["deferred_route_registration", "router_call_limit"]


@pytest.mark.parametrize(
    "imports",
    [
        "if os.environ.get('X'):\n    from . import b  # noqa\napp = FastAPI()\napp.include_router(router)\n"
        "from . import b as b2  # noqa\n",
        "if os.environ.get('X'): from . import b  # noqa\napp = FastAPI()\napp.include_router(router)\n",
    ],
    ids=["imported-again-after-the-include", "one-line-if"],
)
def test_a_module_first_imported_under_a_branch_is_conditional(tmp_path: Path, imports: str) -> None:
    """Whether its routes exist when the include runs depends on the branch,
    so they are conditional even if the module is imported again later."""
    model = build(
        tmp_path,
        {
            "pkg/a.py": "from fastapi import APIRouter\nrouter = APIRouter()\n@router.get('/a')\ndef a(): ...\n",
            "pkg/b.py": "from .a import router\n@router.get('/b')\ndef b(): ...\n",
            "pkg/main.py": "import os\nfrom fastapi import FastAPI\nfrom .a import router\n" + imports,
        },
    )
    assert [(r.path, r.mount) for r in model.routes] == [("/a", "static"), ("/b", "conditional")]
    line = 5 if "\n    from" in imports else 4
    assert limits(model) == [("conditional_import", "pkg/main.py", line)]


def test_a_long_chain_of_re_exports_resolves_without_deep_recursion(tmp_path: Path) -> None:
    hops = 400
    files = {"pkg/__init__.py": "", "pkg/m0.py": "from fastapi import APIRouter\nrouter = APIRouter()\n"}
    files["pkg/m0.py"] += "@router.get('/x')\ndef x(): ...\n"
    files.update({f"pkg/m{i}.py": f"from .m{i - 1} import router\n" for i in range(1, hops)})
    files["pkg/main.py"] = (
        f"from fastapi import FastAPI\nfrom .m{hops - 1} import router\napp = FastAPI()\napp.include_router(router)\n"
    )
    write(tmp_path, files)
    model = build_backend_model(tmp_path, "pkg", MAIN, tmp_path)
    assert [r.path for r in model.routes] == ["/x"]
    assert model.limitations == ()


def test_messages_name_the_argument_and_the_part_that_failed(tmp_path: Path) -> None:
    model = build(
        tmp_path,
        {
            "pkg/routers.py": """
                from fastapi import APIRouter
                class Base(APIRouter):
                    def __init__(self, **kw):
                        kw.setdefault("prefix", "/base")
                        super().__init__(**kw)
                class Leaf(Base):
                    pass
                """,
            "pkg/main.py": """
                from fastapi import APIRouter, FastAPI
                from .routers import Leaf
                class Cfg:
                    prefix = "/old"
                Cfg.prefix = "/new"
                router = Leaf()
                other = APIRouter(prefix=Cfg.prefix)
                def add(path):
                    @other.get(path)
                    def h(): ...
                for p in ("/a", "/b"):
                    add(p)
                app = FastAPI()
                app.include_router(router)
                app.include_router(other)
                """,
        },
    )
    messages = sorted(p.message for p in model.limitations)
    assert any(
        m.startswith("router pkg.main:router is built by class Leaf, which overrides Base.__init__") for m in messages
    )
    assert "router pkg.main:router: its prefix could not be evaluated (Leaf.__init__ may set it)" in "\n".join(messages)
    assert "router pkg.main:other: prefix=Cfg.prefix could not be evaluated (Cfg: changed in place)" in "\n".join(
        messages
    )
    assert "@other.get(...) on h(): its path could not be evaluated (p: loop target)" in "\n".join(messages)


def test_routers_made_by_one_factory_on_one_line_get_distinct_ids(tmp_path: Path) -> None:
    """A second router from the same line is told apart by the call's column."""
    model = build(
        tmp_path,
        {
            "pkg/main.py": """
                from fastapi import APIRouter, FastAPI
                def make(p):
                    return APIRouter(prefix=p)
                app = FastAPI()
                app.include_router(make("/a")); app.include_router(make("/b")); app.include_router(make("/c"))
                """,
        },
    )
    ids = [n.id for n in model.routers]
    assert len(ids) == len(set(ids)) == 4
    assert sorted((n.id, n.prefix) for n in model.routers if n.kind == "APIRouter") == [
        ("pkg.main:L5", "/a"),
        ("pkg.main:L5:51", "/b"),
        ("pkg.main:L5:83", "/c"),
    ]


def test_a_local_assigned_twice_is_reported_not_read_as_the_module_name(tmp_path: Path) -> None:
    model = build(
        tmp_path,
        {
            "pkg/main.py": """
                from fastapi import APIRouter, FastAPI
                PATH = "/module"
                def make(flag):
                    PATH = "/a"
                    if flag:
                        PATH = "/b"
                    r = APIRouter()
                    @r.get(PATH)
                    def x(): ...
                    return r
                app = FastAPI()
                app.include_router(make(True))
                """,
        },
    )
    assert [r.path for r in model.routes] == ["<unresolved: PATH>"]
    (limit,) = model.limitations
    assert limit.kind == "unresolved_route_path"
    assert "local variable of make(), assigned more than once" in limit.message


def test_a_module_nothing_imports_changes_no_live_router(tmp_path: Path) -> None:
    model = build(
        tmp_path,
        {
            "pkg/a.py": """
                from fastapi import APIRouter
                router = APIRouter()
                @router.get("/a")
                def a(): ...
                """,
            "pkg/extra.py": """
                from pkg.a import router
                @router.get("/extra")
                def extra(): ...
                """,
            "pkg/main.py": """
                from fastapi import FastAPI
                from pkg.a import router
                app = FastAPI()
                app.include_router(router)
                """,
        },
    )
    assert [r.path for r in model.routes] == ["/a"]
    assert limits(model) == [("unimported_router_change", "pkg/extra.py", 2)]
    assert "pkg.extra is not imported when pkg.main:app is imported" in model.limitations[0].message


def test_an_app_passed_to_an_outside_library_is_one_limitation(tmp_path: Path) -> None:
    """The Echo shape: under a setting, the app is handed to a package function
    that hands it to an outside library's object. That call is the one place
    routes may come from that the analysis cannot see, so it is reported once,
    at the call in the app module."""
    model = build(
        tmp_path,
        {
            "pkg/echo.py": """
                from importlib import import_module
                def _echo_app():
                    return import_module("echo_service.app")
                def mount_echo(app, path):
                    _echo_app().mount_echo(app, path)
                    app.state.authenticator = object()
                """,
            "pkg/routes.py": """
                from fastapi import APIRouter
                router = APIRouter(prefix="/r")
                @router.get("/x")
                def x(): ...
                """,
            "pkg/main.py": """
                from fastapi import FastAPI
                from pkg.echo import mount_echo
                from pkg.routes import router
                ECHO = "/etc/echo.toml"
                app = FastAPI(redirect_slashes=False)
                if ECHO:
                    mount_echo(app, ECHO)
                app.include_router(router)
                """,
        },
    )
    assert [r.path for r in model.routes] == ["/r/x"]
    assert limits(model) == [("opaque_router_call", "pkg/main.py", 7)]
    assert "is passed to mount_echo(...)" in model.limitations[0].message


@pytest.mark.parametrize(
    ("reaches_it", "reason"),
    [
        ('TARGET = "pkg.hooks:later"\n', "later is used at pkg/main.py:2 other than as a call the analysis follows"),
        ('TARGET = "pkg.hooks.later"\n', "later is used at pkg/main.py:2 other than as a call the analysis follows"),
        (
            "print(hooks)\n",
            "every name of pkg.hooks is reachable (module pkg.hooks is used as a value at pkg/main.py:2)",
        ),
        ("from inspect import getmembers as gm\ngm(hooks)\n", "inspect.getmembers() at pkg/main.py:3"),
        ("import importlib as il\nil.import_module(NAME)\n", "importlib.import_module() at pkg/main.py:3"),
        ("import sys\nsys.modules\n", "sys.modules at pkg/main.py:3"),
        ("dir(hooks)\n", "dir(...) at pkg/main.py:2"),
    ],
)
def test_every_way_to_reach_a_function_keeps_its_route_deferred(tmp_path: Path, reaches_it: str, reason: str) -> None:
    """A string naming the function, its module object used as a value, or a
    dynamic lookup (also through an import alias or an attribute call) may run
    it at import before the include, so its route is never left out."""
    model = build(
        tmp_path,
        {
            "pkg/hooks.py": """
                from fastapi import APIRouter
                child = APIRouter()
                def handler(): ...
                def later():
                    child.add_api_route("/late", handler)
                """,
            "pkg/main.py": "from pkg import hooks\n"
            + reaches_it
            + "from fastapi import FastAPI\napp = FastAPI()\napp.include_router(hooks.child)\n",
        },
    )
    assert [(r.path, r.mount) for r in model.routes] == [("/late", "deferred")]
    (message,) = [p.message for p in model.limitations if p.kind == "deferred_route_registration"]
    assert reason in message


def test_a_module_read_only_through_constant_attributes_does_not_hide_its_functions(tmp_path: Path) -> None:
    """``hooks.child`` and ``import_module("pkg.hooks").child`` read one name
    each, so ``later`` still provably runs only after the include."""
    model = build(
        tmp_path,
        {
            "pkg/hooks.py": """
                from fastapi import APIRouter
                child = APIRouter()
                def handler(): ...
                def later():
                    child.add_api_route("/late", handler)
                """,
            "pkg/main.py": """
                import importlib
                from pkg import hooks
                from fastapi import FastAPI
                app = FastAPI()
                app.include_router(hooks.child)
                importlib.import_module("pkg.hooks").child
                """,
        },
    )
    assert model.routes == ()
    assert [p.kind for p in model.limitations] == ["deferred_route_registration"]
    assert "not served through that include" in model.limitations[0].message


@pytest.mark.parametrize(
    ("lookup", "reason"),
    [
        (
            'import importlib.metadata\nfor ep in importlib.metadata.entry_points(group="pkg.plugins"):\n    ep.load()()\n',
            "importlib.metadata.entry_points() at pkg/main.py:3",
        ),
        (
            'from importlib.metadata import entry_points\nfor ep in entry_points(group="pkg.plugins"):\n    ep.load()()\n',
            "importlib.metadata.entry_points() at pkg/main.py:3",
        ),
        (
            'import pkg_resources\nfor ep in pkg_resources.iter_entry_points("pkg.plugins"):\n    ep.load()()\n',
            "pkg_resources.iter_entry_points() at pkg/main.py:3",
        ),
    ],
)
def test_an_entry_point_lookup_keeps_a_route_deferred(tmp_path: Path, lookup: str, reason: str) -> None:
    """Entry points name their target (``late = pkg.hooks:later``) in package
    metadata, outside the source, so loading them may run any function at
    import: the route stays listed, and the reason names the lookup."""
    model = build(
        tmp_path,
        {
            "pkg/hooks.py": """
                from fastapi import APIRouter
                child = APIRouter()
                def handler(): ...
                def later():
                    child.add_api_route("/late", handler)
                """,
            "pkg/main.py": "from pkg import hooks\n"
            + lookup
            + "from fastapi import FastAPI\napp = FastAPI()\napp.include_router(hooks.child)\n",
        },
    )
    assert [(r.path, r.mount) for r in model.routes] == [("/late", "deferred")]
    (message,) = [p.message for p in model.limitations if p.kind == "deferred_route_registration"]
    assert reason in message


def test_a_call_in_an_evaluated_string_annotation_is_a_use(tmp_path: Path) -> None:
    """``def g(x: "later()")`` runs ``later()`` when the annotation is evaluated
    (typing.get_type_hints, FastAPI reading a signature), so the route stays listed."""
    model = build(
        tmp_path,
        {
            "pkg/main.py": """
                from fastapi import APIRouter, FastAPI
                child = APIRouter()
                def handler(): ...
                def later():
                    child.add_api_route("/late", handler)
                def g(x: "later()"): ...
                app = FastAPI()
                app.include_router(child)
                """,
        },
    )
    assert [(r.path, r.mount) for r in model.routes] == [("/late", "deferred")]
    (message,) = [p.message for p in model.limitations if p.kind == "deferred_route_registration"]
    assert "later is used at pkg/main.py:6 other than as a call the analysis follows" in message


def test_methods_none_is_fastapis_default_get(tmp_path: Path) -> None:
    """``methods=None``, written out, through a name or from ``**`` arguments,
    is FastAPI's default: GET, not ANY, and nothing is reported."""
    model = build(
        tmp_path,
        {
            "pkg/main.py": """
                from fastapi import APIRouter, FastAPI
                router = APIRouter()
                NONE = None
                OPTS = {"methods": None}
                def h(): ...
                router.add_api_route("/literal", h, methods=None)
                router.add_api_route("/name", h, methods=NONE)
                router.add_api_route("/kwargs", h, **OPTS)
                router.add_api_route("/kwargs-set", h, **{"methods": ["put"]})
                @router.api_route("/decorated", methods=None)
                def decorated(): ...
                app = FastAPI()
                app.include_router(router)
                """,
        },
    )
    assert [(r.method, r.path) for r in model.routes] == [
        ("GET", "/literal"),
        ("GET", "/name"),
        ("GET", "/kwargs"),
        ("PUT", "/kwargs-set"),
        ("GET", "/decorated"),
    ]
    assert model.limitations == ()


def test_methods_from_unknown_kwargs_are_reported_as_any(tmp_path: Path) -> None:
    model = build(
        tmp_path,
        {
            "pkg/main.py": """
                from fastapi import APIRouter, FastAPI
                router = APIRouter()
                def h(): ...
                def opts(): return {}
                router.add_api_route("/x", h, **opts())
                app = FastAPI()
                app.include_router(router)
                """,
        },
    )
    assert [(r.method, r.path) for r in model.routes] == [("ANY", "/x")]
    (problem,) = [p for p in model.limitations if p.kind == "unresolved_route_methods"]
    assert "router.add_api_route(...): **opts() could not be evaluated" in problem.message


def test_methods_set_by_a_kwargs_mapping_are_named_with_their_source(tmp_path: Path) -> None:
    model = build(
        tmp_path,
        {
            "pkg/main.py": """
                from fastapi import APIRouter, FastAPI
                router = APIRouter()
                def h(): ...
                OPTS = {"methods": "GET"}
                router.add_api_route("/x", h, **OPTS)
                app = FastAPI()
                app.include_router(router)
                """,
        },
    )
    assert [(r.method, r.path) for r in model.routes] == [("ANY", "/x")]
    (problem,) = [p for p in model.limitations if p.kind == "unresolved_route_methods"]
    assert 'methods="GET" (from **OPTS) could not be evaluated (not a sequence)' in problem.message


# ── The include model of the locked FastAPI version ──────────────────────

LATE = {
    "pkg/deps.py": DEPS,
    "pkg/items.py": """
        from fastapi import APIRouter
        router = APIRouter(prefix="/items")
        @router.get("/early")
        def early(): ...
        """,
    "pkg/main.py": """
        from fastapi import FastAPI, Depends
        from pkg.deps import verify_key
        from pkg.items import router
        app = FastAPI()
        app.include_router(router, prefix="/v1", dependencies=[Depends(verify_key)])
        @router.get("/late")
        def late(): ...
        """,
}


def test_a_route_added_after_the_include_is_served_only_by_the_live_model(tmp_path: Path) -> None:
    copy = build(tmp_path / "copy", LATE, include_model=COPY)
    assert rows(copy) == [("GET", "/v1/items/early", "early", ["verify_key"])]
    assert copy.limitations == ()

    live = build(tmp_path / "live", LATE, include_model=LIVE)
    assert rows(live) == [
        ("GET", "/v1/items/early", "early", ["verify_key"]),
        ("GET", "/v1/items/late", "late", ["verify_key"]),
    ]
    assert live.limitations == () and live.include_model is LIVE


def test_an_unknown_fastapi_version_lists_a_late_route_as_deferred_naming_the_lock(tmp_path: Path) -> None:
    """With no ``backend/uv.lock`` to read, the late route is neither dropped
    (the copy model) nor served (the live model): it is listed as deferred,
    and the limitation says why the version is unknown."""
    model = build(tmp_path, LATE, include_model=None, lock="backend/uv.lock")
    assert model.include_model.kind == "unknown"
    assert [(r.path, r.mount) for r in model.routes] == [("/v1/items/early", "static"), ("/v1/items/late", "deferred")]
    assert limits(model) == [("deferred_route_registration", "pkg/main.py", 6)]
    (message,) = [p.message for p in model.limitations]
    assert "app.include_router(pkg.items:router) at pkg/main.py:5" in message
    assert "0.137 and later" in message
    assert "backend/uv.lock: could not read lockfile" in message


def test_the_locked_version_in_the_backend_lock_picks_the_model(tmp_path: Path) -> None:
    lock = 'version = 1\n\n[[package]]\nname = "fastapi"\nversion = "{}"\n'
    write(tmp_path / "copy", {"backend/uv.lock": lock.format("0.135.2")})
    write(tmp_path / "live", {"backend/uv.lock": lock.format("0.143.0")})
    assert [r.path for r in build(tmp_path / "copy", LATE, include_model=None, lock="backend/uv.lock").routes] == [
        "/v1/items/early"
    ]
    assert [r.path for r in build(tmp_path / "live", LATE, include_model=None, lock="backend/uv.lock").routes] == [
        "/v1/items/early",
        "/v1/items/late",
    ]


NEVER_CALLED = {
    "pkg/items.py": """
        from fastapi import APIRouter
        router = APIRouter()
        """,
    "pkg/main.py": """
        from fastapi import FastAPI
        from pkg.items import router
        app = FastAPI()
        app.include_router(router, prefix="/v1")
        def register():
            @router.get("/maybe")
            def maybe(): ...
        """,
}


def test_a_registration_that_may_never_run_stays_deferred(tmp_path: Path) -> None:
    """``register()`` is never called at import but an importer may call it:
    the copy model drops the route (the include ran first) and says so; the
    live model would serve it if it ever ran, so it is listed as deferred."""
    copy = build(tmp_path / "copy", NEVER_CALLED, include_model=COPY)
    assert copy.routes == ()
    assert [k for k, _, _ in limits(copy)] == ["deferred_route_registration"]
    for name, model in (("live", LIVE), ("unknown", UNKNOWN)):
        built = build(tmp_path / name, NEVER_CALLED, include_model=model)
        assert [(r.path, r.mount) for r in built.routes] == [("/v1/maybe", "deferred")], name
        assert {k for k, _, _ in limits(built)} == {"deferred_route_registration"}, name


MOUNTED = {
    "pkg/items.py": """
        from fastapi import APIRouter, FastAPI
        sub = FastAPI()
        @sub.get("/s")
        def s(): ...
        router = APIRouter(prefix="/items")
        router.mount("/sub", sub)
        @router.get("/one")
        def one(): ...
        """,
    "pkg/main.py": """
        from fastapi import FastAPI
        from pkg.items import router
        app = FastAPI()
        app.include_router(router, prefix="/v1")
        """,
}


def test_a_mount_on_an_included_router_is_served_only_by_the_live_model(tmp_path: Path) -> None:
    copy = build(tmp_path / "copy", MOUNTED, include_model=COPY)
    assert [(r.path, r.mount) for r in copy.routes] == [("/v1/items/one", "static")]

    live = build(tmp_path / "live", MOUNTED, include_model=LIVE)
    # The router's own prefix does not apply to a mount; the include's does.
    assert sorted((r.path, r.mount) for r in live.routes) == [("/v1/items/one", "static"), ("/v1/sub/s", "static")]
    assert live.limitations == ()

    unknown = build(tmp_path / "unknown", MOUNTED, include_model=UNKNOWN)
    assert sorted((r.path, r.mount) for r in unknown.routes) == [("/v1/items/one", "static"), ("/v1/sub/s", "deferred")]
    assert [k for k, _, _ in limits(unknown)] == ["deferred_router_mount"]
    assert "no lock in this test" in unknown.limitations[0].message


def test_a_mount_at_a_path_the_analysis_cannot_evaluate_is_reported(tmp_path: Path) -> None:
    model = build(
        tmp_path,
        {
            "pkg/main.py": """
                import os
                from fastapi import FastAPI
                sub = FastAPI()
                @sub.get("/items")
                def items(): ...
                app = FastAPI()
                app.mount(os.environ["P"], sub)
                """,
        },
    )
    assert [r.path for r in model.routes] == ['<unresolved: os.environ["P"]>/items']
    assert limits(model) == [("unresolved_mount_path", "pkg/main.py", 7)]


def test_mutually_recursive_escapes_are_reported_at_every_call_site(tmp_path: Path) -> None:
    """Following f cuts its call back into g; g's own site must still be checked."""
    model = build(
        tmp_path,
        {
            "pkg/main.py": """
                from fastapi import FastAPI
                def f(app):
                    g(app)
                    app.router.routes.clear()
                def g(app):
                    f(app)
                app = FastAPI()
                f(app)
                g(app)
                """,
        },
    )
    assert {(kind, line) for kind, _file, line in limits(model)} >= {
        ("opaque_router_change", 8),
        ("opaque_router_change", 9),
    }


def test_a_change_past_the_escape_depth_is_a_change_whichever_site_comes_first(tmp_path: Path) -> None:
    """`a1` reaches `k` past the depth limit and `h` within it; the first
    answer must not decide the second site's."""
    body = """
        from fastapi import FastAPI
        def k(app):
            app.router.routes.clear()
        def h(app):
            k(app)
        def a4(app):
            h(app)
        def a3(app):
            a4(app)
        def a2(app):
            a3(app)
        def a1(app):
            a2(app)
        app = FastAPI()
        """
    for calls in ("a1(app)\nh(app)", "h(app)\na1(app)"):
        source = body.rstrip() + "".join(f"\n        {c}" for c in calls.splitlines()) + "\n"
        model = build(tmp_path / calls[:2], {"pkg/main.py": source})
        assert not model.diagnostics
        assert {kind for kind, _file, _line in limits(model)} == {"opaque_router_change"}, calls


@pytest.mark.parametrize(
    ("use", "expected"),
    [
        ("get_openapi(title='x', version='1', routes=app.routes)", []),
        ("get_openapi('x', '1', app.routes)", []),
        ("sorted(app.routes, key=id)", []),
        ("[*app.routes]", []),
        ("logger.info('routes %s', app.routes)", ["opaque_router_change"]),
        ("keep(routes=app.routes)", ["opaque_router_change"]),
    ],
)
def test_a_route_list_handed_on_inside_a_function_is_a_change_unless_only_read(
    tmp_path: Path, use: str, expected: list[str]
) -> None:
    model = build(
        tmp_path,
        {
            "pkg/main.py": f"""
                import logging
                from fastapi import FastAPI
                from fastapi.openapi.utils import get_openapi
                logger = logging.getLogger(__name__)
                def keep(routes): ...
                def install(app):
                    return {use}
                app = FastAPI()
                install(app)
                """,
        },
    )
    assert [k for k, _, _ in limits(model)] == expected


def test_a_route_under_an_earlier_mount_is_reported_as_never_reached(tmp_path: Path) -> None:
    model = build(
        tmp_path,
        {
            "pkg/main.py": """
                from fastapi import FastAPI
                sub = FastAPI()
                @sub.get("/items")
                def items(): ...
                app = FastAPI()
                app.mount("/sub", sub)
                @app.get("/sub/x")
                def shadowed(): ...
                @app.get("/subway")
                def beside(): ...
                """,
        },
    )
    assert [r.path for r in model.routes] == ["/sub/items", "/sub/x", "/subway"]
    assert limits(model) == [("mount_shadows_route", "pkg/main.py", 7)]
    assert "GET /sub/x is registered after pkg.main:app.mount() at pkg/main.py:6" in model.limitations[0].message


@pytest.mark.parametrize(
    ("mount", "route", "caught"),
    [
        ("/sub", "/sub/x", "always"),
        ("/sub", "/subway", None),
        ("/sub", "/sub", None),
        ("/sub", "/sub/", "always"),
        ("", "/x", "always"),
        ("/t/{tenant}", "/t/{tid}/x", "always"),
        ("/t/{tenant:int}", "/t/{tid}/x", "sometimes"),
        ("/v2", "/{version}/health", "sometimes"),
        ("/v2", "/v1/health", None),
        ("/files", "/{rest:path}", "sometimes"),
    ],
)
def test_which_later_routes_a_mount_catches(mount: str, route: str, caught: str | None) -> None:
    assert router_graph._mount_catches(mount, route) == caught


def test_a_route_a_parameterized_mount_may_catch_is_reported(tmp_path: Path) -> None:
    model = build(
        tmp_path,
        {
            "pkg/main.py": """
                from fastapi import FastAPI
                sub = FastAPI()
                app = FastAPI()
                app.mount("/v2", sub)
                @app.get("/{version}/health")
                def health(version: str): ...
                """,
        },
    )
    assert limits(model) == [("mount_shadows_route", "pkg/main.py", 5)]
    assert "may send some of the route's requests to the mount" in model.limitations[0].message
