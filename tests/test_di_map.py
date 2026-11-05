"""``di_map``: DI JSON v2 over a backend model, the text report and the CLI.

Models are built by hand here (the router graph has its own tests), with the
``NameResolver`` double from ``test_dependency_resolver``.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from fixture_repo import MINIMAL_CONFIG, make_repo, write
from test_dependency_resolver import DEPS, FakeResolver, FakeSources

from code_intel import cli, di_map, problems
from code_intel.backend_model import BackendModel, DepLayer, IncludeEdge, LiveRoute, Located, RouterNode, Unknown
from code_intel.dependency_resolver import DependencyResolver

FILES = {
    "pkg/__init__.py": "",
    "pkg/deps.py": DEPS,
    "pkg/policy.py": """
    from fastapi import Depends

    def get_policy_context():
        return "ctx"

    def require_any_permission(*permissions):
        def _check(ctx=Depends(get_policy_context)):
            return ctx
        return _check
    """,
    "pkg/main.py": """
    from fastapi import Depends, FastAPI
    from pkg.deps import audit, verify_key

    APP_DEPS = [Depends(audit)]
    INCLUDE_DEPS = [Depends(verify_key)]
    app = FastAPI(dependencies=APP_DEPS, redirect_slashes=False)
    """,
    "pkg/locations.py": """
    from fastapi import APIRouter, Depends
    from pkg.deps import get_db, verify_key
    from pkg.policy import require_any_permission

    OWN = [Depends(verify_key)]
    router = APIRouter(dependencies=OWN)
    _require_list_access = require_any_permission(SQUAD_VIEW, "article.view")
    DECORATOR = [Depends(_require_list_access)]

    def list_locations(db=Depends(get_db)): ...
    """,
    "pkg/heartbeat_ws.py": """
    from fastapi import Depends

    def _get_heartbeat_manager():
        return "t"

    async def article_ws(manager=Depends(_get_heartbeat_manager)): ...
    """,
    "pkg/squad_heartbeat_ws.py": """
    from fastapi import Depends
    from pkg.deps import get_user

    def _get_heartbeat_manager(user=Depends(get_user)):
        return "f"

    async def squad_ws(manager=Depends(_get_heartbeat_manager)): ...
    """,
    "pkg/dead.py": """
    from fastapi import Depends
    from pkg.deps import get_admin

    def dead(admin=Depends(get_admin)): ...
    """,
}


def _function(sources: FakeSources, module: str, name: str) -> ast.AST:
    mod = sources.module(module)
    assert mod is not None
    return next(
        n for n in ast.walk(mod.tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
    )


def _elements(sources: FakeSources, module: str, name: str) -> tuple[Located, ...]:
    mod = sources.module(module)
    assert mod is not None
    for stmt in mod.tree.body:
        if isinstance(stmt, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in stmt.targets):
            assert isinstance(stmt.value, ast.List)
            return tuple(Located(module, e) for e in stmt.value.elts)
    raise AssertionError(name)


def _route(
    sources: FakeSources,
    module: str,
    handler: str,
    path: str,
    *,
    method: str = "GET",
    layers: tuple[DepLayer, ...] = (),
    chain: tuple[str, ...] = ("pkg.main:app",),
    order: int = 0,
) -> LiveRoute:
    fn = _function(sources, module, handler)
    mod = sources.module(module)
    assert mod is not None
    return LiveRoute(
        method=method,
        path=path,
        kind="websocket" if method == "WS" else "http",
        handler=handler,
        handler_id=f"{module}:{handler}",
        file=mod.rel,
        line=fn.lineno,  # type: ignore[attr-defined]
        registered_file=mod.rel,
        registered_line=fn.lineno,  # type: ignore[attr-defined]
        chain=chain,
        mount="static",
        layers=layers,
        order=order,
        endpoint=Located(module, fn),
    )


def _model(*, diagnostics: tuple = (), limitations: tuple = ()) -> tuple[BackendModel, FakeSources]:
    sources = FakeSources(FILES)
    app_layer = DepLayer("app", "pkg.main:app", _elements(sources, "pkg.main", "APP_DEPS"))
    include = DepLayer("router", "pkg.main:app->pkg.locations:router", _elements(sources, "pkg.main", "INCLUDE_DEPS"))
    own = DepLayer("router", "pkg.locations:router", _elements(sources, "pkg.locations", "OWN"))
    decorator = DepLayer(
        "route decorator", "pkg.locations:list_locations", _elements(sources, "pkg.locations", "DECORATOR")
    )
    chain = ("pkg.main:app", "pkg.locations:router")
    routes = (
        _route(
            sources,
            "pkg.locations",
            "list_locations",
            "/v1/locations",
            layers=(app_layer, include, own, decorator),
            chain=chain,
            order=0,
        ),
        # The same router included twice yields a second live route (c11).
        _route(
            sources,
            "pkg.locations",
            "list_locations",
            "/v2/locations",
            layers=(app_layer, own, decorator),
            chain=chain,
            order=1,
        ),
        _route(
            sources, "pkg.heartbeat_ws", "article_ws", "/ws/articles/{vin}", method="WS", layers=(app_layer,), order=2
        ),
        _route(sources, "pkg.squad_heartbeat_ws", "squad_ws", "/ws/squad", method="WS", layers=(app_layer,), order=3),
    )
    unmounted = (_route(sources, "pkg.dead", "dead", "/dead", chain=("pkg.dead:router",)),)
    routers = (
        RouterNode(
            "pkg.main:app", "pkg.main", "backend/pkg/main.py", 6, "FastAPI", "", app_layer.dependencies, False, ()
        ),
        RouterNode(
            "pkg.locations:router",
            "pkg.locations",
            "backend/pkg/locations.py",
            6,
            "APIRouter",
            "",
            own.dependencies,
            True,
            (),
        ),
        RouterNode(
            "pkg.dead:router",
            "pkg.dead",
            "backend/pkg/dead.py",
            4,
            "APIRouter",
            Unknown("unbound", "PREFIX"),
            (),
            True,
            (),
        ),
    )
    edges = (
        IncludeEdge(
            "pkg.main:app",
            "pkg.locations:router",
            "/v1",
            include.dependencies,
            "backend/pkg/main.py",
            8,
            3,
            "static",
        ),
    )
    model = BackendModel(
        app_id="pkg.main:app",
        redirect_slashes=False,
        routes=routes,
        unmounted_routes=unmounted,
        routers=routers,
        edges=edges,
        diagnostics=diagnostics,
        limitations=limitations,
        files_scanned=len(FILES),
        sources=sources,
        resolver=FakeResolver(sources),
    )
    return model, sources


POLICY = "pkg.policy:require_any_permission"
CONTEXT = "pkg.policy:get_policy_context"


def test_build_di_identifies_dependencies_by_module_and_qualname() -> None:
    """The real `_get_heartbeat_manager` shape gives two ids, each with its own sub-deps."""
    model, _ = _model()
    data = di_map.build_di(model)
    funcs = data["dependency_functions"]

    ws = funcs["pkg.heartbeat_ws:_get_heartbeat_manager"]
    squad = funcs["pkg.squad_heartbeat_ws:_get_heartbeat_manager"]
    assert (ws["file"], ws["line"], ws["sub_deps"]) == ("backend/pkg/heartbeat_ws.py", 3, [])
    assert (squad["file"], squad["line"], squad["sub_deps"]) == (
        "backend/pkg/squad_heartbeat_ws.py",
        4,
        ["pkg.deps:get_user"],
    )
    assert squad["transitive_deps"] == ["pkg.deps:get_user", "pkg.deps:get_db"]
    assert data["reverse_index"]["pkg.heartbeat_ws:_get_heartbeat_manager"] == ["WS /ws/articles/{vin}"]
    assert data["reverse_index"]["pkg.squad_heartbeat_ws:_get_heartbeat_manager"] == ["WS /ws/squad"]


def test_build_di_resolves_guard_aliases_to_the_factory() -> None:
    model, _ = _model()
    data = di_map.build_di(model)
    factory = data["dependency_functions"][POLICY]
    assert factory == {
        "id": POLICY,
        "name": "require_any_permission",
        "module": "pkg.policy",
        "qualname": "require_any_permission",
        "kind": "factory_result",
        "file": "backend/pkg/policy.py",
        "line": 6,
        "sub_deps": [CONTEXT],
        "transitive_deps": [CONTEXT],
        "aliases": ["_require_list_access"],
    }
    first = data["routes"][0]
    guard = next(d for d in first["deps"] if d["via"] == "route decorator")
    assert guard == {
        "id": POLICY,
        "display": "require_any_permission(SQUAD_VIEW, article.view)",
        "via": "route decorator",
        "param": None,
        "alias": "_require_list_access",
        "marker": "Depends",
        "file": "backend/pkg/locations.py",
        "line": 8,
    }
    assert di_map.dep_label(guard) == "require_any_permission(SQUAD_VIEW, article.view) [as _require_list_access]"


def test_build_di_route_rows_keep_fastapi_order_and_every_layer() -> None:
    model, _ = _model()
    data = di_map.build_di(model)
    v1, v2, ws, squad = data["routes"]
    assert (v1["method"], v1["path"], v1["handler_id"], v1["order"]) == (
        "GET",
        "/v1/locations",
        "pkg.locations:list_locations",
        0,
    )
    assert (v1["file"], v1["line"], v1["mount"], v1["chain"]) == (
        "backend/pkg/locations.py",
        10,
        "static",
        ["pkg.main:app", "pkg.locations:router"],
    )
    assert v1["app_deps"] == ["audit"]
    assert v1["router_deps"] == ["verify_key", "verify_key"], "include and router-own layers are not deduplicated"
    assert v1["decorator_deps"] == ["require_any_permission(SQUAD_VIEW, article.view)"]
    assert v1["direct_deps"] == ["get_db"]
    assert [(d["via"], d["param"]) for d in v1["deps"]] == [
        ("app", None),
        ("router", None),
        ("router", None),
        ("route decorator", None),
        ("direct", "db"),
    ]
    assert v1["all_deps"] == ["pkg.deps:audit", "pkg.deps:verify_key", POLICY, CONTEXT, "pkg.deps:get_db"]
    assert v2["router_deps"] == ["verify_key"], "the second include carries no include dependencies"
    assert (ws["kind"], ws["method"]) == ("websocket", "WS")
    assert squad["all_deps"] == [
        "pkg.deps:audit",
        "pkg.squad_heartbeat_ws:_get_heartbeat_manager",
        "pkg.deps:get_user",
        "pkg.deps:get_db",
    ]


def test_build_di_reverse_index_counts_only_live_routes() -> None:
    model, _ = _model()
    data = di_map.build_di(model)
    assert data["reverse_index"]["pkg.deps:get_db"] == ["GET /v1/locations", "GET /v2/locations", "WS /ws/squad"]
    assert "pkg.deps:get_admin" not in data["reverse_index"]
    (dead,) = data["unmounted_routes"]
    assert (dead["path"], dead["direct_deps"]) == ("/dead", ["get_admin"])
    assert "pkg.deps:get_admin" in data["dependency_functions"]


def test_build_di_routers_summary_and_app() -> None:
    model, _ = _model()
    data = di_map.build_di(model)
    assert data["schema_version"] == 2
    assert data["app"] == {
        "id": "pkg.main:app",
        "redirect_slashes": False,
        "fastapi_version": None,
        "include_model": "unknown",
        "include_model_reason": "no FastAPI version was given to the analysis",
    }
    assert data["routers"] == [
        {
            "id": "pkg.main:app",
            "file": "backend/pkg/main.py",
            "line": 6,
            "kind": "FastAPI",
            "prefix": "",
            "prefix_resolved": True,
            "deps": ["audit"],
            "mounted": True,
        },
        {
            "id": "pkg.locations:router",
            "file": "backend/pkg/locations.py",
            "line": 6,
            "kind": "APIRouter",
            "prefix": "",
            "prefix_resolved": True,
            "deps": ["verify_key"],
            "mounted": True,
        },
        {
            "id": "pkg.dead:router",
            "file": "backend/pkg/dead.py",
            "line": 4,
            "kind": "APIRouter",
            "prefix": None,
            "prefix_resolved": False,
            "deps": [],
            "mounted": False,
        },
    ]
    assert data["summary"] == {
        "routes": 4,
        "http": 2,
        "websocket": 2,
        "dependency_functions": len(data["dependency_functions"]),
        "files_scanned": len(FILES),
    }
    assert data["complete"] is True
    assert data["diagnostics"] == [] and data["analysis_limitations"] == []


def test_build_di_merges_model_and_resolver_problems() -> None:
    read = problems.diagnostic(
        "read_failed",
        stage="load python sources",
        step="read file",
        file="backend/pkg/x.py",
        exc=PermissionError("denied"),
    )
    mount = problems.limitation(
        "opaque_router_call", stage="router graph", file="backend/pkg/main.py", line=9, message="app escapes"
    )
    model, sources = _model(diagnostics=(read,), limitations=(mount, mount))
    dep = DependencyResolver(model.resolver, sources)
    dep._limit("unresolved_dependency", "pkg.dead", 4, "example")
    data = di_map.build_di(model, dep)
    assert data["complete"] is False
    assert data["diagnostics"] == [read.as_dict()]
    assert data["analysis_limitations"] == [
        mount.as_dict(),
        {
            "kind": "unresolved_dependency",
            "stage": "resolve dependencies",
            "file": "backend/pkg/dead.py",
            "line": 4,
            "message": "backend/pkg/dead.py:4: example",
        },
    ]


def test_text_report_shows_aliases_and_ids_for_colliding_names() -> None:
    model, _ = _model()
    text = di_map.format_text(di_map.build_di(model))
    assert "require_any_permission(SQUAD_VIEW, article.view) [as _require_list_access]" in text
    assert "pkg.heartbeat_ws:_get_heartbeat_manager" in text
    assert "pkg.squad_heartbeat_ws:_get_heartbeat_manager" in text
    assert "Direct:  _get_heartbeat_manager (pkg.heartbeat_ws:_get_heartbeat_manager)" in text
    assert "Status: COMPLETE, 0 diagnostics" in text
    assert "Unmounted routes (1), not live" in text


# ── CLI ──────────────────────────────────────────────────────────────────


def _repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path)


def _dump_di(*args: str | Path) -> int:
    return cli.main(["dump", "di", *map(str, args)])


def test_cli_prints_json_and_exits_zero(monkeypatch, tmp_path, capsys) -> None:
    model, _ = _model()
    result = di_map.build_di(model)
    seen: list[tuple[Path, str]] = []
    monkeypatch.setattr(di_map, "compute", lambda root, backend: seen.append((root, backend.package)) or result)
    assert _dump_di("--json", "--repo-root", _repo(tmp_path)) == 0
    out = capsys.readouterr()
    assert json.loads(out.out) == result
    assert out.err == ""
    assert seen == [(tmp_path.resolve(), "service_backend")]


def test_cli_reports_diagnostics_by_their_own_message_on_stderr(monkeypatch, tmp_path, capsys) -> None:
    """A read failure is reported as the read failure, never as "could not parse"."""
    read = problems.diagnostic(
        "read_failed",
        stage="load python sources",
        step="read file",
        file="backend/service_backend/x.py",
        exc=PermissionError("denied"),
    )
    model, _ = _model(diagnostics=(read,))
    monkeypatch.setattr(di_map, "compute", lambda root, backend: di_map.build_di(model))
    assert _dump_di("--repo-root", _repo(tmp_path)) == 2
    err = capsys.readouterr().err
    assert "backend/service_backend/x.py: could not read file (PermissionError: denied)" in err
    assert "1 file could not be read" in err
    assert "could not parse" not in err


def test_cli_refuses_a_too_old_interpreter(monkeypatch, tmp_path, capsys) -> None:
    repo = _repo(tmp_path)
    write(repo / "backend" / ".python-version", "99.0\n")
    monkeypatch.setattr(di_map, "compute", lambda root, backend: pytest.fail("analysis must not run"))
    assert _dump_di("--json", "--repo-root", repo) == 1
    assert "older than the repo minimum 99.0" in capsys.readouterr().err


def test_cli_names_a_missing_backend_package(tmp_path, capsys) -> None:
    write(tmp_path / ".code-intel" / "config.toml", MINIMAL_CONFIG)
    assert _dump_di("--json", "--repo-root", tmp_path) == 1
    err = capsys.readouterr().err
    assert str(tmp_path.resolve() / "backend" / "service_backend") in err
    assert "backend package directory not found" in err


def test_cli_runs_end_to_end_on_a_small_backend(tmp_path) -> None:
    files = {
        "service_backend/__init__.py": "",
        "service_backend/deps.py": DEPS,
        "service_backend/main.py": """
        from fastapi import Depends, FastAPI
        from service_backend.deps import get_db, verify_key

        app = FastAPI()

        @app.get("/items", dependencies=[Depends(verify_key)])
        def items(db=Depends(get_db)): ...
        """,
    }
    make_repo(tmp_path)
    for rel, source in files.items():
        write(tmp_path / "backend" / rel, textwrap.dedent(source).lstrip("\n"))
    proc = subprocess.run(
        [sys.executable, "-m", "code_intel.cli", "dump", "di", "--json", "--repo-root", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    (route,) = data["routes"]
    assert (route["method"], route["path"], route["handler_id"]) == ("GET", "/items", "service_backend.main:items")
    assert route["decorator_deps"] == ["verify_key"] and route["direct_deps"] == ["get_db"]
    assert route["all_deps"] == ["service_backend.deps:verify_key", "service_backend.deps:get_db"]
    assert data["dependency_functions"]["service_backend.deps:get_db"]["file"] == "backend/service_backend/deps.py"


def test_text_report_leads_with_banners_and_json_carries_limitations(monkeypatch, tmp_path, capsys) -> None:
    mount = problems.limitation(
        "opaque_router_call", stage="router graph", file="backend/pkg/main.py", line=9, message="app escapes"
    )
    read = problems.diagnostic(
        "read_failed", stage="load python sources", step="read file", file="backend/pkg/x.py", exc=OSError("gone")
    )
    repo = _repo(tmp_path)

    model, _ = _model(limitations=(mount,))
    monkeypatch.setattr(di_map, "compute", lambda root, backend: di_map.build_di(model))
    assert _dump_di("--repo-root", repo) == 0
    text = capsys.readouterr().out
    assert text.lstrip().startswith("ⓘ 1 analysis limitation(s)"), "the limitations banner leads the report"
    assert "[opaque_router_call] backend/pkg/main.py:9: app escapes" in text

    assert _dump_di("--json", "--repo-root", repo) == 0
    out = capsys.readouterr().out
    assert "ⓘ" not in out
    assert json.loads(out)["analysis_limitations"] == [mount.as_dict()]

    clean, _ = _model()
    monkeypatch.setattr(di_map, "compute", lambda root, backend: di_map.build_di(clean))
    assert _dump_di("--repo-root", repo) == 0
    assert "ⓘ" not in capsys.readouterr().out

    both, _ = _model(diagnostics=(read,), limitations=(mount,))
    monkeypatch.setattr(di_map, "compute", lambda root, backend: di_map.build_di(both))
    assert _dump_di("--repo-root", repo) == 2
    out = capsys.readouterr().out
    assert out.lstrip().startswith("⚠ INCOMPLETE")
    assert "ⓘ 1 analysis limitation(s)" in out
