"""Tests for router_bases: which expression a route decorator, include or
mount names, under Python's scope and execution rules, and the per-module
events the router graph is built from.

Scenario tests put one module next to an app that includes nothing, so its
routers are unmounted and their routes come back in ``model.unmounted_routes``
with the prefix the module itself gives them. A base the analysis cannot pin
to one router is reported as ``unresolved_route_router`` and its route is not
counted, because no live path can be given for it."""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import pytest
import scaling_check

from code_intel.backend_model import (
    DepLayer,
    IncludeCall,
    Located,
    MountCall,
    PyModule,
    RouteReg,
    RouterEscape,
    Unknown,
)
from code_intel.code_paths import FileStamp
from code_intel.py_sources import PySources
from code_intel.router_bases import RouterBases, analyze, module_may_touch_routers, prefilter_facts
from code_intel.router_graph import build_backend_model

APP = "from fastapi import FastAPI\napp = FastAPI()\n"


def write(root: Path, files: dict[str, str]) -> None:
    for rel, source in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")


def scan(tmp_path: Path, source: str, extra: dict[str, str] | None = None):
    """Unmounted ``(method, path, handler)`` rows and ``(kind, line)`` limitations
    (other than ``unmounted_router``) for ``pkg/r.py``."""
    write(tmp_path, {"pkg/__init__.py": "", "pkg/main.py": APP, "pkg/r.py": source, **(extra or {})})
    model = build_backend_model(tmp_path, "pkg", "pkg.main:app", tmp_path)
    assert model.diagnostics == (), [d.message for d in model.diagnostics]
    rows = sorted((r.method, r.path, r.handler) for r in model.unmounted_routes)
    limits = sorted((p.kind, p.line) for p in model.limitations if p.kind != "unmounted_router")
    return rows, limits, model


def _located(layer: DepLayer) -> tuple[Located, ...]:
    assert not isinstance(layer.dependencies, Unknown)
    return layer.dependencies


def guards(model) -> list[tuple[str, list[str]]]:
    """Router-level dependency targets per unmounted route."""
    out = []
    for route in model.unmounted_routes:
        names: list[str] = []
        for layer in route.layers:
            if layer.via == "route decorator":
                continue
            for loc in layer.dependencies:
                node = loc.node
                names.append(ast.unparse(node.args[0]) if isinstance(node, ast.Call) and node.args else "?")
        out.append((route.path, names))
    return sorted(out)


def module_of(tmp_path: Path, source: str, name: str = "pkg.r") -> PyModule:
    rel = name.replace(".", "/") + ".py"
    write(tmp_path, {"pkg/__init__.py": "", rel: source})
    sources = PySources(tmp_path, "pkg", tmp_path).load()
    mod = sources.module(name)
    assert mod is not None
    return mod


def events(tmp_path: Path, source: str) -> list:
    return list(RouterBases(module_of(tmp_path, source)).events)


# ── Which bases are routers ─────────────────────────────────────────────


def test_route_decorators_register_only_on_routers(tmp_path: Path) -> None:
    """``@not_a_router.get("/x")`` registered a route. Only a base bound to
    ``APIRouter(...)``/``FastAPI(...)`` counts; an import the analysis cannot
    follow is reported."""
    rows, limits, _ = scan(
        tmp_path,
        """
        import fastapi
        from fastapi import APIRouter as Router
        from pkg.shared import router as shared_router

        plain = fastapi.APIRouter()
        aliased = Router(prefix="/al")
        cache = {}

        def helper(): ...

        @plain.get("/p")
        def p(): ...

        @aliased.post("/q")
        def q(): ...

        @cache.get("/c")
        def c(): ...

        @helper.get("/h")
        def h(): ...

        @not_a_router.get("/x")
        def x(): ...

        @shared_router.get("/s")
        def s(): ...
        """,
    )
    assert rows == [("GET", "/p", "p"), ("POST", "/al/q", "q")]
    assert limits == [("unresolved_route_router", 26)]


def test_an_imported_router_takes_its_own_modules_prefix(tmp_path: Path) -> None:
    """An imported base is the defining module's router, prefix included."""
    rows, limits, _ = scan(
        tmp_path,
        """
        from pkg.shared import router as shared_router
        @shared_router.get("/s")
        def s(): ...
        """,
        {"pkg/shared.py": "from fastapi import APIRouter\nrouter = APIRouter(prefix='/sh')\n"},
    )
    assert rows == [("GET", "/sh/s", "s")]
    assert limits == []


@pytest.mark.parametrize(
    ("body", "expected", "limited"),
    [
        # The factory call site is its own router, so its prefix is known.
        (
            "def make():\n    return APIRouter(prefix='/m')\nrouter = make()\n@router.get('/a')\ndef factory(): ...\n",
            [("GET", "/m/a", "factory")],
            False,
        ),
        (
            "def build():\n    router = APIRouter(prefix='/in')\n    @router.get('/i')\n    def inner(): ...\n"
            "    return router\nrouter = build()\n",
            [("GET", "/in/i", "inner")],
            False,
        ),
        (
            "router = APIRouter(prefix='/p')\npublic = router\n@public.get('/a')\ndef alias(): ...\n",
            [("GET", "/p/a", "alias")],
            False,
        ),
        (
            "router = APIRouter(prefix='/a') if FLAG else APIRouter(prefix='/b')\n@router.get('/x')\ndef ifexp(): ...\n",
            [],
            True,
        ),
        (
            "class MyRouter(APIRouter): ...\nrouter = MyRouter(prefix='/s')\n@router.get('/a')\ndef subclass(): ...\n",
            [("GET", "/s/a", "subclass")],
            False,
        ),
        (
            "from typing import cast\nrouter = cast(APIRouter, APIRouter(prefix='/c'))\n@router.get('/a')\ndef casted(): ...\n",
            [("GET", "/c/a", "casted")],
            False,
        ),
        (
            "router = 3\nclass C:\n    router = APIRouter(prefix='/k')\n    @router.get('/c')\n    def klass(self): ...\n",
            [("GET", "/k/c", "klass")],
            False,
        ),
        (
            "router = APIRouter(prefix='/l')\n@router.get('/a')\ndef later(): ...\nrouter = None\n",
            [("GET", "/l/a", "later")],
            False,
        ),
        ("cache = {}\n@cache.get('/x')\ndef literal(): ...\n", [], False),
    ],
    ids=["factory", "inner", "alias", "ifexp", "subclass", "casted", "klass", "later", "literal"],
)
def test_route_decorators_resolve_scopes_aliases_and_factories(
    tmp_path: Path, body: str, expected: list, limited: bool
) -> None:
    """Routes vanished with no limitation when the base was not a literal
    module-level ``APIRouter(...)``. Each shape now resolves to its router, with
    its prefix, or is reported; only a provable non-router drops silently.
    ``ifexp`` gives ``/a/x`` or ``/b/x`` at runtime, never ``/x``."""
    rows, limits, _ = scan(tmp_path, "from fastapi import APIRouter\n" + body)
    assert rows == expected
    assert [kind for kind, _ in limits] == (["unresolved_route_router"] if limited else [])


def test_router_merges_keep_prefix_and_guards_only_when_they_agree(tmp_path: Path) -> None:
    """Two branches binding a router merged to a bare verdict, dropping prefix
    and guards. Identical configurations keep them; different ones are reported."""
    rows, limits, model = scan(
        tmp_path,
        """
        from fastapi import APIRouter, Depends
        def auth(): ...
        if FLAG:
            same = APIRouter(prefix="/p", dependencies=[Depends(auth)])
            diff = APIRouter(prefix="/p")
        else:
            same = APIRouter(prefix="/p", dependencies=[Depends(auth)])
            diff = APIRouter(prefix="/q")

        @same.get("/a")
        def a(): ...

        @diff.get("/b")
        def b(): ...
        """,
    )
    assert rows == [("GET", "/p/a", "a")]
    assert limits == [("unresolved_route_router", 13)]
    assert guards(model) == [("/p/a", ["auth"])]


def test_global_router_costs_the_prefix_only_when_assigned(tmp_path: Path) -> None:
    """Any ``global router`` in the file made every ``@router`` base unresolved.
    Only a function that assigns the name rebinds it; the import-time decorator
    keeps the import-time router and gets a limitation."""
    rows, limits, model = scan(
        tmp_path,
        """
        from fastapi import APIRouter, Depends
        def auth(): ...
        read = APIRouter(prefix="/r", dependencies=[Depends(auth)])
        rebound = APIRouter(prefix="/b", dependencies=[Depends(auth)])
        method = APIRouter(prefix="/m")

        def get():
            global read
            return read

        def reset():
            global rebound
            rebound = APIRouter(prefix="/b", dependencies=[Depends(auth)])

        class C:
            def f(self):
                global method
                return method

        @read.get("/a")
        def a(): ...

        @rebound.get("/a")
        def b(): ...

        @method.get("/a")
        def c(): ...
        """,
    )
    assert rows == [("GET", "/b/a", "b"), ("GET", "/m/a", "c"), ("GET", "/r/a", "a")]
    assert limits == [("unresolved_route_router", 23)]
    assert guards(model) == [("/b/a", ["auth"]), ("/m/a", []), ("/r/a", ["auth"])]


# ── Blocks that never run, or run later ─────────────────────────────────


def test_type_checking_blocks_register_no_routes(tmp_path: Path) -> None:
    """A route under ``if TYPE_CHECKING:`` never reaches FastAPI; its ``else:`` does."""
    rows, limits, _ = scan(
        tmp_path,
        """
        import os
        import typing
        from typing import TYPE_CHECKING
        from fastapi import APIRouter
        router = APIRouter(prefix="/p")

        if TYPE_CHECKING:
            router = APIRouter(prefix="/q")
            @router.get("/t1")
            def t1(): ...
        else:
            @router.get("/e")
            def e(): ...

        if typing.TYPE_CHECKING:
            @router.get("/t2")
            def t2(): ...

        if os.environ.get("X"):
            @router.get("/x")
            def x(): ...
        elif TYPE_CHECKING:
            @router.get("/t3")
            def t3(): ...

        @router.get("/a")
        def a(): ...
        """,
    )
    assert rows == [("GET", "/p/a", "a"), ("GET", "/p/e", "e"), ("GET", "/p/x", "x")]
    assert limits == []


def test_type_checking_is_only_the_typing_flag(tmp_path: Path) -> None:
    """Only ``typing``/``typing_extensions``' flag, through any alias and never
    rebound, is never true, also under ``not``, ``and`` and ``or``."""
    rows, limits, _ = scan(
        tmp_path,
        """
        import typing as t
        from typing import TYPE_CHECKING as TC
        from fastapi import APIRouter
        x = 1
        router = APIRouter(prefix="/p")

        class settings:
            TYPE_CHECKING = True

        if TC:
            @router.get("/t1")
            def t1(): ...

        if t.TYPE_CHECKING and x:
            @router.get("/t2")
            def t2(): ...

        if not TC:
            @router.get("/a")
            def a(): ...
        else:
            @router.get("/t3")
            def t3(): ...

        if settings.TYPE_CHECKING:
            @router.get("/s")
            def s(): ...
        """,
    )
    assert rows == [("GET", "/p/a", "a"), ("GET", "/p/s", "s")]
    assert limits == []
    rows, limits, _ = scan(
        tmp_path / "rebound",
        """
        from fastapi import APIRouter
        TYPE_CHECKING = True
        router = APIRouter(prefix="/p")
        if TYPE_CHECKING:
            @router.get("/r")
            def r(): ...
        """,
    )
    assert rows == [("GET", "/p/r", "r")]
    assert limits == []


def test_nested_decorator_sees_only_the_bindings_that_reach_it(tmp_path: Path) -> None:
    """A decorator in a function body joined every binding of the router name.
    Only the binding live at the ``def`` and later ones can reach it."""
    rows, limits, model = scan(
        tmp_path,
        """
        from fastapi import APIRouter, Depends
        def auth(): ...
        router = APIRouter(prefix="/x")
        router = APIRouter(prefix="/p", dependencies=[Depends(auth)])

        def reg():
            @router.get("/a")
            def a(): ...

        class C:
            def reg(self):
                def inner():
                    @router.get("/b")
                    def b(): ...

        def late():
            @other.get("/c")
            def c(): ...

        other = APIRouter(prefix="/o")
        other = APIRouter(prefix="/n")
        """,
    )
    assert rows == [("GET", "/p/a", "a"), ("GET", "/p/b", "b")]
    # `late` may run after either binding of `other`; nothing calls `reg` or `inner`.
    assert sorted(limits) == [
        ("deferred_route_registration", 7),
        ("deferred_route_registration", 13),
        ("unresolved_route_router", 17),
    ]
    assert guards(model) == [("/p/a", ["auth"]), ("/p/b", ["auth"])]


def test_elif_walrus_binds_only_from_its_own_test(tmp_path: Path) -> None:
    """The walrus of every ``elif`` test was bound before any branch ran. A
    test's walrus binds for its own block and the ones after it."""
    rows, limits, _ = scan(
        tmp_path,
        """
        from fastapi import APIRouter
        x = 1
        router = APIRouter(prefix="/p")
        other = APIRouter(prefix="/p")
        if x:
            @router.get("/a")
            def a(): ...

            @other.get("/b")
            def b(): ...
        elif (router := 5):
            pass
        elif (other := APIRouter(prefix="/q")) is None:
            pass
        else:
            @other.get("/c")
            def c(): ...
        """,
    )
    assert rows == [("GET", "/p/a", "a"), ("GET", "/p/b", "b"), ("GET", "/q/c", "c")]
    assert limits == []


def test_except_handler_starts_from_values_the_try_body_gave(tmp_path: Path) -> None:
    """A handler started from the state before the ``try``; it now starts from
    the join of that state and every value the body gave."""
    rows, limits, _ = scan(
        tmp_path,
        """
        from fastapi import APIRouter
        router = APIRouter(prefix="/p")
        same = APIRouter(prefix="/s")
        try:
            try:
                router = APIRouter(prefix="/q")
                same = APIRouter(prefix="/s")
                int("z")
            except KeyError:
                pass
        except ValueError:
            @router.get("/a")
            def a(): ...

            @same.get("/b")
            def b(): ...
        """,
    )
    assert rows == [("GET", "/s/b", "b")]
    assert limits == [("unresolved_route_router", 12)]


def test_global_in_a_class_body_rebinds_the_module_router(tmp_path: Path) -> None:
    """``global router`` in a class body runs at import, so the route lands on
    the ``/q`` router. Inside a function the write is deferred and reported."""
    rows, limits, _ = scan(
        tmp_path,
        """
        from fastapi import APIRouter
        router = APIRouter(prefix="/p")
        later = APIRouter(prefix="/p")

        class C:
            global router
            router = APIRouter(prefix="/q")

        def f():
            class D:
                global later
                later = APIRouter(prefix="/q")

        @router.get("/a")
        def a(): ...

        @later.get("/b")
        def b(): ...
        """,
    )
    assert rows == [("GET", "/p/b", "b"), ("GET", "/q/a", "a")]
    assert limits == [("unresolved_route_router", 17)]


def test_nonlocal_writer_flags_routes_that_read_the_name(tmp_path: Path) -> None:
    """A nested function rebinding the router through ``nonlocal`` may run
    before the reader, so the route's router is not known."""
    rows, limits, _ = scan(
        tmp_path,
        """
        from fastapi import APIRouter
        def outer():
            r = APIRouter(prefix="/n")
            def reg():
                @r.get("/a")
                def a(): ...
            def setr():
                nonlocal r
                r = APIRouter(prefix="/m")
            setr()
            reg()
            return r
        router = outer()
        """,
    )
    assert [kind for kind, _ in limits] == ["unresolved_route_router"]
    assert limits[0][1] == 5
    assert all(path in ("/n/a", "/m/a") for _, path, _ in rows)


def test_parameter_shadowing_a_router_registers_nothing_known(tmp_path: Path) -> None:
    """A parameter named like the module router may be any router. With no
    caller passing one, the decorator never registers on the module router."""
    rows, limits, _ = scan(
        tmp_path,
        """
        from fastapi import APIRouter
        router = APIRouter(prefix="/p")

        def register(router):
            @router.get("/a")
            def a(): ...
        """,
    )
    assert rows == []
    assert limits == []


# ── Methods and labels ──────────────────────────────────────────────────


def test_api_route_registers_one_route_per_method(tmp_path: Path) -> None:
    """``api_route`` defaults ``methods`` to GET and upper-cases the rest;
    ``dependencies=`` guards every method; non-literal methods are reported."""
    rows, limits, model = scan(
        tmp_path,
        """
        from fastapi import APIRouter, Depends

        router = APIRouter(prefix="/r")
        VERBS = ["PUT", "DELETE"]

        @router.api_route("/m", methods=["get", "POST", "GET"], dependencies=[Depends(guard)])
        def multi(): ...

        @router.api_route(path="/d")
        def default(): ...

        @router.api_route("/v", methods=VERBS)
        def named(): ...

        @router.api_route("/u", methods=verbs())
        def dynamic(): ...
        """,
    )
    assert rows == [
        ("ANY", "/r/u", "dynamic"),
        ("DELETE", "/r/v", "named"),
        ("GET", "/r/d", "default"),
        ("GET", "/r/m", "multi"),
        ("POST", "/r/m", "multi"),
        ("PUT", "/r/v", "named"),
    ]
    assert limits == [("unresolved_route_methods", 15)]
    multi = [r for r in model.unmounted_routes if r.handler == "multi"]
    decorator = [
        [ast.unparse(loc.node) for layer in r.layers if layer.via == "route decorator" for loc in _located(layer)]
        for r in multi
    ]
    assert decorator == [["Depends(guard)"], ["Depends(guard)"]]


def test_route_limitations_survive_deep_attribute_chains(tmp_path: Path) -> None:
    """A 500-segment ``@x.x.….get(...)`` base made ``ast.unparse`` recurse."""
    rows, limits, model = scan(
        tmp_path,
        "from fastapi import APIRouter\nrouter = APIRouter()\n"
        "@" + ".".join(["x"] * 500) + ".get('/a')\ndef a(): ...\n"
        "@router.get('/b')\ndef b(): ...\n",
    )
    assert rows == [("GET", "/b", "b")]
    assert limits == [("unresolved_route_router", 3)]
    (message,) = [p.message for p in model.limitations if p.kind == "unresolved_route_router"]
    assert message.startswith("@x.x.x.….x.x.get(...) on a()")


# ── Robustness ──────────────────────────────────────────────────────────


def test_long_elif_chains_do_not_crash_the_replay(tmp_path: Path) -> None:
    """A 3,000-branch ``elif`` chain raised RecursionError."""
    rows, limits, _ = scan(
        tmp_path,
        "from fastapi import APIRouter\nrouter = APIRouter(prefix='/p')\n"
        "v = 1\nif v == 0:\n    x = 0\n"
        + "".join(f"elif v == {i}:\n    x = {i}\n" for i in range(1, 3000))
        + "@router.get('/a')\ndef a(): ...\n",
    )
    assert rows == [("GET", "/p/a", "a")]
    assert limits == []


def test_deeply_nested_expressions_are_a_parse_diagnostic(tmp_path: Path) -> None:
    """``ast.parse`` raises RecursionError on ``X = 1 + 1 + …`` (200,000 terms);
    that file is a diagnostic and the rest of the package is still analyzed."""
    write(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/deep.py": "X = 1" + " + 1" * 200000 + "\n",
            "pkg/ok.py": "from fastapi import APIRouter\nrouter = APIRouter()\n@router.get('/ok')\ndef ok(): ...\n",
            "pkg/main.py": APP + "from pkg.ok import router\napp.include_router(router)\n",
        },
    )
    model = build_backend_model(tmp_path, "pkg", "pkg.main:app", tmp_path)
    assert [r.path for r in model.routes] == ["/ok"]
    assert [(d.kind, d.file) for d in model.diagnostics] == [("parse_failed", "pkg/deep.py")]


def test_router_replay_branches_stay_linear() -> None:
    """Every ``if`` and ``try`` copied the whole name state (10,000 assignments
    took 6-12 s). ``nonlocal`` writes and per-branch bindings stay cheap too."""

    def replay(source: str) -> None:
        tree = ast.parse(source)
        RouterBases(PyModule("pkg.r", Path("r.py"), "r.py", FileStamp(0, 0, 0, 0), tree, source, False))

    for block in ("if x{i}:\n    pass\n", "try:\n    pass\nexcept E:\n    pass\n"):
        scaling_check.assert_scales_linearly(
            lambda n, block=block: "".join(f"x{i} = {i}\n" + block.format(i=i) for i in range(n)), replay, 2500
        )
    scaling_check.assert_scales_linearly(
        lambda n: (
            "from fastapi import APIRouter\ndef outer():\n    r = APIRouter()\n"
            + "".join(
                f"    def w{i}():\n        nonlocal r\n        r = APIRouter()\n"
                f"    def h{i}():\n        @r.get('/a')\n        def a(): ...\n"
                for i in range(n)
            )
        ),
        replay,
        3000,
    )
    for make in (
        lambda n: "if a:\n    pass\n" + "".join(f"elif (y{i} := {i}):\n    r = 1\n" for i in range(n)),
        lambda n: (
            "try:\n"
            + "".join(f"    z{i} = 1\n" for i in range(n))
            + "".join(f"except E{i}:\n    w{i} = 1\n" for i in range(n))
        ),
    ):
        scaling_check.assert_scales_linearly(make, replay, 2000)


# ── Events and contexts ─────────────────────────────────────────────────


def test_events_carry_their_execution_context(tmp_path: Path) -> None:
    """Includes record whether they run at import, under a branch, in a
    function body or lazily, which the graph turns into mount states."""
    found = events(
        tmp_path,
        """
        from fastapi import APIRouter
        from pkg.child import router as child
        router = APIRouter()
        router.include_router(child)
        if FLAG:
            router.include_router(child)
        def install():
            router.include_router(child)
        hook = lambda: router.include_router(child)
        x = [router.include_router(child) for _ in range(1)]
        y = FLAG and router.include_router(child)
        """,
    )
    includes = [e for e in found if isinstance(e, IncludeCall)]
    contexts = sorted((e.call.lineno, e.context.deferred_in, e.context.conditional, e.context.lazy) for e in includes)
    assert contexts == [
        (4, None, False, False),
        (6, None, True, False),
        (8, "install", False, False),
        (9, None, False, True),
        (10, None, True, False),
        (11, None, True, False),
    ]
    assert all(
        e.parent[0] == "router" and not isinstance(e.child, Unknown) and e.child[:2] == ("import", "pkg.child")
        for e in includes
    )
    # Function bodies are replayed after the module body, so their events come last.
    assert [e.seq for e in found] == sorted(e.seq for e in found)
    assert includes[-1].context.deferred_in == "install"


def test_events_name_routes_mounts_and_escapes(tmp_path: Path) -> None:
    found = events(
        tmp_path,
        """
        from fastapi import APIRouter, FastAPI
        app = FastAPI()
        sub = FastAPI()
        router = APIRouter()

        @router.get("/a")
        @router.post("/a")
        def a(): ...

        router.add_api_route("/b", a, methods=["PUT"])
        app.mount("/sub", sub)
        register(router)
        router.prefix = "/x"
        """,
    )
    regs = [(e.methods, e.decorator, e.call.lineno) for e in found if isinstance(e, RouteReg)]
    # Stacked decorators apply bottom-up.
    assert regs == [(("POST",), True, 7), (("GET",), True, 6), (("PUT",), False, 10)]
    (mount,) = [e for e in found if isinstance(e, MountCall)]
    assert not isinstance(mount.app, Unknown)
    assert (mount.parent[0], mount.app[0]) == ("router", "router")
    escapes = [(ast.unparse(e.callee), type(e.call).__name__) for e in found if isinstance(e, RouterEscape)]
    assert ("register", "Call") in escapes
    assert ("prefix", "Assign") in escapes or any(callee.endswith("prefix") for callee, _ in escapes)


def test_a_router_method_used_without_a_call_is_an_escape(tmp_path: Path) -> None:
    """``get = router.get`` or ``include = app.include_router`` registers or
    includes through a name the replay cannot follow."""
    found = events(
        tmp_path,
        """
        from fastapi import APIRouter, FastAPI
        app = FastAPI()
        router = APIRouter()
        get = router.get
        include = app.include_router
        handlers = [router.get("/a")]
        """,
    )
    refs = [(ast.unparse(e.callee), e.call is e.callee) for e in found if isinstance(e, RouterEscape)]
    assert refs == [("router.get", True), ("app.include_router", True)]


def test_a_routers_list_bound_to_a_name_or_passed_on_is_an_escape(tmp_path: Path) -> None:
    """``deps = router.dependencies`` then ``deps.append(...)`` changes the
    router through a name the replay does not follow; reading the list is fine."""
    found = events(
        tmp_path,
        """
        from fastapi import APIRouter
        router = APIRouter()
        deps = router.dependencies
        extend(router.routes)
        count = len(router.routes)
        for route in router.routes:
            pass
        class Sub(APIRouter):
            def add_api_route(self, path, endpoint, **kw):
                return super().add_api_route(path, endpoint, **kw)
        """,
    )
    escapes = [
        (ast.unparse(e.callee), type(e.call).__name__)
        for e in found
        if isinstance(e, RouterEscape) and e.router[0] != "param"
    ]
    assert escapes == [("router.dependencies", "Assign"), ("router.routes", "Call")]
    assert not [e for e in found if isinstance(e, RouteReg)]


def test_methods_names_are_left_for_the_graph_and_calls_are_recorded(tmp_path: Path) -> None:
    """A ``methods=`` name is evaluated where the registration runs (it may be
    rebound later); every call with a named callee is recorded with its context."""
    bases = RouterBases(
        module_of(
            tmp_path,
            """
            from fastapi import APIRouter
            router = APIRouter()
            M = ["GET"]
            @router.api_route("/x", methods=M)
            def h(): ...
            M = ["POST"]
            def setup():
                register_all(router)
            if FLAG:
                setup()
            if __name__ == "__main__":
                run()
            """,
        )
    )
    (reg,) = [e for e in bases.events if isinstance(e, RouteReg)]
    assert isinstance(reg.methods, Located) and ast.unparse(reg.methods.node) == "M" and reg.methods.at == 3
    calls = {(c.name, c.context.deferred_in, c.context.conditional, c.at) for c in bases.calls}
    assert ("setup", None, True, 6) in calls
    assert ("register_all", "setup", False, None) in calls
    assert not any(c.name == "run" for c in bases.calls), "an `if __name__ == '__main__'` body never runs on import"


def test_a_replay_failure_is_an_analysis_problem(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mod = module_of(tmp_path, "from fastapi import APIRouter\nrouter = APIRouter()\n")

    def fail(self, module) -> None:
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(RouterBases, "__init__", fail)
    analysis = analyze(mod)
    assert analysis.facts.events == ()
    assert [(p.kind, p.step, p.file) for p in analysis.facts.problems] == [
        ("analysis_failed", "analyze module", "pkg/r.py")
    ]


# ── Prefilter ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("source", "touches"),
    [
        ("x = 1\n", False),
        ("from fastapi import APIRouter\nrouter = APIRouter()\n", True),
        ("import fastapi\napp = fastapi.FastAPI()\n", True),
        ("def f(r):\n    r.include_router(other)\n", True),
        ("@thing.get('/a')\ndef a(): ...\n", True),
        ("from pkg.other import helper\nhelper()\n", False),
    ],
    ids=["plain", "router-ctor", "app-ctor", "include-call", "route-decorator", "unrelated-import"],
)
def test_the_prefilter_keeps_every_module_that_can_touch_a_router(tmp_path: Path, source: str, touches: bool) -> None:
    mod = module_of(tmp_path, source)
    assert module_may_touch_routers(mod) is touches


def test_the_prefilter_keeps_modules_that_use_a_known_router_name(tmp_path: Path) -> None:
    """A module that only passes an imported router to a helper must be kept
    once that router is known, so its escape is seen."""
    mod = module_of(tmp_path, "from pkg.routes import router\nregister(router)\n")
    assert not module_may_touch_routers(mod)
    assert module_may_touch_routers(mod, frozenset({("pkg.routes", "router")}))
    assert prefilter_facts(mod).touches(frozenset({("pkg.routes", "router")}))
